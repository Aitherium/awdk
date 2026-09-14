"""Tests for GatewayMCPClient — self-hosted agent MCP gateway access.

Tests authentication, tool discovery, tool calling, and fail-soft behavior.
Uses mocked httpx to avoid hitting the real gateway.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.client import GatewayMCPClient, create_gateway_mcp_client


class TestGatewayMCPClient:
    """Test GatewayMCPClient core functionality."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful connection to gateway."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock successful /health response
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.get.return_value = mock_resp

            result = await client.connect()

            assert result is True
            assert client._connected is True
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_unreachable(self):
        """Test connection failure when gateway is unreachable."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock connection error
            mock_client.get.side_effect = Exception("Connection refused")

            result = await client.connect()

            assert result is False
            assert client._connected is False

    @pytest.mark.asyncio
    async def test_list_tools_no_key(self):
        """Test tool listing without API key — should fail soft."""
        client = GatewayMCPClient(gateway_url="https://mcp.aitherium.com", api_key="")

        result = await client.list_tools()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_tools_success(self):
        """Test successful tool discovery."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock successful tool list response
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "jsonrpc": "2.0",
                "result": {
                    "tools": [
                        {
                            "name": "get_secret",
                            "description": "Get secret from vault",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                        {
                            "name": "search_code",
                            "description": "Search codebase",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ]
                },
                "id": 1,
            }
            mock_client.post.return_value = mock_resp

            result = await client.list_tools()

            assert len(result) == 2
            assert result[0]["name"] == "get_secret"
            assert result[1]["name"] == "search_code"
            # ASSERT THE tools/list CALL, NOT THE CALL COUNT. This was
            # `post.assert_called_once()`, which silently encoded "the client makes exactly
            # one POST" — i.e. "no MCP handshake exists". MCP StreamableHTTP REQUIRES
            # `initialize` + `notifications/initialized` before any request, so the moment
            # that handshake was implemented correctly this test went red while the client
            # was right. Assert the thing that matters and the handshake can evolve freely.
            methods = [
                (c.kwargs.get("json") or {}).get("method")
                for c in mock_client.post.call_args_list
            ]
            assert "tools/list" in methods, methods
            assert "initialize" in methods, "the MCP handshake must precede tools/list"


    @pytest.mark.asyncio
    async def test_list_tools_auth_error(self):
        """Test tool listing with auth failure (401)."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_invalid",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock 401 response
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_resp.text = "Unauthorized"
            mock_client.post.return_value = mock_resp

            result = await client.list_tools()

            assert result == []

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        """Test successful tool call."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock successful tool call response
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "jsonrpc": "2.0",
                "result": {
                    "content": [{"type": "text", "text": "secret_value_123"}]
                },
                "id": 1,
            }
            mock_client.post.return_value = mock_resp

            result = await client.call_tool("get_secret", {"name": "api_key"})

            assert result["success"] is True
            assert result["text"] == "secret_value_123"

    @pytest.mark.asyncio
    async def test_call_tool_no_key(self):
        """Test tool call without API key — should return error dict."""
        # Explicitly pass empty key and patch env vars to ensure they're not set
        with patch.dict("os.environ", {"AITHER_API_KEY": "", "AITHER_MCP_KEY": ""}, clear=False):
            client = GatewayMCPClient(
                gateway_url="https://mcp.aitherium.com", api_key=""
            )

            result = await client.call_tool("get_secret", {"name": "api_key"})

            # Empty api_key means we can't call tools
            assert "error" in result

    @pytest.mark.asyncio
    async def test_call_tool_insufficient_balance(self):
        """Test tool call with insufficient balance (402)."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_sk_live_test",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock 402 response
            mock_resp = MagicMock()
            mock_resp.status_code = 402
            mock_client.post.return_value = mock_resp

            result = await client.call_tool("expensive_tool", {})

            assert result["error"] == "insufficient_balance"

    @pytest.mark.asyncio
    async def test_call_tool_access_denied(self):
        """Test tool call with insufficient permissions (403)."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_free_tier",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock 403 response
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_client.post.return_value = mock_resp

            result = await client.call_tool("enterprise_tool", {})

            assert result["error"] == "access_denied"

    @pytest.mark.asyncio
    async def test_call_tool_gateway_error(self):
        """Test tool call with gateway error (500)."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock 500 response
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_client.post.return_value = mock_resp

            result = await client.call_tool("any_tool", {})

            assert result["error"] == "gateway_error"
            assert result["status"] == 500

    @pytest.mark.asyncio
    async def test_call_tool_mcp_error(self):
        """Test tool call with JSON-RPC error response."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock MCP error response
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "jsonrpc": "2.0",
                "error": {"code": -32001, "message": "Tool not found"},
                "id": 1,
            }
            mock_client.post.return_value = mock_resp

            result = await client.call_tool("nonexistent_tool", {})

            assert result["error"] == "mcp_error"
            assert result["code"] == -32001
            assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_headers_with_token(self):
        """Test that auth headers are correctly built."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        headers = client._headers()

        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer aither_pat_test123"
        assert "Content-Type" in headers
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_headers_no_token(self):
        """Test headers when no token is set."""
        # Explicitly patch env vars to ensure they're empty
        with patch.dict("os.environ", {"AITHER_API_KEY": "", "AITHER_MCP_KEY": ""}, clear=False):
            client = GatewayMCPClient(
                gateway_url="https://mcp.aitherium.com", api_key=""
            )

            headers = client._headers()

            assert "Authorization" not in headers
            assert "Content-Type" in headers


class TestCreateGatewayMCPClient:
    """Test factory function create_gateway_mcp_client."""

    @pytest.mark.asyncio
    async def test_create_with_token_success(self):
        """Test factory creates client and connects successfully."""
        with patch("adk.client._gateway_mcp.GatewayMCPClient") as mock_class:
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_class.return_value = mock_client

            result = await create_gateway_mcp_client(
                api_key="aither_pat_test123",
                gateway_url="https://mcp.aitherium.com",
            )

            assert result is not None
            assert result is mock_client

    @pytest.mark.asyncio
    async def test_create_no_token(self):
        """Test factory returns None when no token is configured."""
        with patch.dict("os.environ", {"AITHER_API_KEY": "", "AITHER_MCP_KEY": ""}):
            result = await create_gateway_mcp_client(
                api_key="",
                gateway_url="https://mcp.aitherium.com",
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_create_connection_failed(self):
        """Test factory returns None when connection fails."""
        with patch("adk.client._gateway_mcp.GatewayMCPClient") as mock_class:
            mock_client = AsyncMock()
            mock_client.connect = AsyncMock(return_value=False)
            mock_class.return_value = mock_client

            result = await create_gateway_mcp_client(
                api_key="aither_pat_test123",
                gateway_url="https://mcp.aitherium.com",
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_create_exception_handling(self):
        """Test factory handles exceptions gracefully (fail-soft)."""
        with patch("adk.client._gateway_mcp.GatewayMCPClient") as mock_class:
            mock_class.side_effect = Exception("Connection error")

            # Should not raise — fail soft
            result = await create_gateway_mcp_client(
                api_key="aither_pat_test123",
                gateway_url="https://mcp.aitherium.com",
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_create_env_override(self):
        """Test factory reads from environment variables."""
        with patch.dict("os.environ", {"AITHER_API_KEY": "aither_pat_env"}):
            with patch("adk.client._gateway_mcp.GatewayMCPClient") as mock_class:
                mock_client = AsyncMock()
                mock_client.connect = AsyncMock(return_value=True)
                mock_class.return_value = mock_client

                result = await create_gateway_mcp_client()

                assert result is not None
                # Verify that the env key was used
                mock_class.assert_called_once()
                call_args = mock_class.call_args
                assert call_args.kwargs.get("api_key") == "aither_pat_env"


class TestGatewayMCPIntegration:
    """Integration-style tests combining multiple operations."""

    @pytest.mark.asyncio
    async def test_full_workflow_success(self):
        """Test complete workflow: connect → list tools → call tool."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_test123",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock responses for connect, list, call
            responses = [
                MagicMock(status_code=200),  # connect /health
                MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "jsonrpc": "2.0",
                            "result": {
                                "tools": [
                                    {
                                        "name": "get_secret",
                                        "description": "Get secret",
                                        "inputSchema": {},
                                    }
                                ]
                            },
                            "id": 1,
                        }
                    ),
                ),  # list_tools
                MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "jsonrpc": "2.0",
                            "result": {"content": [{"type": "text", "text": "secret"}]},
                            "id": 2,
                        }
                    ),
                ),  # call_tool
            ]

            mock_client.get.return_value = responses[0]

            # DISPATCH ON THE JSON-RPC METHOD, NOT ON CALL ORDER. `side_effect =
            # responses[1:]` assumed POST #1 is tools/list and POST #2 is call_tool. The MCP
            # handshake inserts `initialize` and `notifications/initialized` ahead of both,
            # so every response landed one or two calls off and the list ran out — a failure
            # that reads as a broken client when it is really a test pinned to call order.
            by_method = {"tools/list": responses[1], "tools/call": responses[2]}
            init_resp = MagicMock(status_code=200)
            init_resp.headers = {"mcp-session-id": "test-session"}
            init_resp.json = MagicMock(return_value={"jsonrpc": "2.0", "result": {}, "id": 0})

            def _post(*args, **kwargs):
                method = (kwargs.get("json") or {}).get("method")
                return by_method.get(method, init_resp)

            mock_client.post.side_effect = _post

            # Execute workflow
            connected = await client.connect()
            assert connected is True

            tools = await client.list_tools()
            assert len(tools) == 1
            assert tools[0]["name"] == "get_secret"

            result = await client.call_tool("get_secret", {"name": "api_key"})
            assert result["success"] is True
            assert result["text"] == "secret"

    @pytest.mark.asyncio
    async def test_fail_soft_on_auth_error(self):
        """Test that auth errors don't crash — client continues."""
        client = GatewayMCPClient(
            gateway_url="https://mcp.aitherium.com",
            api_key="aither_pat_bad",
        )

        with patch("adk.client._gateway_mcp.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # All requests return 401
            mock_resp = MagicMock(status_code=401)
            mock_client.get.return_value = mock_resp
            mock_client.post.return_value = mock_resp

            # These should all return gracefully (not raise)
            connected = await client.connect()
            assert connected is False

            tools = await client.list_tools()
            assert tools == []

            result = await client.call_tool("any_tool", {})
            assert result["error"] == "auth_failed"
