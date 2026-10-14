"""Planning and escalation on a streaming turn (adk.turn_control + LLMRouter.chat_stream).

Pins: one reasoning-tier consult per turn however many signals fire; no plan below
effort 6; a planner fault is a degraded event, never an exception; and a streaming call
at effort 8 runs on the reasoning provider, like ``chat()`` does.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from adk.intent import IntentDecision
from adk.llm import LLMRouter
from adk.llm.base import LLMResponse, StreamChunk
from adk.turn_control import HINT_MAX_CHARS, PLAN_MAX_CHARS, EscalationController


def _run(coro):
    return asyncio.run(coro)


class _Tools:
    """The two calls the controller makes on an agent's registry."""

    def __init__(self, answer: str = "Read config.yaml instead.") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer

    def get(self, name):
        return object() if name == "ask_reasoner" else None

    async def execute(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"model": "reasoner", "answer": self.answer})


class _LLM:
    def __init__(self, content: str = "", raises: Exception | None = None) -> None:
        self.calls = 0
        self.content = content
        self.raises = raises

    async def chat(self, messages, effort=None, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return LLMResponse(content=self.content, model="orchestrator")


def _decision(effort: int, agentic: bool = True, grounding: bool = False) -> IntentDecision:
    return IntentDecision(intent="analysis", effort=effort, agentic=agentic,
                          reasoning_depth="light", requires_grounding=grounding,
                          grounding_label="your calendar" if grounding else "")


def _agent(llm=None, tools=None):
    return SimpleNamespace(llm=llm or _LLM(), _tools=tools or _Tools())


_PLAN_JSON = json.dumps({
    "intent": "tool_use", "effort": 2,
    "route": {"lane": "reasoning", "model_hint": "", "fallback": []},
    "plan": [{"step": 1, "lane": "reasoning", "goal": "read the config"},
             {"step": 2, "lane": "chat", "goal": "answer"}],
    "escalation": {"stuck_threshold": 3, "next_effort": 3},
})


# -- one budget per turn ------------------------------------------------------------

def test_three_identical_zero_progress_steps_escalate_once_and_consult_once():
    tools = _Tools()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)
    ctl.task = "why does the daemon not bind crystal?"

    async def go():
        return [await ctl.observe(i, "file_search", {"path": "a.py"}, "[]", True)
                for i in (1, 2, 3)]

    hints = _run(go())
    signals = [e for e in ctl.events if e["type"] in ("replan", "escalate")]
    assert len(signals) == 1, ctl.events
    assert len(tools.calls) == 1 and tools.calls[0][0] == "ask_reasoner"
    assert hints[:2] == [None, None]
    assert hints[2] and "Read config.yaml" in hints[2] and len(hints[2]) <= HINT_MAX_CHARS
    # The question names the stalled step, not a rewrite of the whole turn.
    assert "a.py" in tools.calls[0][1]["context"]

    async def more():
        out = [await ctl.observe(i, "file_search", {"path": "a.py"}, "[]", True)
               for i in (4, 5, 6, 7)]
        out.append(await ctl.on_stall())
        return out

    _run(more())
    assert len(tools.calls) == 1, "a second consult in the same turn"
    assert ctl.consulted


def test_failing_identical_steps_replan_once_without_a_consult():
    tools = _Tools()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)

    async def go():
        return [await ctl.observe(i, "shell_exec", {"command": "Get-X"}, '{"error":"no"}',
                                  False) for i in (1, 2, 3)]

    hints = _run(go())
    signals = [e for e in ctl.events if e["type"] in ("replan", "escalate")]
    assert len(signals) == 1 and signals[0]["type"] == "replan", ctl.events
    assert hints[2] and "[RE-ROUTER]" in hints[2]
    assert tools.calls == []


def test_loop_consult_shares_the_budget():
    tools = _Tools()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)
    ctl.mark_consulted()  # the loop's own ask_reasoner consult after two failures
    assert _run(ctl.on_stall()) is None
    assert tools.calls == []


def test_new_information_is_progress():
    tools = _Tools()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)

    async def go():
        for i in range(1, 6):
            await ctl.observe(i, "file_read", {"path": f"f{i}.py"}, f"content {i}", True)

    _run(go())
    assert not [e for e in ctl.events if e["type"] in ("replan", "escalate")]
    assert tools.calls == []


# -- plan ---------------------------------------------------------------------------

@pytest.mark.parametrize("effort", [1, 3, 5])
def test_plan_skipped_at_effort_5_or_below(effort):
    llm = _LLM(_PLAN_JSON)
    ctl = EscalationController(_agent(llm=llm), _decision(effort, agentic=True), 6)
    ctl.task = "list my files"
    assert _run(ctl.before_loop()) == ""
    assert llm.calls == 0


def test_plan_produced_at_effort_7():
    llm = _LLM(_PLAN_JSON)
    ctl = EscalationController(_agent(llm=llm), _decision(7, agentic=False), 10)
    ctl.task = "refactor the loader"
    block = _run(ctl.before_loop())
    assert "read the config" in block and len(block) <= PLAN_MAX_CHARS
    assert llm.calls == 1
    assert [e["type"] for e in ctl.events] == ["plan"]
    assert _run(ctl.before_loop()) == "" and llm.calls == 1  # once per turn


def test_planner_that_raises_yields_degraded_event_not_exception():
    llm = _LLM(raises=RuntimeError("pool down"))
    ctl = EscalationController(_agent(llm=llm), _decision(9), 14)
    ctl.task = "refactor the loader"
    assert _run(ctl.before_loop()) == ""
    events = ctl.drain_events()
    assert events and events[0]["type"] == "degraded"
    assert events[0]["stage"] == "turn_control" and "pool down" in events[0]["error"]
    assert ctl.drain_events() == []


def test_planner_contract_failure_fails_open():
    ctl = EscalationController(_agent(llm=_LLM("not json at all")), _decision(9), 14)
    ctl.task = "refactor the loader"
    assert _run(ctl.before_loop()) == ""
    assert ctl.events[0]["type"] == "degraded"


def test_reasoner_fault_is_degraded_and_the_turn_still_gets_a_hint():
    class _Broken(_Tools):
        async def execute(self, name, args):
            raise ConnectionError("reasoner unreachable")

    ctl = EscalationController(_agent(tools=_Broken()), _decision(8), max_steps=14)

    async def go():
        return [await ctl.observe(i, "file_search", {"path": "a"}, "[]", True) for i in (1, 2, 3)]

    hints = _run(go())
    assert hints[2] and "[RE-ROUTER]" in hints[2]
    assert any(e["type"] == "degraded" for e in ctl.events)


# -- grounding --------------------------------------------------------------------------

def test_grounding_nudge_once_when_final_comes_before_any_tool():
    ctl = EscalationController(_agent(), _decision(4, grounding=True), 6)
    hint = ctl.grounding_nudge(0, tools_run=0)
    assert hint and "your calendar" in hint
    assert ctl.grounding_nudge(1, tools_run=0) is None
    plain = EscalationController(_agent(), _decision(4, grounding=False), 6)
    assert plain.grounding_nudge(0, tools_run=0) is None


# -- chat_stream effort routing -----------------------------------------------------------

class _StreamProv:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.models: list = []

    async def chat_stream(self, messages, model=None, **kw):
        self.models.append(model)
        if self.fail:
            raise ConnectionError(f"{self.name} down")
        yield StreamChunk(content=f"from {self.name}", model=self.name)
        yield StreamChunk(content="", done=True, model=self.name)


def _router(reasoning=None, cluster=None):
    r = LLMRouter()
    r._model = "orchestrator"
    r._provider = _StreamProv("primary")
    r._provider_name = "primary"
    if reasoning is not None:
        r._reasoning_provider = reasoning
        r._reasoning_provider_name = "reasoning"
        r._reasoning_model = "big-reasoner"
    if cluster is not None:
        r._cluster_provider = cluster
        r._cluster_model = "grid"
    return r


def _collect(router, effort):
    async def go():
        return "".join([c.content async for c in router.chat_stream([], effort=effort)])
    return _run(go())


def test_chat_stream_effort_8_picks_the_reasoning_provider():
    reasoning = _StreamProv("reasoning")
    r = _router(reasoning=reasoning)
    assert _collect(r, 8) == "from reasoning"
    assert reasoning.models == ["big-reasoner"]
    assert _collect(r, 5) == "from primary"
    assert _collect(r, None) == "from primary"


def test_chat_stream_effort_9_picks_the_cluster():
    r = _router(reasoning=_StreamProv("reasoning"), cluster=_StreamProv("cluster"))
    assert _collect(r, 9) == "from cluster"


def test_chat_stream_falls_back_when_the_tier_fails_before_a_chunk():
    r = _router(reasoning=_StreamProv("reasoning", fail=True))
    assert _collect(r, 8) == "from primary"


def test_escalation_controller_keeps_neutral_values_without_agent():
    ctl = EscalationController(agent=None, decision=None, max_steps=6)
    assert _run(ctl.before_loop()) == ""
    assert _run(ctl.on_stall()) is None


# -- review fixes ---------------------------------------------------------------------------

def test_deep_reasoning_error_is_degraded_not_advice():
    class _Deep(_Tools):
        def get(self, name):
            return object() if name == "deep_reasoning" else None

        async def execute(self, name, args):
            self.calls.append((name, args))
            # What ToolRegistry.execute returns when the tool raised.
            return json.dumps({"error": "reasoning pool down"})

    tools = _Deep()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)
    ctl.task = "why"
    hint = _run(ctl.on_stall())
    assert hint is None, hint
    assert tools.calls and tools.calls[0][0] == "deep_reasoning"
    assert not [e for e in ctl.events if e["type"] == "escalated"]
    assert any(e["type"] == "degraded" and "pool down" in e["error"] for e in ctl.events)


def test_rerouter_done_never_emits_the_terminal_done_event():
    ctl = EscalationController(_agent(), _decision(8), max_steps=14)

    async def go():
        await ctl.observe(1, "file_edit", {"path": "a.py"}, "ok", True)
        return await ctl.observe(2, "run_tests", {"path": "tests"}, "3 passed in 0.1s", True)

    hint = _run(go())
    assert hint and "[RE-ROUTER]" in hint
    types = [e["type"] for e in ctl.drain_events()]
    assert "done" not in types and "reroute_done" in types


def test_non_dict_tool_args_still_reach_the_rerouter():
    # The loop passes ``_parsed or {}``: a model that writes a JSON list or a bare string
    # as its arguments hands observe() a non-dict. That must not blind the re-router.
    tools = _Tools()
    ctl = EscalationController(_agent(tools=tools), _decision(8), max_steps=14)

    async def go():
        return [await ctl.observe(i, "web_search", ["same query"], "same result", True)
                for i in (1, 2, 3, 4)]

    hints = _run(go())
    assert not [e for e in ctl.events if e["type"] == "degraded"], ctl.events
    assert [e["type"] for e in ctl.events if e["type"] == "escalate"] == ["escalate"]
    assert hints[3] and len(tools.calls) == 1
