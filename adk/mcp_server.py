"""MCP server — expose agent tools via JSON-RPC 2.0 at POST /mcp.

Every awnode running ``aither-serve`` becomes both an MCP **client**
(consuming tools from mcp.aitherium.com via MCPBridge) and an MCP **server**
(exposing its own registered tools to any MCP-compatible caller).

Protocol — same wire format as AitherOS/awnode (FastMCP):

    POST /mcp
    Content-Type: application/json

    {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    → {"jsonrpc": "2.0", "result": {"tools": [...]}, "id": 1}

    {"jsonrpc": "2.0", "method": "tools/call",
     "params": {"name": "search_web", "arguments": {"query": "hello"}}, "id": 2}
    → {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "..."}]}, "id": 2}

Usage:

    from adk.mcp_server import MCPServer

    server = MCPServer(tool_registry=agent._tools)
    response = await server.handle(request_body)  # dict → dict

    # Or mount on FastAPI:
    server.mount(app)  # adds POST /mcp
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adk.tools import ToolRegistry

# FastAPI symbols are imported at MODULE level (not inside ``mount()``) so that,
# under ``from __future__ import annotations``, FastAPI can resolve the
# ``request: Request`` handler annotation against this module's globals. A local
# import leaves the string annotation unresolvable → FastAPI treats ``request``
# as a required query param → HTTP 422 on every /mcp call. Guarded because
# fastapi only ships with the ``[node]`` / server extra; ``.mount()`` is the
# only thing that needs it.
try:
    from fastapi import Request
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - fastapi optional until .mount() is used
    Request = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]

logger = logging.getLogger("adk.mcp_server")

# Where an auto-generated session key is written (mode 600). Every server and
# every /mcp-workstation mint gets its OWN file under ~/.aither/mcp-session-keys/
# named <instance>-<pid>-<fingerprint>.key, created with O_EXCL: the key is never
# logged, so this file is the only way to recover it, and a second process must
# never overwrite the first one's. AITHER_MCP_KEY_FILE names an explicit file
# instead; if that file already exists it is left alone and the key goes to a
# per-instance sibling (<file>.<pid>-<fingerprint>).
_SESSION_KEY_ENV = "AITHER_MCP_KEY_FILE"
_SESSION_KEY_DIR = "mcp-session-keys"


def key_fingerprint(key: str) -> str:
    """A non-reversible, loggable identifier for a bearer (never the value)."""
    import hashlib

    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _safe_instance(instance: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in instance)
    return cleaned.strip("._")[:64] or "adk-mcp"


def session_key_path(key: str, instance: str = "adk-mcp") -> Path:
    """The per-instance file ``key`` is persisted to. Unique per (instance,
    process, key), so two servers on one host never share a file."""
    suffix = f"{os.getpid()}-{key_fingerprint(key).split(':', 1)[1]}"
    override = os.getenv(_SESSION_KEY_ENV, "").strip()
    if override:
        return Path(override)
    return (Path.home() / ".aither" / _SESSION_KEY_DIR
            / f"{_safe_instance(instance)}-{suffix}.key")


def _write_exclusive(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug("chmod 600 on %s failed: %s", path, exc)


def persist_session_key(key: str, instance: str = "adk-mcp") -> Path | None:
    """Write ``key`` to its own mode-600 file and return the path, or None when
    it could not be written (the caller logs only a fingerprint). Never
    truncates or replaces an existing file: that file holds another live
    server's only copy of its bearer."""
    path = session_key_path(key, instance)
    try:
        try:
            _write_exclusive(path, key)
        except FileExistsError:
            if path.read_text(encoding="utf-8") == key:
                return path
            # An explicit AITHER_MCP_KEY_FILE already holds another server's
            # key: keep it and write a per-instance sibling instead.
            suffix = f"{os.getpid()}-{key_fingerprint(key).split(':', 1)[1]}"
            path = path.with_name(f"{path.name}.{suffix}")
            _write_exclusive(path, key)
        return path
    except OSError as exc:
        logger.warning("could not persist MCP session key to %s: %s", path, exc)
        return None


# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TOOL_NOT_FOUND = -32001  # custom: tool doesn't exist


@dataclass
class MCPServerInfo:
    """Server metadata returned by initialize."""
    name: str = "adk-node"
    version: str = ""
    capabilities: dict = field(default_factory=lambda: {
        "tools": {"listChanged": False},
    })


class MCPServer:
    """JSON-RPC 2.0 MCP server exposing a ToolRegistry.

    Handles methods:
      - initialize     → server info + capabilities
      - ping           → pong
      - tools/list     → all registered tools
      - tools/call     → execute a tool by name
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        server_name: str = "adk-node",
        server_version: str = "",
        require_auth: bool | None = None,
    ):
        self._registry = tool_registry or ToolRegistry()
        self._info = MCPServerInfo(
            name=server_name,
            version=server_version or _get_version(),
        )
        self._call_count = 0
        self._error_count = 0
        self._start_time = time.time()
        # Auth: require_auth=None means auto-detect from env
        # If AITHER_SERVER_API_KEY or AITHER_MCP_KEY is set, auth is enforced
        import os
        import secrets as _secrets
        self._mcp_key = os.getenv("AITHER_MCP_KEY", "") or os.getenv("AITHER_SERVER_API_KEY", "")
        if require_auth is None:
            if self._mcp_key:
                self._require_auth = True
            else:
                # Auto-generate a session key so MCP is never wide-open
                self._mcp_key = f"adk_mcp_{_secrets.token_hex(16)}"
                self._require_auth = True
                # Never log the bearer itself: write it to a mode-600 file and
                # log only where it is plus a fingerprint.
                key_path = persist_session_key(self._mcp_key, server_name)
                logger.info(
                    "Auto-generated MCP session key %s (pass via Authorization header); "
                    "stored at %s",
                    key_fingerprint(self._mcp_key),
                    key_path or "<not persisted: set AITHER_MCP_KEY>",
                )
        else:
            self._require_auth = require_auth

        # License entitlement gate (logs tier, wires metering quotas; never blocks unless
        # AITHER_LICENSE_REQUIRE=1).
        try:
            from adk.license_startup import gate_startup
            gate_startup(product="mcp")
        except Exception as _lic_exc:
            logger.debug("license gate skipped: %s", _lic_exc)

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @registry.setter
    def registry(self, value: ToolRegistry):
        self._registry = value

    # ── Core handler ──────────────────────────────────────────────────────

    async def handle(self, body: dict | str | bytes) -> dict:
        """Handle a JSON-RPC 2.0 request and return a response dict.

        Accepts raw bytes/string (parses JSON) or a pre-parsed dict.
        """
        # Parse if needed
        if isinstance(body, (str, bytes)):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return _error_response(None, PARSE_ERROR, "Parse error")

        if not isinstance(body, dict):
            return _error_response(None, INVALID_REQUEST, "Invalid request")

        jsonrpc = body.get("jsonrpc")
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        if jsonrpc != "2.0":
            return _error_response(req_id, INVALID_REQUEST,
                                   "Missing or invalid jsonrpc version")

        if not method:
            return _error_response(req_id, INVALID_REQUEST, "Missing method")

        # Route to handler
        if method == "initialize":
            return self._handle_initialize(req_id, params)
        elif method == "ping":
            return _success_response(req_id, {})
        elif method == "tools/list":
            return self._handle_tools_list(req_id, params)
        elif method == "tools/call":
            return await self._handle_tools_call(req_id, params)
        else:
            return _error_response(req_id, METHOD_NOT_FOUND,
                                   f"Method not found: {method}")

    # ── Method handlers ───────────────────────────────────────────────────

    def _handle_initialize(self, req_id: Any, params: dict) -> dict:
        """MCP initialize handshake."""
        return _success_response(req_id, {
            "protocolVersion": "2025-03-26",
            "serverInfo": {
                "name": self._info.name,
                "version": self._info.version,
            },
            "capabilities": self._info.capabilities,
        })

    def _handle_tools_list(self, req_id: Any, params: dict) -> dict:
        """Return all registered tools in MCP format."""
        tools = []
        for td in self._registry.list_tools():
            tools.append({
                "name": td.name,
                "description": td.description,
                "inputSchema": td.parameters,
            })
        return _success_response(req_id, {"tools": tools})

    async def _handle_tools_call(self, req_id: Any, params: dict) -> dict:
        """Execute a tool and return the result."""
        if not isinstance(params, dict):
            self._error_count += 1
            return _error_response(req_id, INVALID_PARAMS,
                                   "params must be an object")

        name = params.get("name", "")
        arguments = params.get("arguments", {})

        if not name:
            self._error_count += 1
            return _error_response(req_id, INVALID_PARAMS,
                                   "Missing tool name")

        td = self._registry.get(name)
        if not td:
            self._error_count += 1
            return _error_response(req_id, TOOL_NOT_FOUND,
                                   f"Tool not found: {name}")

        self._call_count += 1

        try:
            result_str = await self._registry.execute(name, arguments)
            return _success_response(req_id, {
                "content": [{"type": "text", "text": result_str}],
            })
        except Exception as exc:
            self._error_count += 1
            logger.error("MCP tool %s failed: %s", name, exc)
            return _error_response(req_id, INTERNAL_ERROR,
                                   f"Tool execution failed: {exc}")

    # ── FastAPI mount ─────────────────────────────────────────────────────

    def _check_auth(self, request) -> str | None:
        """Check Bearer token auth. Returns None if OK, error message if denied."""
        if not self._require_auth:
            return None
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return "Missing or invalid Authorization header"
        token = auth_header[7:]
        if not hmac.compare_digest(token, self._mcp_key):
            return "Invalid MCP API key"
        return None

    def mount(self, app, path: str = "/mcp"):
        """Mount the MCP endpoint on a FastAPI app.

        Adds ``POST /mcp`` (or custom path) that handles JSON-RPC 2.0.
        Auth is enforced when AITHER_MCP_KEY or AITHER_SERVER_API_KEY is set.
        """
        if Request is None:  # fastapi not installed
            raise RuntimeError(
                "MCPServer.mount() requires fastapi — install awdk[node]"
            )

        @app.post(path)
        async def _mcp_endpoint(request: Request):
            auth_err = self._check_auth(request)
            if auth_err:
                return JSONResponse({"error": auth_err}, status_code=401)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    _error_response(None, PARSE_ERROR, "Parse error"),
                    status_code=200,  # JSON-RPC errors use 200
                )
            result = await self.handle(body)
            return JSONResponse(result)

        @app.get(path)
        async def _mcp_info(request: Request):
            """GET /mcp returns server info (for discovery).

            Anonymous callers get name/version/protocol only; the tool
            inventory is disclosed only to a caller that passes ``_check_auth``.
            """
            info: dict[str, Any] = {
                "name": self._info.name,
                "version": self._info.version,
                "protocol": "mcp",
                "protocolVersion": "2025-03-26",
            }
            if self._check_auth(request) is None:
                tools = self._registry.list_tools()
                info["tools_count"] = len(tools)
                info["tools"] = [td.name for td in tools]
            return info

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return server status."""
        tools = self._registry.list_tools()
        return {
            "name": self._info.name,
            "version": self._info.version,
            "tools_count": len(tools),
            "tools": [td.name for td in tools],
            "calls": self._call_count,
            "errors": self._error_count,
            "uptime_s": round(time.time() - self._start_time, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC helpers
# ─────────────────────────────────────────────────────────────────────────────

def _success_response(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err, "id": req_id}


def _get_version() -> str:
    try:
        from adk import __version__
        return __version__
    except Exception:
        return "0.0.0"
