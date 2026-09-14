"""Tests for MCP bridge authentication — ACTA, Identity, ext agent, error handling."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Auth context
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthContext:
    def test_defaults(self):
        from adk.mcp import AuthContext
        ctx = AuthContext()
        assert not ctx.authenticated
        assert ctx.tier == "free"
        assert not ctx.is_admin
        assert not ctx.has_balance

    def test_admin_check(self):
        from adk.mcp import AuthContext
        ctx = AuthContext(roles=["admin"], authenticated=True)
        assert ctx.is_admin
        ctx2 = AuthContext(roles=["super_admin"], authenticated=True)
        assert ctx2.is_admin
        ctx3 = AuthContext(roles=["user"], authenticated=True)
        assert not ctx3.is_admin

    def test_has_balance(self):
        from adk.mcp import AuthContext
        ctx = AuthContext(token_balance=100)
        assert ctx.has_balance
        ctx2 = AuthContext(token_balance=0)
        assert not ctx2.has_balance


# ─────────────────────────────────────────────────────────────────────────────
# Key type detection
# ─────────────────────────────────────────────────────────────────────────────

class TestKeyTypeDetection:
    def test_acta_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_abc123")
        assert auth._detect_key_type() == "acta"

    def test_ext_agent_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_ext_myagent_xyz")
        assert auth._detect_key_type() == "ext_agent"

    def test_identity_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_abc123def456")
        assert auth._detect_key_type() == "identity"

    def test_admin_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="some-random-key-123")
        assert auth._detect_key_type() == "admin_key"

    def test_empty_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="")
        assert auth._detect_key_type() == ""


# ─────────────────────────────────────────────────────────────────────────────
# MCPAuth properties
# ─────────────────────────────────────────────────────────────────────────────

class TestMCPAuthProperties:
    def test_headers_empty_without_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="")
        assert auth.headers == {}

    def test_headers_acta(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_test")
        auth._context.key_type = "acta"
        h = auth.headers
        assert "Authorization" in h
        assert h["Authorization"] == "Bearer aither_sk_live_test"

    def test_headers_identity(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_some_token")
        auth._context.key_type = "identity"
        h = auth.headers
        assert "Authorization" in h
        assert "X-API-Key" in h
        assert h["X-API-Key"] == "aither_some_token"

    def test_not_authenticated_initially(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_test")
        assert not auth.authenticated

    def test_env_var_fallback(self):
        from adk.mcp import MCPAuth
        with patch.dict(os.environ, {"AITHER_API_KEY": "env_key_123"}):
            auth = MCPAuth()
            assert auth.api_key == "env_key_123"

    def test_env_gateway_url(self):
        from adk.mcp import MCPAuth
        with patch.dict(os.environ, {"AITHER_GATEWAY_URL": "https://custom.gateway.com"}):
            auth = MCPAuth()
            assert auth.gateway_url == "https://custom.gateway.com"


# ─────────────────────────────────────────────────────────────────────────────
# ACTA authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestACTAAuth:
    @pytest.mark.asyncio
    async def test_acta_success(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_test123", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "user_id": "user_acta_1",
            "tokens": 5000,
            "balance": 5000,
            "plan": "builder",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert ctx.authenticated
        assert ctx.user_id == "user_acta_1"
        assert ctx.token_balance == 5000
        assert ctx.plan == "builder"
        assert ctx.tier == "pro"
        assert ctx.key_type == "acta"

    @pytest.mark.asyncio
    async def test_acta_invalid_key(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_bad", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert not ctx.authenticated

    @pytest.mark.asyncio
    async def test_acta_zero_balance(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_broke", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "user_id": "user_broke",
            "plan": "explorer",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert ctx.authenticated  # Still authenticated, just no balance
        assert ctx.token_balance == 0
        assert ctx.plan == "explorer"

    @pytest.mark.asyncio
    async def test_acta_gateway_unreachable(self):
        from adk.mcp import MCPAuth
        import httpx as _httpx
        auth = MCPAuth(api_key="aither_sk_live_test", gateway_url="http://test")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=_httpx.ConnectError("refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert not ctx.authenticated

    @pytest.mark.asyncio
    async def test_acta_plan_mapping(self):
        from adk.mcp import MCPAuth
        plans_and_tiers = [
            ("explorer", "free"),
            ("builder", "pro"),
            ("enterprise", "enterprise"),
            ("admin", "enterprise"),
            ("unknown", "free"),
        ]
        for plan, expected_tier in plans_and_tiers:
            auth = MCPAuth(api_key=f"aither_sk_live_{plan}", gateway_url="http://test")
            # Skip disk cache to test pure plan→tier mapping
            auth._load_cached_context = lambda: None
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {
                "user_id": "u1",
                "tokens": 100,
                "plan": plan,
            }
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            with patch("httpx.AsyncClient", return_value=mock_client):
                ctx = await auth.authenticate()
            assert ctx.tier == expected_tier, f"plan={plan} expected tier={expected_tier}"


# ─────────────────────────────────────────────────────────────────────────────
# Identity authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentityAuth:
    @pytest.mark.asyncio
    async def test_identity_success(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_token_abc", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "id": "user123",
            "username": "admin1",
            "tenant_id": "tenant_a",
            "roles": ["admin", "developer"],
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert ctx.authenticated
        assert ctx.user_id == "user123"
        assert ctx.tenant_id == "tenant_a"
        assert ctx.tier == "enterprise"  # has admin role
        assert ctx.token_balance == 999999

    @pytest.mark.asyncio
    async def test_identity_invalid_token(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_bad_token", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert not ctx.authenticated

    @pytest.mark.asyncio
    async def test_identity_tier_resolution(self):
        from adk.mcp import MCPAuth
        cases = [
            (["enterprise"], "enterprise"),
            (["super_admin"], "enterprise"),
            (["admin"], "enterprise"),
            (["pro"], "pro"),
            (["developer"], "pro"),
            (["operator"], "pro"),
            (["user"], "free"),
            ([], "free"),
        ]
        for roles, expected_tier in cases:
            auth = MCPAuth(api_key=f"aither_token_{'-'.join(roles) or 'none'}", gateway_url="http://test")
            auth._load_cached_context = lambda: None
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.headers = {"content-type": "application/json"}
            mock_resp.json.return_value = {
                "id": "u1",
                "roles": roles,
            }
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            with patch("httpx.AsyncClient", return_value=mock_client):
                ctx = await auth.authenticate()
            assert ctx.tier == expected_tier, f"roles={roles} expected={expected_tier}"


# ─────────────────────────────────────────────────────────────────────────────
# External agent authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestExtAgentAuth:
    @pytest.mark.asyncio
    async def test_ext_agent_success(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_ext_mybot_key123", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "agent_id": "mybot",
            "owner_tenant_id": "tenant_ext",
            "tier": "pro",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert ctx.authenticated
        assert ctx.user_id == "mybot"
        assert ctx.tenant_id == "tenant_ext"
        assert ctx.tier == "pro"
        assert ctx.key_type == "ext_agent"

    @pytest.mark.asyncio
    async def test_ext_agent_invalid(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_ext_bad_key", gateway_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert not ctx.authenticated


# ─────────────────────────────────────────────────────────────────────────────
# Token caching
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenCaching:
    @pytest.mark.asyncio
    async def test_validation_ttl_skips_reauth(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_cached", gateway_url="http://test")
        # Pre-populate as if authenticated
        auth._context.authenticated = True
        auth._context.user_id = "cached_user"
        auth._context.tier = "pro"
        auth._last_validated = __import__("time").time()

        # Should return cached without calling any HTTP
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("should not be called"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.authenticate()

        assert ctx.authenticated
        assert ctx.user_id == "cached_user"

    @pytest.mark.asyncio
    async def test_disk_cache_roundtrip(self):
        import adk.mcp
        from adk.mcp import MCPAuth

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "test_cache.json"
            saved_path = adk.mcp._KEY_CACHE_PATH
            adk.mcp._KEY_CACHE_PATH = cache_path

            try:
                # Authenticate to populate cache
                auth = MCPAuth(api_key="aither_sk_live_disk", gateway_url="http://test")
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.headers = {"content-type": "application/json"}
                mock_resp.json.return_value = {
                    "user_id": "disk_user",
                    "tokens": 3000,
                    "plan": "builder",
                }
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client.get = AsyncMock(return_value=mock_resp)

                with patch("httpx.AsyncClient", return_value=mock_client):
                    await auth.authenticate()

                assert cache_path.exists()

                # New auth instance should load from disk
                auth2 = MCPAuth(api_key="aither_sk_live_disk", gateway_url="http://test")
                cached = auth2._load_cached_context()
                assert cached is not None
                assert cached.user_id == "disk_user"
                assert cached.tier == "pro"
            finally:
                adk.mcp._KEY_CACHE_PATH = saved_path

    def test_invalidate_clears_state(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_inv")
        auth._context.authenticated = True
        auth._context.user_id = "test"
        auth._last_validated = 999999
        auth.invalidate()
        assert not auth._context.authenticated
        assert auth._last_validated == 0.0

    @pytest.mark.asyncio
    async def test_refresh_forces_revalidation(self):
        from adk.mcp import MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_refresh", gateway_url="http://test")
        auth._context.authenticated = True
        auth._last_validated = __import__("time").time()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "user_id": "refreshed",
            "tokens": 9000,
            "plan": "enterprise",
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            ctx = await auth.refresh()

        assert ctx.user_id == "refreshed"
        assert ctx.plan == "enterprise"


# ─────────────────────────────────────────────────────────────────────────────
# MCPBridge with auth
# ─────────────────────────────────────────────────────────────────────────────

class TestMCPBridgeAuth:
    def test_bridge_uses_auth_url(self):
        from adk.mcp import MCPBridge, MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_x", gateway_url="https://custom.gw.com")
        bridge = MCPBridge(auth=auth)
        assert bridge.mcp_url == "https://custom.gw.com"
        assert bridge.api_key == "aither_sk_live_x"

    def test_bridge_legacy_mode(self):
        from adk.mcp import MCPBridge
        bridge = MCPBridge(api_key="legacy_key", mcp_url="http://localhost:8080")
        assert bridge.mcp_url == "http://localhost:8080"
        assert bridge.api_key == "legacy_key"

    def test_bridge_headers_from_auth(self):
        from adk.mcp import MCPBridge, MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_hdr")
        auth._context.authenticated = True
        auth._context.key_type = "acta"
        bridge = MCPBridge(auth=auth)
        headers = bridge._headers()
        assert headers["Authorization"] == "Bearer aither_sk_live_hdr"

    @pytest.mark.asyncio
    async def test_list_tools_auth_error(self):
        from adk.mcp import MCPBridge, MCPAuth, MCPAuthError
        auth = MCPAuth(api_key="aither_sk_live_denied")
        auth._context.authenticated = True
        auth._context.key_type = "acta"
        bridge = MCPBridge(auth=auth)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.headers = {"content-type": "application/json"}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(MCPAuthError):
                await bridge.list_tools()

    @pytest.mark.asyncio
    async def test_list_tools_balance_error(self):
        from adk.mcp import MCPBridge, MCPAuth, MCPBalanceError
        auth = MCPAuth(api_key="aither_sk_live_broke")
        auth._context.authenticated = True
        bridge = MCPBridge(auth=auth)

        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.headers = {"content-type": "application/json"}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(MCPBalanceError):
                await bridge.list_tools()

    @pytest.mark.asyncio
    async def test_call_tool_retries_on_401(self):
        from adk.mcp import MCPBridge, MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_retry", gateway_url="http://test")
        auth._context.authenticated = True
        auth._context.key_type = "acta"
        bridge = MCPBridge(auth=auth)

        # First call returns 401, refresh succeeds, second call succeeds
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.headers = {"content-type": "application/json"}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.headers = {"content-type": "application/json"}
        resp_ok.json.return_value = {
            "result": {"content": [{"type": "text", "text": "success"}]},
        }
        resp_ok.raise_for_status = MagicMock()

        # Mock the _do_call to return 401 first, then 200
        call_count = 0

        async def mock_do_call(name, arguments):
            nonlocal call_count
            call_count += 1
            return resp_401 if call_count == 1 else resp_ok

        # Mock refresh on auth
        auth.refresh = AsyncMock(return_value=auth._context)

        bridge._do_call = mock_do_call
        result = await bridge.call_tool("test_tool")
        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_call_tool_jsonrpc_error(self):
        from adk.mcp import MCPBridge, MCPError
        bridge = MCPBridge(mcp_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        mock_resp.raise_for_status = MagicMock()

        async def mock_do_call(name, arguments):
            return mock_resp

        bridge._do_call = mock_do_call
        with pytest.raises(MCPError, match="Invalid Request"):
            await bridge.call_tool("bad_tool")

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self):
        from adk.mcp import MCPBridge, MCPToolNotFound
        bridge = MCPBridge(mcp_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "error": {"code": -32001, "message": "Tool not found"},
        }
        mock_resp.raise_for_status = MagicMock()

        async def mock_do_call(name, arguments):
            return mock_resp

        bridge._do_call = mock_do_call
        with pytest.raises(MCPToolNotFound):
            await bridge.call_tool("nonexistent_tool")

    @pytest.mark.asyncio
    async def test_list_tools_success(self):
        from adk.mcp import MCPBridge
        bridge = MCPBridge(mcp_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {
            "result": {
                "tools": [
                    {"name": "explore_code", "description": "Search code", "inputSchema": {}},
                    {"name": "remember", "description": "Store memory", "inputSchema": {}},
                ]
            }
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            tools = await bridge.list_tools()

        assert len(tools) == 2
        assert tools[0].name == "explore_code"
        assert tools[1].name == "remember"

    @pytest.mark.asyncio
    async def test_list_tools_cached(self):
        from adk.mcp import MCPBridge, MCPTool
        bridge = MCPBridge(mcp_url="http://test")
        bridge._tools_cache = [MCPTool("cached_tool", "test")]

        tools = await bridge.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "cached_tool"

    @pytest.mark.asyncio
    async def test_register_tools(self):
        from adk.mcp import MCPBridge, MCPTool
        bridge = MCPBridge(mcp_url="http://test")
        bridge._tools_cache = [
            MCPTool("tool_a", "Tool A"),
            MCPTool("tool_b", "Tool B"),
        ]

        agent = MagicMock()
        agent._tools = MagicMock()
        agent._tools.register = MagicMock()

        count = await bridge.register_tools(agent)
        assert count == 2
        assert agent._tools.register.call_count == 2

    @pytest.mark.asyncio
    async def test_health_check(self):
        from adk.mcp import MCPBridge
        bridge = MCPBridge(mcp_url="http://test")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            assert await bridge.health() is True

    @pytest.mark.asyncio
    async def test_health_check_unreachable(self):
        from adk.mcp import MCPBridge
        bridge = MCPBridge(mcp_url="http://unreachable")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("nope"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            assert await bridge.health() is False

    @pytest.mark.asyncio
    async def test_get_balance_acta(self):
        from adk.mcp import MCPBridge, MCPAuth
        auth = MCPAuth(api_key="aither_sk_live_bal", gateway_url="http://test")
        auth._context.authenticated = True
        auth._context.key_type = "acta"
        auth._context.token_balance = 4200
        auth._context.plan = "builder"
        auth._context.tier = "pro"
        bridge = MCPBridge(auth=auth)

        # Mock refresh
        auth.refresh = AsyncMock(return_value=auth._context)
        info = await bridge.get_balance()
        assert info["balance"] == 4200
        assert info["plan"] == "builder"
        assert info["tier"] == "pro"

    @pytest.mark.asyncio
    async def test_get_balance_non_acta(self):
        from adk.mcp import MCPBridge, MCPAuth
        auth = MCPAuth(api_key="aither_token_123", gateway_url="http://test")
        auth._context.authenticated = True
        auth._context.key_type = "identity"
        bridge = MCPBridge(auth=auth)
        info = await bridge.get_balance()
        assert "error" in info

    @pytest.mark.asyncio
    async def test_get_tier_info(self):
        from adk.mcp import MCPBridge, MCPAuth, MCPTool
        auth = MCPAuth(api_key="aither_sk_live_info", gateway_url="http://test")
        auth._context.authenticated = True
        auth._context.tier = "pro"
        auth._context.plan = "builder"
        auth._context.user_id = "user_info"
        bridge = MCPBridge(auth=auth)
        bridge._tools_cache = [MCPTool("t1", ""), MCPTool("t2", ""), MCPTool("t3", "")]

        info = await bridge.get_tier_info()
        assert info["tier"] == "pro"
        assert info["tools_available"] == 3
        assert info["authenticated"] is True


# ─────────────────────────────────────────────────────────────────────────────
# connect_mcp convenience
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectMCP:
    @pytest.mark.asyncio
    async def test_connect_mcp_with_key(self):
        from adk.mcp import connect_mcp

        # Mock auth.authenticate and bridge.health
        mock_acta_resp = MagicMock()
        mock_acta_resp.status_code = 200
        mock_acta_resp.headers = {"content-type": "application/json"}
        mock_acta_resp.json.return_value = {
            "user_id": "connect_user",
            "tokens": 1000,
            "plan": "explorer",
        }

        mock_health_resp = MagicMock()
        mock_health_resp.status_code = 200
        mock_health_resp.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        call_idx = 0
        async def mock_get(url, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if "balance" in url:
                return mock_acta_resp
            return mock_health_resp

        mock_client.get = mock_get

        with patch("httpx.AsyncClient", return_value=mock_client):
            bridge = await connect_mcp(api_key="aither_sk_live_connect")

        assert bridge._auth is not None
        assert bridge._auth.context.authenticated

    @pytest.mark.asyncio
    async def test_connect_mcp_no_key(self):
        from adk.mcp import connect_mcp

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            bridge = await connect_mcp()

        assert bridge._auth is not None
        # Without a key, auth shouldn't be attempted
        assert not bridge._auth.authenticated


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_exception_hierarchy(self):
        from adk.mcp import MCPError, MCPAuthError, MCPBalanceError, MCPToolNotFound
        assert issubclass(MCPAuthError, MCPError)
        assert issubclass(MCPBalanceError, MCPError)
        assert issubclass(MCPToolNotFound, MCPError)

    def test_errors_are_catchable(self):
        from adk.mcp import MCPError, MCPAuthError
        try:
            raise MCPAuthError("test")
        except MCPError:
            pass  # Should catch it via base class


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

class TestMCPExports:
    def test_package_exports(self):
        import adk
        _ = adk.MCPAuth
        _ = adk.MCPBridge
        _ = adk.MCPError
        _ = adk.MCPAuthError
        _ = adk.MCPBalanceError

    def test_key_prefixes_exported(self):
        from adk.mcp import ACTA_KEY_PREFIX, EXT_AGENT_KEY_PREFIX
        assert ACTA_KEY_PREFIX == "aither_sk_live_"
        assert EXT_AGENT_KEY_PREFIX == "aither_ext_"

    def test_hash_key(self):
        from adk.mcp import _hash_key
        h1 = _hash_key("test_key_1")
        h2 = _hash_key("test_key_2")
        assert h1 != h2
        assert len(h1) == 16
        # Same key = same hash
        assert _hash_key("test_key_1") == h1


class TestParseRpcTransports:
    """`_parse_rpc` handles both StreamableHTTP shapes.

    Written after 4 tests in this file failed with
    "SSE response carried no data event" while intending to test plain JSON.
    Their mock responses carried no `headers`, so
    `resp.headers.get("content-type", "")` returned a MagicMock whose
    `.startswith(...)` is TRUTHY — the parser took the SSE branch, and
    `.text.splitlines()` on a MagicMock iterates as empty.

    The mocks were fixed to carry headers, which left the SSE branch with no
    coverage at all: it had only ever been reached by accident. These tests
    exercise it on purpose.
    """

    @staticmethod
    def _resp(ctype, text=None, payload=None):
        from unittest.mock import MagicMock

        r = MagicMock()
        r.headers = {"content-type": ctype}
        if text is not None:
            r.text = text
        if payload is not None:
            r.json.return_value = payload
        return r

    def test_plain_json_is_parsed(self):
        from adk.mcp import MCPBridge

        r = self._resp("application/json", payload={"result": {"ok": True}})
        assert MCPBridge._parse_rpc(r) == {"result": {"ok": True}}

    def test_sse_data_event_is_parsed(self):
        from adk.mcp import MCPBridge

        nl = chr(10)
        body = nl.join(["event: message", 'data: {"result": {"ok": 1}}', ""])
        r = self._resp("text/event-stream", text=body)
        assert MCPBridge._parse_rpc(r) == {"result": {"ok": 1}}

    def test_sse_uses_the_last_data_event(self):
        """A stream can carry several frames; the final one is the answer."""
        from adk.mcp import MCPBridge

        nl = chr(10)
        body = nl.join([
            'data: {"result": "first"}',
            'data: {"result": "last"}',
            "",
        ])
        assert MCPBridge._parse_rpc(self._resp("text/event-stream", text=body)) == {
            "result": "last"
        }

    def test_sse_with_no_data_event_raises(self):
        """The error that started this: reported, never silently empty."""
        import pytest
        from adk.mcp import MCPBridge, MCPError

        nl = chr(10)
        body = nl.join([": keep-alive comment", "event: ping", ""])
        with pytest.raises(MCPError, match="no data event"):
            MCPBridge._parse_rpc(self._resp("text/event-stream", text=body))

    def test_a_charset_suffix_still_counts_as_sse(self):
        """Servers send `text/event-stream; charset=utf-8`."""
        from adk.mcp import MCPBridge

        nl = chr(10)
        body = nl.join(['data: {"result": 2}', ""])
        r = self._resp("text/event-stream; charset=utf-8", text=body)
        assert MCPBridge._parse_rpc(r) == {"result": 2}
