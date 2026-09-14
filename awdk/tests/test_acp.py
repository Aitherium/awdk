"""Tests for the ACP (Agent Client Protocol) client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adk.acp import (
    ACPApprovalRequest,
    ACPCapabilities,
    ACPClient,
    ACPPromptResult,
    ACPToolCall,
    ACPUsage,
)

logger = logging.getLogger(__name__)


class QueueBasedStreamReader:
    """Mock StreamReader that reads from a queue."""

    def __init__(self, queue: asyncio.Queue):
        """Initialize."""
        self.queue = queue

    async def readline(self) -> bytes:
        """Read a line from the queue."""
        line = await self.queue.get()
        if isinstance(line, str):
            return (line + "\n").encode("utf-8")
        return line


class QueueBasedStreamWriter:
    """Mock StreamWriter that writes to a queue."""

    def __init__(self, queue: asyncio.Queue):
        """Initialize."""
        self.queue = queue

    def write(self, data: bytes) -> None:
        """Write to queue."""
        self.queue.put_nowait(data.decode("utf-8").strip())

    async def drain(self) -> None:
        """Drain (no-op for queue)."""
        pass

    def close(self) -> None:
        """Close (no-op for queue)."""
        pass

    async def wait_closed(self) -> None:
        """Wait closed (no-op for queue)."""
        pass


class SimpleACPMockServer:
    """Simple in-process mock ACP server using queues."""

    def __init__(self):
        """Initialize the mock server."""
        self.client_to_server: asyncio.Queue = asyncio.Queue()
        self.server_to_client: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the server loop."""
        self._running = True
        self._task = asyncio.create_task(self._server_loop())

    async def _server_loop(self) -> None:
        """Main server loop."""
        while self._running:
            try:
                request_line = await asyncio.wait_for(
                    self.client_to_server.get(), timeout=1.0
                )
                request = json.loads(request_line)
                response = await self._handle_request(request)
                if response:
                    await self.server_to_client.put(json.dumps(response))
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Server error: %s", e)
                break

    async def _handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a JSON-RPC request."""
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocol_version": 1,
                    "agent_info": {"name": "mock-agent", "version": "1.0.0"},
                    "agent_capabilities": {
                        "load_session": True,
                        "prompt_capabilities": {"image": True},
                        "session_capabilities": {"fork": {}, "list": {}, "resume": {}},
                    },
                },
            }

        elif method == "session/new":
            session_id = f"sess-{id(params) % 10000:04d}"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"session_id": session_id},
            }

        elif method == "session/prompt":
            session_id = params.get("session_id")
            # Emit session updates before responding
            await self._emit_prompt_updates(session_id)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"stop_reason": "end_turn"},
            }

        elif method == "session/set_model":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}

        elif method == "session/load":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}

        elif method == "session/resume":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}

        elif method == "session/fork":
            forked_id = f"sess-{id(params) % 10000:04d}"
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"session_id": forked_id},
            }

        elif method == "session/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "sessions": [{"session_id": "sess-001", "title": "Session 1"}],
                    "next_cursor": None,
                },
            }

        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

    async def _emit_prompt_updates(self, session_id: str) -> None:
        """Emit session/update notifications for a prompt."""
        await asyncio.sleep(0.01)
        # tool_call_start
        await self.server_to_client.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "session_id": session_id,
                        "session_update": "tool_call_start",
                        "tool_call_id": "tc-test-001",
                        "tool_name": "read_file",
                        "function_args": {"path": "/test.txt"},
                    },
                }
            )
        )
        await asyncio.sleep(0.01)
        # agent_message_chunk
        await self.server_to_client.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "session_id": session_id,
                        "session_update": "agent_message_chunk",
                        "content": {"type": "text", "text": "I will read that file."},
                    },
                }
            )
        )
        await asyncio.sleep(0.01)
        # tool_call_complete
        await self.server_to_client.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "session_id": session_id,
                        "session_update": "tool_call_complete",
                        "tool_call_id": "tc-test-001",
                        "result": "File contents",
                    },
                }
            )
        )
        await asyncio.sleep(0.01)
        # usage_update
        await self.server_to_client.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "session_id": session_id,
                        "session_update": "usage_update",
                        "input_tokens": 42,
                        "output_tokens": 18,
                    },
                }
            )
        )

    async def stop(self) -> None:
        """Stop the server."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class TestACPClient:
    """Tests for ACPClient."""

    @pytest.mark.asyncio
    async def test_initialize_success(self) -> None:
        """Test successful initialization."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            caps = await client.initialize()
            assert caps.agent_name == "mock-agent"
            assert caps.agent_version == "1.0.0"
            assert caps.protocol_version == 1
            assert caps.load_session is True
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_create_session(self) -> None:
        """Test creating a new session."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            session_id = await client.create_session(cwd="/tmp", model="gpt-4")
            assert session_id.startswith("sess-")
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_prompt_with_tool_calls(self) -> None:
        """Test prompting with tool call streaming."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            result = await client.prompt("sess-test", "Read /test.txt")
            assert isinstance(result, ACPPromptResult)
            assert "read" in result.text.lower()
            assert len(result.tool_calls) > 0
            assert result.tool_calls[0].tool_name == "read_file"
            assert result.tool_calls[0].arguments.get("path") == "/test.txt"
            assert result.usage.input_tokens == 42
            assert result.usage.output_tokens == 18
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_approval_callback_default_deny(self) -> None:
        """Test that approval callback defaults to deny."""
        approval_results: list[bool] = []

        def capture_approval(req: ACPApprovalRequest) -> bool:
            approval_results.append(False)
            return False

        client = ACPClient(approval_callback=capture_approval)

        # Mock writer with async methods
        mock_writer = AsyncMock()
        client._writer = mock_writer

        # Send an approval request notification
        approval_req = {
            "jsonrpc": "2.0",
            "method": "approval_request",
            "params": {
                "request_id": "req-001",
                "kind": "edit",
                "description": "Edit /test.py",
            },
        }

        await client._process_message(approval_req)
        await asyncio.sleep(0.1)

        assert len(approval_results) > 0
        assert approval_results[0] is False

    @pytest.mark.asyncio
    async def test_approval_callback_approve(self) -> None:
        """Test approval callback can approve."""
        approval_results: list[bool] = []

        def approve_callback(req: ACPApprovalRequest) -> bool:
            approval_results.append(True)
            return True

        client = ACPClient(approval_callback=approve_callback)

        # Mock writer with async methods
        mock_writer = AsyncMock()
        client._writer = mock_writer

        approval_req = {
            "jsonrpc": "2.0",
            "method": "approval_request",
            "params": {
                "request_id": "req-001",
                "kind": "edit",
                "description": "Edit /test.py",
            },
        }

        await client._process_message(approval_req)
        await asyncio.sleep(0.1)

        assert len(approval_results) > 0
        assert approval_results[0] is True

    @pytest.mark.asyncio
    async def test_usage_parsing(self) -> None:
        """Test that usage updates are correctly parsed."""
        result = ACPPromptResult(
            text="Hello", tool_calls=[], usage=ACPUsage(input_tokens=10, output_tokens=5)
        )
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_tool_call_structure(self) -> None:
        """Test ACPToolCall structure."""
        tool_call = ACPToolCall(
            tool_call_id="tc-001",
            tool_name="read_file",
            arguments={"path": "/test.txt"},
            result="File content",
        )
        assert tool_call.tool_call_id == "tc-001"
        assert tool_call.tool_name == "read_file"
        assert tool_call.arguments["path"] == "/test.txt"
        assert tool_call.result == "File content"

    @pytest.mark.asyncio
    async def test_set_session_model(self) -> None:
        """Test setting session model."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            await client.set_session_model("sess-001", "gpt-4-turbo")
            # Should not raise
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_fork_session(self) -> None:
        """Test forking a session."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            new_session_id = await client.fork_session("sess-001", cwd="/tmp")
            assert new_session_id.startswith("sess-")
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_list_sessions(self) -> None:
        """Test listing sessions."""
        server = SimpleACPMockServer()
        await server.start()

        reader = QueueBasedStreamReader(server.server_to_client)
        writer = QueueBasedStreamWriter(server.client_to_server)

        client = ACPClient()
        client._reader = reader
        client._writer = writer
        client._read_task = asyncio.create_task(client._read_loop())

        try:
            result = await client.list_sessions()
            assert "sessions" in result
            assert isinstance(result["sessions"], list)
        finally:
            await server.stop()
            await asyncio.sleep(0.05)
            client._read_task.cancel()
            try:
                await client._read_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_capabilities_parsing(self) -> None:
        """Test ACPCapabilities parsing."""
        caps = ACPCapabilities(
            protocol_version=1,
            agent_name="test-agent",
            agent_version="2.0.0",
            load_session=True,
        )
        assert caps.protocol_version == 1
        assert caps.agent_name == "test-agent"
        assert caps.agent_version == "2.0.0"
        assert caps.load_session is True
