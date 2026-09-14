"""ACP v2 SERVER tests: drive ACPServer through a raw JSON-RPC client.

These use a minimal in-process raw client (NOT adk.acp.ACPClient) so the exact
wire is asserted — the state machine, the permission bridge, session ops, and
batch handling — without testing the client against itself. Runs over real
socket-pair streams so it works on Windows Proactor too (same reason the
conformance tests do).

Every test runs on ONE event loop: the server task, the socket-pair streams and
the client all live inside ``asyncio.run(_with_server(...))``.
"""
from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from adk.acp_server import ACPServer


class RawClient:
    """Minimal in-process JSON-RPC 2.0 client: requests, notifications, and
    answers to the server's outbound requests (session/request_permission)."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self.writer = writer
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.notifications: list[dict] = []
        self.requests: list[dict] = []  # inbound requests from the server
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                break
            obj = json.loads(line.decode("utf-8", errors="replace"))
            if isinstance(obj, list):
                for entry in obj:
                    self._ingest(entry)
            else:
                self._ingest(obj)

    def _ingest(self, obj: dict) -> None:
        if obj.get("method") is not None and obj.get("id") is not None:
            # An inbound REQUEST from the server (e.g. request_permission).
            self.requests.append(obj)
            return
        if obj.get("id") is not None:
            fut = self._pending.pop(obj["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(obj)
            return
        self.notifications.append(obj)

    async def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, 10.0)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def answer(self, request: dict, result: dict) -> None:
        await self._send({"jsonrpc": "2.0", "id": request["id"], "result": result})

    async def send_batch(self, items: list[tuple[str, dict | None]]) -> list[asyncio.Future]:
        """Send a JSON-RPC batch; returns a future per request (resolved when
        the server's array response arrives)."""
        batch = []
        futures: list[asyncio.Future] = []
        for method, params in items:
            self._id += 1
            rid = self._id
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            futures.append(fut)
            batch.append({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        await self._send(batch)
        return futures

    async def _send(self, obj: Any) -> None:
        self.writer.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self.writer.drain()

    def updates(self) -> list[dict]:
        """Flattened session/update payloads (v2 nested shape)."""
        out = []
        for n in self.notifications:
            if n.get("method") == "session/update":
                params = n.get("params", {})
                out.append(params.get("update", params))
        return out

    def pending_permission(self) -> dict | None:
        return next(
            (r for r in self.requests if r.get("method") == "session/request_permission"),
            None,
        )

    def pending_permission_for(self, sid: str) -> dict | None:
        """The permission request the server sent for ONE session (if any)."""
        return next(
            (r for r in self.requests
             if r.get("method") == "session/request_permission"
             and (r.get("params") or {}).get("sessionId") == sid),
            None,
        )

    def session_updates(self, sid: str) -> list[dict]:
        """Flattened session/update payloads for ONE session only."""
        out = []
        for n in self.notifications:
            if n.get("method") == "session/update":
                params = n.get("params", {}) or {}
                if params.get("sessionId") == sid:
                    out.append(params.get("update", params))
        return out

    async def drain_until(self, pred: Callable[["RawClient"], bool], timeout: float = 3.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if pred(self):
                return True
            await asyncio.sleep(0.02)
        return False


async def _with_server(agent: Any, body: Callable[[RawClient, Any], Awaitable[Any]]) -> Any:
    """Run *body* against ACPServer over a socket pair on the CURRENT loop."""
    a, b = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=a)
    client_reader, client_writer = await asyncio.open_connection(sock=b)
    server = ACPServer(agent, name="v2test", version="2.0.0")
    task = asyncio.create_task(server.serve(server_reader, server_writer))
    client = RawClient(client_reader, client_writer)
    try:
        return await body(client, agent)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        a.close()
        b.close()


async def _init(client: RawClient) -> dict:
    resp = await client.request("initialize", {
        "protocolVersion": 2,
        "capabilities": {},
        "info": {"name": "raw", "version": "1"},
    })
    assert "error" not in resp, resp
    return resp["result"]


async def _new_session(client: RawClient) -> str:
    resp = await client.request("session/new", {"cwd": ".", "mcpServers": []})
    assert "error" not in resp, resp
    return resp["result"]["sessionId"]


# ── agents ─────────────────────────────────────────────────────────────────


class EchoAgent:
    """Plain agent: content + tokens, no approval pause."""

    async def run(self, prompt: str, **kwargs):
        return SimpleNamespace(
            content=f"hello:{prompt}", finish_reason="stop",
            tokens_used=10, prompt_tokens=4, completion_tokens=6,
        )


class GatedAgent:
    """First call pauses for approval; resume() completes the turn."""

    def __init__(self) -> None:
        self.pending = [{"tool_use_id": "t1", "tool": "bash", "args": {"cmd": "ls"}}]
        self.resume_calls: list[list[dict]] = []

    async def run(self, prompt: str, **kwargs):
        return SimpleNamespace(
            content="paused", requires_action=True, pending=self.pending
        )

    async def resume(self, session_id: str, decisions: list[dict]):
        self.resume_calls.append(decisions)
        return SimpleNamespace(content="done", finish_reason="stop", tokens_used=5)


# ── tests ──────────────────────────────────────────────────────────────────


def test_initialize_v2():
    async def body(client, agent):
        result = await _init(client)
        assert result["protocolVersion"] == 2
        caps = result.get("capabilities") or {}
        assert caps.get("session", {}).get("list") == {}
        assert result["info"]["name"] == "v2test"
        # Was `== []` while auth was a stub. The ACP registry rejects an agent
        # that advertises no method, so this now asserts the real advertisement;
        # its full contract lives in tests/test_acp_registry_contract.py.
        assert [m["type"] for m in result["authMethods"]] == ["agent", "terminal"]

    asyncio.run(_with_server(EchoAgent(), body))


def test_prompt_lifecycle_full_state_machine():
    async def body(client, agent):
        await _init(client)
        sid = await _new_session(client)
        resp = await client.request("session/prompt",
                                    {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})
        # Acknowledged immediately, before the turn runs. The response carries a
        # placeholder stopReason for the reference SDK's PromptResponse (v1 relic);
        # the authoritative stop arrives in the idle state_update.
        assert resp["result"].get("stopReason") == "end_turn"
        ok = await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          for u in c.updates())
        )
        assert ok, "never reached idle"
        updates = client.updates()
        # available_commands_update is emitted once at session/new, not per turn.
        turn = [u for u in updates
                if u.get("sessionUpdate") != "available_commands_update"]
        kinds = [u.get("sessionUpdate") for u in turn]

        assert kinds[0] == "user_message"
        assert turn[0]["messageId"]
        assert {"sessionUpdate": "state_update", "state": "running"} in updates
        msg = next(u for u in updates if u.get("sessionUpdate") == "agent_message_chunk")
        assert msg["content"]["text"] == "hello:hi"
        idle = next(u for u in updates if u.get("sessionUpdate") == "state_update"
                    and u.get("state") == "idle")
        assert idle["stopReason"] == "end_turn"
        usage = next(u for u in updates if u.get("sessionUpdate") == "usage_update")
        assert usage["used"]["input_tokens"] == 4

    asyncio.run(_with_server(EchoAgent(), body))


def test_permission_approve_then_resume():
    async def body(client, agent):
        await _init(client)
        sid = await _new_session(client)
        await client.request("session/prompt",
                             {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        ok = await client.drain_until(lambda c: c.pending_permission() is not None)
        assert ok, "no request_permission sent"
        req = client.pending_permission()
        params = req["params"]
        assert params["sessionId"] == sid
        assert params["subject"]["toolCall"]["toolCallId"] == "t1"
        assert any(o["optionId"] == "allow-once" for o in params["options"])
        await client.answer(req, {"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        ok = await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          for u in c.updates())
        )
        assert ok, "never reached idle after approval"
        assert agent.resume_calls == [[{"tool_use_id": "t1", "tool": "bash", "result": "allow"}]]

    asyncio.run(_with_server(GatedAgent(), body))


def test_permission_deny_emits_cancelled_tool_call():
    async def body(client, agent):
        await _init(client)
        sid = await _new_session(client)
        await client.request("session/prompt",
                             {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        await client.drain_until(lambda c: c.pending_permission() is not None)
        req = client.pending_permission()
        await client.answer(req, {"outcome": {"outcome": "selected", "optionId": "reject-once"}})
        await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          for u in c.updates())
        )
        tc = [u for u in client.updates() if u.get("sessionUpdate") == "tool_call_update"]
        cancelled = [u for u in tc if u.get("status") == "cancelled"]
        assert agent.resume_calls == [[{"tool_use_id": "t1", "tool": "bash", "result": "deny"}]]
        assert cancelled and cancelled[0]["toolCallId"] == "t1"

    asyncio.run(_with_server(GatedAgent(), body))


def test_cancel_while_blocked_on_permission():
    async def body(client, agent):
        await _init(client)
        sid = await _new_session(client)
        await client.request("session/prompt",
                             {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        await client.drain_until(lambda c: c.pending_permission() is not None)
        await client.notify("session/cancel", {"sessionId": sid})
        ok = await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          and u.get("stopReason") == "cancelled" for u in c.updates())
        )
        assert ok

    asyncio.run(_with_server(GatedAgent(), body))


def test_cancelling_one_session_does_not_cancel_another():
    """Cancel/close must resolve ONLY the named session's pending permission.

    Regression: _answer_pending_permissions used to resolve EVERY in-flight
    session/request_permission, so with two concurrent sessions blocked on
    approval, cancelling A wrongly ended B's turn with idle(cancelled).
    """
    async def body(client, agent):
        await _init(client)
        sid_a = await _new_session(client)
        sid_b = await _new_session(client)
        # Both sessions pause on their first prompt (GatedAgent requires_action).
        await client.request("session/prompt",
                             {"sessionId": sid_a, "prompt": [{"type": "text", "text": "a"}]})
        await client.request("session/prompt",
                             {"sessionId": sid_b, "prompt": [{"type": "text", "text": "b"}]})
        ok = await client.drain_until(
            lambda c: c.pending_permission_for(sid_a) is not None
                      and c.pending_permission_for(sid_b) is not None
        )
        assert ok, "both sessions should be blocked on a permission"

        # Cancel A only.
        await client.notify("session/cancel", {"sessionId": sid_a})
        a_done = await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          and u.get("stopReason") == "cancelled"
                          for u in c.session_updates(sid_a))
        )
        assert a_done, "A should reach idle(cancelled)"

        # B must NOT have been cancelled — its permission is still pending and
        # no idle state has been emitted for it.
        b_idle = any(
            u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
            for u in client.session_updates(sid_b)
        )
        assert not b_idle, "B must not have reached idle after A was cancelled"
        assert client.pending_permission_for(sid_b) is not None, \
            "B's permission must still be awaiting an answer"

        # Answering B's permission lets B complete normally.
        b_req = client.pending_permission_for(sid_b)
        await client.answer(b_req, {"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        b_done = await client.drain_until(
            lambda c: any(u.get("sessionUpdate") == "state_update" and u.get("state") == "idle"
                          and u.get("stopReason") == "end_turn"
                          for u in c.session_updates(sid_b))
        )
        assert b_done, "B should complete its turn normally after approval"

    asyncio.run(_with_server(GatedAgent(), body))


def test_session_list_resume_close_delete():
    async def body(client, agent):
        await _init(client)
        sid = await _new_session(client)
        listed = await client.request("session/list", {})
        assert listed["result"]["sessions"][0]["sessionId"] == sid
        resumed = await client.request("session/resume", {"sessionId": sid, "cwd": "."})
        assert resumed["result"] == {}
        closed = await client.request("session/close", {"sessionId": sid})
        assert closed["result"] == {}
        listed2 = await client.request("session/list", {})
        assert listed2["result"]["sessions"] == []
        deleted = await client.request("session/delete", {"sessionId": sid})
        assert deleted["result"] == {}  # deleting a missing session succeeds silently

    asyncio.run(_with_server(EchoAgent(), body))


def test_batch_handling():
    async def body(client, agent):
        await _init(client)
        futs = await client.send_batch([
            ("session/new", {"cwd": "."}),
            ("session/list", {}),
        ])
        results = await asyncio.gather(*futs)
        assert "error" not in results[0], results[0]
        assert results[0]["result"]["sessionId"]
        assert any(s["sessionId"] == results[0]["result"]["sessionId"]
                   for s in results[1]["result"]["sessions"])

    asyncio.run(_with_server(EchoAgent(), body))
