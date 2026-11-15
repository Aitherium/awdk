"""awsh over the kit's durable sessions (measured gaps, 2026-11-12).

* ``awsh_resumable(harness="awdk")`` answered "no transcript store is known":
  it now lists the kit's sessions from the store the loop writes.
* A resumed awdk session was a NEW conversation and could not be prompted: the
  resumed id is the id every turn carries, and the principal that resumed it
  (and nobody else) reaches it -- the spawner rule of #16396, not widened.
* ``awsh_interrupt`` refused a streaming turn as ``state=idle``: the agent's
  ``session_start`` no longer marks a running turn idle, and the interrupt is
  delivered to the agent (``POST /chat/steer`` cancel), which the kit implements.
* The awdk harness ignored the spawn ``cwd``: it travels with each turn and the
  kit binds it for the turn's tools.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from adk import turn_cwd
from adk import turn_journal as tj
from adk.harnesses import mcp_stdio as m
from adk.memory import Memory

TOKEN = "root-bearer-for-tests-only"


# ── awsh_resumable lists the kit's sessions ─────────────────────────────────────────


def test_awsh_resumable_lists_kit_sessions_and_flags_a_cut_off_turn(tmp_path, monkeypatch):
    Memory(db_path=tmp_path / "aither.db", agent_name="aither")
    conn = sqlite3.connect(tmp_path / "aither.db")
    conn.execute("INSERT INTO conversations (session_id, role, content, timestamp) "
                 "VALUES ('done-session', 'user', 'hello', ?)", (time.time() - 60,))
    conn.commit()
    conn.close()
    tj.TurnJournal(tmp_path / "aither.db").accept(
        "cut-session", agent="aither", mode="stream_react", message="run the job",
        kwargs={}, auth_bound=False)
    monkeypatch.setattr(tj, "memory_dir", lambda: tmp_path)
    monkeypatch.setattr(m, "_unsearched_resumable", lambda searched: [])

    out = m._resumable({"harness": "awdk"})

    assert "error" not in out, out
    assert out["searched"] == ["awdk"] and out["count"] == 2
    rows = {r["session_id"]: r for r in out["sessions"]}
    assert rows["cut-session"]["open_turn"] is True
    assert rows["cut-session"]["last_turn"]["message"] == "run the job"
    assert rows["done-session"]["open_turn"] is False and rows["done-session"]["messages"] == 1
    assert all(r["harness"] == "awdk" and r["agent"] == "aither" for r in rows.values())
    assert "awsh_resume" in out["next"]


def test_awsh_resumable_still_names_a_harness_it_cannot_see(monkeypatch):
    out = m._resumable({"harness": "no-such-harness"})
    assert "no transcript store is known" in out["error"]


# ── a daemon with real routes; the relay's HTTP stream is the only stub ─────────────


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.delenv("AITHER_HARNESS_MODEL_REQUIRED", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_ROOTS", raising=False)
    registry_path = tmp_path / "harness_tokens.json"
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry_path))

    import adk.harnesses.agents as agents_mod
    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(directory_mod, "_directory",
                        directory_mod.SessionDirectory(discover_fn=lambda: []))
    monkeypatch.setattr(daemon, "local_profile_refusal", lambda profile: "")

    turns: list[dict] = []
    gate = threading.Event()
    gate.set()                       # turns end at once unless a test holds them
    streaming = threading.Event()

    def fake_stream(self, httpx, body, turn):
        turns.append({"session": self.id, **body})
        # what the kit sends first on every turn
        for event in agents_mod.translate_genesis(
                "session_start", {"agent": self.agent, "model": "m",
                                  "session_id": body["session_id"]}):
            event.turn = turn
            self._observe(event)
            self._emit(event)
        streaming.set()
        gate.wait(timeout=10)

    monkeypatch.setattr(agents_mod.AgentRelaySession, "_stream_turn", fake_stream)

    mgr = SessionManager(root=tmp_path / "sessions")
    client = TestClient(daemon.create_app(manager=mgr, token=TOKEN))

    def agent(label: str) -> dict:
        token = daemon.mint_scoped_token(
            principal_id=f"agent:{label}", paths=m.AGENT_TOKEN_PATHS, plan="agent",
            path=registry_path)
        return {"Authorization": f"Bearer {token}"}

    def settle(session_id: str) -> None:
        for thread in mgr.get_session(session_id)._threads:
            thread.join(timeout=10)

    return {"client": client, "mgr": mgr, "turns": turns, "agent": agent, "gate": gate,
            "streaming": streaming, "settle": settle, "agents_mod": agents_mod,
            "owner": {"Authorization": f"Bearer {TOKEN}"}}


def test_a_resumed_awdk_session_continues_the_conversation_for_its_resumer_only(harness):
    tab_a, tab_b = harness["agent"]("tab-a"), harness["agent"]("tab-b")
    resp = harness["client"].post("/sessions", headers=tab_a, json={
        "harness": "awdk", "agent": "aither", "cwd": "",
        "resume_session_id": "kit-session-7"})
    assert resp.status_code == 200, resp.text
    info = resp.json()
    sid = info["id"]
    assert info["spawned_by"] == "agent:tab-a" and info["allow_peer_input"] is False

    landed = harness["client"].post(f"/sessions/{sid}/input",
                                    json={"text": "carry on with the job"}, headers=tab_a)
    assert landed.status_code == 200, landed.text
    harness["settle"](sid)
    (turn,) = harness["turns"]
    # the kit's conversation id, not this daemon session's new id
    assert turn["session_id"] == "kit-session-7"
    # on the wire as peer text naming the daemon's principal, never bare owner text
    assert "carry on with the job" in turn["message"] and "agent:tab-a" in turn["message"]
    assert turn["message"] != "carry on with the job"

    # the rule is not widened: another agent is refused, the owner's own resume too
    refused = harness["client"].post(f"/sessions/{sid}/input", json={"text": "mine now"},
                                     headers=tab_b)
    assert refused.status_code == 403 and "did not opt in to peer input" in refused.text
    owned = harness["client"].post("/sessions", headers=harness["owner"], json={
        "harness": "awdk", "agent": "aither", "cwd": "", "resume_session_id": "kit-8"})
    assert owned.status_code == 200
    closed = harness["client"].post(f"/sessions/{owned.json()['id']}/input",
                                    json={"text": "x"}, headers=tab_a)
    assert closed.status_code == 403
    assert len(harness["turns"]) == 1


def test_a_streaming_relay_turn_is_busy_and_interrupt_reaches_the_agent(harness, monkeypatch):
    import httpx

    owner = harness["owner"]
    sid = harness["client"].post("/sessions", headers=owner, json={
        "harness": "awdk", "agent": "aither", "cwd": ""}).json()["id"]

    # idle: nothing to interrupt, and the refusal says so
    idle = harness["client"].post(f"/sessions/{sid}/interrupt", headers=owner).json()
    assert idle["interrupted"] is False and "no turn is running" in idle["reason"]

    harness["gate"].clear()
    sent = harness["client"].post(f"/sessions/{sid}/input", json={"text": "long job"},
                                  headers=owner)
    assert sent.status_code == 200, sent.text
    assert harness["streaming"].wait(timeout=10)
    # session_start arrived and the turn is still streaming: busy, not idle
    assert harness["client"].get(f"/sessions/{sid}", headers=owner).json()["state"] == "busy"

    posted: list[dict] = []

    class Reply:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "cancelled": True}

    def fake_post(url, json=None, **kw):
        posted.append({"url": url, **(json or {})})
        return Reply()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = harness["client"].post(f"/sessions/{sid}/interrupt", headers=owner).json()
    assert out == {"interrupted": True}
    (call,) = posted
    assert call["url"].endswith("/chat/steer") and call["action"] == "cancel"
    assert call["session_id"] == sid

    harness["gate"].set()
    harness["settle"](sid)
    assert harness["client"].get(f"/sessions/{sid}", headers=owner).json()["state"] == "idle"


def test_the_awdk_relay_sends_the_spawn_cwd_with_each_turn(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    sid = harness["client"].post("/sessions", headers=harness["owner"], json={
        "harness": "awdk", "agent": "aither", "cwd": str(work)}).json()["id"]
    harness["client"].post(f"/sessions/{sid}/input", json={"text": "where am I"},
                           headers=harness["owner"])
    harness["settle"](sid)
    assert harness["turns"][-1]["cwd"] == str(work)


# ── the kit honours it: cwd bound for the turn, cancel implemented ──────────────────


def test_turn_cwd_resolves_tool_paths_only_while_bound(tmp_path):
    @turn_cwd.in_turn_cwd
    def where(path: str = ".", pattern: str = "*") -> str:
        """doc survives the wrapper"""
        return path

    assert where("rel/file.txt") == "rel/file.txt" and where() == "."   # nothing bound
    token = turn_cwd.TURN_CWD.set(str(tmp_path))
    try:
        assert where("rel/file.txt") == os.path.join(str(tmp_path), "rel", "file.txt")
        assert where() == str(tmp_path)
        absolute = str(tmp_path / "abs.txt")
        assert where(absolute) == absolute
        assert turn_cwd.current() == str(tmp_path)
    finally:
        turn_cwd.TURN_CWD.reset(token)
    assert where.__doc__ == "doc survives the wrapper"
    assert turn_cwd.usable(str(tmp_path / "missing")) == ""
    assert turn_cwd.usable(str(tmp_path)) == str(tmp_path)


def test_shell_exec_runs_in_the_turn_cwd(tmp_path):
    from adk.builtin_tools import shell_exec

    token = turn_cwd.TURN_CWD.set(str(tmp_path))
    try:
        out = shell_exec("pwd")
    finally:
        turn_cwd.TURN_CWD.reset(token)
    assert tmp_path.name in out, out


@pytest.fixture()
def kit(tmp_path, monkeypatch):
    """The kit server with a real agent loop and a scripted streaming model."""
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "off")
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("AITHER_TURN_JOURNAL", raising=False)
    (tmp_path / "home").mkdir()

    import adk.server as server
    from adk.agent import AitherAgent
    from adk.intent import IntentDecision
    from adk.llm.base import StreamChunk

    state = {"replies": [], "seen_cwd": [], "hang": None, "started": threading.Event()}

    async def _stream(messages, *a, **k):
        text = state["replies"].pop(0) if state["replies"] else "FINAL: done"
        yield StreamChunk(content=text, done=True, model="t")

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    llm.model = "t"

    class _Agent(AitherAgent):
        async def classify_intent(self, message, history=None):
            return IntentDecision(intent="chat", effort=3, agentic=True,
                                  reasoning_depth="skip")

    agent = _Agent(name="kit", llm=llm, builtin_tools=False,
                   memory=Memory(db_path=tmp_path / "kit.db", agent_name="kit"))
    agent._graph = None

    @agent.tool(name="where", description="report the working directory")
    async def _where() -> str:
        state["seen_cwd"].append(turn_cwd.current())
        return turn_cwd.current()

    @agent.tool(name="hang", description="blocks until cancelled")
    async def _hang() -> str:
        state["started"].set()
        await asyncio.sleep(60)
        return "never"

    app = server.create_app(agent=agent)
    return app, state, tmp_path, agent


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


def test_chat_stream_binds_the_callers_cwd_for_the_turns_tools(kit):
    app, state, tmp_path, _agent = kit
    work = tmp_path / "work"
    work.mkdir()
    state["replies"] = ["ACTION: where\nINPUT: {}", "FINAL: here"]
    with TestClient(app) as client:
        reply = client.post("/chat/stream", json={
            "message": "where are you", "session_id": "cwd-1", "cwd": str(work)})
        assert reply.status_code == 200, reply.text
        assert state["seen_cwd"] == [str(work)]

        # a cwd that does not exist is ignored, not trusted
        state["replies"] = ["ACTION: where\nINPUT: {}", "FINAL: here"]
        client.post("/chat/stream", json={
            "message": "and now", "session_id": "cwd-2", "cwd": str(tmp_path / "nope")})
        assert state["seen_cwd"][-1] == os.getcwd()


def test_steer_cancel_ends_the_stream_typed_and_the_journal_says_abandoned(kit):
    app, state, _tmp, agent = kit
    state["replies"] = ["ACTION: hang\nINPUT: {}"]
    with TestClient(app) as client:
        nothing = client.post("/chat/steer", json={"session_id": "cx", "action": "cancel"})
        assert nothing.status_code == 404

        box: dict = {}

        def run():
            box["reply"] = client.post("/chat/stream", json={
                "message": "hang please", "session_id": "cx"})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert state["started"].wait(timeout=30), "the tool never started"
        cancel = client.post("/chat/steer", json={"session_id": "cx", "action": "cancel"})
        assert cancel.status_code == 200 and cancel.json()["cancelled"] is True
        thread.join(timeout=30)
        assert not thread.is_alive(), "the stream did not end after the cancel"

        frames = _frames(box["reply"].text)
        errors = [d for name, d in frames if name == "error"]
        assert errors and errors[-1].get("interrupted") is True
        assert frames[-1][0] == "complete" and frames[-1][1].get("interrupted") is True

        status = client.get("/sessions/cx/turns").json()
        (turn,) = status["turns"]
        assert turn["outcome"] == tj.ABANDONED and "cancelled" in turn["reason"]
        assert status["open"] == []
        health = client.get("/health").json()["turns"]
        assert health["enabled"] is True and health["open"] == 0
        assert health["last"]["outcome"] == tj.ABANDONED


def test_a_relay_turn_cut_by_a_kit_kill_collects_the_resumed_answer(harness, monkeypatch):
    """The stream broke mid-turn; the kit came back and resumed the turn from its
    journal. The awsh session emits that answer as the turn's answer."""
    from adk.harnesses.events import EventKind

    sid = harness["client"].post("/sessions", headers=harness["owner"], json={
        "harness": "awdk", "agent": "aither", "cwd": ""}).json()["id"]
    session = harness["mgr"].get_session(sid)
    replies = [
        {"turns": [{"turn_id": "t1", "message": "do the job", "outcome": "open"}]},
        {"turns": [{"turn_id": "t1", "message": "do the job", "outcome": "resumed",
                    "attempts": 1, "result": "the job is done"}]},
        {"turns": []},      # the ack
    ]
    asked: list[dict] = []

    class FakeHttpx:
        class TransportError(Exception):
            pass

        @staticmethod
        def Timeout(*a, **k):
            return None

        @staticmethod
        def get(url, params=None, **kw):
            asked.append({"url": url, **(params or {})})

            class Reply:
                status_code = 200

                @staticmethod
                def json():
                    return replies.pop(0)

            return Reply()

    broke = FakeHttpx.TransportError("connection reset")
    # a stream that never opened is a refused connection, not a killed turn
    session._stream_opened = False
    assert session._collect_resumed_turn(FakeHttpx, "do the job", 1, broke) is False
    assert asked == []

    session._stream_opened = True
    assert session._collect_resumed_turn(FakeHttpx, "do the job", 1, broke) is True
    assert asked[0]["url"].endswith(f"/sessions/{sid}/turns") and asked[0]["agent"] == "aither"
    assert asked[-1]["ack"] == "t1"
    events = harness["mgr"].read_events(sid, after=0)["events"]
    done = [e for e in events if e["kind"] == EventKind.TURN_COMPLETED.value][-1]
    assert done["data"]["resumed_after_kill"] is True and done["data"]["is_error"] is False
    assert any(e["kind"] == EventKind.TEXT_DELTA.value and e["text"] == "the job is done"
               for e in events)

    # a kit that never accepted the turn: nothing to wait for, the error stands
    replies[:] = [{"turns": []}]
    assert session._collect_resumed_turn(FakeHttpx, "another", 2, broke) is False
