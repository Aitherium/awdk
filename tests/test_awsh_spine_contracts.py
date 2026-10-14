"""The streaming-turn spine's provider contracts import, accept their kwargs, and return
their neutral values.

Several changes to the streaming loop are built in parallel against these signatures.
A stub that drifted (a renamed kwarg, a None where "" was promised) would surface only
when the branches meet; this pins the shape at the point they all start from.
"""

from __future__ import annotations

import inspect

import pytest

from adk.agent import AitherAgent
from adk.context_assembly import Source, assemble
from adk.crystal import Crystal
from adk.neurons import AutoNeuronFire, NeuronPool
from adk.reach import reach_lines
from adk.turn_control import EscalationController
from adk.turn_memory import after_turn, graph_block, skills_block


def _params(fn) -> dict:
    return dict(inspect.signature(fn).parameters)


def test_stream_react_accepts_decision_and_effort_defaulting_to_none():
    p = _params(AitherAgent.stream_react)
    assert p["decision"].default is None
    assert p["effort"].default is None
    assert p["steering"].default is None  # pre-existing kwarg kept


class _Store:
    def __init__(self):
        self.rows: dict = {}

    def put(self, key, value, meta):
        self.rows[key] = (value, meta)

    def scan(self, limit):
        return [(k, v, m) for k, (v, m) in list(self.rows.items())[:limit]]


async def test_crystal_recall_block_takes_exclude_and_max_chars_and_records_last_recall():
    store = _Store()
    store.put("fact:a", "the cache lives in state.py " * 4, {"kind": "fact"})
    store.put("fact:b", "pytest tests/units/test_state.py failed on KeyError", {"kind": "fact"})
    c = Crystal(scope="aitherium:demiurge:contract", store=store, max_chars=1500)
    assert c.last_recall == {}
    p = _params(Crystal.recall_block)
    assert p["exclude"].default == () and p["max_chars"].default is None
    full = await c.recall_block("state cache test")
    assert full.startswith("[CRYSTAL]")
    assert c.last_recall["facts"] >= 1 and c.last_recall["chars"] > 0
    capped = await c.recall_block("state cache test", exclude=("x",), max_chars=200)
    assert len(capped) <= 200 < len(full)
    assert c.max_chars == 1500  # the per-call cap never leaks into the instance


async def test_neurons_gather_context_new_signature_skip_and_last_fired():
    p = _params(AutoNeuronFire.gather_context)
    assert p["intent_category"].default is None
    assert p["effort"].default is None
    assert p["max_chars"].default == 1200
    assert p["skip"].default == ()
    for name in ("intent_category", "effort", "max_chars", "skip"):
        assert p[name].kind is inspect.Parameter.KEYWORD_ONLY
    auto = AutoNeuronFire(pool=NeuronPool())
    assert auto.last_fired == []
    msg = "remember what we discussed about related topics"
    assert auto.detect_neurons(msg)  # this message does fire neurons
    out = await auto.gather_context(msg, skip=tuple(auto.detect_neurons(msg)))
    assert out == "" and auto.last_fired == []
    await auto.gather_context(msg, intent_category=None, effort=3, max_chars=800)
    assert sorted(auto.last_fired) == sorted(auto.detect_neurons(msg))


async def test_turn_memory_neutral_values():
    assert await graph_block(None, "hello", limit=5) == ""
    assert await skills_block(None, "hello", k=3) == ""
    out = await after_turn(None, message="m", answer="a", tools=[], observations=[], sid=None)
    assert out == {}
    with pytest.raises(TypeError):  # after_turn's data args are keyword-only
        await after_turn(None, "m", "a", [], [], None)  # type: ignore[misc]


async def test_escalation_controller_neutral_values():
    ctl = EscalationController(agent=None, decision=None, max_steps=6)
    assert await ctl.before_loop() == ""
    assert await ctl.observe(1, "read_file", {"path": "x"}, "obs", True) is None
    assert await ctl.on_stall() is None


def test_reach_lines_neutral_value():
    assert reach_lines(None) == ""
    assert reach_lines(None, decision=None) == ""


def test_assemble_keeps_pinned_then_priority_and_evicts_whole():
    srcs = [
        Source("neurons", "n" * 300, priority=1),
        Source("constraints", "c" * 500, priority=0, pinned=True),
        Source("crystal", "f" * 300, priority=5),
        Source("empty", "", priority=9),
    ]
    kept, evicted = assemble(srcs, budget_chars=700, query="q", history_texts=())
    assert [s.name for s in kept] == ["constraints"]  # pinned even though 500 + 300 > 700
    assert evicted == ["crystal", "neurons"]
    kept, evicted = assemble(srcs, budget_chars=1200, query="q")
    assert [s.name for s in kept] == ["constraints", "crystal", "neurons"]
    assert evicted == []
