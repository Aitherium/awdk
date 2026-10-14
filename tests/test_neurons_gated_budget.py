"""Neurons are gated by intent and effort, budgeted, and tolerate a slow neuron.

Each assertion pins one measured failure of the old pool: greetings fired neurons,
generic words ("find", "current") pushed web search into code turns, one slow neuron
timed out the whole batch, the block had no size cap, memory lines were dataclass
reprs, and AITHER_LICENSE_ENFORCE=0 did not unlock the pool.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from adk.memory import MemoryEntry
from adk.neurons import (
    AutoNeuronFire,
    BaseNeuron,
    MemoryNeuron,
    NeuronPool,
    NeuronResult,
    NeuronScaler,
)


class _Fixed(BaseNeuron):
    def __init__(self, name: str, content: str, relevance: float = 0.5, delay: float = 0.0):
        self.name = name
        self._content = content
        self._relevance = relevance
        self._delay = delay
        self.calls = 0

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return NeuronResult(neuron=self.name, content=self._content,
                            relevance=self._relevance)


def _pool_with(*neurons: BaseNeuron) -> NeuronPool:
    pool = NeuronPool()
    for name in list(pool.neurons):
        pool.unregister(name)
    for n in neurons:
        pool.register(n)
    return pool


@pytest.mark.asyncio
async def test_greeting_fires_nothing():
    web = _Fixed("web_search", "should never appear")
    mem = _Fixed("memory", "should never appear")
    auto = AutoNeuronFire(pool=_pool_with(web, mem))
    for msg in ("hi", "hello there", "thanks!"):
        assert auto.detect_neurons(msg) == []
        assert await auto.gather_context(msg) == ""
    assert auto.last_fired == []
    assert web.calls == 0 and mem.calls == 0
    assert NeuronScaler.classify_complexity("hi") == "greeting"
    # The old "always include graph when the agent has one" rule fired on "hi" too.
    agent = MagicMock()
    agent._graph = MagicMock()
    graph = _Fixed("graph", "should never appear")
    with_graph = AutoNeuronFire(pool=_pool_with(graph), agent=agent)
    assert with_graph.detect_neurons("hi") == []
    assert await with_graph.gather_context("hi") == "" and graph.calls == 0


@pytest.mark.asyncio
async def test_low_effort_fires_nothing():
    mem = _Fixed("memory", "a fact")
    auto = AutoNeuronFire(pool=_pool_with(mem))
    msg = "remember what we discussed about the related deploy history"
    assert auto.detect_neurons(msg)  # it would fire at normal effort
    assert await auto.gather_context(msg, effort=2) == ""
    assert auto.last_fired == [] and mem.calls == 0


@pytest.mark.asyncio
async def test_code_intent_fires_no_web_neuron():
    web_search = _Fixed("web_search", "duckduckgo hit")
    web = _Fixed("web", "web hit")
    graph = _Fixed("graph", "- Foo: module foo")
    svc = _Fixed("service_inventory", "- svc-a")
    auto = AutoNeuronFire(pool=_pool_with(web_search, web, graph, svc))
    msg = "find the current status of X"
    for intent in ("code", "CODE"):
        names = auto.detect_neurons(msg, intent)
        assert "web_search" not in names and "web" not in names
        assert "graph" in names  # the code category's neurons do fire
    out = await auto.gather_context(msg, intent_category="CODE", effort=5)
    assert out  # something fired
    assert "web_search" not in auto.last_fired and "web" not in auto.last_fired
    assert web_search.calls == 0 and web.calls == 0
    assert "duckduckgo" not in out
    # A research intent still reaches the web.
    assert "web_search" in auto.detect_neurons(msg, "research")


@pytest.mark.asyncio
async def test_slow_neuron_does_not_drop_the_others():
    slow = _Fixed("web_search", "slow result", relevance=0.9, delay=5.0)
    fast_a = _Fixed("memory", "- key: fast memory", relevance=0.7)
    fast_b = _Fixed("graph", "- Node: fast graph", relevance=0.8)
    pool = _pool_with(slow, fast_a, fast_b)
    t0 = time.perf_counter()
    results = await pool.fire(["web_search", "memory", "graph"], "q", timeout=0.3)
    assert time.perf_counter() - t0 < 2.0
    assert sorted(r.neuron for r in results) == ["graph", "memory"]

    # Per-neuron budget: the batch ceiling is generous, the neuron's own is not.
    slow.budget_s = 0.2
    results = await pool.fire(["web_search", "memory"], "q", timeout=10.0)
    assert [r.neuron for r in results] == ["memory"]

    # Through gather_context, the slow one is missing and the others are present.
    auto = AutoNeuronFire(pool=pool)
    auto.timeout_s = 0.3
    out = await auto.gather_context(
        "search for the latest news we discussed about related memory",
        effort=5,
    )
    assert "fast memory" in out and "fast graph" in out
    assert "slow result" not in out


@pytest.mark.asyncio
async def test_output_respects_max_chars_in_relevance_order():
    big = "\n".join(f"- line {i}: " + "x" * 60 for i in range(40))
    graph = _Fixed("graph", big, relevance=0.9)
    mem = _Fixed("memory", big, relevance=0.7)
    svc = _Fixed("service_inventory", big, relevance=0.3)
    auto = AutoNeuronFire(pool=_pool_with(graph, mem, svc))
    msg = "how does the architecture of the running services relate to history"
    for cap in (1200, 400, 90):
        auto._cache.clear()
        out = await auto.gather_context(msg, effort=5, max_chars=cap)
        assert len(out) <= cap
        assert out.startswith("[KNOWLEDGE GRAPH]")  # highest relevance first
        assert auto.last_budget["chars_kept"] == len(out)
        assert auto.last_budget["chars_dropped"] > 0
    # Skipped names never fire; tool_inventory is always skipped.
    tools = _Fixed("tool_inventory", "- a_tool: does things")
    auto2 = AutoNeuronFire(pool=_pool_with(graph, mem, tools))
    out = await auto2.gather_context("what tools can you use for recall of history",
                                     effort=5, skip=("graph",))
    assert "graph" not in auto2.last_fired and "tool_inventory" not in auto2.last_fired
    assert "AVAILABLE TOOLS" not in out


@pytest.mark.asyncio
async def test_memory_lines_are_key_value_not_reprs():
    agent = MagicMock()
    agent.memory = MagicMock()
    agent.memory.search = AsyncMock(return_value=[
        MemoryEntry(key="deploy_host", value="awnix " + "y" * 400, timestamp=1.0,
                    metadata={"source": "x"}),
        MemoryEntry(key="owner", value="prefers short output"),
    ])
    result = await MemoryNeuron(agent=agent).fire("deploy")
    assert "MemoryEntry(" not in result.content
    assert "timestamp" not in result.content
    lines = result.content.splitlines()
    assert lines[0].startswith("- deploy_host: awnix ")
    assert len(lines[0]) == len("- deploy_host: ") + 200
    assert lines[1] == "- owner: prefers short output"


@pytest.mark.asyncio
async def test_gather_context_fails_open():
    pool = MagicMock()
    pool.fire = AsyncMock(side_effect=RuntimeError("pool exploded"))
    auto = AutoNeuronFire(pool=pool)
    out = await auto.gather_context("remember what we discussed about related topics")
    assert out == ""
    assert "pool exploded" in auto.last_budget.get("error", "")


@pytest.mark.asyncio
async def test_record_consumption_writes_jsonl(tmp_path):
    mem = _Fixed("memory", "- deploy_host: awnix quadlets podman", relevance=0.7)
    graph = _Fixed("graph", "- Unrelated: nothing matching", relevance=0.8)
    auto = AutoNeuronFire(pool=_pool_with(mem, graph))
    await auto.gather_context("remember what we discussed about related deploys",
                              effort=5)
    path = tmp_path / "fires.jsonl"
    rows = auto.record_consumption("q", "It runs on awnix as podman quadlets.", path=path)
    by_name = {r["neuron"]: r for r in rows}
    assert by_name["memory"]["consumed"] is True
    assert by_name["graph"]["consumed"] is False
    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(written) == 2


def test_can_use_auto_neurons_honours_enforcement_off(monkeypatch, tmp_path):
    from adk.licensing import Tier, get_license_manager, reset_license_manager

    for var in ("AITHER_TENANT_SLUG", "AITHER_LICENSE_KEY", "AITHER_LICENSE_FILE",
                "AITHER_LICENSE_PUBLIC_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "0")
    reset_license_manager()
    try:
        lm = get_license_manager()
        assert lm.license.tier is Tier.COMMUNITY
        assert lm.license.entitlements.auto_neurons is False
        assert lm.can_use_auto_neurons() is True
        monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "1")
        assert lm.can_use_auto_neurons() is False  # paid-tier gate unchanged
    finally:
        reset_license_manager()


@pytest.mark.asyncio
async def test_skip_does_not_consume_the_scaler_cap():
    # A "simple" turn (<= 6 words) gets 2 neurons. The spine skips memory and graph
    # when the crystal already answered; those names must not eat the 2 slots.
    mem = _Fixed("memory", "- m: should be skipped", relevance=0.7)
    graph = _Fixed("graph", "- g: should be skipped", relevance=0.8)
    svc = _Fixed("service_inventory", "- svc-a: running", relevance=0.3)
    auto = AutoNeuronFire(pool=_pool_with(mem, graph, svc))
    msg = "recall related running services"
    assert NeuronScaler.max_neurons(msg, None, 5) == 2
    out = await auto.gather_context(msg, effort=5, skip=("memory", "graph"))
    assert auto.last_fired == ["service_inventory"]
    assert "svc-a" in out
    assert mem.calls == 0 and graph.calls == 0


@pytest.mark.asyncio
async def test_cache_hit_restores_that_turns_trace_and_results(tmp_path):
    mem = _Fixed("memory", "- deploy_host: awnix quadlets podman", relevance=0.7)
    svc = _Fixed("service_inventory", "- svc-a: running", relevance=0.3)
    auto = AutoNeuronFire(pool=_pool_with(mem, svc))
    turn_a = "remember what we discussed about deploys"
    turn_b = "which services are running in the fleet now please"
    await auto.gather_context(turn_a, effort=5)
    fired_a = list(auto.last_fired)
    await auto.gather_context(turn_b, effort=5)
    assert auto.last_fired != fired_a
    await auto.gather_context(turn_a, effort=5)  # cache hit
    assert mem.calls == 1
    assert auto.last_fired == fired_a
    rows = auto.record_consumption(turn_a, "awnix podman quadlets", path=tmp_path / "f.jsonl")
    assert {r["neuron"] for r in rows} == {"memory"}


@pytest.mark.asyncio
async def test_cancelled_gather_cancels_its_neurons():
    slow = _Fixed("memory", "late", delay=2.0)
    auto = AutoNeuronFire(pool=_pool_with(slow))
    before = set(asyncio.all_tasks())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            auto.pool.fire(["memory"], "q", timeout=10.0), timeout=0.1)
    await asyncio.sleep(0.05)  # let the cancellations land; the neuron sleeps 2 s
    leaked = [t for t in asyncio.all_tasks() - before if not t.done()]
    assert leaked == []


def test_default_intent_is_neutral_for_the_web_gate():
    # coarse_code_intent returns "DEFAULT" for most input and documents it as neutral.
    # Treating it as a non-research intent meant web search could never fire once the
    # spine passed its intent, even for "search the web for ...".
    auto = AutoNeuronFire(pool=_pool_with(_Fixed("web_search", "x"), _Fixed("memory", "y")))
    msg = "search the web for the latest python release notes please"
    assert "web_search" in auto.detect_neurons(msg, intent_category="DEFAULT")
    assert NeuronScaler.classify_complexity(msg, "DEFAULT") == "research"
    assert auto.detect_neurons(msg, intent_category="DEFAULT") == auto.detect_neurons(msg)
    # A real non-research intent still gates the web out.
    assert "web_search" not in auto.detect_neurons(msg, intent_category="CODE")


@pytest.mark.asyncio
async def test_cache_does_not_grow_without_bound():
    auto = AutoNeuronFire(pool=_pool_with(_Fixed("memory", "remembered fact")))
    auto._cache_ttl = 0.0  # every entry is expired as soon as it is written
    for i in range(50):
        await auto.gather_context(f"remember what we decided about item number {i} last week")
    assert len(auto._cache) <= 1
    auto._cache_ttl = 60.0
    auto._cache_max = 8
    for i in range(50):
        await auto.gather_context(f"remember what we decided about thing number {i} last week")
    assert len(auto._cache) <= 8
