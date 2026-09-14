"""MCP bridge — connect ADK agents to AitherOS tools via mcp.aitherium.com.

LAYER: Enterprise client bridge for AitherOS MCP gateway and awnode.
ROLE: Provides full authentication (ACTA billing, Identity tokens, external
       agent keys), token balance tracking, and tool discovery/invocation.
       NOT a generic MCP adapter — specific to AitherOS platform.

See also: adk.core.mcp — minimal generic JSON-RPC adapter for any MCP server.

Supports three authentication modes:

1. **ACTA API keys** (``aither_sk_live_*``) — billing-backed keys from the
   Aither Credit & Token Authority. Validated against /v1/billing/balance.
   Token balance is tracked and deducted per tool call.

2. **AitherIdentity tokens** — bearer tokens from AitherIdentity /auth/me.
   Used for admin access and internal users.

3. **External agent keys** (``aither_ext_*``) — keys issued to third-party
   agents via the developer portal. Validated against the ExternalAgentRegistry.

Auth flow for external developers:
    auth = MCPAuth(api_key="aither_sk_live_xxxx")
    await auth.authenticate()  # validates key, gets balance + tier
    bridge = MCPBridge(auth=auth)
    tools = await bridge.list_tools()  # filtered by tier
    result = await bridge.call_tool("explore_code", {"query": "..."})

Auth flow for local awnode:
    bridge = MCPBridge(mcp_url="http://localhost:8080")
    # No auth needed for local — tools available directly
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("adk.mcp")

_DEFAULT_MCP_URL = "https://mcp.aitherium.com"

# Key prefixes
ACTA_KEY_PREFIX = "aither_sk_live_"
EXT_AGENT_KEY_PREFIX = "aither_ext_"

# Persistent key cache
_KEY_CACHE_PATH = Path(
    os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
) / "mcp_key_cache.json"


# ─────────────────────────────────────────────────────────────────────────────
# Auth context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AuthContext:
    """Resolved identity from MCP gateway authentication."""
    user_id: str = ""
    tenant_id: str = ""
    tier: str = "free"           # free, pro, enterprise
    plan: str = "explorer"       # explorer, builder, enterprise
    roles: list[str] = field(default_factory=list)
    token_balance: int = 0       # Remaining Aitherium tokens (ACTA)
    key_type: str = ""           # acta, identity, ext_agent, admin_key
    authenticated: bool = False

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles or "super_admin" in self.roles

    @property
    def has_balance(self) -> bool:
        return self.token_balance > 0


# ─────────────────────────────────────────────────────────────────────────────
# Auth manager
# ─────────────────────────────────────────────────────────────────────────────

class MCPAuth:
    """Handles authentication against the MCP gateway.

    Supports ACTA keys, Identity tokens, external agent keys, and
    legacy admin keys. Caches credentials to disk to avoid re-auth
    on every startup.

    Usage:
        auth = MCPAuth(api_key="aither_sk_live_xxxx")
        await auth.authenticate()
        if auth.context.authenticated:
            print(f"Tier: {auth.context.tier}, Balance: {auth.context.token_balance}")
    """

    def __init__(
        self,
        api_key: str = "",
        gateway_url: str = "",
        timeout: float = 10.0,
    ):
        self.api_key = (
            api_key
            or os.getenv("AITHER_API_KEY", "")
            or os.getenv("AITHER_MCP_KEY", "")
            or os.getenv("MCP_SERVICE_TOKEN", "")
        )
        self.gateway_url = (
            gateway_url
            or os.getenv("AITHER_GATEWAY_URL", "")
            or os.getenv("AITHER_MCP_URL", "")
            or _DEFAULT_MCP_URL
        ).rstrip("/")
        self._timeout = timeout
        self._context = AuthContext()
        self._last_validated: float = 0.0
        self._validation_ttl: float = 300.0  # 5 minutes

    @property
    def context(self) -> AuthContext:
        return self._context

    @property
    def authenticated(self) -> bool:
        return self._context.authenticated

    @property
    def headers(self) -> dict[str, str]:
        """Return auth headers for API calls."""
        if not self.api_key:
            return {}
        h: dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
        # Identity API keys also send X-API-Key
        if self._context.key_type == "identity":
            h["X-API-Key"] = self.api_key
        return h

    def _detect_key_type(self) -> str:
        """Detect auth key type from prefix."""
        if not self.api_key:
            return ""
        if self.api_key.startswith(ACTA_KEY_PREFIX):
            return "acta"
        if self.api_key.startswith(EXT_AGENT_KEY_PREFIX):
            return "ext_agent"
        if self.api_key.startswith("aither_"):
            return "identity"
        # Legacy admin key or opaque token
        return "admin_key"

    async def authenticate(self) -> AuthContext:
        """Validate the API key and resolve identity.

        Routes to the appropriate verification endpoint based on key prefix.
        Results are cached for 5 minutes.
        """
        if not self.api_key:
            logger.warning("MCPAuth: no API key provided")
            self._context = AuthContext()
            return self._context

        # Check if recent validation is still fresh
        if (
            self._context.authenticated
            and time.time() - self._last_validated < self._validation_ttl
        ):
            return self._context

        # Try cached key from disk first
        cached = self._load_cached_context()
        if cached and cached.authenticated:
            self._context = cached
            self._last_validated = time.time()
            logger.debug("MCPAuth: using cached auth context")
            return self._context

        key_type = self._detect_key_type()
        self._context.key_type = key_type

        try:
            if key_type == "acta":
                await self._verify_acta()
            elif key_type == "ext_agent":
                await self._verify_ext_agent()
            elif key_type == "identity":
                await self._verify_identity()
            else:
                # Admin key / opaque token — try Identity endpoint
                await self._verify_identity()
        except Exception as exc:
            logger.warning("MCPAuth: authentication failed: %s", exc)
            self._context.authenticated = False

        if self._context.authenticated:
            self._last_validated = time.time()
            self._save_cached_context()

        return self._context

    async def refresh(self) -> AuthContext:
        """Force re-validation of the current key."""
        self._last_validated = 0.0
        self._context.authenticated = False
        return await self.authenticate()

    def invalidate(self):
        """Clear cached auth state (e.g., after a 401/403 response)."""
        self._context = AuthContext()
        self._last_validated = 0.0
        try:
            if _KEY_CACHE_PATH.exists():
                _KEY_CACHE_PATH.unlink()
        except Exception:
            pass

    # ── ACTA verification ─────────────────────────────────────────────────

    async def _verify_acta(self):
        """Validate ACTA key against /v1/billing/balance."""
        url = f"{self.gateway_url}/v1/billing/balance"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )

            if resp.status_code == 401:
                logger.error("MCPAuth: ACTA key invalid (401)")
                return
            if resp.status_code == 402:
                logger.warning("MCPAuth: ACTA key has zero balance (402)")
                data = resp.json()
                self._context.user_id = data.get("user_id", "")
                self._context.token_balance = 0
                self._context.plan = data.get("plan", "explorer")
                self._context.tier = _plan_to_tier(self._context.plan)
                self._context.authenticated = True
                return
            if resp.status_code != 200:
                logger.warning("MCPAuth: ACTA returned %d", resp.status_code)
                return

            data = resp.json()
            self._context.user_id = data.get("user_id", "")
            self._context.tenant_id = data.get("user_id", "")
            self._context.token_balance = data.get(
                "tokens", data.get("balance", 0)
            )
            self._context.plan = data.get("plan", "explorer")
            self._context.tier = _plan_to_tier(self._context.plan)
            self._context.roles = [self._context.plan]
            self._context.authenticated = True
            logger.info(
                "MCPAuth: ACTA authenticated — plan=%s, balance=%d",
                self._context.plan,
                self._context.token_balance,
            )
        except httpx.ConnectError:
            # Gateway unreachable — try to use cached context
            logger.warning("MCPAuth: gateway unreachable at %s", self.gateway_url)
        except Exception as exc:
            logger.warning("MCPAuth: ACTA verification failed: %s", exc)

    # ── Identity verification ─────────────────────────────────────────────

    async def _verify_identity(self):
        """Validate bearer token against gateway /auth/me relay."""
        # The gateway proxies to AitherIdentity /auth/me
        url = f"{self.gateway_url}/auth/me"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "X-API-Key": self.api_key,
                    },
                )

            if resp.status_code != 200:
                logger.warning("MCPAuth: Identity token invalid (%d)", resp.status_code)
                return

            data = resp.json()
            self._context.user_id = data.get("id") or data.get("username", "")
            self._context.tenant_id = data.get("tenant_id", self._context.user_id)
            self._context.roles = data.get("roles", [])

            if any(r in self._context.roles for r in ("enterprise", "super_admin", "admin")):
                self._context.tier = "enterprise"
            elif any(r in self._context.roles for r in ("pro", "developer", "operator")):
                self._context.tier = "pro"
            else:
                self._context.tier = "free"

            self._context.plan = self._context.tier
            self._context.token_balance = 999999  # Identity users aren't metered
            self._context.authenticated = True
            logger.info("MCPAuth: Identity authenticated — user=%s, tier=%s",
                        self._context.user_id, self._context.tier)
        except httpx.ConnectError:
            logger.warning("MCPAuth: gateway unreachable at %s", self.gateway_url)
        except Exception as exc:
            logger.warning("MCPAuth: Identity verification failed: %s", exc)

    # ── External agent key verification ───────────────────────────────────

    async def _verify_ext_agent(self):
        """Validate external agent key against gateway /auth/agent relay."""
        url = f"{self.gateway_url}/auth/agent"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url,
                    json={"api_key": self.api_key},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )

            if resp.status_code != 200:
                logger.warning("MCPAuth: ext agent key invalid (%d)", resp.status_code)
                return

            data = resp.json()
            self._context.user_id = data.get("agent_id", "")
            self._context.tenant_id = data.get("owner_tenant_id", self._context.user_id)
            self._context.tier = data.get("tier", "free")
            self._context.plan = data.get("tier", "explorer")
            self._context.roles = [data.get("tier", "explorer")]
            self._context.token_balance = 999999
            self._context.authenticated = True
            logger.info("MCPAuth: ext agent authenticated — agent=%s, tier=%s",
                        self._context.user_id, self._context.tier)
        except httpx.ConnectError:
            logger.warning("MCPAuth: gateway unreachable at %s", self.gateway_url)
        except Exception as exc:
            logger.warning("MCPAuth: ext agent verification failed: %s", exc)

    # ── Key cache ─────────────────────────────────────────────────────────

    def _load_cached_context(self) -> AuthContext | None:
        """Load cached auth context from disk."""
        try:
            if not _KEY_CACHE_PATH.exists():
                return None
            data = json.loads(_KEY_CACHE_PATH.read_text(encoding="utf-8"))
            # Reject stale cache (>24h)
            if time.time() - data.get("_ts", 0) > 86400:
                return None
            # Only use if it's for the same key
            if data.get("api_key_hash") != _hash_key(self.api_key):
                return None
            return AuthContext(
                user_id=data.get("user_id", ""),
                tenant_id=data.get("tenant_id", ""),
                tier=data.get("tier", "free"),
                plan=data.get("plan", "explorer"),
                roles=data.get("roles", []),
                token_balance=data.get("token_balance", 0),
                key_type=data.get("key_type", ""),
                authenticated=True,
            )
        except Exception:
            return None

    def _save_cached_context(self):
        """Persist auth context to disk."""
        try:
            _KEY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _KEY_CACHE_PATH.write_text(
                json.dumps({
                    "api_key_hash": _hash_key(self.api_key),
                    "user_id": self._context.user_id,
                    "tenant_id": self._context.tenant_id,
                    "tier": self._context.tier,
                    "plan": self._context.plan,
                    "roles": self._context.roles,
                    "token_balance": self._context.token_balance,
                    "key_type": self._context.key_type,
                    "_ts": time.time(),
                }),
                encoding="utf-8",
            )
            try:
                os.chmod(str(_KEY_CACHE_PATH), 0o600)
            except OSError:
                pass
        except Exception as exc:
            logger.debug("MCPAuth: failed to cache context: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# MCPTool
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MCPTool:
    """A tool available via MCP."""
    name: str
    description: str
    parameters: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# MCPBridge
# ─────────────────────────────────────────────────────────────────────────────

class MCPBridge:
    """Client for AitherOS MCP gateway at mcp.aitherium.com.

    Lets ADK agents use AitherOS tools (code search, memory, agent dispatch,
    etc.) without running the full stack locally.

    Usage:
        # With auth manager (recommended for cloud)
        auth = MCPAuth(api_key="aither_sk_live_xxxx")
        await auth.authenticate()
        bridge = MCPBridge(auth=auth)
        tools = await bridge.list_tools()
        result = await bridge.call_tool("explore_code", {"query": "agent dispatch"})

        # Simple mode (local awnode, no auth needed)
        bridge = MCPBridge(mcp_url="http://localhost:8080")
        tools = await bridge.list_tools()

        # Legacy mode (bare API key)
        bridge = MCPBridge(api_key="your-key")
    """

    def __init__(
        self,
        mcp_url: str = "",
        api_key: str = "",
        auth: MCPAuth | None = None,
        timeout: float = 30.0,
    ):
        self._auth = auth
        if auth:
            self.mcp_url = auth.gateway_url
            self.api_key = auth.api_key
        else:
            self.mcp_url = (mcp_url or _DEFAULT_MCP_URL).rstrip("/")
            self.api_key = api_key
        self._timeout = timeout
        self._tools_cache: list[MCPTool] | None = None
        self._request_id = 0
        self._session_id: str | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        # MCP StreamableHTTP REQUIRES both media types in Accept. The cloud
        # worker tolerates its absence; a spec-strict gateway answers 406 — so
        # this bridge worked against mcp.aitherium.com and failed against the
        # local gateway, which reads as "gateway broken" rather than a missing
        # header.
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._auth and self._auth.authenticated:
            h.update(self._auth.headers)
        elif self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, headers=self._headers())

    @staticmethod
    def _parse_rpc(resp: httpx.Response) -> dict:
        """Parse a StreamableHTTP response — plain JSON or an SSE event stream."""
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            payload = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
            if payload is None:
                raise MCPError("SSE response carried no data event")
            return json.loads(payload)
        return resp.json()

    async def _ensure_session(self, client: httpx.AsyncClient) -> None:
        """Perform the MCP initialize handshake once per bridge.

        Spec-strict gateways reject bare tools/list with "Missing session ID";
        the session id arrives in the Mcp-Session-Id response header.
        """
        if self._session_id:
            return
        resp = await client.post(
            f"{self.mcp_url}/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "awdk", "version": "3.x"},
                },
                "id": self._next_id(),
            },
        )
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
            # Per-request client: session header must reach THIS client too.
            client.headers["Mcp-Session-Id"] = sid
            await client.post(
                f"{self.mcp_url}/mcp",
                json={"jsonrpc": "2.0",
                      "method": "notifications/initialized"},
            )

    async def list_tools(self, refresh: bool = False) -> list[MCPTool]:
        """List available tools from the MCP gateway.

        Tools may be filtered by the gateway based on auth tier:
        - free/explorer: ~50 read-only tools
        - pro/builder: ~200 tools including write operations
        - enterprise: All 449+ tools
        """
        if self._tools_cache and not refresh:
            return self._tools_cache

        async with self._client() as client:
            await self._ensure_session(client)
            resp = await client.post(
                f"{self.mcp_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/list",
                    "id": self._next_id(),
                },
            )
            # Handle auth errors
            if resp.status_code == 401:
                raise MCPAuthError("Authentication required — provide an API key")
            if resp.status_code == 402:
                raise MCPBalanceError("Insufficient Aitherium token balance")
            if resp.status_code == 403:
                raise MCPAuthError("Access denied — insufficient permissions")
            resp.raise_for_status()
            data = self._parse_rpc(resp)

        # Check for JSON-RPC error
        if "error" in data:
            err = data["error"]
            raise MCPError(f"MCP error {err.get('code', -1)}: {err.get('message', '')}")

        tools = []
        for t in data.get("result", {}).get("tools", []):
            tools.append(MCPTool(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("inputSchema", {}),
            ))
        self._tools_cache = tools
        return tools

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call a tool on the MCP gateway and return the result as string.

        Handles auth errors gracefully:
        - 401: Attempts to re-authenticate once, then raises
        - 402: Raises MCPBalanceError with balance info
        - 403: Raises MCPAuthError
        """
        resp = await self._do_call(name, arguments)

        # If 401 and we have an auth manager, try refreshing
        if resp.status_code == 401 and self._auth:
            logger.info("MCPBridge: got 401, attempting token refresh")
            await self._auth.refresh()
            resp = await self._do_call(name, arguments)

        if resp.status_code == 401:
            raise MCPAuthError("Authentication failed")
        if resp.status_code == 402:
            raise MCPBalanceError("Insufficient Aitherium token balance")
        if resp.status_code == 403:
            raise MCPAuthError(f"Tool '{name}' not allowed for your tier")
        resp.raise_for_status()

        data = self._parse_rpc(resp)

        # Check JSON-RPC error
        if "error" in data:
            err = data["error"]
            code = err.get("code", -1)
            msg = err.get("message", "")
            if code == -32001:  # Custom: tool not found
                raise MCPToolNotFound(f"Tool '{name}' not found")
            raise MCPError(f"MCP error {code}: {msg}")

        result = data.get("result", {})
        content_parts = result.get("content", [])

        # Concatenate text content
        texts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part["text"])
            elif isinstance(part, str):
                texts.append(part)

        return "\n".join(texts) if texts else json.dumps(result)

    async def _do_call(self, name: str, arguments: dict | None) -> httpx.Response:
        """Execute a JSON-RPC tool call, returning the raw response."""
        async with self._client() as client:
            await self._ensure_session(client)
            return await client.post(
                f"{self.mcp_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                    "id": self._next_id(),
                },
            )

    async def register_tools(self, agent) -> int:
        """Register all MCP tools into an AitherAgent's tool registry.

        Returns the number of tools registered.
        """
        tools = await self.list_tools()
        count = 0

        for mcp_tool in tools:
            # Create a closure for each tool
            tool_name = mcp_tool.name

            async def _call(bridge=self, tn=tool_name, **kwargs) -> str:
                return await bridge.call_tool(tn, kwargs)

            _call.__name__ = tool_name
            _call.__doc__ = mcp_tool.description

            agent._tools.register(
                _call,
                name=tool_name,
                description=mcp_tool.description,
            )
            count += 1

        return count

    async def health(self) -> bool:
        """Check if the MCP gateway is reachable."""
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.mcp_url}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def get_balance(self) -> dict[str, Any]:
        """Get current ACTA balance (only for ACTA keys)."""
        if self._auth and self._auth.context.key_type == "acta":
            await self._auth.refresh()
            return {
                "balance": self._auth.context.token_balance,
                "plan": self._auth.context.plan,
                "tier": self._auth.context.tier,
            }
        return {"error": "Balance check only available for ACTA keys"}

    async def get_tier_info(self) -> dict[str, Any]:
        """Get information about the current auth tier and tool access."""
        if self._auth and self._auth.authenticated:
            tools = await self.list_tools()
            return {
                "tier": self._auth.context.tier,
                "plan": self._auth.context.plan,
                "tools_available": len(tools),
                "user_id": self._auth.context.user_id,
                "authenticated": True,
            }
        return {
            "tier": "anonymous",
            "tools_available": 0,
            "authenticated": False,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class MCPError(Exception):
    """Base MCP error."""
    pass


class MCPAuthError(MCPError):
    """Authentication or authorization failure."""
    pass


class MCPBalanceError(MCPError):
    """Insufficient Aitherium token balance."""
    pass


class MCPToolNotFound(MCPError):
    """Requested tool does not exist."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_PLAN_TO_TIER = {
    "explorer": "free",
    "creator": "creator",
    "builder": "pro",
    "creator_pro": "creator_pro",
    "enterprise": "enterprise",
    "admin": "enterprise",
}


def _plan_to_tier(plan: str) -> str:
    return _PLAN_TO_TIER.get(plan, "free")


def _hash_key(key: str) -> str:
    """Hash an API key for safe disk storage (never store raw keys)."""
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience
# ─────────────────────────────────────────────────────────────────────────────

async def connect_mcp(
    api_key: str = "",
    mcp_url: str = _DEFAULT_MCP_URL,
) -> MCPBridge:
    """Quick helper to create, authenticate, and verify an MCP bridge.

    Usage:
        bridge = await connect_mcp(api_key="aither_sk_live_xxxx")
        tools = await bridge.list_tools()
    """
    auth = MCPAuth(api_key=api_key, gateway_url=mcp_url)
    if api_key:
        await auth.authenticate()

    bridge = MCPBridge(auth=auth)
    if await bridge.health():
        logger.info("Connected to MCP gateway at %s (tier=%s)",
                     mcp_url, auth.context.tier)
    else:
        logger.warning("MCP gateway at %s unreachable — tools will fail", mcp_url)

    return bridge
