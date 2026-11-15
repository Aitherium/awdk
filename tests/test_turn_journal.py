"""The turn journal and the resume decision, driven through the REAL agent loop.

A process kill cannot be staged inside one pytest process, so a "kill" here is a
``BaseException`` raised from inside the tool that is in flight: neither the loop
nor the journal catches it, so the journal is left exactly as a killed process
leaves it (turn accepted, tool started, no finish, no terminal record). The boot
id is then changed, which is what a restart does. The test that kills a real
server process is ``test_turn_resume_kill.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from adk import turn_journal as tj
from adk.agent import AitherAgent
from adk.llm.base import LLMResponse, StreamChunk, ToolCall
from adk.memory import Memory


class _Killed(BaseException):
    """Stands in for SIGKILL: nothing between the tool and the test catches it."""


class ScriptedLLM:
    provider_name = "scripted"
    model = "claude-opus-5"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[tuple[str, str]]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append([(getattr(m, "role", "?"), str(getattr(m, "content", "") or ""))
                           for m in messages])
        if self.replies:
            return self.replies.pop(0)
        return LLMResponse(content="final answer", model="scripted", finish_reason="stop")

    def sent(self) -> str:
        return "\n".join(c for call in self.calls for _, c in call)


def _call(name, call_id="c1", **args):
    return LLMResponse(content="", model="scripted", finish_reason="tool_calls",
                       tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


def _text(content):
    return LLMResponse(content=content, model="scripted", finish_reason="stop")


class Box:
    """One agent database plus the tools' side effects, across 'restarts'."""

    def __init__(self, tmp_path):
        self.db = tmp_path / "durable.db"
        self.ran: list[str] = []
        self.kill_in: set[str] = set()

    def agent(self, llm) -> AitherAgent:
        memory = Memory(db_path=self.db, agent_name="durable")
        agent = AitherAgent("durable", llm=llm, memory=memory, builtin_tools=False)
        agent._graph = None
        agent._safety = None

        @agent.tool(name="step_one", description="first step, has a side effect")
        async def _step_one(n: int = 1) -> str:
            self.ran.append("step_one")
            return json.dumps({"ok": True, "receipt": f"one-{n}"})

        @agent.tool(name="slow_job", description="a side effect that must not repeat")
        async def _slow_job(amount: int = 5) -> str:
            self.ran.append("slow_job")
            if "slow_job" in self.kill_in:
                raise _Killed()
            return json.dumps({"ok": True, "charged": amount})

        @agent.tool(name="fetch_rate", description="a pure read", repeat_safe=True)
        async def _fetch(currency: str = "EUR") -> str:
            self.ran.append("fetch_rate")
            if "fetch_rate" in self.kill_in:
                raise _Killed()
            return json.dumps({"rate": 1.08, "currency": currency})

        return agent

    def journal(self) -> tj.TurnJournal:
        return tj.journal_at(str(self.db))

    def history(self, sid) -> list[tuple[str, str]]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY id",
                (sid,)).fetchall()
        finally:
            conn.close()


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_TURN_JOURNAL", raising=False)
    monkeypatch.setattr(tj, "_trim_conversation_store", _no_store)
    for key in list(tj.STATS):
        tj.STATS[key] = None if key == "last" else 0
    tj._RESUMING.clear()
    return Box(tmp_path)


async def _no_store(session_id, message):   # the JSON mirror lives in the real home
    return None


def _restart(monkeypatch, boot="restarted-1"):
    """What a new process is: another boot id (the dead one's pid is this pid)."""
    monkeypatch.setattr(tj, "BOOT_ID", boot)


# ── the journal itself ───────────────────────────────────────────────────────


def test_journal_records_every_step_and_the_terminal_outcome(tmp_path):
    journal = tj.TurnJournal(tmp_path / "j.db")
    turn_id = journal.accept("s1", agent="a", mode="chat", message="do it",
                             kwargs={"effort": 3}, auth_bound=False)
    journal.append("s1", turn_id, "tool_started",
                   {"key": "k1", "name": "t", "arguments": {"x": 1}, "attempt": 0})
    assert [t.turn_id for t in journal.open_turns()] == [turn_id]
    journal.append("s1", turn_id, "tool_finished", {"key": "k1", "name": "t", "result": "r"})
    journal.finish(turn_id, "s1", tj.FINISHED, result="answer", model="m")

    turn = journal.turn(turn_id)
    assert turn.outcome == tj.FINISHED and turn.result == "answer"
    assert turn.message == "do it" and turn.kwargs == {"effort": 3}
    assert turn.steps["k1"].status == "finished" and turn.steps["k1"].result == "r"
    assert journal.open_turns() == []
    kinds = [e["kind"] for e in journal.events("s1")]
    assert kinds == ["turn_accepted", "tool_started", "tool_finished", "turn_finished"]


def test_an_accepted_turn_is_on_disk_before_accept_returns(tmp_path):
    """The write is committed with synchronous=FULL on its own connection: a
    second connection (another process, after a kill) reads it at once."""
    journal = tj.TurnJournal(tmp_path / "j.db")
    turn_id = journal.accept("s1", agent="a", mode="chat", message="m", kwargs={},
                             auth_bound=False)
    other = sqlite3.connect(tmp_path / "j.db")
    try:
        assert other.execute("SELECT kind FROM turn_journal WHERE turn_id = ?",
                             (turn_id,)).fetchall() == [("turn_accepted",)]
        conn = journal._connect()
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL
        conn.close()
    finally:
        other.close()


def test_prune_drops_old_finished_turns_and_never_an_open_one(tmp_path):
    journal = tj.TurnJournal(tmp_path / "j.db")
    old_done = journal.accept("s", agent="a", mode="chat", message="1", kwargs={},
                              auth_bound=False)
    journal.finish(old_done, "s", tj.FINISHED, result="r")
    old_open = journal.accept("s", agent="a", mode="chat", message="2", kwargs={},
                              auth_bound=False)
    conn = sqlite3.connect(tmp_path / "j.db")
    conn.execute("UPDATE turn_journal SET ts = ts - 10000")
    conn.commit()
    conn.close()
    fresh = journal.accept("s", agent="a", mode="chat", message="3", kwargs={},
                           auth_bound=False)
    journal.finish(fresh, "s", tj.FINISHED, result="r")

    assert journal.prune(keep_s=5000) == 2          # accepted + finished of old_done
    assert journal.turn(old_done) is None
    assert journal.turn(old_open).is_open and journal.turn(fresh).outcome == tj.FINISHED


def test_step_key_is_stable_per_occurrence():
    a0 = tj.step_key("t", {"b": 1, "a": 2}, 0)
    assert a0 == tj.step_key("t", {"a": 2, "b": 1}, 0)        # argument order
    assert a0 != tj.step_key("t", {"a": 2, "b": 1}, 1)        # the second such call
    assert a0 != tj.step_key("u", {"a": 2, "b": 1}, 0)


# ── the resume decision table ────────────────────────────────────────────────


@pytest.mark.parametrize("status,repeat_safe,expected", [
    (None, False, tj.RUN),
    (None, True, tj.RUN),
    ("finished", False, tj.REPLAY),
    ("finished", True, tj.REPLAY),
    ("error", False, tj.REPLAY),
    ("started", True, tj.RERUN),
    ("started", False, tj.UNKNOWN),
])
def test_resume_decision_table(status, repeat_safe, expected):
    record = None if status is None else tj.StepRecord(key="k", name="t", status=status)
    assert tj.decide(record, repeat_safe) == expected


def test_repeat_safe_is_declared_never_guessed():
    from adk.tools import ToolRegistry

    reg = ToolRegistry()

    async def get_thing() -> str:   # a read by NAME only
        return "x"

    async def lookup() -> str:
        return "x"

    reg.register(get_thing)
    reg.register(lookup, name="declared", repeat_safe=True)
    reg.register(lookup, name="by_class", action_class="read")
    assert reg.repeat_safe("get_thing") is False
    assert reg.repeat_safe("declared") is True
    assert reg.repeat_safe("by_class") is True
    assert reg.repeat_safe("no_such_tool") is False


# ── through the real loop ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_normal_turn_is_journaled_and_finished(box):
    llm = ScriptedLLM([_call("step_one", n=1), _text("all done")])
    resp = await box.agent(llm).chat("run step one", session_id="s1")
    assert resp.content == "all done"
    [turn] = box.journal().session_turns("s1")
    assert turn.outcome == tj.FINISHED and turn.attempts == 0 and turn.result == "all done"
    [step] = turn.steps.values()
    assert step.name == "step_one" and step.status == "finished"
    assert box.journal().open_turns() == []
    assert tj.STATS["accepted"] == 1 and tj.STATS[tj.FINISHED] == 1


@pytest.mark.asyncio
async def test_kill_mid_tool_then_resume_does_not_rerun_finished_or_unknown(box, monkeypatch):
    # attempt 0: step_one finishes, slow_job is in flight when the process dies
    box.kill_in.add("slow_job")
    llm = ScriptedLLM([_call("step_one", n=1), _call("slow_job", "c2", amount=5)])
    with pytest.raises(_Killed):
        await box.agent(llm).chat("pay the invoice", session_id="s1")
    assert box.ran == ["step_one", "slow_job"]
    [turn] = box.journal().open_turns()
    assert turn.is_open and [s.status for s in turn.steps.values()] == ["finished", "started"]
    assert box.history("s1") == [("user", "pay the invoice")]

    # restart; the model makes the same two calls again
    _restart(monkeypatch)
    box.kill_in.clear()
    llm2 = ScriptedLLM([_call("step_one", n=1), _call("slow_job", "c2", amount=5),
                        _text("paid, I think: outcome of the charge is unknown")])
    report = await tj.resume_open_turns(_getter(box.agent(llm2)), wait=True)

    assert [r["turn_id"] for r in report["resumed"]] == [turn.turn_id]
    assert box.ran == ["step_one", "slow_job"], "neither tool may run again"
    sent = llm2.sent()
    assert "TURN RESUMED AFTER A RESTART" in sent
    assert "outcome_unknown" in sent and "one-1" in sent     # unknown told; receipt replayed
    done = box.journal().turn(turn.turn_id)
    assert done.outcome == tj.RESUMED and done.attempts == 1
    assert done.result.startswith("paid, I think")
    assert [n["kind"] for n in done.notes] == ["tool_replayed", "tool_unknown"]
    # the client never reconnected: the answer is in history, the user row once
    assert box.history("s1") == [
        ("user", "pay the invoice"),
        ("assistant", "paid, I think: outcome of the charge is unknown")]
    assert box.journal().open_turns() == []


@pytest.mark.asyncio
async def test_the_model_may_decide_to_run_an_unknown_step_again(box, monkeypatch):
    box.kill_in.add("slow_job")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("slow_job", amount=5)])).chat(
            "charge it", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()
    llm2 = ScriptedLLM([_call("slow_job", "c1", amount=5),      # told: unknown
                        _call("slow_job", "c2", amount=5),      # its decision: run it
                        _text("charged")])
    await tj.resume_open_turns(_getter(box.agent(llm2)), wait=True)
    assert box.ran == ["slow_job", "slow_job"]
    [turn] = box.journal().session_turns("s1")
    assert turn.outcome == tj.RESUMED
    assert sorted(s.status for s in turn.steps.values()) == ["finished", "started"]


@pytest.mark.asyncio
async def test_a_repeat_safe_step_in_flight_is_run_again(box, monkeypatch):
    box.kill_in.add("fetch_rate")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("fetch_rate", currency="EUR")])).chat(
            "what is the rate", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()
    llm2 = ScriptedLLM([_call("fetch_rate", currency="EUR"), _text("1.08")])
    await tj.resume_open_turns(_getter(box.agent(llm2)), wait=True)
    assert box.ran == ["fetch_rate", "fetch_rate"]
    assert "outcome_unknown" not in llm2.sent()
    assert tj.STATS["rerun_steps"] == 1


@pytest.mark.asyncio
async def test_the_reconnecting_client_carries_the_resume(box, monkeypatch):
    """No startup resume ran: the same request for the same session IS the resume."""
    box.kill_in.add("slow_job")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("step_one", n=1), _call("slow_job", "c2")])
                        ).chat("pay", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()
    llm2 = ScriptedLLM([_call("step_one", n=1), _text("resumed answer")])
    resp = await box.agent(llm2).chat("pay", session_id="s1")
    assert resp.content == "resumed answer"
    assert box.ran == ["step_one", "slow_job"]
    [turn] = box.journal().session_turns("s1")
    assert turn.outcome == tj.RESUMED and turn.delivered is True


@pytest.mark.asyncio
async def test_a_late_reconnect_gets_the_answer_once_without_a_new_turn(box, monkeypatch):
    box.kill_in.add("slow_job")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("slow_job")])).chat("pay", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()
    agent = box.agent(ScriptedLLM([_text("the resumed answer")]))
    await tj.resume_open_turns(_getter(agent), wait=True)
    [turn] = box.journal().session_turns("s1")
    assert turn.outcome == tj.RESUMED and turn.delivered is False

    quiet = ScriptedLLM([])
    late = await box.agent(quiet).chat("pay", session_id="s1")
    assert late.content == "the resumed answer"
    assert quiet.calls == [], "the recorded answer is handed over; no model call"
    assert box.journal().turn(turn.turn_id).delivered is True

    again = ScriptedLLM([_text("a new turn")])
    assert (await box.agent(again).chat("pay", session_id="s1")).content == "a new turn"
    assert len(box.journal().session_turns("s1")) == 2


@pytest.mark.asyncio
async def test_a_reconnect_during_the_resume_joins_it(box, monkeypatch):
    box.kill_in.add("slow_job")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("slow_job")])).chat("pay", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()

    gate = asyncio.Event()

    class SlowLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            await gate.wait()
            return await super().chat(messages, **kwargs)

    agent = box.agent(SlowLLM([_text("one answer")]))
    report = await tj.resume_open_turns(_getter(agent))
    assert report["tasks"] == 1
    await asyncio.sleep(0.05)
    joiner = asyncio.create_task(agent.chat("pay", session_id="s1"))
    await asyncio.sleep(0.05)
    assert not joiner.done()
    gate.set()
    assert (await joiner).content == "one answer"
    assert len(agent.llm.calls) == 1, "one turn ran, two callers got it"
    assert len(box.journal().session_turns("s1")) == 1


@pytest.mark.asyncio
async def test_resume_attempts_are_bounded_then_the_turn_is_abandoned(box, monkeypatch):
    box.kill_in.add("slow_job")
    for n, boot in enumerate(("b1", "b2", "b3")):
        # a different call each time, so every attempt dies in a fresh step
        with pytest.raises(_Killed):
            await box.agent(ScriptedLLM([_call("slow_job", amount=n)])
                            ).chat("pay", session_id="s1")
        _restart(monkeypatch, boot)
    report = await tj.resume_open_turns(_getter(box.agent(ScriptedLLM([]))), wait=True)
    assert report["resumed"] == [] and len(report["abandoned"]) == 1
    [turn] = box.journal().session_turns("s1")
    assert turn.outcome == tj.ABANDONED and "attempts exhausted" in turn.reason
    assert turn.attempts == 2


@pytest.mark.asyncio
async def test_stale_and_auth_bound_turns_are_not_resumed_at_startup(box, monkeypatch):
    journal = box.journal()
    box.agent(ScriptedLLM([]))                       # creates the conversations table
    old = journal.accept("old", agent="durable", mode="chat", message="m", kwargs={},
                         auth_bound=False)
    bound = journal.accept("bound", agent="durable", mode="chat", message="m", kwargs={},
                           auth_bound=True)
    conn = sqlite3.connect(box.db)
    conn.execute("UPDATE turn_journal SET ts = ts - 200000 WHERE turn_id = ?", (old,))
    conn.commit()
    conn.close()
    _restart(monkeypatch)
    llm = ScriptedLLM([])
    report = await tj.resume_open_turns(_getter(box.agent(llm)), wait=True)
    assert [r["turn_id"] for r in report["abandoned"]] == [old]
    assert [r["turn_id"] for r in report["waiting"]] == [bound]
    assert llm.calls == []
    assert journal.turn(old).outcome == tj.ABANDONED
    assert journal.turn(bound).is_open           # waits for its caller


@pytest.mark.asyncio
async def test_a_live_process_turn_is_never_claimed(box):
    journal = box.journal()
    turn_id = journal.accept("s1", agent="durable", mode="chat", message="m", kwargs={},
                             auth_bound=False)
    assert journal.turn(turn_id).orphaned() is False     # this boot owns it
    assert journal.claim(turn_id) is None


@pytest.mark.asyncio
async def test_failed_and_cancelled_turns_get_a_terminal_record(box):
    class Boom(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("backend exploded")

    with pytest.raises(RuntimeError):
        await box.agent(Boom([])).chat("x", session_id="f")
    [failed] = box.journal().session_turns("f")
    assert failed.outcome == tj.FAILED and "backend exploded" in failed.reason

    class Hang(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            await asyncio.sleep(30)

    hanging = asyncio.Event()

    class Hang(ScriptedLLM):        # noqa: F811 -- signals once it is really waiting
        async def chat(self, messages, **kwargs):
            hanging.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(box.agent(Hang([])).chat("x", session_id="c"))
    await asyncio.wait_for(hanging.wait(), timeout=20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [cancelled] = box.journal().session_turns("c")
    assert cancelled.outcome == tj.ABANDONED and "cancelled" in cancelled.reason
    assert box.journal().open_turns() == []


@pytest.mark.asyncio
async def test_outcome_is_emitted_as_an_event_and_shown_in_status_and_health(box):
    agent = box.agent(ScriptedLLM([_call("step_one"), _text("ok")]))
    seen: list[dict] = []

    class Events:
        async def emit(self, kind, **data):
            seen.append({"kind": kind, **data})
            return 1

    agent._events = Events()
    await agent.chat("go", session_id="s1")
    outcomes = [e for e in seen if e["kind"] == "turn_outcome"]
    assert len(outcomes) == 1 and outcomes[0]["outcome"] == tj.FINISHED

    status = tj.session_status(agent, "s1")
    assert status["open"] == [] and status["turns"][0]["outcome"] == tj.FINISHED
    assert [e["kind"] for e in status["events"]] == [
        "turn_accepted", "tool_started", "tool_finished", "turn_finished"]
    health = tj.health(agent)
    assert health["enabled"] is True and health["open"] == 0
    assert health["last"]["outcome"] == tj.FINISHED


@pytest.mark.asyncio
async def test_switch_off_writes_nothing(box, monkeypatch):
    monkeypatch.setenv("AITHER_TURN_JOURNAL", "0")
    resp = await box.agent(ScriptedLLM([_call("step_one"), _text("ok")])).chat(
        "go", session_id="s1")
    assert resp.content == "ok"
    conn = sqlite3.connect(box.db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    finally:
        conn.close()
    assert "turn_journal" not in tables


@pytest.mark.asyncio
async def test_a_broken_journal_never_fails_the_turn(box, monkeypatch):
    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(tj.TurnJournal, "append", boom)
    resp = await box.agent(ScriptedLLM([_call("step_one"), _text("still answers")])).chat(
        "go", session_id="s1")
    assert resp.content == "still answers" and box.ran == ["step_one"]
    assert tj.STATS["journal_errors"] >= 1


@pytest.mark.asyncio
async def test_stream_react_turns_are_journaled_and_resumed(box, monkeypatch):
    """The streaming loop (what awsh's awdk harness drives) stores the user
    message only at the END of a turn, so before the journal a kill lost the
    request itself."""
    def streaming(replies):
        llm = ScriptedLLM([])
        queue = list(replies)

        async def _stream(messages, *a, **k):
            llm.calls.append([(getattr(m, "role", "?"), str(getattr(m, "content", "") or ""))
                              for m in messages])
            yield StreamChunk(content=queue.pop(0) if queue else "FINAL: done",
                              done=True, model="t")

        llm.chat_stream = _stream
        return llm

    box.kill_in.add("slow_job")
    first = streaming(['ACTION: step_one\nINPUT: {"n": 1}',
                       'ACTION: slow_job\nINPUT: {"amount": 5}'])
    with pytest.raises(_Killed):
        await box.agent(first).stream_react("pay", lambda e: None, session_id="s1")
    assert box.ran == ["step_one", "slow_job"]
    [turn] = box.journal().open_turns()
    assert turn.mode == "stream_react" and box.history("s1") == []

    _restart(monkeypatch)
    box.kill_in.clear()
    second = streaming(['ACTION: step_one\nINPUT: {"n": 1}',
                        'ACTION: slow_job\nINPUT: {"amount": 5}',
                        "FINAL: resumed and reported"])
    await tj.resume_open_turns(_getter(box.agent(second)), wait=True)
    assert box.ran == ["step_one", "slow_job"]
    done = box.journal().turn(turn.turn_id)
    assert done.outcome == tj.RESUMED and done.result == "resumed and reported"
    assert "outcome_unknown" in second.sent()
    assert box.history("s1")[:2] == [("user", "pay"), ("assistant", "resumed and reported")]


def test_list_resumable_reads_the_kit_store_read_only(tmp_path):
    db = tmp_path / "aither.db"
    Memory(db_path=db, agent_name="aither")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO conversations (session_id, role, content, timestamp) "
                 "VALUES ('sess-a', 'user', 'hello', 100.0)")
    conn.commit()
    conn.close()
    rows = tj.list_resumable(root=tmp_path)
    assert [(r["session_id"], r["agent"], r["messages"], r["open_turn"]) for r in rows] == [
        ("sess-a", "aither", 1, False)]
    conn = sqlite3.connect(db)
    try:   # the listing created nothing in a store it does not own
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'turn_journal'"
                            ).fetchone() is None
    finally:
        conn.close()

    journal = tj.TurnJournal(db)
    journal.accept("sess-b", agent="aither", mode="chat", message="unfinished", kwargs={},
                   auth_bound=False)
    rows = {r["session_id"]: r for r in tj.list_resumable(root=tmp_path)}
    assert rows["sess-b"]["open_turn"] is True
    assert rows["sess-b"]["last_turn"]["outcome"] == tj.OPEN


def test_pid_alive_never_signals():
    assert tj.pid_alive(os.getpid()) is True
    assert tj.pid_alive(0) is False
    assert tj.pid_alive(2 ** 22 + 12345) is False


def _getter(agent):
    async def get_agent(name=None, **_):
        return agent
    return get_agent


@pytest.mark.asyncio
async def test_two_sessions_at_once_do_not_see_each_others_message(box):
    """Found by the kill test: the agent's context manager is one shared object
    and ``chat`` awaited the history read between ``clear()`` and ``build()``, so
    two concurrent sessions each got the other's user message in their prompt
    (and ran the other's tool). Resumed turns start together at startup, so the
    resume depends on this holding."""
    gate = asyncio.Event()

    class Recorder(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            await gate.wait()
            return await super().chat(messages, **kwargs)

    llm = Recorder([])
    agent = box.agent(llm)
    real_history = agent.memory.get_history

    async def slow_history(sid, limit=50):      # widen the window the race needs
        await asyncio.sleep(0.05)
        return await real_history(sid, limit=limit)

    agent.memory.get_history = slow_history
    first = asyncio.create_task(agent.chat("ALPHA request", session_id="a"))
    second = asyncio.create_task(agent.chat("BRAVO request", session_id="b"))
    await asyncio.sleep(0.3)
    gate.set()
    await asyncio.gather(first, second)
    prompts = [" ".join(c for role, c in call if role == "user") for call in llm.calls]
    assert len(prompts) == 2
    for prompt in prompts:
        assert ("ALPHA" in prompt) != ("BRAVO" in prompt), prompts


@pytest.mark.asyncio
async def test_an_empty_answer_is_a_failed_turn_not_a_finished_one(box):
    resp = await box.agent(ScriptedLLM([_text("")])).chat("say something", session_id="e")
    assert resp.content == ""
    [turn] = box.journal().session_turns("e")
    assert turn.outcome == tj.FAILED and "empty answer" in turn.reason


@pytest.mark.asyncio
async def test_a_step_cancelled_by_this_process_is_recorded_as_ended(box):
    agent = box.agent(ScriptedLLM([_call("nap"), _text("moved on")]))
    napping = asyncio.Event()

    @agent.tool(name="nap", description="sleeps")
    async def _nap() -> str:
        napping.set()
        await asyncio.sleep(30)
        return "woke"

    task = asyncio.create_task(agent.chat("nap then answer", session_id="n"))
    await asyncio.wait_for(napping.wait(), timeout=20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [turn] = box.journal().session_turns("n")
    [step] = turn.steps.values()
    assert step.status == "error" and "cancelled" in step.error     # not "in flight"
    assert turn.outcome == tj.ABANDONED


@pytest.mark.asyncio
async def test_startup_resume_never_starts_a_second_turn_when_the_client_got_there_first(
        box, monkeypatch):
    box.kill_in.add("slow_job")
    with pytest.raises(_Killed):
        await box.agent(ScriptedLLM([_call("slow_job")])).chat("pay", session_id="s1")
    _restart(monkeypatch)
    box.kill_in.clear()
    [orphan] = box.journal().open_turns()          # what the startup scan saw

    gate = asyncio.Event()

    class SlowLLM(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            await gate.wait()
            return await super().chat(messages, **kwargs)

    agent = box.agent(SlowLLM([_text("the client's answer")]))
    client = asyncio.create_task(agent.chat("pay", session_id="s1"))   # claims it
    await asyncio.sleep(0.3)
    late = asyncio.create_task(tj._resume_one(agent, orphan))          # the scan's task
    await asyncio.sleep(0.3)
    gate.set()
    assert (await client).content == "the client's answer"
    await late
    assert len(agent.llm.calls) == 1
    turns = box.journal().session_turns("s1")
    assert len(turns) == 1 and turns[0].outcome == tj.RESUMED and turns[0].delivered
