"""Integration tests driving the REAL AitherAgent.chat() ReAct loop.

tests/test_context_budget.py unit-tests the helpers. That is not enough: a helper
can be perfect and still never be reached, which is the "silent no-op" failure —
every assertion passes and the feature is inert. These tests drive the actual loop
with a scripted LLM and assert the behaviour is observable from OUTSIDE:

  - the effort-scaled ceiling actually bounds iterations,
  - an abandoned turn is REPORTED as abandoned (finish_reason), not as "stop",
  - the model is warned before its last tool turn and after the budget is spent,
  - continue-on-budget actually re-invokes the model, and
  - with no budget configured, nothing changes at all.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock

from adk.agent import AitherAgent
from adk.llm.base import LLMResponse, ToolCall
from adk.memory import Memory


class ScriptedLLM:
    """An LLM whose replies are scripted; records every message list it saw."""

    provider_name = "scripted"
    model = "claude-opus-5"  # 200k window → compaction stays out of the way

    def __init__(self, replies: list[LLMResponse], default: LLMResponse | None = None):
        self.replies = list(replies)
        self.default = default or LLMResponse(
            content="final answer", model="scripted", tokens_used=10,
        )
        self.calls: list[list] = []
        # TRUE total content size per call. `calls` truncates each message to 400
        # chars so substring assertions stay cheap — measuring history growth off
        # that truncated copy silently measures the truncation, not the history.
        self.sizes: list[int] = []

    async def chat(self, messages, **kwargs):
        # Deep-ish copy: the loop mutates `messages`, so store role/content now.
        self.sizes.append(
            sum(len(str(getattr(m, "content", "") or "")) for m in messages)
        )
        self.calls.append([
            (getattr(m, "role", "?"), str(getattr(m, "content", ""))[:400])
            for m in messages
        ])
        if self.replies:
            return self.replies.pop(0)
        return self.default

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def sent_text(self) -> str:
        """All message content ever sent, flattened — for substring assertions."""
        return "\n".join(c for call in self.calls for _, c in call)


def _tool_reply(name: str = "probe", call_id: str = "c1", **args) -> LLMResponse:
    return LLMResponse(
        content="",
        model="scripted",
        tokens_used=50,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args or {"q": "x"})],
        finish_reason="tool_calls",
    )


def _text_reply(content: str = "done", tokens: int = 50) -> LLMResponse:
    return LLMResponse(
        content=content, model="scripted", tokens_used=tokens, finish_reason="stop",
    )


@pytest.fixture
def agent_factory(tmp_path):
    """Build an agent with one trivial tool and a scripted LLM."""
    created: list[AitherAgent] = []

    def _make(llm, name="loop_test"):
        memory = Memory(db_path=tmp_path / f"{name}.db", agent_name=name)
        agent = AitherAgent(name, llm=llm, memory=memory)

        @agent.tool(name="probe", description="A trivial probe tool")
        async def _probe(q: str = "x") -> str:
            return json.dumps({"ok": True, "q": q})

        # Keep the loop deterministic: no graph ingest, no safety rewrite.
        agent._graph = None
        agent._safety = None
        created.append(agent)
        return agent

    return _make


# ── The ceiling actually bounds the loop, and scales with effort ──────────


@pytest.mark.asyncio
async def test_low_effort_ceiling_bounds_iterations(agent_factory):
    """A model that never stops asking for tools must be cut off at the TRIAGE
    ceiling (4), not at the old flat 10."""
    llm = ScriptedLLM([], default=_tool_reply())
    agent = agent_factory(llm)

    await agent.chat("go", effort=1)

    # 4 loop iterations + 1 forced synthesis call.
    assert llm.call_count == 5, f"expected 4 iterations + synthesis, got {llm.call_count}"


@pytest.mark.asyncio
async def test_high_effort_gets_a_deeper_ceiling(agent_factory):
    """The whole point of scaling: a deep task is not held to a triage budget."""
    low = ScriptedLLM([], default=_tool_reply())
    high = ScriptedLLM([], default=_tool_reply())

    await agent_factory(low, name="low").chat("go", effort=1)
    await agent_factory(high, name="high").chat("go", effort=9)

    assert high.call_count > low.call_count, (
        f"effort 9 ({high.call_count} calls) must run deeper than effort 1 "
        f"({low.call_count} calls)"
    )


@pytest.mark.asyncio
async def test_env_override_caps_the_ceiling(agent_factory, monkeypatch):
    """Operators on a metered backend need a hard cap they control."""
    monkeypatch.setenv("ADK_MAX_TOOL_LOOPS", "2")
    llm = ScriptedLLM([], default=_tool_reply())

    await agent_factory(llm).chat("go", effort=9)

    assert llm.call_count == 3, f"expected 2 iterations + synthesis, got {llm.call_count}"


# ── A truncated turn must be REPORTED as truncated ────────────────────────


@pytest.mark.asyncio
async def test_exhausted_loop_reports_max_tool_loops(agent_factory):
    """This is the defect with the widest blast radius: the synthesis call returns
    finish_reason="stop", so an abandoned turn used to be indistinguishable from a
    completed one to every caller."""
    llm = ScriptedLLM([], default=_tool_reply())
    agent = agent_factory(llm)

    resp = await agent.chat("go", effort=1)

    assert resp.finish_reason == "max_tool_loops", (
        f"truncated turn reported {resp.finish_reason!r} — callers cannot tell it "
        "was cut off"
    )


@pytest.mark.asyncio
async def test_completed_turn_does_not_claim_truncation(agent_factory):
    """The converse — the flag must not fire on a turn that finished normally."""
    llm = ScriptedLLM([_text_reply("all done")])
    agent = agent_factory(llm)

    resp = await agent.chat("go", effort=1)

    assert resp.finish_reason != "max_tool_loops"
    assert resp.content == "all done"


@pytest.mark.asyncio
async def test_model_is_warned_before_and_at_the_end_of_its_budget(agent_factory):
    """Silent truncation becomes stated truncation only if the model is actually
    told — otherwise it writes up partial work as though it were finished."""
    llm = ScriptedLLM([], default=_tool_reply())
    agent = agent_factory(llm)

    await agent.chat("go", effort=1)

    sent = llm.sent_text()
    assert "FINAL TOOL ITERATION" in sent, "model never warned its last turn was next"
    assert "TOOL BUDGET EXHAUSTED" in sent, "model never told the budget ran out"


# ── Continue-when-there-is-room ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_budget_configured_changes_nothing(agent_factory):
    """Ordinary conversational turns must be completely unaffected. If this ever
    fails, every chat turn silently got more expensive."""
    llm = ScriptedLLM([_text_reply("hi")])
    agent = agent_factory(llm)

    resp = await agent.chat("hello")

    assert llm.call_count == 1, "model was re-invoked with no budget configured"
    assert resp.content == "hi"


@pytest.mark.asyncio
async def test_budget_nudges_a_model_that_stops_early(agent_factory):
    """The POSITIVE assertion this feature needed: with budget remaining, a model
    that returns no tool calls is pushed to keep working rather than accepted."""
    # Must start with a tool call: a tool-less turn is a conversational answer
    # and is deliberately never nudged (see test_a_toolless_turn_is_never_nudged).
    llm = ScriptedLLM([
        _tool_reply(),
        _text_reply("I've made a good start.", tokens=100),
        _text_reply("Now actually finished.", tokens=100),
    ])
    agent = agent_factory(llm)

    resp = await agent.chat("do the whole job", effort=5, token_budget=100_000)

    assert llm.call_count >= 3, "model stopped early and was taken at its word"
    assert "BUDGET" in llm.sent_text(), "no continuation nudge was sent"
    # It keeps pushing while budget remains and output is still moving — it does
    # NOT stop after exactly one nudge. Termination comes from diminishing
    # returns (tiny deltas) or the loop ceiling, so the turn still ends.
    assert llm.call_count < 16, (
        f"nudged {llm.call_count} times — continuation is not converging"
    )
    assert resp.content, "turn produced no content"


@pytest.mark.asyncio
async def test_budget_stops_once_nearly_spent(agent_factory):
    """Having pushed, it must also know when to stop — a turn that has burned its
    budget is not nudged for more."""
    llm = ScriptedLLM([_text_reply("done", tokens=9_800)])
    agent = agent_factory(llm)

    resp = await agent.chat("go", effort=5, token_budget=10_000)

    assert llm.call_count == 1, "nudged despite the budget being nearly spent"
    assert resp.content == "done"


@pytest.mark.asyncio
async def test_budget_nudge_does_not_leak_into_the_response(agent_factory):
    """The nudge is loop scaffolding; it must never surface as agent output."""
    llm = ScriptedLLM([
        _text_reply("partial", tokens=100),
        _text_reply("complete", tokens=100),
    ])
    agent = agent_factory(llm)

    resp = await agent.chat("go", effort=5, token_budget=100_000)

    assert "BUDGET" not in resp.content, "loop scaffolding leaked into agent output"
    assert "tokens]" not in resp.content
    assert "KEEP GOING" not in resp.content


# ── Compaction actually fires inside the loop ─────────────────────────────


class SmallWindowLLM(ScriptedLLM):
    """Same scripting, but a tiny context window so compaction must engage."""

    model = "gemma-2b"  # 8_192-token window in context_budget's table


@pytest.mark.asyncio
async def test_compaction_engages_as_tool_results_accumulate(agent_factory, tmp_path):
    """The unit tests prove maybe_compact() works. They cannot prove the loop
    REACHES it — which is exactly how the finish_reason defect hid.

    Note this must grow the history via TOOL RESULTS, not via the `history=`
    argument: adk's ContextManager already budgets the initial history once
    before the loop starts (a 120kB seed arrives as ~1.3kB). In-loop growth from
    accumulating tool output is the gap this compaction closes, so that is what
    the fixture has to reproduce.
    """
    bulk = "y" * 40_000  # each tool result is ~11k tokens on an 8k-window model

    llm = SmallWindowLLM([], default=_tool_reply())
    memory = Memory(db_path=tmp_path / "compact.db", agent_name="compact")
    agent = AitherAgent("compact", llm=llm, memory=memory)

    @agent.tool(name="probe", description="Returns a large payload")
    async def _probe(q: str = "x") -> str:
        return bulk

    agent._graph = None
    agent._safety = None

    await agent.chat("go", effort=3)

    assert llm.call_count >= 4, "loop did not run enough iterations to accumulate"
    peak = max(llm.sizes)
    assert peak > 40_000, (
        f"tool results never accumulated (peak {peak} chars) — the fixture is not "
        "exercising in-loop growth and the test would pass vacuously"
    )
    assert llm.sizes[-1] < peak, (
        f"history only ever grew (sizes: {llm.sizes}) — compaction is not being "
        "reached from the loop"
    )


# ── The budget is actually wired to a real caller ─────────────────────────


def test_low_effort_gets_no_budget_by_default():
    """The cost-safety property. Ordinary and procedural turns must not silently
    start costing several times more."""
    from adk.context_budget import default_token_budget

    for effort in (1, 2, 3, 4, 5, 6):
        assert default_token_budget(effort) is None, (
            f"effort {effort} was granted a continuation budget — every turn at "
            "this tier just got more expensive"
        )
    assert default_token_budget(None) is None


def test_deep_effort_gets_a_budget_by_default():
    """Otherwise the feature stays dormant and the whole thing is inert."""
    from adk.context_budget import default_token_budget

    assert default_token_budget(7) is not None
    assert default_token_budget(10) > default_token_budget(7)


def test_budget_can_be_forced_or_disabled_by_env(monkeypatch):
    from adk.context_budget import default_token_budget

    monkeypatch.setenv("ADK_TURN_TOKEN_BUDGET", "12345")
    assert default_token_budget(1) == 12345      # forced on at any effort

    monkeypatch.setenv("ADK_TURN_TOKEN_BUDGET", "0")
    assert default_token_budget(10) is None      # escape hatch: fully off

    monkeypatch.setenv("ADK_TURN_TOKEN_BUDGET", "junk")
    assert default_token_budget(10) is not None  # junk ignored, not fatal


@pytest.mark.asyncio
async def test_deep_effort_turn_actually_nudges_without_an_explicit_budget(agent_factory):
    """End-to-end proof the default is WIRED, not just computed: a high-effort
    turn nudges a model that stopped early, with no token_budget= passed."""
    llm = ScriptedLLM([
        _tool_reply(),  # tool-gate: only real (tool-using) work gets nudged
        _text_reply("stopping early", tokens=100),
        _text_reply("actually finished", tokens=100),
    ])
    agent = agent_factory(llm, name="deep")

    await agent.chat("do the whole job", effort=9)

    assert llm.call_count >= 3, "deep-effort turn was taken at its word"
    assert "BUDGET" in llm.sent_text()


@pytest.mark.asyncio
async def test_a_toolless_turn_is_never_nudged(agent_factory):
    """A turn that called no tools is a conversational answer, not unfinished
    work. Nudging it produces padding.

    Observed live at effort 9: "name three risks, be brief" was answered on turn
    one and then nudged 19+ times. This is the gate for that — real work in this
    loop goes through tools, so if none were called there is nothing for another
    iteration to finish.
    """
    llm = ScriptedLLM([
        _text_reply("Three risks: a, b, c.", tokens=100),
        _text_reply("padding nobody asked for", tokens=100),
    ])
    agent = agent_factory(llm, name="toolless")

    resp = await agent.chat("name three risks, be brief", effort=9)

    assert llm.call_count == 1, (
        f"a tool-less turn was nudged ({llm.call_count} model calls) — this is "
        "the runaway-padding failure seen live"
    )
    assert resp.content == "Three risks: a, b, c."


@pytest.mark.asyncio
async def test_a_tool_using_turn_is_still_nudged(agent_factory):
    """The converse — the gate must not disable the feature it protects."""
    llm = ScriptedLLM([
        _tool_reply(),                                  # uses a tool
        _text_reply("stopping early", tokens=100),      # then quits with budget left
        _text_reply("actually finished", tokens=100),
    ])
    agent = agent_factory(llm, name="toolful")

    await agent.chat("do the whole job", effort=9)

    assert llm.call_count >= 3, "a tool-using turn was taken at its word"
    assert "BUDGET" in llm.sent_text()


@pytest.mark.asyncio
async def test_default_effort_turn_is_untouched(agent_factory):
    """The same turn at default effort must invoke the model exactly once."""
    llm = ScriptedLLM([_text_reply("stopping early", tokens=100)])
    agent = agent_factory(llm, name="shallow")

    await agent.chat("do the whole job", effort=5)

    assert llm.call_count == 1, "a default-effort turn started nudging"


@pytest.mark.asyncio
async def test_explicit_budget_overrides_the_effort_default(agent_factory):
    """An explicit token_budget=0 must disable nudging even at deep effort."""
    llm = ScriptedLLM([_text_reply("stopping early", tokens=100)])
    agent = agent_factory(llm, name="explicit")

    await agent.chat("go", effort=9, token_budget=0)

    assert llm.call_count == 1, "explicit token_budget=0 was ignored"


@pytest.mark.asyncio
async def test_compaction_leaves_a_small_history_alone(agent_factory):
    """Compaction must be inert on ordinary turns — it costs an LLM call."""
    llm = ScriptedLLM([_text_reply("hi")])
    agent = agent_factory(llm, name="nocompact")

    await agent.chat("hello", effort=5)

    assert llm.call_count == 1, "a short turn triggered a summarization call"
