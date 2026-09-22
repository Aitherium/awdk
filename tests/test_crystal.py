"""adk.crystal: context is memory, not a window (orchestration spec section 8).

The store and graph are FAKES here so the tests run wherever the wheel does; the shipped
adapters (awm, awgraph, the scheduler embedder) are exercised by the harness live. The
last two arms are source-level: the agent loop must crystallize inside the Layer 2
summarizer and recall before the typed-memory injection, or the module is decoration.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adk.crystal import (
    Crystal, cosine, fact_key, keyword_score, split_facts,
)

SUMMARY = """Notes to self:
- reflex/state.py: BaseState.__new__ recomputes the var index on every subclass; a module-level cache keyed by class name avoids it.
- Ran pytest tests/units/test_state.py::test_var_index -> FAILED (KeyError on 'computed').
- Decided: do NOT touch reflex/vars/base.py, the failing path is in state.py.
- ok
Outstanding:
- write the cache and re-run the target test
"""


class FakeStore:
    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}

    def put(self, key, value, meta):
        self.rows[key] = (value, meta)

    def scan(self, limit):
        return [(k, v, m) for k, (v, m) in list(self.rows.items())[:limit]]


class BrokenStore:
    def put(self, key, value, meta):
        raise OSError("disk full")

    def scan(self, limit):
        raise OSError("disk gone")


class FakeGraph:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def query(self, text, k):
        self.calls += 1
        return self.rows[:k]


async def fake_embed(texts):
    # A 3-dim "embedding": state-ness, test-ness, decision-ness. Deterministic.
    out = []
    for t in texts:
        low = t.lower()
        out.append([float("state" in low), float("pytest" in low or "test" in low),
                    float("decided" in low or "do not" in low)])
    return out


def test_split_facts_keeps_facts_drops_headings_noise_and_duplicates():
    facts = split_facts(SUMMARY + SUMMARY)
    assert len(facts) == 4, facts
    assert facts[0].startswith("reflex/state.py:")
    assert all(not f.startswith("-") for f in facts)
    assert "ok" not in facts and "Outstanding:" not in facts


def test_fact_key_is_stable_under_whitespace_and_case():
    assert fact_key("Ran  pytest x") == fact_key("ran pytest X")
    assert fact_key("a" * 30) != fact_key("b" * 30)


def test_scores_are_bounded_and_meaningful():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == 0.0 and cosine([], [1]) == 0.0
    assert keyword_score("fix the var index in reflex/state.py", "reflex/state.py index cache") > 0
    assert keyword_score("hello", "unrelated words here") == 0.0


@pytest.mark.asyncio
async def test_crystallize_writes_every_fact_with_its_vector():
    store = FakeStore()
    c = Crystal(scope="aitherium:demiurge:reflex-4129", store=store, embed=fake_embed)
    n = await c.crystallize(SUMMARY)
    assert n == 4 and len(store.rows) == 4
    assert all("vec" in m and m["src"] == "compaction" for _v, m in store.rows.values())
    assert c.telemetry["writes"] == 4 and c.telemetry["compactions"] == 1
    assert c.telemetry["degraded"] == ["awgraph:unbound"]  # the only plane not bound
    # Idempotent: the same summary again rewrites the same keys, never duplicates.
    await c.crystallize(SUMMARY)
    assert len(store.rows) == 4


@pytest.mark.asyncio
async def test_recall_ranks_by_embedding_when_bound_and_caps_the_block():
    store = FakeStore()
    c = Crystal(scope="aitherium:demiurge:reflex-4129", store=store, embed=fake_embed,
                max_chars=260)
    await c.crystallize(SUMMARY)
    block = await c.recall_block("what did the pytest run say?")
    assert block.startswith("[CRYSTAL]")
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert lines and "target test" in lines[0], lines  # nearest neighbour first
    assert len(block) <= 260
    assert c.telemetry["recalls"] == 1 and c.telemetry["recalled_facts"] == len(lines)


@pytest.mark.asyncio
async def test_recall_falls_back_to_keywords_without_an_embedder():
    store = FakeStore()
    c = Crystal(scope="aitherium:demiurge:t", store=store)
    await c.crystallize(SUMMARY)
    block = await c.recall_block("the cache in reflex/state.py")
    first = [ln for ln in block.splitlines() if ln.startswith("- ")][0]
    assert "reflex/state.py" in first
    assert "embed:unbound" in c.telemetry["degraded"]


@pytest.mark.asyncio
async def test_graph_hits_ride_in_the_same_block_and_a_rerun_recalls_before_acting():
    store = FakeStore()
    graph = FakeGraph([{"name": "BaseState.__new__", "path": "reflex/state.py", "line": 412,
                        "signature": "def __new__(cls, *a, **kw)"}])
    first = Crystal(scope="aitherium:demiurge:t", store=store, graph=graph)
    await first.crystallize(SUMMARY)
    # A NEW crystal on the same store (the re-run) recalls the first run's facts.
    rerun = Crystal(scope="aitherium:demiurge:t", store=store, graph=graph)
    block = await rerun.recall_block("fix BaseState var index")
    assert "[CRYSTAL]" in block and "[CODE GRAPH]" in block
    assert "reflex/state.py:412 def __new__" in block
    assert rerun.telemetry["recalled_facts"] >= 1 and rerun.telemetry["graph_hits"] == 1


@pytest.mark.asyncio
async def test_every_plane_degrades_honestly_and_never_raises():
    # No store, no graph, no embedder: nothing recalled, every absence named.
    bare = Crystal(scope="aitherium:demiurge:t")
    assert await bare.crystallize(SUMMARY) == 0
    assert await bare.recall_block("anything") == ""
    assert set(bare.telemetry["degraded"]) == {"awm:unbound", "awgraph:unbound", "embed:unbound"}

    async def bad_embed(texts):
        raise ConnectionError("scheduler down")

    async def bad_query(text, k):
        raise RuntimeError("no index")

    class BadGraph:
        query = staticmethod(bad_query)

    broken = Crystal(scope="aitherium:demiurge:t", store=BrokenStore(), graph=BadGraph(),
                     embed=bad_embed)
    assert await broken.crystallize(SUMMARY) == 0
    assert await broken.recall_block("x") == ""
    assert "embed:ConnectionError" in broken.telemetry["degraded"]
    assert "awm:OSError" in broken.telemetry["degraded"]
    assert "awgraph:RuntimeError" in broken.telemetry["degraded"]


def _agent_tree():
    src = (Path(__file__).resolve().parents[1] / "adk" / "agent.py").read_text(encoding="utf-8")
    return src, ast.parse(src)


def test_the_loop_crystallizes_inside_the_layer2_summarizer():
    """Source-level: _summarize_history awaits crystal.crystallize on the summary it
    returns, so a compaction is a WRITE, not a throwaway message."""
    _src, tree = _agent_tree()
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "_summarize_history"]
    assert fns, "_summarize_history is gone"
    awaited = [c for c in ast.walk(fns[0]) if isinstance(c, ast.Call)
               and getattr(c.func, "attr", "") == "crystallize"]
    assert awaited, "the summarizer never crystallizes its summary"


def test_the_turn_recalls_before_the_typed_memory_injection():
    """Source-level: chat() awaits crystal.recall_block(message) and inserts the block
    at index 1 BEFORE the typed-memory block, i.e. ahead of the first tool call."""
    src, tree = _agent_tree()
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat"]
    assert fns, "chat() is gone"
    calls = [c for c in ast.walk(fns[0]) if isinstance(c, ast.Call)
             and getattr(c.func, "attr", "") == "recall_block"]
    assert calls, "chat() never recalls the crystal"
    i_recall = src.index("self.crystal.recall_block(message)")
    i_typed = src.index("if self._typed:")
    assert i_recall < i_typed, "recall must precede the typed-memory injection"
    assert "AitherAgent" in src and "crystal: \"Crystal | None\" = None" in src
