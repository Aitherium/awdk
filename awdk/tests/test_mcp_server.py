"""Tests for the MCP server (JSON-RPC 2.0 tool serving)."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.mcp_server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TOOL_NOT_FOUND,
    MCPServer,
    MCPServerInfo,
)
from adk.tools import ToolRegistry, tool


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_registry_with_tools() -> ToolRegistry:
    """Build a ToolRegistry with a few test tools."""
    registry = ToolRegistry()

    def greet(name: str) -> str:
        """Greet someone by name."""
        return f"Hello, {name}!"

    async def add(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    def fail_tool() -> str:
        """A tool that always fails."""
        raise ValueError("intentional failure")

    registry.register(greet)
    registry.register(add)
    registry.register(fail_tool)
    return registry


@pytest.fixture
def registry():
    return _make_registry_with_tools()


@pytest.fixture
def server(registry):
    return MCPServer(tool_registry=registry, server_name="test-node", server_version="1.0.0")


# ── Protocol Tests ────────────────────────────────────────────────────────


class TestProtocol:
    @pytest.mark.asyncio
    async def test_missing_jsonrpc(self, server):
        resp = await server.handle({"method": "ping", "id": 1})
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_wrong_jsonrpc_version(self, server):
        resp = await server.handle({"jsonrpc": "1.0", "method": "ping", "id": 1})
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_missing_method(self, server):
        resp = await server.handle({"jsonrpc": "2.0", "id": 1})
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_unknown_method(self, server):
        resp = await server.handle({"jsonrpc": "2.0", "method": "nonexistent", "id": 1})
        assert "error" in resp
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    @pytest.mark.asyncio
    async def test_parse_error_bad_json(self, server):
        resp = await server.handle(b"not json at all{{{")
        assert "error" in resp
        assert resp["error"]["code"] == PARSE_ERROR

    @pytest.mark.asyncio
    async def test_parse_string_body(self, server):
        body = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1})
        resp = await server.handle(body)
        assert "result" in resp

    @pytest.mark.asyncio
    async def test_parse_bytes_body(self, server):
        body = json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}).encode()
        resp = await server.handle(body)
        assert "result" in resp

    @pytest.mark.asyncio
    async def test_non_dict_body(self, server):
        resp = await server.handle([1, 2, 3])
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_id_preserved(self, server):
        resp = await server.handle({"jsonrpc": "2.0", "method": "ping", "id": 42})
        assert resp["id"] == 42

    @pytest.mark.asyncio
    async def test_null_id(self, server):
        resp = await server.handle({"jsonrpc": "2.0", "method": "ping", "id": None})
        assert resp["id"] is None


# ── Initialize ────────────────────────────────────────────────────────────


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
        })
        assert "result" in resp
        result = resp["result"]
        assert result["serverInfo"]["name"] == "test-node"
        assert result["serverInfo"]["version"] == "1.0.0"
        assert "protocolVersion" in result
        assert "capabilities" in result
        assert "tools" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_initialize_with_params(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "test"}},
            "id": 1,
        })
        assert "result" in resp


# ── Ping ──────────────────────────────────────────────────────────────────


class TestPing:
    @pytest.mark.asyncio
    async def test_ping(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "ping", "id": 1,
        })
        assert "result" in resp
        assert resp["result"] == {}


# ── tools/list ────────────────────────────────────────────────────────────


class TestToolsList:
    @pytest.mark.asyncio
    async def test_list_tools(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert "result" in resp
        tools = resp["result"]["tools"]
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert "greet" in names
        assert "add" in names
        assert "fail_tool" in names

    @pytest.mark.asyncio
    async def test_tool_has_input_schema(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        tools = resp["result"]["tools"]
        greet = next(t for t in tools if t["name"] == "greet")
        assert "inputSchema" in greet
        assert greet["inputSchema"]["type"] == "object"
        assert "name" in greet["inputSchema"]["properties"]

    @pytest.mark.asyncio
    async def test_tool_has_description(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        tools = resp["result"]["tools"]
        greet = next(t for t in tools if t["name"] == "greet")
        assert "description" in greet
        assert "Greet" in greet["description"]

    @pytest.mark.asyncio
    async def test_empty_registry(self):
        server = MCPServer(tool_registry=ToolRegistry())
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert resp["result"]["tools"] == []


# ── tools/call ────────────────────────────────────────────────────────────


class TestToolsCall:
    @pytest.mark.asyncio
    async def test_call_sync_tool(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "greet", "arguments": {"name": "World"}},
            "id": 1,
        })
        assert "result" in resp
        content = resp["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Hello, World!"

    @pytest.mark.asyncio
    async def test_call_async_tool(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 3, "b": 4}},
            "id": 1,
        })
        assert "result" in resp
        content = resp["result"]["content"]
        assert content[0]["text"] == "7"

    @pytest.mark.asyncio
    async def test_call_nonexistent_tool(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}},
            "id": 1,
        })
        assert "error" in resp
        assert resp["error"]["code"] == TOOL_NOT_FOUND

    @pytest.mark.asyncio
    async def test_call_missing_name(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"arguments": {}},
            "id": 1,
        })
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_call_missing_params(self, server):
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "id": 1,
        })
        # Empty params dict → missing name
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS

    @pytest.mark.asyncio
    async def test_call_empty_arguments(self, server):
        """Tool call with no arguments defaults to empty dict."""
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "fail_tool"},
            "id": 1,
        })
        # ToolRegistry.execute() catches exceptions and returns {"error": "..."} as text
        assert "result" in resp
        text = resp["result"]["content"][0]["text"]
        assert "intentional failure" in text

    @pytest.mark.asyncio
    async def test_call_tool_error_in_content(self, server):
        """Tool that raises returns error info in content text (ToolRegistry catches it)."""
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "fail_tool", "arguments": {}},
            "id": 1,
        })
        # ToolRegistry.execute() wraps the error as JSON string, not JSON-RPC error
        assert "result" in resp
        content = resp["result"]["content"]
        assert content[0]["type"] == "text"
        assert "intentional failure" in content[0]["text"]

    @pytest.mark.asyncio
    async def test_call_increments_counter(self, server):
        assert server._call_count == 0
        await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "greet", "arguments": {"name": "X"}},
            "id": 1,
        })
        assert server._call_count == 1

    @pytest.mark.asyncio
    async def test_error_increments_counter(self, server):
        assert server._error_count == 0
        await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
            "id": 1,
        })
        assert server._error_count == 1

    @pytest.mark.asyncio
    async def test_call_with_non_dict_params(self, server):
        """params that isn't a dict should return INVALID_PARAMS."""
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": "not a dict",
            "id": 1,
        })
        assert "error" in resp
        assert resp["error"]["code"] == INVALID_PARAMS


# ── Status ────────────────────────────────────────────────────────────────


class TestStatus:
    def test_status(self, server):
        status = server.status()
        assert status["name"] == "test-node"
        assert status["version"] == "1.0.0"
        assert status["tools_count"] == 3
        assert "greet" in status["tools"]
        assert status["calls"] == 0
        assert status["errors"] == 0
        assert status["uptime_s"] >= 0

    @pytest.mark.asyncio
    async def test_status_after_calls(self, server):
        await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "greet", "arguments": {"name": "X"}},
            "id": 1,
        })
        await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
            "id": 2,
        })
        status = server.status()
        assert status["calls"] == 1
        assert status["errors"] == 1


# ── Registry property ────────────────────────────────────────────────────


class TestRegistryProperty:
    def test_get_registry(self, server, registry):
        assert server.registry is registry

    def test_set_registry(self, server):
        new_reg = ToolRegistry()
        server.registry = new_reg
        assert server.registry is new_reg


# ── MCPServerInfo ─────────────────────────────────────────────────────────


class TestServerInfo:
    def test_defaults(self):
        info = MCPServerInfo()
        assert info.name == "adk-node"
        assert info.version == ""
        assert "tools" in info.capabilities


# ── FastAPI mount ─────────────────────────────────────────────────────────


class TestMount:
    def test_mount_registers_routes(self, server):
        """Verify mount() adds POST and GET routes."""
        app = MagicMock()
        app.post = MagicMock(return_value=lambda f: f)
        app.get = MagicMock(return_value=lambda f: f)
        server.mount(app)
        app.post.assert_called_once_with("/mcp")
        app.get.assert_called_once_with("/mcp")

    def test_mount_custom_path(self, server):
        app = MagicMock()
        app.post = MagicMock(return_value=lambda f: f)
        app.get = MagicMock(return_value=lambda f: f)
        server.mount(app, path="/v1/mcp")
        app.post.assert_called_once_with("/v1/mcp")
        app.get.assert_called_once_with("/v1/mcp")


# ── Integration: full round-trip with @tool decorator ─────────────────────


class TestIntegration:
    @pytest.mark.asyncio
    async def test_tool_decorator_to_mcp(self):
        """Register tools with @tool decorator, serve via MCPServer, consume via JSON-RPC."""
        registry = ToolRegistry()

        def multiply(x: int, y: int) -> str:
            """Multiply two numbers."""
            return str(x * y)

        registry.register(multiply)

        server = MCPServer(tool_registry=registry)

        # List
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "multiply"
        assert tools[0]["inputSchema"]["properties"]["x"]["type"] == "integer"

        # Call
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "multiply", "arguments": {"x": 6, "y": 7}},
            "id": 2,
        })
        assert resp["result"]["content"][0]["text"] == "42"

    @pytest.mark.asyncio
    async def test_client_server_wire_compat(self):
        """Verify MCPServer output matches what MCPBridge expects to parse."""
        registry = ToolRegistry()

        def echo(msg: str) -> str:
            """Echo back."""
            return msg

        registry.register(echo)
        server = MCPServer(tool_registry=registry)

        # tools/list — MCPBridge parses result.tools[].{name, description, inputSchema}
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        for t in resp["result"]["tools"]:
            assert "name" in t
            assert "description" in t
            assert "inputSchema" in t

        # tools/call — MCPBridge parses result.content[].{type, text}
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "echo", "arguments": {"msg": "hello"}},
            "id": 2,
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 2
        content = resp["result"]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "hello"


# ── Default constructor ──────────────────────────────────────────────────


class TestDefaultConstructor:
    def test_no_args(self):
        """MCPServer with no registry still works (empty tool list)."""
        server = MCPServer()
        assert len(server.registry.list_tools()) == 0
        assert server._info.name == "adk-node"

    @pytest.mark.asyncio
    async def test_no_tools_list(self):
        server = MCPServer()
        resp = await server.handle({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert resp["result"]["tools"] == []
