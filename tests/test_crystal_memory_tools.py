"""register_crystal_memory_tools: the model can save and query facts, honestly.

Measured need (2026-10-05): the crystal auto-recalled per turn and auto-wrote on
compaction, but the MODEL had no way to save or search a fact mid-turn — a recall with
no way to feed it back is half a memory. These tests pin the two tools and their honest
failure modes: an unbound store or a raised store call returns an ERROR naming the
reason, never an empty success.
"""

import json

from adk.agent import register_crystal_memory_tools
from adk.tools import ToolRegistry


class FakeAgent:
    def __init__(self):
        self.name = "fake"
        self._tools = ToolRegistry()

    def tools_by_name(self):
        return {t.name: t for t in self._tools.list_tools()}


class FakeStore:
    def __init__(self, boom=False):
        self.written = []
        self.boom = boom

    def put_fact(self, fact, meta):
        if self.boom:
            raise RuntimeError("store on fire")
        self.written.append((fact, dict(meta)))
        return ("legacy", None)


class FakeCrystal:
    def __init__(self, store=True, boom=False):
        self.store = FakeStore(boom=boom) if store else None
        self.telemetry = {"degraded": ["awgraph:unbound"]}

    async def recall_facts(self, message, limit=20):
        return ["port 9001 serves the daemon", "the fleet is podman"][:limit]


def _wire(store=True, boom=False):
    agent = FakeAgent()
    crystal = FakeCrystal(store=store, boom=boom)
    n = register_crystal_memory_tools(agent, crystal)
    return agent, crystal, n


def test_registers_both_tools_with_intent_tags():
    agent, _, n = _wire()
    assert n == 2
    tools = agent.tools_by_name()
    assert {"remember_fact", "recall_facts"} <= set(tools)
    assert "question" in tools["remember_fact"].intent_categories
    assert "analysis" in tools["recall_facts"].intent_categories


async def test_remember_writes_through_the_store():
    agent, crystal, _ = _wire()
    out = json.loads(await agent.tools_by_name()["remember_fact"].fn("the sky is blue"))
    assert out["ok"] is True and out["route"] == "legacy"
    fact, meta = crystal.store.written[0]
    assert fact == "the sky is blue"
    assert meta["src"] == "agent-tool"


async def test_remember_reports_an_unbound_store_not_a_success():
    agent, _, _ = _wire(store=False)
    out = json.loads(await agent.tools_by_name()["remember_fact"].fn("x"))
    assert out["error"] == "awm_unbound"
    assert "awgraph:unbound" in out["degraded"]


async def test_remember_refuses_an_empty_fact():
    agent, crystal, _ = _wire()
    out = json.loads(await agent.tools_by_name()["remember_fact"].fn("   "))
    assert out["error"] == "empty_fact"
    assert crystal.store.written == []


async def test_store_fault_is_reported_never_raised():
    agent, _, _ = _wire(boom=True)
    out = json.loads(await agent.tools_by_name()["remember_fact"].fn("x"))
    assert out["error"] == "RuntimeError"
    assert "store on fire" in out["message"]


async def test_recall_returns_facts_and_degraded_planes():
    agent, _, _ = _wire()
    out = json.loads(await agent.tools_by_name()["recall_facts"].fn("fleet"))
    assert any("fleet" in f for f in out["facts"])
    assert out["degraded"] == ["awgraph:unbound"]


def test_memory_tools_are_in_the_core_menu():
    # Measured 2026-10-05: outside the core set, "call remember_fact with the fact:
    # ..." wrote a FILE named after the fact — the 8B reaches for what it can see.
    from adk.tool_selection import CORE_TOOL_NAMES

    assert {"remember_fact", "recall_facts"} <= CORE_TOOL_NAMES


def test_scrub_orphan_think_removes_bare_tags():
    # Measured 2026-10-05: aither-orchestrator opened with a bare "</think>" and it
    # leaked into the answer text and the streamed tokens.
    from adk.agent import scrub_orphan_think

    assert scrub_orphan_think("</think>\n\nThe code graph indicates X") == \
        "\n\nThe code graph indicates X"
    assert scrub_orphan_think("<think>") == ""
    assert scrub_orphan_think("plain answer") == "plain answer"
