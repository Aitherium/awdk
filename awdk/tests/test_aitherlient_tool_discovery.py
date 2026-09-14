"""Tests for AitherClient.auto_discover_tools() and call_discovered_tool()."""

import sys
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from adk.client._client import AitherClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client(response_data=None, status_code=200, side_effect=None):
    """Create a mock httpx.AsyncClient context manager."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_data or {}
    mock_resp.text = "error text"
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
    else:
        client.post = AsyncMock(return_value=mock_resp)
        client.get = AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, mock_resp


def _patch_httpx(client):
    """Patch httpx.AsyncClient to return our mock."""
    return patch("httpx.AsyncClient", return_value=client)


# ---------------------------------------------------------------------------
# auto_discover_tools tests
# ---------------------------------------------------------------------------

class TestAutoDiscoverTools:
    """Tests for AitherClient.auto_discover_tools()."""

    @pytest.mark.asyncio
    async def test_discover_tools_success(self):
        """Test successful tool discovery."""
        aitherClient = AitherClient(api_key="test-key")
        tools_data = {
            "tools": [
                {
                    "name": "explore_code",
                    "description": "Explore codebase",
                    "parameters": {"type": "object", "properties": {
                        "query": {"type": "string"}
                    }},
                },
                {
                    "name": "search_memory",
                    "description": "Search memory graph",
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string"}
                    }},
                },
            ]
        }
        client, resp = _mock_client(tools_data)

        with _patch_httpx(client):
            tools = await aitherClient.auto_discover_tools()
            assert len(tools) == 2
            assert tools[0]["name"] == "explore_code"
            assert tools[1]["name"] == "search_memory"
            # Verify it called the manifest endpoint
            client.get.assert_called_once()
            call_url = client.get.call_args[0][0]
            assert "/tools/manifest" in call_url

    @pytest.mark.asyncio
    async def test_discover_tools_with_raw_list(self):
        """Test tool discovery when manifest returns raw list (not wrapped)."""
        aitherClient = AitherClient(api_key="test-key")
        tools_data = [
            {
                "name": "tool_a",
                "description": "Tool A",
                "parameters": {},
            },
            {
                "name": "tool_b",
                "description": "Tool B",
                "parameters": {},
            },
        ]
        client, resp = _mock_client(tools_data)

        with _patch_httpx(client):
            tools = await aitherClient.auto_discover_tools()
            assert len(tools) == 2
            assert tools[0]["name"] == "tool_a"

    @pytest.mark.asyncio
    async def test_discover_tools_caching(self):
        """Test that auto_discover_tools caches results."""
        aitherClient = AitherClient(api_key="test-key")
        tools_data = {
            "tools": [
                {"name": "tool1", "description": "Tool 1", "parameters": {}},
            ]
        }
        client, resp = _mock_client(tools_data)

        with _patch_httpx(client):
            # First call
            tools1 = await aitherClient.auto_discover_tools()
            assert len(tools1) == 1

            # Second call should use cache (no new GET call)
            tools2 = await aitherClient.auto_discover_tools()
            assert len(tools2) == 1
            # Only one GET call total (from the first auto_discover_tools)
            assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_discover_tools_refresh(self):
        """Test that refresh=True bypasses the cache."""
        aitherClient = AitherClient(api_key="test-key")
        tools_data_v1 = {
            "tools": [
                {"name": "tool1", "description": "Tool 1", "parameters": {}},
            ]
        }
        client, resp = _mock_client(tools_data_v1)

        with _patch_httpx(client):
            # First call
            tools1 = await aitherClient.auto_discover_tools()
            assert len(tools1) == 1

            # Change the mock response for the second call
            tools_data_v2 = {
                "tools": [
                    {"name": "tool1", "description": "Tool 1", "parameters": {}},
                    {"name": "tool2", "description": "Tool 2", "parameters": {}},
                ]
            }
            client.get.return_value.json.return_value = tools_data_v2

            # Second call with refresh=True
            tools2 = await aitherClient.auto_discover_tools(refresh=True)
            assert len(tools2) == 2
            # Two GET calls total
            assert client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_discover_tools_401_error(self):
        """Test 401 (auth failure) when discovering tools."""
        aitherClient = AitherClient(api_key="bad-key")
        client, resp = _mock_client({}, status_code=401)

        with _patch_httpx(client):
            with pytest.raises(ValueError) as exc_info:
                await aitherClient.auto_discover_tools()
            assert "Authentication failed" in str(exc_info.value)
            assert "401" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_discover_tools_403_error(self):
        """Test 403 (access denied) when discovering tools."""
        aitherClient = AitherClient(api_key="test-key")
        client, resp = _mock_client({}, status_code=403)

        with _patch_httpx(client):
            with pytest.raises(ValueError) as exc_info:
                await aitherClient.auto_discover_tools()
            assert "Access denied" in str(exc_info.value)
            assert "403" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_discover_tools_500_error(self):
        """Test 500 (server error) when discovering tools."""
        aitherClient = AitherClient(api_key="test-key")
        client, resp = _mock_client({}, status_code=500)

        with _patch_httpx(client):
            with pytest.raises(Exception) as exc_info:
                await aitherClient.auto_discover_tools()
            assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_discover_tools_custom_mcp_url(self):
        """Test using custom MCP URL."""
        aitherClient = AitherClient(
            api_key="test-key",
            mcp_url="https://custom-mcp.example.com"
        )
        tools_data = {"tools": []}
        client, resp = _mock_client(tools_data)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()
            call_url = client.get.call_args[0][0]
            assert "https://custom-mcp.example.com" in call_url


# ---------------------------------------------------------------------------
# call_discovered_tool tests
# ---------------------------------------------------------------------------

class TestCallDiscoveredTool:
    """Tests for AitherClient.call_discovered_tool()."""

    @pytest.mark.asyncio
    async def test_call_discovered_tool_success(self):
        """Test successful tool call."""
        aitherClient = AitherClient(api_key="test-key")

        # First, discover tools
        tools_data = {
            "tools": [
                {
                    "name": "explore_code",
                    "description": "Explore codebase",
                    "parameters": {},
                },
            ]
        }

        # Then, call the tool
        tool_result = {
            "result": {
                "content": [
                    {"type": "text", "text": "Found 42 functions"},
                ]
            }
        }

        # We need to mock two separate calls: one for discovery, one for tool call
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = tools_data

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = tool_result

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_get_resp)
        client.post = AsyncMock(return_value=mock_post_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            # Discover tools first
            await aitherClient.auto_discover_tools()

            # Now call a discovered tool
            result = await aitherClient.call_discovered_tool(
                "explore_code", query="agent dispatch"
            )
            assert "Found 42 functions" in result
            # Verify POST was called with correct payload
            client.post.assert_called_once()
            call_json = client.post.call_args[1]["json"]
            assert call_json["method"] == "tools/call"
            assert call_json["params"]["name"] == "explore_code"
            assert call_json["params"]["arguments"] == {"query": "agent dispatch"}

    @pytest.mark.asyncio
    async def test_call_discovered_tool_no_discovery_yet(self):
        """Test calling tool before discovery raises error."""
        aitherClient = AitherClient(api_key="test-key")

        with pytest.raises(ValueError) as exc_info:
            await aitherClient.call_discovered_tool("explore_code", query="test")
        assert "No tools discovered yet" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_discovered_tool_not_in_set(self):
        """Test calling tool that was not discovered raises error."""
        aitherClient = AitherClient(api_key="test-key")

        # Discover one tool
        tools_data = {
            "tools": [
                {"name": "tool_a", "description": "Tool A", "parameters": {}},
            ]
        }
        client, resp = _mock_client(tools_data)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()

            # Try to call a different tool
            with pytest.raises(ValueError) as exc_info:
                await aitherClient.call_discovered_tool("tool_b", query="test")
            assert "tool_b" in str(exc_info.value)
            assert "not in discovered tools" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_discovered_tool_401_error(self):
        """Test 401 (auth failure) when calling tool."""
        aitherClient = AitherClient(api_key="test-key")

        # First, discover tools
        tools_data = {
            "tools": [
                {"name": "explore_code", "description": "Explore", "parameters": {}},
            ]
        }

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = tools_data

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 401
        mock_post_resp.text = "Unauthorized"

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_get_resp)
        client.post = AsyncMock(return_value=mock_post_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()

            with pytest.raises(ValueError) as exc_info:
                await aitherClient.call_discovered_tool("explore_code")
            assert "Authentication failed" in str(exc_info.value)
            assert "401" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_discovered_tool_402_balance_error(self):
        """Test 402 (insufficient balance) when calling tool."""
        aitherClient = AitherClient(api_key="test-key")

        # First, discover tools
        tools_data = {
            "tools": [
                {"name": "explore_code", "description": "Explore", "parameters": {}},
            ]
        }

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = tools_data

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 402
        mock_post_resp.text = "Payment Required"

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_get_resp)
        client.post = AsyncMock(return_value=mock_post_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()

            with pytest.raises(ValueError) as exc_info:
                await aitherClient.call_discovered_tool("explore_code")
            assert "Insufficient token balance" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_discovered_tool_json_rpc_error(self):
        """Test JSON-RPC error response from tool call."""
        aitherClient = AitherClient(api_key="test-key")

        # First, discover tools
        tools_data = {
            "tools": [
                {"name": "explore_code", "description": "Explore", "parameters": {}},
            ]
        }

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = tools_data

        # JSON-RPC error response
        error_response = {
            "error": {
                "code": -32001,
                "message": "Tool not found",
            }
        }

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = error_response

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_get_resp)
        client.post = AsyncMock(return_value=mock_post_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()

            with pytest.raises(Exception) as exc_info:
                await aitherClient.call_discovered_tool("explore_code")
            assert "MCP error" in str(exc_info.value)
            assert "Tool not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_discovered_tool_text_result(self):
        """Test tool call with text result."""
        aitherClient = AitherClient(api_key="test-key")

        # First, discover tools
        tools_data = {
            "tools": [
                {"name": "explore_code", "description": "Explore", "parameters": {}},
            ]
        }

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = tools_data

        # Simple text result
        tool_result = {
            "result": {
                "content": [
                    {"type": "text", "text": "Result line 1"},
                    {"type": "text", "text": "Result line 2"},
                ]
            }
        }

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = tool_result

        client = MagicMock()
        client.get = AsyncMock(return_value=mock_get_resp)
        client.post = AsyncMock(return_value=mock_post_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with _patch_httpx(client):
            await aitherClient.auto_discover_tools()

            result = await aitherClient.call_discovered_tool("explore_code")
            assert "Result line 1" in result
            assert "Result line 2" in result
