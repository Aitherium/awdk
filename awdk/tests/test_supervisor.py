"""Tests for Layer 3 supervised reasoning (``adk.supervisor``)."""

from __future__ import annotations

import asyncio

import pytest
from adk.reasoning_session import ReasoningSession
from adk.supervisor import (
    Goal,
    Supervisor,
    derive_goal,
    register_reasoning_tool,
)


# ── pure assessment ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_assess_escalate_on_repeated_failures():
    sup = Supervisor(ReasoningSession("a"), Goal("find it"), fail_threshold=2)
    v = await sup.assess({"failures": 2, "steps": 1})
    assert v.action == "escalate"


@pytest.mark.asyncio
async def test_assess_escalate_on_drift_without_answer():
    sup = Supervisor(ReasoningSession("a"), Goal("g"), drift_steps=4)
    v = await sup.assess({"failures": 0, "steps": 5, "answered": False})
    assert v.action == "escalate"


@pytest.mark.asyncio
async def test_assess_nudge_on_low_alignment():
    sup = Supervisor(ReasoningSession("a"), Goal("g"), align_fn=lambda st, goal: 0.1)
    v = await sup.assess({"failures": 0, "steps": 1})
    assert v.action == "nudge"
    assert v.score < 0.5


@pytest.mark.asyncio
async def test_assess_proceed_when_aligned():
    sup = Supervisor(ReasoningSession("a"), Goal("g"))  # default alignment 1.0
    v = await sup.assess({"failures": 0, "steps": 1})
    assert v.action == "proceed"


@pytest.mark.asyncio
async def test_alignment_score_async_fn():
    async def af(state, goal):
        return 0.3
    sup = Supervisor(ReasoningSession("a"), Goal("g"), align_fn=af)
    assert await sup.alignment_score({}) == pytest.approx(0.3)


# ── watcher loop reuses Layer-2 steering to intervene ────────────────────────
@pytest.mark.asyncio
async def test_supervisor_escalates_via_steering():
    """A loop that keeps failing tools → supervisor injects a deep_reasoning steer
    through the session's steering queue (the Layer-2 mechanism) + emits events."""
    sess = ReasoningSession("sup1")

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom"})
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom2"})
        await asyncio.sleep(0.05)
        return "done"

    sup = Supervisor(sess, Goal("answer the question"), fail_threshold=2)
    sess.start(loop)
    verdict = await sup.run(tick_s=0.03)

    assert sup._escalated is True
    assert verdict.action == "escalate"
    pending = sess.pop_steering()
    assert any("[Supervisor]" in p and "deep_reasoning" in p for p in pending)


@pytest.mark.asyncio
async def test_supervisor_detects_run_react_vocab_failures():
    """awkit run_react emits step{ok:False} (not tool_result) on tool failure —
    the supervisor must still escalate. Locks in the integration contract."""
    sess = ReasoningSession("sup-rr")

    async def loop(s):
        s.emit({"type": "turn_start", "turn": 1})
        s.emit({"type": "step", "step": 1, "name": "list_events", "ok": False, "result": {"error": "x"}})
        s.emit({"type": "turn_start", "turn": 2})
        s.emit({"type": "step", "step": 2, "name": "list_events", "ok": False, "result": {"error": "y"}})
        await asyncio.sleep(0.05)
        return "done"

    sup = Supervisor(sess, Goal("look it up"), reasoning_tool_name="deep_reason", fail_threshold=2)
    sess.start(loop)
    verdict = await sup.run(tick_s=0.03)
    assert verdict.action == "escalate"
    assert any("deep_reason" in p for p in sess.pop_steering())


@pytest.mark.asyncio
async def test_supervisor_forces_reasoner_injection():
    """A stuck loop with a `reasoner` wired → the supervisor calls the reasoner
    ON THE AGENT'S BEHALF and steers the CONCLUSION (not just an advisory to call
    a tool). A fast router does not self-diagnose being stuck (measured:
    deep_reasoning advertised on 12/12 instances, called 0 times)."""
    sess = ReasoningSession("sup-forced")
    calls = []

    async def reasoner(problem: str) -> str:
        calls.append(problem)
        return "CONCLUSION: the answer is 42"

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom"})
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom2"})
        await asyncio.sleep(0.05)
        return "done"

    sup = Supervisor(sess, Goal("solve the problem"),
                     fail_threshold=2, reasoner=reasoner)
    sess.start(loop)
    verdict = await sup.run(tick_s=0.03)

    assert sup._escalated is True
    assert verdict.action == "escalate"
    # The reasoner ran ONCE, with the goal text, and its conclusion reached the
    # steering queue (the live injection channel the loop drains each step).
    assert calls == ["solve the problem"]
    pending = sess.pop_steering()
    assert any("CONCLUSION: the answer is 42" in p for p in pending)


@pytest.mark.asyncio
async def test_supervisor_reasoner_failure_falls_back_to_advisory():
    """A reasoner that raises/returns empty must not lose the escalation — the
    advisory steer (the old behaviour) remains the fallback."""
    sess = ReasoningSession("sup-reasoner-fail")

    async def reasoner(problem: str) -> str:
        raise RuntimeError("reasoner down")

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom"})
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom2"})
        await asyncio.sleep(0.05)
        return "done"

    sup = Supervisor(sess, Goal("solve it"),
                     fail_threshold=2, reasoner=reasoner,
                     reasoning_tool_name="deep_reason")
    sess.start(loop)
    await sup.run(tick_s=0.03)
    pending = sess.pop_steering()
    assert any("deep_reason" in p for p in pending)  # advisory fallback fired


@pytest.mark.asyncio
async def test_supervisor_empty_reasoner_conclusion_falls_back_to_advisory():
    """An empty conclusion (reasoner returned nothing) → advisory steer, not
    silence — 'escalated and the conclusion was useless' must not look like
    'never escalated'."""
    sess = ReasoningSession("sup-empty-conclusion")

    async def reasoner(problem: str) -> str:
        return "   "

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom"})
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom2"})
        await asyncio.sleep(0.05)
        return "done"

    sup = Supervisor(sess, Goal("solve it"),
                     fail_threshold=2, reasoner=reasoner,
                     reasoning_tool_name="deep_reason")
    sess.start(loop)
    await sup.run(tick_s=0.03)
    pending = sess.pop_steering()
    assert any("deep_reason" in p for p in pending)  # advisory fallback fired


@pytest.mark.asyncio
async def test_supervisor_skips_steer_when_loop_finished():
    """The reasoner await can outlast the loop. A steer into a finished session
    is dropped anyway — skipping it keeps 'escalated and the conclusion was
    useless' distinguishable from 'never escalated' in the log."""
    sess = ReasoningSession("sup-late-reasoner")

    async def reasoner(problem: str) -> str:
        await asyncio.sleep(0.15)   # outlives the loop
        return "CONCLUSION: too late"

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom"})
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": False, "error": "boom2"})
        s.mark_done()               # loop finishes immediately after the failures
        return "done"

    sup = Supervisor(sess, Goal("solve it"), fail_threshold=2, reasoner=reasoner)
    sess.start(loop)
    await sup.run(tick_s=0.03)
    assert sup._escalated is True
    assert sess.pop_steering() == []   # nothing steered into a dead session


@pytest.mark.asyncio
async def test_supervisor_proceeds_quietly_when_aligned():
    sess = ReasoningSession("sup2")

    async def loop(s):
        s.emit({"type": "turn_start"})
        s.emit({"type": "tool_result", "ok": True})
        s.emit({"type": "done"})
        return "ok"

    sup = Supervisor(sess, Goal("g"))
    sess.start(loop)
    await sup.run(tick_s=0.03)
    assert sup._escalated is False
    assert sess.pop_steering() == []   # no intervention when on track


# ── reasoning-as-a-tool ──────────────────────────────────────────────────────
class _FakeTools:
    def __init__(self):
        self.registered = {}

    def register(self, fn, *, name=None, description=None):
        self.registered[name or fn.__name__] = fn


class _FakeAgent:
    def __init__(self):
        self._tools = _FakeTools()


@pytest.mark.asyncio
async def test_register_reasoning_tool_custom_reasoner():
    a = _FakeAgent()

    async def reasoner(p):
        return "CONCLUSION: " + p

    name = register_reasoning_tool(a, reasoner=reasoner, name="deep_reasoning")
    assert name == "deep_reasoning"
    fn = a._tools.registered["deep_reasoning"]
    assert await fn("hard problem") == "CONCLUSION: hard problem"


def test_register_reasoning_tool_disabled_is_noop():
    a = _FakeAgent()
    assert register_reasoning_tool(a, enabled=False) is None
    assert a._tools.registered == {}


def test_derive_goal_trims():
    g = derive_goal("  build the thing  ", "c1")
    assert g.text == "build the thing"
    assert g.conversation_id == "c1"
