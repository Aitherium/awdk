"""ACP v2 CLIENT tests: inbound request handling, cancel, and auth.

Covers the v2 client behaviors added in the Phase 2 client upgrade that the
v1 tests do not: an inbound ``session/request_permission`` REQUEST is answered
on its JSON-RPC id (the v1 client silently dropped it), unknown inbound
requests fail loud, and ``cancel``/``auth_login``/``auth_logout`` use the right
envelope. Queue-based in-process mocks, like ``test_acp.py``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from adk.acp import ACPApprovalRequest, ACPClient

logger = logging.getLogger(__name__)


class QueueReader:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    async def readline(self) -> bytes:
        line = await self.queue.get()
        if isinstance(line, str):
            return (line + "\n").encode("utf-8")
        return line


class QueueWriter:
    def __init__(self, queue: asyncio.Queue):
        self.queue = queue

    def write(self, data: bytes) -> None:
        self.queue.put_nowait(data.decode("utf-8").strip())

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _client(*, approve: bool = True) -> tuple[ACPClient, asyncio.Queue, asyncio.Queue, list]:
    """Build a connected ACPClient over two queues; returns (client, to_client,
    from_client, approval_requests)."""
    to_client: asyncio.Queue = asyncio.Queue()
    from_client: asyncio.Queue = asyncio.Queue()
    seen: list = []

    async def callback(req: ACPApprovalRequest) -> bool:
        seen.append(req)
        return approve

    client = ACPClient(approval_callback=callback)
    client._reader = QueueReader(to_client)
    client._writer = QueueWriter(from_client)
    client._read_task = asyncio.create_task(client._read_loop())
    return client, to_client, from_client, seen


@pytest.mark.asyncio
async def test_request_permission_request_is_answered_on_its_id():
    """A v2 server's request_permission REQUEST gets a RESULT on the same id."""
    client, to_client, from_client, seen = _client(approve=True)
    try:
        await to_client.put(json.dumps({
            "jsonrpc": "2.0", "id": 42, "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "title": "Allow bash?",
                "subject": {"type": "tool_call", "toolCall": {"toolCallId": "t1"}},
                "options": [{"optionId": "allow-once", "name": "Allow", "kind": "allow_once"}],
            },
        }))
        response = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert response["id"] == 42
        assert response["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
        # The callback saw the v2 context.
        assert seen and seen[0].session_id == "s1"
        assert seen[0].tool_call == {"toolCallId": "t1"}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_request_permission_deny_selects_reject():
    client, to_client, from_client, seen = _client(approve=False)
    try:
        await to_client.put(json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "session/request_permission",
            "params": {"toolCall": {"toolCallId": "t1"}, "options": []},
        }))
        response = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert response["result"] == {"outcome": {"outcome": "selected", "optionId": "reject-once"}}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_unknown_inbound_request_fails_loud():
    """An inbound request we do not understand gets a method-not-found error."""
    client, to_client, from_client, _ = _client()
    try:
        await to_client.put(json.dumps({
            "jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file",
            "params": {"path": "/x"},
        }))
        response = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert response["id"] == 9
        assert response["error"]["code"] == -32601
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_cancel_sends_notification():
    """session/cancel is a NOTIFICATION (no id, no response expected)."""
    client, _to_client, from_client, _ = _client()
    try:
        await client.cancel("s1")
        frame = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert frame["method"] == "session/cancel"
        assert frame["params"] == {"sessionId": "s1"}
        assert "id" not in frame
    finally:
        await client.disconnect()


async def _collect_stream(client: ACPClient, text: str = "hi") -> list[dict]:
    """Collect every update yielded by stream_prompt for *text*."""
    return [u async for u in client.stream_prompt("s1", text, drain_timeout=1.0)]


@pytest.mark.asyncio
async def test_stream_prompt_yields_updates_live():
    """stream_prompt yields session/update payloads as they arrive, in order."""
    client, to_client, from_client, _ = _client()
    try:
        stream_task = asyncio.create_task(_collect_stream(client))
        req = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req["method"] == "session/prompt"

        for update in (
            {"sessionUpdate": "user_message", "messageId": "m1", "content": []},
            {"sessionUpdate": "state_update", "state": "running"},
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "hi"}},
            {"sessionUpdate": "state_update", "state": "idle", "stopReason": "end_turn"},
        ):
            await to_client.put(json.dumps({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": "s1", "update": update},
            }))
            await asyncio.sleep(0.01)
        await to_client.put(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}))

        yielded = await asyncio.wait_for(stream_task, 5.0)
        kinds = [u.get("sessionUpdate") for u in yielded]
        assert kinds[0] == "user_message"
        assert {"sessionUpdate": "state_update", "state": "running"} in yielded
        msg = next(u for u in yielded if u.get("sessionUpdate") == "agent_message_chunk")
        assert msg["content"]["text"] == "hi"
        # the terminal idle is the last thing yielded
        assert kinds[-1] == "state_update" and yielded[-1].get("stopReason") == "end_turn"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_close_delete_session_are_requests():
    """session/close and session/delete are REQUESTs with a sessionId."""
    client, to_client, from_client, _ = _client()
    try:
        close_task = asyncio.create_task(client.close_session("s1"))
        req = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req["method"] == "session/close"
        assert req["params"] == {"sessionId": "s1"}
        await to_client.put(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}))
        assert (await asyncio.wait_for(close_task, 2.0)) == {}

        delete_task = asyncio.create_task(client.delete_session("s1"))
        req2 = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req2["method"] == "session/delete"
        await to_client.put(json.dumps({"jsonrpc": "2.0", "id": req2["id"], "result": {}}))
        assert (await asyncio.wait_for(delete_task, 2.0)) == {}
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_auth_login_logout_are_requests():
    """auth/login and auth/logout are REQUESTs (answered with a result)."""
    client, to_client, from_client, _ = _client()
    try:
        # auth/login: the "server" must answer the request id.
        login_task = asyncio.create_task(client.auth_login("gateway"))
        req = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req["method"] == "auth/login"
        assert req["params"] == {"methodId": "gateway"}
        assert "id" in req
        await to_client.put(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}))
        await asyncio.wait_for(login_task, 2.0)

        logout_task = asyncio.create_task(client.auth_logout())
        req2 = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req2["method"] == "auth/logout"
        await to_client.put(json.dumps({"jsonrpc": "2.0", "id": req2["id"], "result": {}}))
        await asyncio.wait_for(logout_task, 2.0)
    finally:
        await client.disconnect()


async def test_batch_response_is_processed():
    """A JSON-RPC BATCH array from the server is split and handled element-wise."""
    client, to_client, from_client, _ = _client()
    try:
        task = asyncio.create_task(client.create_session(cwd="."))
        req = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req["method"] == "session/new"
        # Respond with a batch array (not a single object).
        await to_client.put(json.dumps([
            {"jsonrpc": "2.0", "id": req["id"], "result": {"sessionId": "s-1"}},
        ]))
        assert (await asyncio.wait_for(task, 2.0)) == "s-1"
    finally:
        await client.disconnect()


async def test_eof_fails_inflight_requests():
    """When the peer closes the connection (agent died), an in-flight request
    fails fast with a connection error instead of hanging."""
    client, to_client, from_client, _ = _client()
    try:
        task = asyncio.create_task(client.create_session(cwd="."))
        await asyncio.wait_for(from_client.get(), 2.0)  # request was sent
        await to_client.put(b"")  # EOF (peer closed)
        with pytest.raises(RuntimeError, match="connection closed"):
            await asyncio.wait_for(task, 2.0)
    finally:
        await client.disconnect()


async def test_send_request_respects_custom_timeout():
    """_send_request honors its timeout parameter (a fast failure, not the
    generic 60s — the long timeout is for session/prompt turns specifically)."""
    client, to_client, from_client, _ = _client()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await client._send_request("session/ping", {}, timeout=0.2)
    finally:
        await client.disconnect()


async def test_create_session_sends_additional_directories():
    """session/new carries additionalDirectories (v2 directory scope) when given."""
    client, to_client, from_client, _ = _client()
    try:
        task = asyncio.create_task(
            client.create_session(cwd="/w", additional_directories=["/extra"])
        )
        req = json.loads(await asyncio.wait_for(from_client.get(), 2.0))
        assert req["method"] == "session/new"
        assert req["params"]["additionalDirectories"] == ["/extra"]
        await to_client.put(json.dumps({
            "jsonrpc": "2.0", "id": req["id"], "result": {"sessionId": "s-1"},
        }))
        assert (await asyncio.wait_for(task, 2.0)) == "s-1"
    finally:
        await client.disconnect()
