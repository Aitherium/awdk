"""Tests for protocol drivers (adk.pack_drivers)."""

from __future__ import annotations

import json

import httpx
import pytest

from adk.pack_drivers import (
    A2ADriver,
    DriverResult,
    HttpDriver,
    LangGraphRestDriver,
    McpDriver,
    ToolCall,
    get_driver,
    DRIVER_PROTOCOLS,
)


# ────────────────────────────────────────────────────────────────────────────
# HttpDriver Tests
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_http_driver_success():
    """HttpDriver parses a simple JSON response."""
    def handler(request):
        return httpx.Response(
            200,
            json={"output": "Hello, Agent!"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = HttpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("What is 2+2?")
    assert result.text == "Hello, Agent!"
    assert result.tool_calls == []
    assert result.raw["output"] == "Hello, Agent!"

    await driver.close()


@pytest.mark.asyncio
async def test_http_driver_lenient_keys():
    """HttpDriver tries multiple key names for text."""
    def handler(request):
        return httpx.Response(200, json={"response": "From response key"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = HttpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "From response key"

    await driver.close()


@pytest.mark.asyncio
async def test_http_driver_with_tool_calls():
    """HttpDriver extracts tool_calls from response."""
    def handler(request):
        return httpx.Response(
            200,
            json={
                "output": "Calling tools",
                "tool_calls": [
                    {"name": "calculator", "arguments": {"op": "add", "a": 1, "b": 2}, "id": "tc1"},
                    {"name": "search", "arguments": {"q": "python"}, "id": "tc2"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = HttpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("call tools")
    assert result.text == "Calling tools"
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "calculator"
    assert result.tool_calls[0].arguments == {"op": "add", "a": 1, "b": 2}
    assert result.tool_calls[1].name == "search"

    await driver.close()


@pytest.mark.asyncio
async def test_http_driver_non_json_response():
    """HttpDriver falls back to plain text if response is not JSON."""
    def handler(request):
        return httpx.Response(200, text="Plain text response")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = HttpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "Plain text response"
    assert result.raw == {}

    await driver.close()


@pytest.mark.asyncio
async def test_http_driver_error_response():
    """HttpDriver raises on non-2xx status."""
    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = HttpDriver("http://localhost:8000", client=client)

    with pytest.raises(RuntimeError, match="HTTP error.*500"):
        await driver.prompt("test")

    await driver.close()


# ────────────────────────────────────────────────────────────────────────────
# LangGraphRestDriver Tests
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_langgraph_rest_driver_sse_stream():
    """LangGraphRestDriver parses SSE stream."""
    sse_response = (
        "data: {\"messages\": [[\"user\", \"What is AI?\"]]}\n\n"
        "data: {\"messages\": [[\"assistant\", \"AI is...\"]]}\n\n"
    )

    def handler(request):
        return httpx.Response(200, text=sse_response)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = LangGraphRestDriver("http://localhost:8000", client=client)

    result = await driver.prompt("What is AI?")
    assert result.text == "AI is..."
    assert result.tool_calls == []

    await driver.close()


@pytest.mark.asyncio
async def test_langgraph_rest_driver_content_key():
    """LangGraphRestDriver extracts content from root level."""
    sse_response = 'data: {"content": "Response from content key"}\n\n'

    def handler(request):
        return httpx.Response(200, text=sse_response)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = LangGraphRestDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "Response from content key"

    await driver.close()


@pytest.mark.asyncio
async def test_langgraph_rest_driver_multiple_events():
    """LangGraphRestDriver handles multiple SSE events and keeps last text."""
    sse_response = (
        "data: {\"text\": \"First\"}\n\n"
        ": comment line\n\n"
        "data: {\"text\": \"Second\"}\n\n"
        "data: {\"text\": \"Final\"}\n\n"
    )

    def handler(request):
        return httpx.Response(200, text=sse_response)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = LangGraphRestDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "Final"

    await driver.close()


@pytest.mark.asyncio
async def test_langgraph_rest_driver_error():
    """LangGraphRestDriver raises on HTTP error."""
    def handler(request):
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = LangGraphRestDriver("http://localhost:8000", client=client)

    with pytest.raises(RuntimeError, match="HTTP error.*503"):
        await driver.prompt("test")

    await driver.close()


# ────────────────────────────────────────────────────────────────────────────
# A2ADriver Tests
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_driver_success():
    """A2ADriver sends message/send and parses result."""
    def handler(request):
        req_body = json.loads(request.content)
        assert req_body["method"] == "message/send"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {
                    "task": {
                        "id": "task-1",
                        "history": [
                            {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                            {"role": "agent", "parts": [{"type": "text", "text": "Hello!"}]},
                        ],
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = A2ADriver("http://localhost:8000", client=client)

    result = await driver.prompt("hi")
    assert result.text == "Hello!"
    assert result.tool_calls == []

    await driver.close()


@pytest.mark.asyncio
async def test_a2a_driver_message_field():
    """A2ADriver extracts text from message field in result."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {
                    "message": {
                        "role": "agent",
                        "parts": [{"type": "text", "text": "From message field"}],
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = A2ADriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "From message field"

    await driver.close()


@pytest.mark.asyncio
async def test_a2a_driver_json_rpc_error():
    """A2ADriver raises on JSON-RPC error response."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "error": {"code": -32602, "message": "Invalid params"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = A2ADriver("http://localhost:8000", client=client)

    with pytest.raises(RuntimeError, match="A2A error.*Invalid params"):
        await driver.prompt("test")

    await driver.close()


@pytest.mark.asyncio
async def test_a2a_driver_http_error():
    """A2ADriver raises on HTTP error."""
    def handler(request):
        return httpx.Response(404, text="Not found")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = A2ADriver("http://localhost:8000", client=client)

    with pytest.raises(RuntimeError, match="HTTP error.*404"):
        await driver.prompt("test")

    await driver.close()


# ────────────────────────────────────────────────────────────────────────────
# McpDriver Tests
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_driver_success():
    """McpDriver sends tools/call and parses result."""
    def handler(request):
        req_body = json.loads(request.content)
        assert req_body["method"] == "tools/call"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {"output": "MCP response"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = McpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "MCP response"
    assert result.tool_calls == []

    await driver.close()


@pytest.mark.asyncio
async def test_mcp_driver_custom_tool_name():
    """McpDriver can use a custom tool name."""
    def handler(request):
        req_body = json.loads(request.content)
        assert req_body["params"]["name"] == "my_custom_tool"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {"text": "Custom tool result"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = McpDriver(
        "http://localhost:8000",
        tool_name="my_custom_tool",
        client=client,
    )

    result = await driver.prompt("test")
    assert result.text == "Custom tool result"

    await driver.close()


@pytest.mark.asyncio
async def test_mcp_driver_error_response():
    """McpDriver raises on JSON-RPC error."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "error": {"code": -32603, "message": "Internal error"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = McpDriver("http://localhost:8000", client=client)

    with pytest.raises(RuntimeError, match="MCP error.*Internal error"):
        await driver.prompt("test")

    await driver.close()


@pytest.mark.asyncio
async def test_mcp_driver_lenient_keys():
    """McpDriver tries multiple key names for text in result."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {"content": "From content key"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = McpDriver("http://localhost:8000", client=client)

    result = await driver.prompt("test")
    assert result.text == "From content key"

    await driver.close()


# ────────────────────────────────────────────────────────────────────────────
# get_driver Factory Tests
# ────────────────────────────────────────────────────────────────────────────

def test_get_driver_http():
    """get_driver returns HttpDriver for protocol='http'."""
    driver = get_driver("http", "http://example.com")
    assert isinstance(driver, HttpDriver)


def test_get_driver_langgraph():
    """get_driver returns LangGraphRestDriver for protocol='langgraph_rest'."""
    driver = get_driver("langgraph_rest", "http://example.com")
    assert isinstance(driver, LangGraphRestDriver)


def test_get_driver_a2a():
    """get_driver returns A2ADriver for protocol='a2a'."""
    driver = get_driver("a2a", "http://example.com")
    assert isinstance(driver, A2ADriver)


def test_get_driver_mcp():
    """get_driver returns McpDriver for protocol='mcp'."""
    driver = get_driver("mcp", "http://example.com")
    assert isinstance(driver, McpDriver)


def test_get_driver_with_kwargs():
    """get_driver passes kwargs to driver constructor."""
    driver = get_driver("mcp", "http://example.com", tool_name="custom")
    assert isinstance(driver, McpDriver)
    assert driver.tool_name == "custom"


def test_get_driver_unsupported_protocol():
    """get_driver raises NotImplementedError for unsupported protocol."""
    with pytest.raises(NotImplementedError, match="Protocol.*not supported"):
        get_driver("unknown_protocol", "http://example.com")


def test_driver_protocols_constant():
    """DRIVER_PROTOCOLS contains expected protocol names."""
    assert "http" in DRIVER_PROTOCOLS
    assert "langgraph_rest" in DRIVER_PROTOCOLS
    assert "a2a" in DRIVER_PROTOCOLS
    assert "mcp" in DRIVER_PROTOCOLS
    assert len(DRIVER_PROTOCOLS) == 4


# ────────────────────────────────────────────────────────────────────────────
# Integration / Edge Cases
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_driver_result_dataclass():
    """DriverResult can be created and used."""
    tc = ToolCall(name="test", arguments={"x": 1}, tool_call_id="1")
    result = DriverResult(text="hi", tool_calls=[tc], raw={"key": "value"})

    assert result.text == "hi"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "test"
    assert result.raw["key"] == "value"


@pytest.mark.asyncio
async def test_http_driver_no_client_provided():
    """HttpDriver creates its own client if none provided."""
    def handler(request):
        return httpx.Response(200, json={"output": "ok"})

    transport = httpx.MockTransport(handler)
    # Don't provide a client; driver should create one
    driver = HttpDriver("http://localhost:8000")
    # Override with mock transport by recreating client
    driver._client = httpx.AsyncClient(transport=transport)

    result = await driver.prompt("test")
    assert result.text == "ok"

    await driver.close()


@pytest.mark.asyncio
async def test_a2a_driver_request_ids_increment():
    """A2ADriver increments request IDs."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {"message": {"role": "agent", "parts": [{"type": "text", "text": "ok"}]}},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = A2ADriver("http://localhost:8000", client=client)

    # Send two prompts and verify request IDs increment
    await driver.prompt("test1")
    await driver.prompt("test2")

    assert driver._request_id == 2

    await driver.close()


@pytest.mark.asyncio
async def test_mcp_driver_request_ids_increment():
    """McpDriver increments request IDs."""
    def handler(request):
        req_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": req_body["id"],
                "result": {"text": "ok"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    driver = McpDriver("http://localhost:8000", client=client)

    await driver.prompt("test1")
    await driver.prompt("test2")

    assert driver._request_id == 2

    await driver.close()
