"""Tests for customer self-hosted MCP "hands" registration (GAP-013).

register_mcp_endpoint_tools discovers a remote MCP server's tools (tools/list)
and registers a proxy tool per advertised tool that forwards tools/call. These
attach the customer's OWN tools to their self-hosted adk loop.
"""

import asyncio
from unittest.mock import MagicMock, patch

from adk.mcp_endpoint_tools import (
    _headers_for,
    _make_mcp_proxy_fn,
    register_mcp_endpoint_tools,
)


def _agent():
    agent = MagicMock()
    agent.name = "test"
    agent._tools = MagicMock()
    agent._tools._tools = {}
    return agent


class _AsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return self._resp


class TestHeaders:
    def test_token_becomes_bearer(self):
        h = _headers_for({"url": "x", "token": "abc"})
        assert h["Authorization"] == "Bearer abc"
        assert h["Content-Type"] == "application/json"

    def test_extra_headers_merge(self):
        h = _headers_for({"headers": {"X-Foo": "bar"}})
        assert h["X-Foo"] == "bar"


class TestMakeProxyFn:
    def test_is_async_and_named(self):
        fn = _make_mcp_proxy_fn("http://localhost:9", "search", {})
        assert fn.__name__ == "search"
        assert asyncio.iscoroutinefunction(fn)

    def test_returns_text_content(self):
        fn = _make_mcp_proxy_fn("http://localhost:8000", "echo", {})
        resp = MagicMock()
        resp.json.return_value = {"result": {"content": [{"type": "text", "text": "hi there"}]}}
        resp.raise_for_status = MagicMock()
        with patch("httpx.AsyncClient", return_value=_AsyncClient(resp)):
            out = asyncio.run(fn(text="hello"))
        assert out == "hi there"

    def test_surfaces_jsonrpc_error(self):
        fn = _make_mcp_proxy_fn("http://localhost:8000", "echo", {})
        resp = MagicMock()
        resp.json.return_value = {"error": {"code": -32601, "message": "no such tool"}}
        resp.raise_for_status = MagicMock()
        with patch("httpx.AsyncClient", return_value=_AsyncClient(resp)):
            out = asyncio.run(fn())
        assert "error" in out


class TestRegister:
    def test_empty_returns_zero(self):
        assert register_mcp_endpoint_tools(_agent(), None) == 0
        assert register_mcp_endpoint_tools(_agent(), []) == 0

    def test_skips_endpoint_without_url(self):
        assert register_mcp_endpoint_tools(_agent(), [{"name": "x"}]) == 0

    def test_registers_namespaced_tools(self):
        agent = _agent()
        listed = MagicMock()
        listed.status_code = 200
        listed.json.return_value = {"result": {"tools": [
            {"name": "search", "description": "Search",
             "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
            {"name": "fetch", "description": "Fetch"},
        ]}}
        with patch("httpx.post", return_value=listed):
            count = register_mcp_endpoint_tools(agent, [{"name": "mybox", "url": "http://localhost:8000"}])
        assert count == 2
        assert "mybox__search" in agent._tools._tools
        assert "mybox__fetch" in agent._tools._tools
        # discovered inputSchema is preserved as the tool's parameters
        assert agent._tools._tools["mybox__search"].parameters["properties"]["q"]["type"] == "string"

    def test_unreachable_endpoint_registers_nothing(self):
        agent = _agent()
        with patch("httpx.post", side_effect=Exception("boom")):
            count = register_mcp_endpoint_tools(agent, [{"name": "x", "url": "http://localhost:9999"}])
        assert count == 0
