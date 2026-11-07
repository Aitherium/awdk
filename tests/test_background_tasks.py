"""Background start()/stop() tools: a slow tool outlives the step that launched it.

The refusal arms carry the weight: the cap must refuse, an unknown tool must not be
silently backgroundable, start() outside a loop must not hand back a task that never
runs, and a completion must be delivered exactly once. The happy arms exist so "the
result never arrives" cannot pass.
"""

from __future__ import annotations

import asyncio
import gc
import json
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adk.agent import AitherAgent
from adk.background_tasks import (
    BackgroundTaskError,
    BackgroundTaskLimitError,
    BackgroundTasks,
    bind_turn,
    register_background_tools,
    reset_turn,
)
from adk.llm.base import LLMResponse
from adk.memory import Memory
from adk.tools import ToolRegistry
from adk.turn_control import EscalationController


def _fake_agent(registry: BackgroundTasks) -> tuple[SimpleNamespace, ToolRegistry]:
    tools = ToolRegistry()
    agent = SimpleNamespace(_tools=tools, _background=registry)
    register_background_tools(agent, registry)
    return agent, tools


# -- the registry ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finished_result_is_inline_clipped_and_still_addressable():
    seen: list[dict] = []
    bg = BackgroundTasks(inline_chars=40, full_chars=5000)
    bg.bind_events(seen)
    tid = bg.start("slow", lambda: "answer-" + "x" * 500, timeout=2)
    state = await bg.wait(tid, timeout=2)

    assert state["status"] == "finished"
    assert state["result"] == "answer-" + "x" * 500
    assert state["truncated"] is False
    assert len(state["inline"]) <= 40 and state["inline"].endswith("...")
    assert len(seen) == 1, "a completion must be emitted exactly once"
    assert seen[0]["task_id"] == tid and seen[0]["status"] == "finished"
    assert seen[0]["text"] == f"[background task {tid} slow finished: {state['inline']}]"


@pytest.mark.asyncio
async def test_failure_and_timeout_arms_emit_their_own_events():
    seen: list[dict] = []
    bg = BackgroundTasks()
    bg.bind_events(seen)

    def boom() -> str:
        raise ValueError("kaboom")

    failed = bg.start("boom", boom, timeout=2)
    stopped = bg.start("forever", lambda: asyncio.sleep(30), timeout=0.02)
    failed_state = await bg.wait(failed, timeout=2)
    timeout_state = await bg.wait(stopped, timeout=2)

    assert failed_state["status"] == "failed"
    assert "kaboom" in failed_state["error"]
    by_id = {e["task_id"]: e for e in seen}
    assert "failed: ValueError: kaboom" in by_id[failed]["text"]

    assert timeout_state["status"] == "timeout"
    assert "timed out after" in by_id[stopped]["text"]


@pytest.mark.asyncio
async def test_stop_cancels_a_running_task_and_is_idempotent():
    seen: list[dict] = []
    bg = BackgroundTasks()
    bg.bind_events(seen)
    tid = bg.start("long", lambda: asyncio.sleep(30), timeout=10)
    assert bg.status(tid)["status"] == "running"

    assert bg.stop(tid) is True
    state = await bg.wait(tid, timeout=2)
    assert state["status"] == "stopped"
    assert "stopped]" in seen[0]["text"]
    assert bg.stop(tid) is False
    assert bg.stop("no-such-task") is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_concurrency_cap_refuses_then_frees_after_completion():
    release = asyncio.Event()

    async def blocker() -> str:
        await release.wait()
        return "done"

    bg = BackgroundTasks(max_concurrent=2)
    first = bg.start("blocker", blocker, timeout=5)
    second = bg.start("blocker", blocker, timeout=5)
    with pytest.raises(BackgroundTaskLimitError):
        bg.start("blocker", blocker, timeout=5)

    release.set()
    await bg.wait(first, timeout=2)
    await bg.wait(second, timeout=2)

    third = bg.start("quick", lambda: "now", timeout=2)
    assert (await bg.wait(third, timeout=2))["status"] == "finished"


@pytest.mark.asyncio
async def test_bad_requests_are_refused_before_a_task_exists():
    bg = BackgroundTasks()
    with pytest.raises(BackgroundTaskError):
        bg.start("", lambda: "x")
    with pytest.raises(BackgroundTaskError):
        bg.start("   ", lambda: "x")
    with pytest.raises(BackgroundTaskError):
        bg.start("name", 12345)
    with pytest.raises(BackgroundTaskError):
        bg.start("name", lambda: "x", timeout=0)
    with pytest.raises(BackgroundTaskError):
        BackgroundTasks(max_concurrent=0)
    with pytest.raises(BackgroundTaskError):
        await bg.wait("never-started")


@pytest.mark.asyncio
async def test_start_refuses_a_malformed_timeout_with_a_typed_error():
    # Before the fix start() did float(timeout) and raised a raw ValueError,
    # which no caller's BackgroundTaskError handler catches.
    bg = BackgroundTasks()
    with pytest.raises(BackgroundTaskError):
        bg.start("name", lambda: "x", timeout="abc")


def test_start_outside_a_running_loop_refuses():
    # A task handed back with no loop would sit "running" forever.
    bg = BackgroundTasks()
    with pytest.raises(BackgroundTaskError):
        bg.start("x", lambda: "never runs")


@pytest.mark.asyncio
async def test_async_callables_and_bare_coroutines_both_run():
    bg = BackgroundTasks()

    async def async_fn() -> str:
        await asyncio.sleep(0)
        return "from-fn"

    one = bg.start("async_fn", async_fn, timeout=2)
    two = bg.start("bare", asyncio.sleep(0, "from-coroutine"), timeout=2)
    assert (await bg.wait(one, timeout=2))["result"] == "from-fn"
    assert (await bg.wait(two, timeout=2))["result"] == "from-coroutine"


@pytest.mark.asyncio
async def test_pending_events_flush_into_a_later_sink():
    bg = BackgroundTasks()
    done_before = bg.start("early", lambda: "early", timeout=2)
    await bg.wait(done_before, timeout=2)
    assert len(bg.drain_events()) == 1
    assert bg.drain_events() == []

    pending = bg.start("late", lambda: "late", timeout=2)
    await bg.wait(pending, timeout=2)
    sink: list[dict] = []
    bg.bind_events(sink)
    assert [e["task_id"] for e in sink] == [pending]
    assert bg.drain_events() == []

    bg.bind_events(None)
    assert sink == [], "unconsumed events move back to the pending queue on unbind"
    another = bg.start("after-unbind", lambda: "x", timeout=2)
    await bg.wait(another, timeout=2)
    assert [e["task_id"] for e in bg.drain_events()] == [pending, another]


@pytest.mark.asyncio
async def test_unconsumed_events_carry_to_the_next_sink():
    bg = BackgroundTasks()
    first: list[dict] = []
    bg.bind_events(first)
    tid = bg.start("quick", lambda: "v", timeout=2)
    await bg.wait(tid, timeout=2)
    assert [e["task_id"] for e in first] == [tid]

    bg.bind_events(None)  # the turn ended without consuming the completion
    assert first == []

    second: list[dict] = []
    bg.bind_events(second)
    assert [e["task_id"] for e in second] == [tid]


@pytest.mark.asyncio
async def test_history_cap_prunes_old_finished_records(monkeypatch):
    import adk.background_tasks as bg_mod

    monkeypatch.setattr(bg_mod, "HISTORY_CAP", 2)
    bg = BackgroundTasks()
    ids: list[str] = []
    for i in range(3):
        tid = bg.start(f"t{i}", lambda: "x", timeout=2)
        await bg.wait(tid, timeout=2)
        ids.append(tid)
    assert ids[0] not in bg._tasks, "the oldest finished record must be pruned"
    assert set(ids[1:]) <= set(bg._tasks)


# -- the agent tools ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_bg_start_wraps_a_registered_tool_and_bg_status_returns_the_full_result():
    bg = BackgroundTasks(inline_chars=30)
    agent, tools = _fake_agent(bg)

    async def slow_tool(path: str = "") -> str:
        await asyncio.sleep(0.01)
        return "content:" + path + ":" + "z" * 400

    tools.register(slow_tool, name="slow_tool")
    started = json.loads(await tools.execute(
        "bg_start", {"tool": "slow_tool", "args": {"path": "a"}, "timeout_s": 2}))
    assert started["status"] == "running" and started["tool"] == "slow_tool"
    tid = started["task_id"]

    state = await bg.wait(tid, timeout=2)
    assert state["status"] == "finished"
    assert state["result"].startswith("content:a:")

    status_out = json.loads(await tools.execute("bg_status", {"task_id": tid}))
    assert status_out["result"] == state["result"]
    assert status_out["inline"] == state["inline"]

    listed = json.loads(await tools.execute("bg_status", {}))
    assert listed["running"] == 0
    assert any(t["task_id"] == tid for t in listed["tasks"])


@pytest.mark.asyncio
async def test_bg_start_accepts_json_encoded_args():
    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)

    async def echo(value: str = "") -> str:
        return f"echo:{value}"

    tools.register(echo, name="echo")
    started = json.loads(await tools.execute(
        "bg_start", {"tool": "echo", "args": '{"value": "hi"}'}))
    assert started["task_id"]
    state = await bg.wait(started["task_id"], timeout=2)
    assert state["result"] == "echo:hi"


@pytest.mark.asyncio
async def test_bg_tools_refuse_unknown_recursive_and_malformed_requests():
    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)

    async def anything() -> str:
        return "x"

    tools.register(anything, name="anything")

    unknown = json.loads(await tools.execute("bg_start", {"tool": "no_such_tool"}))
    assert "Unknown tool" in unknown["error"]

    recursive = json.loads(await tools.execute("bg_start", {"tool": "bg_start"}))
    assert "cannot be backgrounded" in recursive["error"]

    malformed = json.loads(await tools.execute(
        "bg_start", {"tool": "anything", "args": "[1, 2]"}))
    assert "object/dict" in malformed["error"]

    assert json.loads(await tools.execute("bg_start", {"tool": ""}))["error"]

    stopped = json.loads(await tools.execute("bg_stop", {"task_id": "nope"}))
    assert stopped == {"task_id": "nope", "stopped": False, "status": None,
                       "error": "unknown_task"}
    assert json.loads(await tools.execute("bg_stop", {}))["error"]


@pytest.mark.asyncio
async def test_bg_start_reports_at_capacity_as_a_model_readable_error():
    bg = BackgroundTasks(max_concurrent=1)
    _, tools = _fake_agent(bg)
    release = asyncio.Event()

    async def blocker() -> str:
        await release.wait()
        return "done"

    tools.register(blocker, name="blocker")
    first = json.loads(await tools.execute("bg_start", {"tool": "blocker", "timeout_s": 5}))
    assert first["task_id"]
    refused = json.loads(await tools.execute("bg_start", {"tool": "blocker"}))
    assert refused["error"] == "at_capacity"
    release.set()
    await bg.wait(first["task_id"], timeout=2)


def test_register_without_a_registry_or_tools_refuses():
    with pytest.raises(BackgroundTaskError):
        register_background_tools(SimpleNamespace(_tools=ToolRegistry()))
    with pytest.raises(BackgroundTaskError):
        register_background_tools(SimpleNamespace(_background=BackgroundTasks()), None)


# -- turn_control routing ----------------------------------------------------------


def test_controller_drains_only_background_events():
    ctl = EscalationController(None, None, 6)
    ctl.events = [
        {"type": "plan", "text": "p"},
        {"type": "background", "text": "b1", "task_id": "t1"},
        {"type": "degraded", "stage": "x"},
        {"type": "background", "text": "b2", "task_id": "t2"},
    ]
    assert [e["task_id"] for e in ctl.drain_background_events()] == ["t1", "t2"]
    assert [e["type"] for e in ctl.events] == ["plan", "degraded"]
    assert ctl.drain_background_events() == []


# -- agent wiring ------------------------------------------------------------------


def _llm() -> AsyncMock:
    llm = AsyncMock()
    llm.provider_name = "mock"
    llm.chat = AsyncMock(return_value=LLMResponse(content="x", model="m"))
    return llm


def test_agent_background_tools_are_off_unless_asked(tmp_path):
    off = AitherAgent("t", llm=_llm(), builtin_tools=False,
                      memory=Memory(db_path=tmp_path / "off.db", agent_name="t"))
    assert off.background_tools is False
    assert off._background is None
    assert off._tools.get("bg_start") is None

    on = AitherAgent("t", llm=_llm(), builtin_tools=False, background_tools=True,
                     memory=Memory(db_path=tmp_path / "on.db", agent_name="t"))
    assert on.background_tools is True
    assert on._background is not None
    for name in ("bg_start", "bg_stop", "bg_status"):
        assert on._tools.get(name) is not None, name


@pytest.mark.asyncio
async def test_stream_react_hands_a_finished_task_to_the_next_model_step(tmp_path):
    from adk.llm.base import StreamChunk

    seen: list[list[str]] = []

    async def _stream(messages, *a, **k):
        seen.append([str(m.content or "") for m in messages])
        yield StreamChunk(content="<think>ok</think>FINAL: done", done=True, model="t")

    llm = _llm()
    llm.chat_stream = _stream
    agent = AitherAgent("s", llm=llm, builtin_tools=False, background_tools=True,
                        memory=Memory(db_path=tmp_path / "s.db", agent_name="s"))
    tid = agent._background.start("quick_tool", lambda: "the answer is 42", timeout=2)
    await agent._background.wait(tid, timeout=2)

    events: list = []
    resp = await agent.stream_react("anything", on_event=events.append)

    assert resp.content == "done"
    assert seen, "the model was never called"
    joined = "\n".join(seen[0])
    assert f"[background task {tid} quick_tool finished" in joined
    assert "the answer is 42" in joined
    assert any(e.get("type") == "background" for e in events if isinstance(e, dict))


# -- reviewer regressions: the gate, the principal, the timeout, the fallback --------


@pytest.fixture()
def isolated_approval(tmp_path, monkeypatch):
    """Keep the real ~/.aither approval state out of these tests."""
    from adk import approval as approval_mod

    monkeypatch.setenv("AITHER_TOOL_ALLOWLIST_FILE", str(tmp_path / "allow.json"))
    monkeypatch.delenv("AITHER_TOOL_APPROVAL_OUTWARD", raising=False)
    return approval_mod


@pytest.mark.asyncio
async def test_bg_start_refuses_a_gated_tool_without_an_allow(isolated_approval,
                                                              monkeypatch):
    """CRITICAL: bg_start must not route around the Allow/Deny card.

    Before the fix the wrapped tool ran immediately with no decision recorded.
    """
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "send_email")
    served: list[str] = []

    async def send_email(to: str = "") -> str:
        served.append(to)
        return f"SENT to {to}"

    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)
    tools.register(send_email, name="send_email")

    out = json.loads(await tools.execute(
        "bg_start", {"tool": "send_email", "args": {"to": "victim"}}))

    assert out["error"] == "approval_required"
    assert out.get("task_id") is None
    assert served == [], "a gated tool ran with no Allow/Deny decision"
    assert json.loads(await tools.execute("bg_status", {}))["tasks"] == []


@pytest.mark.asyncio
async def test_bg_start_runs_a_gated_tool_after_an_explicit_allow(
        isolated_approval, monkeypatch, tmp_path):
    """The refusal is not blanket: a recorded allow for THIS call is honoured."""
    approval_mod = isolated_approval
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "send_email")
    sid = "session-allow"
    store = approval_mod.ApprovalStore(tmp_path / "paused.json")
    store.put_pending(sid, user_message="send it", agent="",
                      pending=[{"tool_use_id": "1", "tool": "send_email",
                                "args": {"to": "friend"}}])
    store.record_decisions(sid, [{"tool_use_id": "1", "result": "allow"}])
    monkeypatch.setattr(approval_mod, "get_approval_store", lambda: store)

    sent: list[str] = []

    async def send_email(to: str = "") -> str:
        sent.append(to)
        return f"SENT to {to}"

    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)
    tools.register(send_email, name="send_email")

    bind_turn(session_id=sid, auth=None)
    try:
        started = json.loads(await tools.execute(
            "bg_start", {"tool": "send_email", "args": {"to": "friend"}}))
    finally:
        reset_turn()

    assert started.get("task_id"), started
    state = await bg.wait(started["task_id"], timeout=2)
    assert state["status"] == "finished"
    assert state["result"] == "SENT to friend"
    assert sent == ["friend"]


@pytest.mark.asyncio
async def test_bg_start_carries_the_callers_auth_context(isolated_approval, monkeypatch):
    """HIGH: a restricted principal must not gain authority by backgrounding.

    Before the fix tools.execute ran with auth=None, which authorizes everything.
    """
    from adk.auth import AuthContext

    monkeypatch.delenv("AITHER_TOOL_APPROVAL", raising=False)
    ran: list[int] = []

    async def dangerous() -> str:
        ran.append(1)
        return "RAN-WITHOUT-AUTH"

    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)
    tools.register(dangerous, name="dangerous", required_clearance=5)

    bind_turn(session_id="s-auth", auth=AuthContext.anonymous())
    try:
        started = json.loads(await tools.execute("bg_start", {"tool": "dangerous"}))
    finally:
        reset_turn()

    assert started.get("task_id"), started
    state = await bg.wait(started["task_id"], timeout=2)
    assert state["status"] == "finished"
    assert json.loads(state["result"])["error"] == "forbidden"
    assert ran == [], "the tool body ran despite the caller lacking clearance"


@pytest.mark.asyncio
async def test_bg_start_with_a_malformed_timeout_is_a_clean_refusal():
    """MEDIUM: a bad timeout_s must not escape as a raw ValueError or leak a coroutine."""
    bg = BackgroundTasks()
    _, tools = _fake_agent(bg)

    async def anything() -> str:
        return "x"

    tools.register(anything, name="anything")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = json.loads(await tools.execute(
            "bg_start", {"tool": "anything", "timeout_s": "abc"}))
        gc.collect()

    assert out["error"] == "cannot_start"
    assert "timeout_s" in out["message"]
    leaked = [w for w in caught if "never awaited" in str(w.message)]
    assert not leaked, f"a coroutine leaked unawaited: {[str(w.message) for w in leaked]}"
    assert json.loads(await tools.execute("bg_status", {}))["tasks"] == []


@pytest.mark.asyncio
async def test_stream_react_without_a_controller_keeps_in_turn_completions(
        tmp_path, monkeypatch):
    """MEDIUM: with no EscalationController the drain must not orphan the sink list."""
    from adk import turn_control
    from adk.llm.base import StreamChunk

    def _no_controller(*_a, **_k):
        raise RuntimeError("controller unavailable in this host")

    monkeypatch.setattr(turn_control, "EscalationController", _no_controller)

    seen: list[list[str]] = []
    calls = {"n": 0}

    async def _stream(messages, *_a, **_k):
        seen.append([str(m.content or "") for m in messages])
        calls["n"] += 1
        if calls["n"] == 1:
            # Finish a task DURING the step, after that step's drain already ran;
            # only a stable sink list delivers it to the next step.
            tid = agent._background.start("mid", lambda: "mid-turn result", timeout=2)
            await agent._background.wait(tid, timeout=2)
            yield StreamChunk(content='ACTION: echo\nINPUT: {"value": "x"}',
                              done=True, model="t")
        else:
            yield StreamChunk(content="FINAL: done", done=True, model="t")

    llm = _llm()
    llm.chat_stream = _stream
    agent = AitherAgent("f", llm=llm, builtin_tools=False, background_tools=True,
                        memory=Memory(db_path=tmp_path / "f.db", agent_name="f"))

    async def echo(value: str = "") -> str:
        return f"echo:{value}"

    agent._tools.register(echo, name="echo")

    events: list = []
    resp = await agent.stream_react("anything", on_event=events.append)

    assert resp.content == "done"
    assert calls["n"] >= 2, "the turn never reached a second model step"
    assert "mid-turn result" in "\n".join(seen[1]), (
        "the completion was orphaned instead of reaching the next model step")
    assert any(e.get("type") == "background" for e in events if isinstance(e, dict))

