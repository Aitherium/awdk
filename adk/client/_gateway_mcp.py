"""Gateway MCP Client for self-hosted agents.

Wires the agent's MCP client to the gateway (mcp.aitherium.com) authenticated as the owner.
This allows self-hosted agents to access platform tools and secrets.

Authentication hierarchy:
  1. Device-flow token (aither_pat_*) from AitherIdentity — preferred for self-hosted
  2. ACTA key (aither_sk_live_*) — cloud billing-backed
  3. Identity Bearer token — internal admin
  4. Local AitherSecrets self-mint (fallback only)

Fail-closed: no token => log error, MCP stays disabled (agent still runs locally).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("adk.client.gateway_mcp")

# The device-flow bearer the local MCP stdio bridge re-reads on EVERY reconnect
# (CLAUDE.md, dispatch rung 1). mint_session_bearer.py rewrites it, so reading the file
# rather than caching an env var is what lets a re-mint reach a running daemon.
SESSION_BEARER_FILE = os.path.join(os.path.expanduser("~"), ".aither", "session-bearer")


def _read_session_bearer() -> str:
    try:
        with open(SESSION_BEARER_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _resolve_gateway_token() -> str:
    """Pick the credential the gateway will actually accept.

    Measured 2026-09-10: the daemon was launched with AITHER_API_KEY set to an
    `aither_sk_live_*` ACTA key. The LOCAL gateway (127.0.0.1:8182) answered that key
    with 401 invalid_token on every single connect, so the daemon came up with
    `tools.mode=builtin-only, registered=0` -- which check_adk_daemon_capability.py
    correctly calls inert, so the watchdog killed and relaunched it, and the replacement
    got the same 401. That is a kill loop: :9001 was destroyed every ~3 minutes for hours
    and `awsh` was dead in the terminal every time the owner reached for it. The token
    that DID work (200) was sitting unread in ~/.aither/session-bearer the whole time.

    So the device-flow bearer file outranks the static key here. A static gateway key is
    break-glass (CLAUDE.md, secrets-access.md) and goes last, not first.
    """
    return (
        os.getenv("AITHER_MCP_KEY", "").strip()
        or _read_session_bearer()
        or os.getenv("AITHER_API_KEY", "").strip()
    )


class GatewayMCPClient:
    """Connects self-hosted agent to gateway MCP endpoint.

    Usage:
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_xxxx"
        )
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("get_secret", {"name": "github_token"})
    """

    def __init__(
        self,
        gateway_url: str = "",
        api_key: str = "",
        timeout: float = 30.0,
    ):
        """Initialize gateway MCP client.

        Args:
            gateway_url: MCP gateway endpoint (default: https://mcp.aitherium.com)
            api_key: Bearer token for authentication (aither_pat_*, aither_sk_live_*, etc.)
            timeout: HTTP request timeout in seconds
        """
        self.gateway_url = (
            gateway_url or os.getenv("AITHER_GATEWAY_URL", "https://mcp.aitherium.com")
        ).rstrip("/")
        self.api_key = api_key or _resolve_gateway_token()
        self._timeout = timeout
        self._connected = False
        self._request_id = 0
        # MCP StreamableHTTP session. The server issues an Mcp-Session-Id on `initialize`
        # and rejects every later call without it ("400 Bad Request: Missing session ID").
        self._session_id: str = ""

    def _next_id(self) -> int:
        """Generate next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        """Build request headers.

        The Accept header is REQUIRED, not cosmetic: an MCP StreamableHTTP server rejects a
        request without it as ``406 Not Acceptable: Client must accept application/json``.
        Because list_tools() treats any >=400 as "no tools" and returns [], omitting it made
        the agent silently report ZERO tools while logging a successful connection — an
        inert tool surface that looks like parity. Measured against the local gateway
        (127.0.0.1:8182) 2026-07-29.
        """
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _ensure_session(self) -> bool:
        """Perform the MCP `initialize` handshake and capture the session id.

        MCP StreamableHTTP is not plain JSON-RPC-over-POST: a client must `initialize`
        first, then send every subsequent request with the returned Mcp-Session-Id. Without
        this the gateway answers "400 Bad Request: Missing session ID", list_tools() maps
        that to [], and the agent runs with ZERO platform tools while reporting a healthy
        connection. Idempotent — a live session is reused.
        """
        if self._session_id:
            return True
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
                resp = await client.post(
                    f"{self.gateway_url}/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "awdk", "version": "1.0"},
                        },
                    },
                )
                if resp.status_code == 401:
                    # A 401 here is usually a bearer that was re-minted under a
                    # long-lived daemon, so re-read the file before giving up. Without
                    # this the ONLY way to pick up a fresh token is a restart -- and the
                    # watchdog's restart is exactly what turned one stale token into a
                    # kill loop (see _resolve_gateway_token).
                    fresh = _read_session_bearer()
                    if fresh and fresh != self.api_key:
                        logger.info("MCP initialize 401 - retrying with the re-minted session bearer")
                        self.api_key = fresh
                        return await self._ensure_session()
                    logger.warning(
                        "MCP initialize failed (401): %s -- the gateway rejected this "
                        "credential. Re-mint with: python AitherOS/dev/tools/mint_session_bearer.py",
                        resp.text[:160],
                    )
                    return False
                if resp.status_code >= 400:
                    logger.warning(
                        "MCP initialize failed (%d): %s", resp.status_code, resp.text[:160]
                    )
                    return False
                # The id arrives as a response header, not in the JSON body.
                self._session_id = (
                    resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id") or ""
                )
                if not self._session_id:
                    logger.warning("MCP initialize returned no Mcp-Session-Id header")
                    return False
                # Best-effort: the spec expects this notification after initialize.
                try:
                    await client.post(
                        f"{self.gateway_url}/mcp",
                        headers=self._headers(),
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                except httpx.HTTPError:
                    pass
                return True
        except httpx.HTTPError as exc:
            logger.warning("MCP initialize error: %s", exc)
            return False

    async def connect(self) -> bool:
        """Test connection to gateway.

        Returns True if gateway is reachable and responds to /health.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers()
            ) as client:
                resp = await client.get(f"{self.gateway_url}/health")
            self._connected = resp.status_code == 200
            if self._connected:
                logger.info("Gateway MCP client connected: %s", self.gateway_url)
            else:
                logger.warning("Gateway returned %d", resp.status_code)
            return self._connected
        except httpx.ConnectError as exc:
            logger.warning("Gateway unreachable at %s: %s", self.gateway_url, exc)
            return False
        except Exception as exc:
            logger.warning("Gateway connection check failed: %s", exc)
            return False

    async def ping(self) -> bool:
        """Cheap liveness probe that exercises the REAL MCP path.

        `connect()` is not a substitute: it GETs `/health`, and this gateway can answer 200
        while `/mcp` is unresponsive. That mismatch can mask real problems. `list_tools()`
        does exercise `/mcp`, but it costs 3.2s and 366 KB (1,227 tool schemas), which is
        far too heavy to run on a watch interval.
        The JSON-RPC `ping` method measures **8ms / 36 bytes** against the same session
        and endpoint, so loss can be detected in seconds instead of minutes for free.

        Returns False on ANY failure — unreachable, auth, malformed, JSON-RPC error — so
        callers get a fail-closed liveness answer rather than an exception to forget.
        """
        if not self.api_key:
            return False
        if not await self._ensure_session():
            return False
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers()
            ) as client:
                resp = await client.post(
                    f"{self.gateway_url}/mcp",
                    json={"jsonrpc": "2.0", "method": "ping", "id": self._next_id()},
                )
            if resp.status_code != 200:
                return False
            return "error" not in resp.json()
        except Exception as exc:  # noqa: BLE001 — a probe must answer, never raise
            logger.debug("Gateway ping failed: %s", exc)
            return False

    async def list_tools(self) -> list[dict]:
        """Discover available tools on gateway.

        Returns list of tool dicts with name, description, parameters.
        Filters by caller's tier and RBAC.
        """
        if not self.api_key:
            logger.error("Cannot list tools: no API key configured")
            return []
        if not await self._ensure_session():
            return []

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers()
            ) as client:
                resp = await client.post(
                    f"{self.gateway_url}/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "tools/list",
                        "id": self._next_id(),
                    },
                )

            if resp.status_code == 401:
                logger.error("Authorization failed: invalid or expired token")
                return []
            if resp.status_code == 402:
                logger.warning("Insufficient balance (402)")
                return []
            if resp.status_code >= 400:
                logger.warning("Gateway returned %d: %s", resp.status_code, resp.text[:200])
                return []

            data = resp.json()

            # Check for JSON-RPC error
            if "error" in data:
                err = data["error"]
                logger.error(
                    "MCP error %d: %s",
                    err.get("code", -1),
                    err.get("message", ""),
                )
                return []

            tools = []
            for t in data.get("result", {}).get("tools", []):
                tools.append({
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {}),
                })
            return tools

        except httpx.ConnectError:
            logger.warning("Gateway unreachable")
            return []
        except Exception as exc:
            logger.warning("Tool discovery failed: %s", exc)
            return []

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool on the gateway.

        Args:
            name: Tool name
            arguments: Tool arguments dict

        Returns:
            Result dict from MCP response (may contain error)
        """
        if not self.api_key:
            return {"error": "no_api_key", "message": "API key not configured"}

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._headers()
            ) as client:
                resp = await client.post(
                    f"{self.gateway_url}/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments or {}},
                        "id": self._next_id(),
                    },
                )

            if resp.status_code == 401:
                logger.error("Authorization failed")
                return {"error": "auth_failed", "message": "Invalid or expired token"}
            if resp.status_code == 402:
                logger.warning("Insufficient balance")
                return {"error": "insufficient_balance", "message": "No Aitherium tokens"}
            if resp.status_code == 403:
                logger.warning("Access denied to tool: %s", name)
                return {"error": "access_denied", "message": f"Tool '{name}' not allowed"}
            if resp.status_code >= 400:
                logger.warning("Gateway error %d: %s", resp.status_code, resp.text[:200])
                return {"error": "gateway_error", "status": resp.status_code}

            data = resp.json()

            # Check for JSON-RPC error
            if "error" in data:
                err = data["error"]
                return {
                    "error": "mcp_error",
                    "code": err.get("code", -1),
                    "message": err.get("message", ""),
                }

            result = data.get("result", {})
            content = result.get("content", [])

            # Extract text from content array
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part["text"])
                elif isinstance(part, str):
                    texts.append(part)

            return {
                "success": True,
                "text": "\n".join(texts) if texts else "",
                "content": content,
                "raw": result,
            }

        except httpx.ConnectError:
            logger.warning("Gateway unreachable")
            return {"error": "gateway_unreachable"}
        except Exception as exc:
            logger.warning("Tool call failed: %s", exc)
            return {"error": "call_failed", "message": str(exc)}


async def create_gateway_mcp_client(
    gateway_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[GatewayMCPClient]:
    """Factory function to create and test gateway MCP client.

    Args:
        gateway_url: Override gateway URL
        api_key: Override API key

    Returns:
        Connected client, or None if no token available or connection failed.
        Never raises — fails soft.
    """
    # Resolve API key in order of preference. The session-bearer file is in the chain
    # (see _resolve_gateway_token) because a caller-supplied key can be the WRONG kind:
    # server.py passes config.aither_api_key, an `aither_sk_live_*` ACTA key, which the
    # LOCAL gateway rejects with 401 while the device-flow bearer in
    # ~/.aither/session-bearer answers 200. _ensure_session retries once with the file
    # on a 401 for exactly that case; this covers the case where there is no key at all.
    key = api_key or _resolve_gateway_token()

    if not key:
        logger.debug("Gateway MCP: no API key configured (not an error)")
        return None

    # Resolve gateway URL
    url = (
        gateway_url
        or os.getenv("AITHER_GATEWAY_URL", "")
        or "https://mcp.aitherium.com"
    )

    try:
        client = GatewayMCPClient(gateway_url=url, api_key=key)
        if await client.connect():
            return client
        else:
            logger.warning("Gateway MCP connection failed (continuing offline)")
            return None
    except Exception as exc:  # noqa: BLE001 — must never crash startup
        logger.warning("Gateway MCP client creation failed: %s (continuing offline)", exc)
        return None
