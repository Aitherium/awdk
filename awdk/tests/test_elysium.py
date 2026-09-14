"""Tests for Elysium onramp — cloud inference connection."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from adk.elysium import Elysium, ElysiumStatus


# ── Construction ─────────────────────────────────────────────────────────


class TestElysiumCreation:
    def test_create_default(self):
        e = Elysium()
        assert "gateway.aitherium.com" in e.inference_url
        assert "gateway.aitherium.com" in e.gateway_url

    def test_create_with_api_key(self):
        e = Elysium(api_key="aither_sk_live_test123")
        assert e.api_key == "aither_sk_live_test123"

    def test_create_with_custom_urls(self):
        e = Elysium(
            gateway_url="https://custom-gw.example.com",
            inference_url="https://custom-inf.example.com/v1",
        )
        assert e.gateway_url == "https://custom-gw.example.com"
        assert e.inference_url == "https://custom-inf.example.com/v1"

    def test_status_defaults(self):
        e = Elysium()
        assert e.status.connected is False
        assert e.status.models_available == []


# ── Auth ──────────────────────────────────────────────────────────────────


class TestElysiumAuth:
    @pytest.mark.asyncio
    async def test_register(self):
        e = Elysium()
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"ok": True, "user_id": "u123"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await e.register("test@example.com", "password123")
            assert result["ok"] is True
            assert result["user_id"] == "u123"

    @pytest.mark.asyncio
    async def test_login_stores_token(self):
        e = Elysium()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "token": "jwt_abc", "expires_at": 9999}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await e.login("test@example.com", "password123")
            assert result["token"] == "jwt_abc"
            assert e._jwt_token == "jwt_abc"

    @pytest.mark.asyncio
    async def test_verify_email(self):
        e = Elysium()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "message": "Email verified"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await e.verify_email("test@example.com", "token123")
            assert result["ok"] is True


# ── Connection ────────────────────────────────────────────────────────────


class TestElysiumConnection:
    @pytest.mark.asyncio
    async def test_connect_requires_api_key(self):
        with pytest.raises(ConnectionError, match="No API key"):
            await Elysium.connect(api_key="")

    @pytest.mark.asyncio
    async def test_connect_success(self):
        health_resp = MagicMock()
        health_resp.status_code = 200

        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [
                {"id": "aither-small", "accessible": True},
                {"id": "aither-orchestrator", "accessible": True},
                {"id": "aither-reasoning", "accessible": False},
            ]
        }

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=[health_resp, models_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            e = await Elysium.connect(api_key="aither_sk_live_test")
            assert e.status.connected is True
            assert "aither-small" in e.status.models_available
            assert "aither-orchestrator" in e.status.models_available
            # Reasoning not accessible, should still be listed (accessible=True filter)
            assert "aither-reasoning" not in e.status.models_available


# ── Router ────────────────────────────────────────────────────────────────


class TestElysiumRouter:
    def test_router_returns_llm_router(self):
        e = Elysium(api_key="test_key")
        router = e.router
        assert router is not None
        assert router._provider_name == "gateway"

    def test_router_uses_inference_url(self):
        e = Elysium(api_key="test_key", inference_url="https://custom.example.com/v1")
        router = e.router
        # The router should be configured with the custom URL
        assert router._provider is not None

    def test_router_cached(self):
        e = Elysium(api_key="test_key")
        r1 = e.router
        r2 = e.router
        assert r1 is r2


# ── Agent Integration ─────────────────────────────────────────────────────


class TestElysiumAttach:
    @pytest.mark.asyncio
    async def test_attach_replaces_llm(self):
        e = Elysium(api_key="test_key")
        e._status.connected = True

        agent = MagicMock()
        agent.name = "test_agent"

        await e.attach(agent)
        assert agent.llm == e.router
        assert agent._provider_name == "elysium"


# ── Agent Registry ────────────────────────────────────────────────────────


class TestElysiumRegistry:
    @pytest.mark.asyncio
    async def test_register_agent(self):
        e = Elysium(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"ok": True, "agent_id": "a123"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await e.register_agent("my_agent", capabilities=["chat"])
            assert result["agent_id"] == "a123"
            assert e.status.agent_id == "a123"

    @pytest.mark.asyncio
    async def test_discover_agents(self):
        e = Elysium(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "agents": [{"id": "a1", "agent_name": "helper"}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            agents = await e.discover_agents()
            assert len(agents) == 1
            assert agents[0]["agent_name"] == "helper"


# ── Models ────────────────────────────────────────────────────────────────


class TestElysiumModels:
    @pytest.mark.asyncio
    async def test_list_models(self):
        e = Elysium(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"id": "aither-small", "accessible": True},
                {"id": "aither-orchestrator", "accessible": True},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            models = await e.models()
            assert len(models) == 2


# ── Health ────────────────────────────────────────────────────────────────


class TestElysiumHealth:
    @pytest.mark.asyncio
    async def test_health_true(self):
        e = Elysium(api_key="test_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            assert await e.health() is True

    @pytest.mark.asyncio
    async def test_health_false_on_error(self):
        e = Elysium(api_key="test_key")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=Exception("connection refused"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            assert await e.health() is False


# ── Preflight ─────────────────────────────────────────────────────────────


class TestElysiumPreflight:
    @pytest.mark.asyncio
    async def test_preflight_all_healthy(self):
        e = Elysium(api_key="test_key")

        # Mock all HTTP calls
        call_count = 0
        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200
            if "/health" in url:
                resp.json.return_value = {"ok": True}
            elif "/billing/balance" in url:
                resp.json.return_value = {"balance": 5000, "plan": "creator"}
            elif "/models" in url:
                resp.json.return_value = {"data": [
                    {"id": "aither-small", "accessible": True},
                    {"id": "aither-orchestrator", "accessible": True},
                ]}
            elif "/agents/mine" in url:
                resp.json.return_value = {"agents": [{"agent_name": "my-bot"}]}
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            status = await e.preflight()

        assert status["ready"] is True
        assert status["checks"]["gateway_reachable"]["ok"] is True
        assert status["checks"]["inference_reachable"]["ok"] is True
        assert status["checks"]["auth_valid"]["ok"] is True
        assert status["checks"]["balance"]["balance"] == 5000
        assert status["checks"]["balance"]["plan"] == "creator"
        assert status["checks"]["models"]["accessible"] == ["aither-small", "aither-orchestrator"]
        assert status["checks"]["agents"]["count"] == 1

    @pytest.mark.asyncio
    async def test_preflight_gateway_down(self):
        e = Elysium(api_key="test_key")

        async def mock_get(url, **kwargs):
            if "gateway" in url:
                raise ConnectionError("gateway down")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"data": [], "agents": []}
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            status = await e.preflight()

        assert status["ready"] is False
        assert status["checks"]["gateway_reachable"]["ok"] is False

    @pytest.mark.asyncio
    async def test_preflight_auth_invalid(self):
        e = Elysium(api_key="bad_key")

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            if "/billing/balance" in url:
                resp.status_code = 401
            elif "/agents/mine" in url:
                resp.status_code = 404
                resp.json.return_value = {"agents": []}
            else:
                resp.status_code = 200
                resp.json.return_value = {"data": []}
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            status = await e.preflight()

        # Gateway+inference are up, so ready is True (critical checks pass)
        assert status["ready"] is True
        assert status["checks"]["auth_valid"]["ok"] is False

    @pytest.mark.asyncio
    async def test_preflight_no_models_accessible(self):
        e = Elysium(api_key="test_key")

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/models" in url:
                resp.json.return_value = {"data": [
                    {"id": "aither-reasoning", "accessible": False},
                ]}
            elif "/billing/balance" in url:
                resp.json.return_value = {"balance": 0, "plan": "free"}
            elif "/agents/mine" in url:
                resp.json.return_value = {"agents": []}
            else:
                resp.json.return_value = {"ok": True}
            return resp

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            status = await e.preflight()

        assert status["checks"]["models"]["ok"] is False
        assert status["checks"]["models"]["accessible"] == []


# ── Export ─────────────────────────────────────────────────────────────────


class TestExport:
    def test_exports(self):
        import adk
        assert hasattr(adk, "Elysium")
