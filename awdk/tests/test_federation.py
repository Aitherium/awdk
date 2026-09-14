"""Tests for adk.federation — AitherOS federation client."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.federation import FederationClient, FederationCredentials, MeshNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    return resp


def _mock_client(responses=None):
    """Create a mock httpx.AsyncClient that returns predefined responses."""
    client = AsyncMock()
    if responses:
        client.post.side_effect = responses
        client.get.side_effect = responses
    return client


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------

class TestFederationClient:
    def test_default_ports(self):
        fed = FederationClient()
        assert fed._ports["genesis"] == 8001
        assert fed._ports["identity"] == 8112
        assert fed._ports["mesh"] == 8125
        assert fed._ports["node"] == 8090

    def test_custom_host(self):
        fed = FederationClient(host="http://192.168.1.100")
        assert fed.host == "http://192.168.1.100"

    def test_url_generation(self):
        fed = FederationClient(host="http://myhost", mode="direct")
        assert fed._url("genesis", "/chat") == "http://myhost:8001/chat"
        assert fed._url("identity", "/auth/me") == "http://myhost:8112/auth/me"

    def test_headers_include_tenant(self):
        fed = FederationClient(tenant="test-tenant")
        headers = fed._headers()
        assert headers["X-Tenant-ID"] == "test-tenant"
        assert headers["X-Caller-Type"] == "public"

    def test_headers_with_token(self):
        fed = FederationClient()
        fed._creds.token = "my-token"
        headers = fed._headers()
        assert headers["Authorization"] == "Bearer my-token"

    def test_node_id_generated(self):
        fed = FederationClient()
        assert fed._node_id.startswith("anode-")
        assert len(fed._node_id) > 10


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_with_api_key(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.get.return_value = _mock_response(200, {"user_id": "u123", "username": "test"})

            creds = await fed.register("test-agent", api_key="existing-key")
            assert creds.api_key == "existing-key"
            assert creds.user_id == "u123"

    @pytest.mark.asyncio
    async def test_register_new_user(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(201, {
                "token": "new-token",
                "user_id": "u456",
            })

            creds = await fed.register("new-agent", password="pass123")
            assert creds.token == "new-token"
            assert creds.user_id == "u456"

    @pytest.mark.asyncio
    async def test_register_falls_back_to_login(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            # Registration fails, login succeeds
            client.post.side_effect = [
                _mock_response(409, {"error": "exists"}),  # register
                _mock_response(200, {"token": "login-token", "user_id": "u789"}),  # login
            ]

            creds = await fed.register("existing-agent", password="pass123")
            assert creds.token == "login-token"

    @pytest.mark.asyncio
    async def test_enroll_with_mesh_key(self):
        fed = FederationClient()
        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(200, {
                "api_key": "mesh-api-key",
                "wireguard_ip": "10.77.1.50",
                "trust_level": 2,
            })

            creds = await fed.enroll_with_mesh_key("mk-test123")
            assert creds.api_key == "mesh-api-key"
            assert creds.wireguard_ip == "10.77.1.50"
            assert creds.trust_level == 2


# ---------------------------------------------------------------------------
# Mesh tests
# ---------------------------------------------------------------------------

class TestMesh:
    @pytest.mark.asyncio
    async def test_join_mesh(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(200, {"mesh_id": "mesh-prod"})

            joined = await fed.join_mesh(capabilities=["text_gen"])
            assert joined is True

            # Cancel heartbeat that was started
            if fed._heartbeat_task:
                fed._heartbeat_task.cancel()
                try:
                    await fed._heartbeat_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_join_mesh_failure(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(500)

            joined = await fed.join_mesh()
            assert joined is False

    @pytest.mark.asyncio
    async def test_list_nodes(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.get.return_value = _mock_response(200, {
                "nodes": [
                    {"node_id": "node-1", "hostname": "gpu-1", "role": "worker", "status": "online"},
                    {"node_id": "node-2", "hostname": "gpu-2", "role": "client", "status": "online"},
                ]
            })

            nodes = await fed.list_nodes()
            assert len(nodes) == 2
            assert nodes[0].node_id == "node-1"
            assert nodes[0].role == "worker"


# ---------------------------------------------------------------------------
# Flux event tests
# ---------------------------------------------------------------------------

class TestFluxEvents:
    @pytest.mark.asyncio
    async def test_emit_event(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(200)

            result = await fed.emit_event("TEST_EVENT", {"key": "value"})
            assert result is True
            client.post.assert_called_once()
            call_args = client.post.call_args
            assert "TEST_EVENT" in json.dumps(call_args.kwargs.get("json", call_args[1].get("json", {})))

    @pytest.mark.asyncio
    async def test_emit_event_failure(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.side_effect = Exception("connection refused")

            result = await fed.emit_event("TEST_EVENT", {})
            assert result is False

    def test_on_event_registers_handler(self):
        fed = FederationClient()
        handler = lambda e: None
        fed.on_event("PAIN_SIGNAL", handler)
        assert "PAIN_SIGNAL" in fed._flux_handlers
        assert handler in fed._flux_handlers["PAIN_SIGNAL"]

    def test_multiple_handlers_per_event(self):
        fed = FederationClient()
        h1 = lambda e: None
        h2 = lambda e: None
        fed.on_event("TEST", h1)
        fed.on_event("TEST", h2)
        assert len(fed._flux_handlers["TEST"]) == 2


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------

class TestMCPTools:
    @pytest.mark.asyncio
    async def test_list_mcp_tools(self):
        fed = FederationClient(host="http://localhost", mode="direct")
        with patch.object(fed, "_mcp_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {
                "result": {"tools": [
                    {"name": "explore_code", "description": "Search code"},
                    {"name": "get_system_status", "description": "Get status"},
                ]}
            }

            tools = await fed.list_mcp_tools()
            assert len(tools) == 2
            assert tools[0]["name"] == "explore_code"

    @pytest.mark.asyncio
    async def test_call_mcp_tool(self):
        fed = FederationClient(host="http://localhost", mode="direct")
        with patch.object(fed, "_mcp_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {
                "result": {"content": [{"type": "text", "text": "Found 5 results"}]}
            }

            result = await fed.call_mcp_tool("explore_code", {"query": "agent"})
            assert "Found 5 results" in result

    @pytest.mark.asyncio
    async def test_call_mcp_tool_empty(self):
        fed = FederationClient(host="http://localhost", mode="direct")
        with patch.object(fed, "_mcp_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {}

            result = await fed.call_mcp_tool("bad_tool")
            assert result == ""


# ---------------------------------------------------------------------------
# Chat tests
# ---------------------------------------------------------------------------

class TestChat:
    @pytest.mark.asyncio
    async def test_chat_via_genesis(self):
        fed = FederationClient(host="http://localhost", mode="direct")
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.post.return_value = _mock_response(200, {
                "response": "System is healthy",
                "model": "nemotron-orchestrator-8b",
            })

            result = await fed.chat("What's the status?")
            assert result["source"] == "genesis"
            assert "healthy" in result["response"]

    @pytest.mark.asyncio
    async def test_chat_falls_back_to_node(self):
        fed = FederationClient(host="http://localhost", mode="direct")
        call_count = 0

        async def mock_post(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Genesis fails
                raise Exception("connection refused")
            # Node succeeds
            return _mock_response(200, {
                "choices": [{"message": {"content": "Node response"}}],
                "model": "llama3.2:3b",
            })

        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)
            client.post.side_effect = mock_post

            result = await fed.chat("Hello")
            assert result["source"] == "node"
            assert result["response"] == "Node response"

    @pytest.mark.asyncio
    async def test_chat_all_fail(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)
            client.post.side_effect = Exception("unreachable")

            result = await fed.chat("Hello")
            assert result["source"] == "none"
            assert "error" in result


# ---------------------------------------------------------------------------
# System status tests
# ---------------------------------------------------------------------------

class TestSystemStatus:
    @pytest.mark.asyncio
    async def test_get_system_status(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.get.return_value = _mock_response(200, {"status": "healthy", "uptime": 12345})

            status = await fed.get_system_status()
            assert status["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_get_system_status_unreachable(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)
            client.get.side_effect = Exception("down")

            status = await fed.get_system_status()
            assert status["status"] == "unreachable"


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_disconnect_cancels_tasks(self):
        fed = FederationClient()
        # Create mock tasks
        fed._heartbeat_task = asyncio.create_task(asyncio.sleep(999))
        fed._flux_task = asyncio.create_task(asyncio.sleep(999))

        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)
            client.post.return_value = _mock_response(200)

            await fed.disconnect()
            assert fed._heartbeat_task.cancelled()
            assert fed._flux_task.cancelled()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with FederationClient() as fed:
            assert fed._node_id.startswith("anode-")
        # After exit, tasks should be cleaned up

    @pytest.mark.asyncio
    async def test_service_health(self):
        fed = FederationClient()
        with patch.object(fed, "_client") as mock_client_fn:
            client = AsyncMock()
            mock_client_fn.return_value.__aenter__ = AsyncMock(return_value=client)
            mock_client_fn.return_value.__aexit__ = AsyncMock(return_value=False)

            client.get.return_value = _mock_response(200, {"status": "healthy"})

            health = await fed.get_service_health("genesis")
            assert health["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_unknown_service_health(self):
        fed = FederationClient()
        health = await fed.get_service_health("nonexistent")
        assert "error" in health
