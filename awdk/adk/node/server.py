"""
awnode MCP Server -- Lightweight local MCP over streamable-http.

Two modes:
- **proxy**: Forwards list_tools / call_tool to the cloud gateway at
  mcp.aitherium.com, authenticating with the user's portal token.
- **standalone**: Registers a minimal set of local development tools
  (file I/O, git, shell) that work without any cloud connection.

Start via CLI:
    aither mcp node                      # proxy mode (default)
    aither mcp node --mode standalone    # local-only, no account needed
    aither mcp node --port 9000          # custom port
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8182
CLOUD_GATEWAY_URL = os.environ.get(
    "AITHER_MCP_GATEWAY", "https://mcp.aitherium.com/mcp"
)
CLOUD_MANIFEST_URL = os.environ.get(
    "AITHER_MCP_MANIFEST", "https://mcp.aitherium.com/tools/manifest"
)


# ── Auth helpers ──────────────────────────────────────────────────────────

def _load_token() -> Optional[str]:
    """Load the user's portal token from ~/.aither/portal.token or env."""
    env_key = os.environ.get("AITHER_API_KEY", "").strip()
    if env_key:
        return env_key

    aither_home = Path(
        os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))
    )

    portal_token = aither_home / "portal.token"
    if portal_token.is_file():
        token = portal_token.read_text(encoding="utf-8").strip()
        if token:
            return token

    auth_json = aither_home / "auth.json"
    if auth_json.is_file():
        try:
            data = json.loads(auth_json.read_text(encoding="utf-8"))
            profile = data.get("profiles", {}).get(data.get("active_profile", ""), {})
            token = profile.get("access_token", "")
            if not token:
                token = data.get("access_token") or data.get("token", "")
            if token:
                return token.strip()
        except (json.JSONDecodeError, KeyError):
            pass

    return None


# ── Proxy mode ────────────────────────────────────────────────────────────

class ProxyMCPNode:
    """Proxies MCP tool requests to the AitherOS cloud gateway."""

    def __init__(self, gateway_url: str, manifest_url: str, token: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.manifest_url = manifest_url.rstrip("/")
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None
        self._cached_tools: Optional[List[Dict[str, Any]]] = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "awnode/1.0",
            },
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Fetch tool manifest from the cloud gateway."""
        if self._cached_tools is not None:
            return self._cached_tools

        assert self._client is not None
        try:
            resp = await self._client.get(self.manifest_url)
            if resp.status_code == 401:
                logger.error("Authentication failed (401). Run 'aither login' to refresh your token.")
                return []
            if resp.status_code == 403:
                logger.error("Access denied (403). Your account may not have MCP access.")
                return []
            resp.raise_for_status()
            data = resp.json()
            # Manifest may be {tools: [...]} or a raw list
            tools = data.get("tools", data) if isinstance(data, dict) else data
            self._cached_tools = tools
            return tools
        except httpx.HTTPStatusError as e:
            logger.error("Failed to fetch tool manifest: %s", e)
            return []
        except httpx.ConnectError:
            logger.error("Cannot reach cloud gateway at %s", self.manifest_url)
            return []

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Forward a tool call to the cloud gateway via MCP protocol."""
        assert self._client is not None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            resp = await self._client.post(self.gateway_url, json=payload)
            if resp.status_code == 401:
                return "Error: authentication failed (401). Run 'aither login' to refresh your token."
            resp.raise_for_status()
            data = resp.json()
            # MCP response: {result: {content: [{type, text}]}}
            result = data.get("result", data)
            if isinstance(result, dict) and "content" in result:
                parts = result["content"]
                return "\n".join(p.get("text", str(p)) for p in parts)
            return json.dumps(result, indent=2)
        except httpx.HTTPStatusError as e:
            return f"Error: gateway returned {e.response.status_code}: {e.response.text[:500]}"
        except httpx.ConnectError:
            return f"Error: cannot reach cloud gateway at {self.gateway_url}"
        except Exception as e:
            return f"Error: {e}"


# ── Server setup ──────────────────────────────────────────────────────────

def _build_mcp_app(mode: str, port: int):
    """Build the MCP Server and Starlette app.

    Returns:
        (starlette_app, cleanup_coro) tuple.
    """
    try:
        from mcp.server import Server
        from mcp.server.streamable_http import StreamableHTTPServerTransport
        from mcp.types import TextContent, Tool
    except ImportError:
        print(
            "Error: the 'mcp' package is required. Install it with:\n"
            "  pip install mcp\n",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from starlette.applications import Starlette
        from starlette.routing import Mount
    except ImportError:
        print(
            "Error: the 'starlette' package is required. Install it with:\n"
            "  pip install starlette\n",
            file=sys.stderr,
        )
        sys.exit(1)

    server = Server("aither-node")
    proxy: Optional[ProxyMCPNode] = None
    local_tool_map: Dict[str, Any] = {}

    if mode == "proxy":
        token = _load_token()
        if not token:
            print(
                "Error: no auth token found.\n"
                "  Run 'aither login' to authenticate, or use --mode standalone.\n",
                file=sys.stderr,
            )
            sys.exit(1)
        proxy = ProxyMCPNode(
            gateway_url=CLOUD_GATEWAY_URL,
            manifest_url=CLOUD_MANIFEST_URL,
            token=token,
        )
    else:
        # Standalone: import local tools
        from adk.node.local_tools import TOOL_DEFINITIONS
        for defn in TOOL_DEFINITIONS:
            local_tool_map[defn["name"]] = defn

    # ── MCP handlers ──────────────────────────────────────────────────

    @server.list_tools()
    async def handle_list_tools() -> List[Tool]:
        if proxy:
            raw_tools = await proxy.list_tools()
            tools = []
            for t in raw_tools:
                tools.append(Tool(
                    name=t.get("name", "unknown"),
                    description=t.get("description", ""),
                    inputSchema=t.get("inputSchema", t.get("input_schema", {"type": "object", "properties": {}})),
                ))
            return tools
        else:
            return [
                Tool(
                    name=defn["name"],
                    description=defn["description"],
                    inputSchema=defn["inputSchema"],
                )
                for defn in local_tool_map.values()
            ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent]:
        if proxy:
            result_text = await proxy.call_tool(name, arguments)
            return [TextContent(type="text", text=result_text)]
        else:
            defn = local_tool_map.get(name)
            if not defn:
                return [TextContent(
                    type="text",
                    text=f"Error: unknown tool '{name}'. Available: {', '.join(local_tool_map.keys())}",
                )]
            fn = defn["fn"]
            try:
                result = fn(**arguments)
                return [TextContent(type="text", text=result)]
            except TypeError as e:
                return [TextContent(type="text", text=f"Error: invalid arguments: {e}")]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]

    # ── Starlette app with streamable-http transport ──────────────────

    transport = StreamableHTTPServerTransport(
        mcp_endpoint="/mcp",
        is_stateless=True,
    )

    async def handle_mcp(scope, receive, send):
        await transport.handle(scope, receive, send)

    # Connect server to transport on startup
    async def on_startup():
        if proxy:
            await proxy.start()
            tools = await proxy.list_tools()
            logger.info("Proxy mode: %d tools from cloud gateway", len(tools))
        else:
            logger.info("Standalone mode: %d local tools", len(local_tool_map))

        # Run the MCP server session in background
        asyncio.create_task(server.run(transport))

    async def on_shutdown():
        if proxy:
            await proxy.stop()

    app = Starlette(
        routes=[Mount("/mcp", app=handle_mcp)],
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
    )

    # Add a simple health endpoint
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(request):
        tool_count = len(local_tool_map) if not proxy else (len(proxy._cached_tools or []))
        return JSONResponse({
            "status": "ok",
            "mode": mode,
            "tools": tool_count,
            "port": port,
        })

    app.routes.insert(0, Route("/health", health))

    return app


def run_node(mode: str = "proxy", port: int = DEFAULT_PORT) -> None:
    """Start the awnode MCP server.

    Args:
        mode: 'proxy' to forward to cloud gateway, 'standalone' for local tools.
        port: HTTP port to listen on (default: 8182).
    """
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is required. Install it with:\n"
            "  pip install uvicorn\n",
            file=sys.stderr,
        )
        sys.exit(1)

    banner = (
        f"\n"
        f"  awnode MCP Server\n"
        f"  Mode: {mode}\n"
        f"  Port: {port}\n"
        f"  MCP endpoint: http://localhost:{port}/mcp\n"
        f"  Health check: http://localhost:{port}/health\n"
    )
    if mode == "proxy":
        banner += f"  Gateway: {CLOUD_GATEWAY_URL}\n"
    else:
        from adk.node.local_tools import TOOL_DEFINITIONS
        banner += f"  Tools: {len(TOOL_DEFINITIONS)} local tools\n"
    banner += "\n"
    print(banner)

    app = _build_mcp_app(mode=mode, port=port)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
