"""Tests for adk/gateway.py — GatewayClient."""

import sys
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from adk.client import GatewayClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client(response_data=None, status_code=200, side_effect=None):
    """Create a mock httpx.AsyncClient context manager."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_data or {}
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=mock_resp,
        )

    # Use MagicMock as the container to avoid AsyncMock child-creation
    # warnings, then attach only the needed async methods explicitly.
    client = MagicMock()
    if side_effect:
        client.post = AsyncMock(side_effect=side_effect)
        client.get = AsyncMock(side_effect=side_effect)
        client.delete = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=mock_resp)
        client.get = AsyncMock(return_value=mock_resp)
        client.delete = AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, mock_resp


def _patch_httpx(client):
    """Patch httpx.AsyncClient in the gateway module to return our mock."""
    return patch("adk.client._gateway.httpx.AsyncClient", return_value=client)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestGatewayClientInit:
    def test_default_url(self):
        gw = GatewayClient()
        assert gw.gateway_url == "https://gateway.aitherium.com"

    def test_custom_url_strips_trailing_slash(self):
        gw = GatewayClient(gateway_url="https://custom.example.com/")
        assert gw.gateway_url == "https://custom.example.com"

    def test_api_key_stored(self):
        gw = GatewayClient(api_key="test-key-123")
        assert gw.api_key == "test-key-123"

    def test_headers_include_bearer(self):
        gw = GatewayClient(api_key="my-key")
        headers = gw._headers()
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"

    def test_headers_no_auth_without_key(self):
        gw = GatewayClient(api_key="")
        headers = gw._headers()
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class TestAuth:
    @pytest.mark.asyncio
    async def test_register(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client, resp = _mock_client({"api_key": "new-key", "user_id": "u123"})

        with _patch_httpx(client):
            result = await gw.register("user@test.com", "password123")
            assert result["api_key"] == "new-key"
            client.post.assert_called_once()
            call_url = client.post.call_args[0][0]
            assert "/v1/auth/register" in call_url

    @pytest.mark.asyncio
    async def test_verify_email(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client, resp = _mock_client({"verified": True})

        with _patch_httpx(client):
            result = await gw.verify_email("tok-abc")
            assert result["verified"] is True
            call_json = client.post.call_args[1]["json"]
            assert call_json["token"] == "tok-abc"

    @pytest.mark.asyncio
    async def test_login_sets_api_key(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client, resp = _mock_client({"ok": True, "token": "jwt-token-123"})

        with _patch_httpx(client):
            result = await gw.login("user@test.com", "pass")
            assert result["token"] == "jwt-token-123"
            assert gw.api_key == "jwt-token-123"

    @pytest.mark.asyncio
    async def test_login_no_token_keeps_old_key(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="old-key")
        client, resp = _mock_client({"ok": False})

        with _patch_httpx(client):
            await gw.login("user@test.com", "bad-pass")
            assert gw.api_key == "old-key"


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

class TestAgentRegistry:
    @pytest.mark.asyncio
    async def test_register_agent(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="key")
        client, resp = _mock_client({"agent_id": "a1", "status": "registered"})

        with _patch_httpx(client):
            result = await gw.register_agent(
                "atlas",
                owner_email="david@aitherium.com",
                capabilities=["search", "analysis"],  # legacy fields ignored per contract
                description="Search agent",
                tools=["web_search"],
            )
            assert result["status"] == "registered"
            call_json = client.post.call_args[1]["json"]
            assert call_json["name"] == "atlas"
            assert call_json["owner_email"] == "david@aitherium.com"

    @pytest.mark.asyncio
    async def test_discover_agents_no_filter(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        agents_data = {"agents": [{"name": "atlas"}, {"name": "demiurge"}]}
        client, resp = _mock_client(agents_data)

        with _patch_httpx(client):
            result = await gw.discover_agents()
            assert len(result) == 2

    @pytest.mark.asyncio
    async def test_discover_agents_with_capability(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        agents_data = {"agents": [{"name": "atlas", "capabilities": ["search"]}]}
        client, resp = _mock_client(agents_data)

        with _patch_httpx(client):
            result = await gw.discover_agents(capability="search", limit=5)
            assert len(result) == 1
            call_params = client.get.call_args[1]["params"]
            assert call_params["capability"] == "search"
            assert call_params["limit"] == 5

    @pytest.mark.asyncio
    async def test_my_agents(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="key")
        client, resp = _mock_client({"agents": [{"name": "my-agent"}]})

        with _patch_httpx(client):
            result = await gw.my_agents()
            assert len(result) == 1
            assert result[0]["name"] == "my-agent"

    @pytest.mark.asyncio
    async def test_my_agents_404_returns_empty(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"agents": []}
        mock_resp.raise_for_status = MagicMock()

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            result = await gw.my_agents()
            assert result == []

    @pytest.mark.asyncio
    async def test_unregister_agent(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="key")
        client, resp = _mock_client({"deleted": True})

        with _patch_httpx(client):
            result = await gw.unregister_agent("agent-123")
            assert result["deleted"] is True
            call_url = client.delete.call_args[0][0]
            assert "agent-123" in call_url


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_network_error_on_register(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client, _ = _mock_client(side_effect=httpx.ConnectError("Connection refused"))

        with _patch_httpx(client):
            with pytest.raises(httpx.ConnectError):
                await gw.register("u@test.com", "pass")

    @pytest.mark.asyncio
    async def test_401_unauthorized(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="bad-key")
        client, _ = _mock_client(status_code=401)

        with _patch_httpx(client):
            with pytest.raises(httpx.HTTPStatusError):
                await gw.register_agent("atlas", owner_email="david@aitherium.com")

    @pytest.mark.asyncio
    async def test_500_server_error(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client, _ = _mock_client(status_code=500)

        with _patch_httpx(client):
            with pytest.raises(httpx.HTTPStatusError):
                await gw.discover_agents()


# ---------------------------------------------------------------------------
# Remote inference
# ---------------------------------------------------------------------------

class TestInference:
    @pytest.mark.asyncio
    async def test_inference_success(self):
        gw = GatewayClient(gateway_url="https://gw.test", api_key="key")
        client, resp = _mock_client({
            "choices": [{"message": {"content": "Hello!"}}],
            "model": "llama3.2",
        })

        with _patch_httpx(client):
            result = await gw.inference(
                messages=[{"role": "user", "content": "Hi"}],
                model="llama3.2",
            )
            assert "choices" in result
            call_json = client.post.call_args[1]["json"]
            assert call_json["model"] == "llama3.2"

    @pytest.mark.asyncio
    async def test_inference_custom_url(self):
        gw = GatewayClient()
        client, resp = _mock_client({"choices": []})

        with _patch_httpx(client):
            await gw.inference(
                messages=[{"role": "user", "content": "test"}],
                inference_url="https://custom-llm.example.com/v1/chat/completions",
            )
            call_url = client.post.call_args[0][0]
            assert "custom-llm.example.com" in call_url


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_true(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            result = await gw.health()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_returns_false_on_error(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        client = MagicMock()
        client.get = AsyncMock(side_effect=Exception("Connection refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            result = await gw.health()
            assert result is False

    @pytest.mark.asyncio
    async def test_health_returns_false_on_500(self):
        gw = GatewayClient(gateway_url="https://gw.test")
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            result = await gw.health()
            assert result is False
