"""The daemon's awsh turn path is wired to the planes it claims to have.

Measured 2026-10-12 on the :9001 daemon, each arm below was false:

- the crystal bound only when an out-of-repo launcher exported ADK_CRYSTAL_SCOPE;
- awsh's default persona ``aither`` built a second, tool-poor agent;
- ``deep_reasoning`` was never registered, so the supervisor had nothing to force;
- the request's ``reasoning: true`` was read and dropped;
- POST /chat/steer answered ok and the message never reached the loop;
- supervisor verdicts never reached the SSE stream;
- /health could not say whether a memory plane was bound.

These run the real ``create_app`` with a scripted LLM, so the frames and the prompt
messages asserted are the ones a client and the model would see.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from adk import server
from adk.agent import AitherAgent, _max_tool_loops
from adk.intent import IntentDecision
from adk.llm.base import StreamChunk

IDENTITY = "adk-daemon"


class _Script:
    """Replies for every chat_stream call, and the prompt each call was given."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.replies: list = []
        self.stream_kwargs: list[dict] = []

    async def next_reply(self, messages) -> str:
        self.calls.append([str(m.content) for m in messages])
        if not self.replies:
            return "FINAL: done"
        item = self.replies.pop(0)
        if callable(item):
            item = await item()
        return item


@pytest.fixture()
def daemon(monkeypatch, tmp_path):
    """create_app on a fake LLM; returns (app, script, built-agents list)."""
    # Set-then-delete so teardown restores "unset", whatever create_app does.
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "placeholder")
    monkeypatch.delenv("ADK_CRYSTAL_SCOPE")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "awm.db"))
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    monkeypatch.delenv("ADK_CRYSTAL_GRAPH_ROOT", raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "off")
    monkeypatch.delenv("ADK_MAX_TOOL_LOOPS", raising=False)

    script = _Script()
    built: list = []
    tool_state = {"flaky": 0}

    def _fake_llm():
        async def _stream(messages, *a, **k):
            text = await script.next_reply(messages)
            yield StreamChunk(content=text, done=True, model="t")

        llm = AsyncMock()
        llm.chat_stream = _stream
        llm.provider_name = "test"
        llm.model = "t"
        llm.chat = AsyncMock(return_value=MagicMock(
            content="Root cause: the flaky tool always fails; stop calling it.",
            tool_calls=[], finish_reason="stop"))
        return llm

    class _Agent(AitherAgent):
        def __init__(self, **kw):
            kw.setdefault("llm", _fake_llm())
            kw.setdefault("builtin_tools", False)
            super().__init__(**kw)
            built.append(self)

            @self.tool
            def flaky(x: str = "") -> str:
                """always fails"""
                tool_state["flaky"] += 1
                return json.dumps({"error": "boom"})

            @self.tool
            async def wait_for_steer(x: str = "") -> str:
                """blocks until the test has posted a steer"""
                script.tool_running.set()
                await asyncio.wait_for(script.steer_posted.wait(), timeout=5.0)
                return "waited"

        async def classify_intent(self, message, history=None):
            return IntentDecision(intent="chat", effort=3, agentic=True,
                                  reasoning_depth="skip")

        async def stream_react(self, message, on_event, **kw):
            script.stream_kwargs.append({"agent": self.name, **kw})
            return await super().stream_react(message, on_event, **kw)

    monkeypatch.setattr(server, "AitherAgent", _Agent)
    app = server.create_app(identity=IDENTITY)
    return app, script, built


def _frames(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, data = block.partition("\ndata: ")
        try:
            out.append((head[len("event: "):], json.loads(data)))
        except ValueError:
            continue
    return out


def _stream(client: TestClient, **body) -> list[tuple[str, dict]]:
    body.setdefault("session_id", "s-wiring")
    r = client.post("/chat/stream", json=body)
    assert r.status_code == 200, r.text
    return _frames(r.text)


def test_crystal_binds_with_scope_unset_and_health_reports_it(daemon):
    app, _script, built = daemon
    assert os.environ.get("ADK_CRYSTAL_SCOPE") is None
    client = TestClient(app)
    health = client.get("/health").json()
    primary = [a for a in built if a.name == IDENTITY][0]
    assert primary.crystal is not None, "the daemon agent binds a crystal by default"
    assert primary.crystal.scope == f"aitherium:{IDENTITY}:desktop"
    # Bound for the daemon's agent only; the process env is left as it was.
    assert os.environ.get("ADK_CRYSTAL_SCOPE") is None
    assert "crystal" in health and health["crystal"]["bound"] is True
    assert health["crystal"]["scope"] == f"aitherium:{IDENTITY}:desktop"
    assert "telemetry" in health["crystal"] and "last_recall" in health["crystal"]


def test_deep_reasoning_is_registered_on_the_daemon_agent(daemon):
    app, _script, built = daemon
    TestClient(app).get("/health")
    primary = [a for a in built if a.name == IDENTITY][0]
    assert primary._tools.get("deep_reasoning") is not None


def test_local_default_persona_gets_the_primary_agent(daemon):
    app, script, _built = daemon
    client = TestClient(app)
    _stream(client, message="hello there", persona="aither", is_local=True)
    assert script.stream_kwargs[-1]["agent"] == IDENTITY
    # A caller that did not say it is the local client still gets what it named.
    _stream(client, message="hello there", persona="aither")
    assert script.stream_kwargs[-1]["agent"] == "aither"


def test_reasoning_flag_reaches_stream_react_as_effort_8(daemon):
    app, script, _built = daemon
    frames = _stream(TestClient(app), message="why does it fail", reasoning=True)
    kw = script.stream_kwargs[-1]
    assert kw["effort"] >= 8, "reasoning:true lifts the classifier's effort 3"
    assert isinstance(kw["decision"], IntentDecision) and kw["decision"].intent == "chat"
    assert kw["max_steps"] == _max_tool_loops(kw["effort"])
    assert callable(kw["steering"])
    effort = [d for e, d in frames if e == "pipeline" and d.get("stage") == "effort"]
    assert effort and effort[0]["message"].startswith(f"effort {kw['effort']}:")
    # Without the flag the classifier's own verdict stands.
    _stream(TestClient(app), message="why does it fail", session_id="s-plain")
    assert script.stream_kwargs[-1]["effort"] == 3


def test_a_chat_steer_lands_in_the_next_steps_messages(daemon):
    app, script, _built = daemon
    script.replies = ['ACTION: wait_for_steer\nINPUT: {"x": "1"}', "FINAL: blue it is"]

    async def _run():
        script.tool_running = asyncio.Event()
        script.steer_posted = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            turn = asyncio.ensure_future(c.post("/chat/stream", json={
                "message": "which file", "session_id": "s-steer"}))
            await asyncio.wait_for(script.tool_running.wait(), timeout=10.0)
            r = await c.post("/chat/steer", json={"session_id": "s-steer",
                                                  "message": "use the blue file"})
            assert r.status_code == 200 and r.json()["queued"] is True
            script.steer_posted.set()
            return await asyncio.wait_for(turn, timeout=20.0)

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert len(script.calls) >= 2
    assert not any("use the blue file" in m for m in script.calls[0])
    assert any("use the blue file" in m for m in script.calls[1]), \
        "the steer reached the step after the one it was posted during"
    assert any(e == "steering" for e, _d in _frames(resp.text))


def test_a_supervisor_escalation_shows_up_in_the_sse_stream(daemon):
    app, script, _built = daemon

    async def _wait_for_hint():
        # The supervisor judges from the event stream in parallel; hold the next
        # model call until its escalation hint is queued, so the run is not a race.
        from adk import steering
        for _ in range(60):
            if steering._steering_hints.get("s-sup"):
                break
            await asyncio.sleep(0.05)
        return 'ACTION: flaky\nINPUT: {"x": "4"}'

    script.replies = [
        'ACTION: flaky\nINPUT: {"x": "1"}',
        'ACTION: flaky\nINPUT: {"x": "2"}',
        'ACTION: flaky\nINPUT: {"x": "3"}',
        _wait_for_hint,
        "FINAL: stopped calling the flaky tool",
    ]
    frames = _stream(TestClient(app), message="fix it", session_id="s-sup")
    sup = [d for e, d in frames if e == "supervision"]
    assert sup and sup[0]["action"] == "escalate", [e for e, _ in frames]
    failed = [d for e, d in frames if e == "tool_result"]
    assert failed and failed[0]["results"][0]["success"] is False
    # The forced deep-reasoning conclusion was steered into a later step's prompt.
    assert any("Root cause: the flaky tool" in m for call in script.calls[4:] for m in call)


# ── review fixes (2026-10-12) ────────────────────────────────────────────────


def test_suite_default_never_binds_the_real_awm_db(monkeypatch):
    """Under the suite's isolated env a daemon agent must not open ~/.aither/awm.

    The daemon now binds a crystal when ADK_CRYSTAL_SCOPE is unset, and conftest
    deletes ADK_CRYSTAL_DB, so without an explicit opt-out every create_app test
    opened (and could write) the developer's real memory.db.
    """
    from adk import crystal as crystal_mod

    opened: list = []

    class _Recorder:
        def __init__(self, scope, db=None, complete=None):
            opened.append(db)
            raise RuntimeError("recorded, not opened")

    monkeypatch.setattr(crystal_mod, "AwmFactStore", _Recorder)
    server._daemon_default_crystal("probe", server._DEFAULT_CRYSTAL_SCOPE)
    assert None not in opened, "a test bound the crystal to the real home memory.db"


def test_code_graph_warm_does_not_block_the_event_loop():
    """Hydrating awgraph is synchronous work (measured 35-55 s); the boot warm must
    run it off the loop, or /health and every turn stall behind it."""
    import time
    from types import SimpleNamespace

    class _Graph:
        def __init__(self):
            self.opened = False

        async def _open(self):
            time.sleep(0.4)  # blocking-ok: stands in for awgraph's sync _load_chunk_cache
            self.opened = True
            return True

        async def query(self, text, k):
            await self._open()
            return []

    graph = _Graph()
    crystal = SimpleNamespace(graph=graph)

    async def _recall_graph(message):
        return await graph.query(message, 6)

    crystal.recall_graph = _recall_graph
    agent = SimpleNamespace(name="probe", crystal=crystal)

    async def _run():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.ensure_future(_ticker())
        ok = await server._warm_crystal(agent)
        t.cancel()
        return ok, ticks

    ok, ticks = asyncio.run(_run())
    assert ok is True and graph.opened is True
    assert ticks >= 10, f"the event loop was blocked during the warm ({ticks} ticks)"


def test_steering_items_are_capped_before_they_reach_the_prompt(daemon):
    """A deep-reasoning conclusion or a pasted steer is appended to msgs verbatim and
    stays for every later step; one item must not be able to flood the context."""
    app, script, _built = daemon
    huge = "x" * 50_000

    async def _hold_then_answer():
        return "FINAL: ok"

    script.replies = ['ACTION: wait_for_steer\nINPUT: {"x": "1"}', _hold_then_answer]

    async def _run():
        script.tool_running = asyncio.Event()
        script.steer_posted = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            turn = asyncio.ensure_future(c.post("/chat/stream", json={
                "message": "which file", "session_id": "s-cap"}))
            await asyncio.wait_for(script.tool_running.wait(), timeout=10.0)
            await c.post("/chat/steer", json={"session_id": "s-cap", "message": huge})
            script.steer_posted.set()
            return await asyncio.wait_for(turn, timeout=20.0)

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    # The loop folds a steer into the previous user message (strict chat templates
    # refuse two user turns in a row), so measure the steer segment, not the message.
    steered = [m[m.index("[Steering]"):] for m in script.calls[1] if "[Steering]" in m]
    assert steered, "the steer reached the next step"
    assert max(len(m) for m in steered) <= server._STEER_MAX_CHARS + 100


def test_a_single_space_scope_opts_out_of_the_daemon_crystal(monkeypatch):
    """PowerShell cannot hold an empty env var, so the launcher's documented opt-out
    is a single space; it must leave the agent with no crystal at all."""
    from adk.crystal import crystal_from_env

    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", " ")
    assert server._daemon_default_crystal("probe", server._DEFAULT_CRYSTAL_SCOPE) is None
    assert crystal_from_env("probe") is None
