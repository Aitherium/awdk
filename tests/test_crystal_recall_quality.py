"""Crystal recall quality: read scopes, score floor, exclusion, graph budget, shared graph.

Measured motivation (2026-10-11, a live daemon turn):
- recall read only the agent's own scope (1 row) while the owner's project facts sat at
  a SIBLING scope it could never see, and subject-less ``kind=fact`` rows were dropped;
- rows scoring 0.0 still filled the block, and facts already in the history were
  repeated;
- a full fact block left no room for code-graph hits, the hits' paths were relative to
  the graph root, and every agent name paid its own 37-55 s cold graph load on its first
  turn;
- facts saved by the model carried no vector, so they ranked by keyword only.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

pytest.importorskip("awm")

from awm.scope import Scope  # noqa: E402
from awm.store import MemoryStore  # noqa: E402

from adk import crystal as crystal_mod  # noqa: E402
from adk.agent import register_crystal_memory_tools  # noqa: E402
from adk.crystal import AwgraphIndex, Crystal, build_crystal  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

WRITE = "aitherium:helper:desk"
READ = "aitherium:owner:project"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(crystal_mod, "_GRAPH_CACHE", {}, raising=False)
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    # A subject-less fact at the owner's scope (a sibling of the agent's write scope),
    # and one legacy crystal row there too.
    store.remember(Scope.parse(READ), "fleet.runtime",
                   "the fleet runs rootful podman quadlets on one host", kind="fact")
    store.remember(Scope.parse(READ), "f:legacy1",
                   "the daemon listens on port 9001 for harness turns", kind="crystal")
    store.close()
    return path


def _rows_at(path, scope: str) -> list:
    store = MemoryStore(path)
    try:
        return [m for m in store.recall(Scope.parse(scope), limit=1000) if m.scope == scope]
    finally:
        store.close()


async def test_read_scope_facts_are_recalled_only_when_configured(db, monkeypatch):
    monkeypatch.delenv("ADK_CRYSTAL_READ_SCOPES", raising=False)
    alone = build_crystal(WRITE, db=db, embed=False)
    assert await alone.recall_facts("how does the fleet run podman quadlets") == []

    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=db, embed=False)
    facts = await c.recall_facts("how does the fleet run podman quadlets")
    assert "fleet.runtime: the fleet runs rootful podman quadlets on one host" in facts
    assert any("port 9001" in f for f in await c.recall_facts("which port does the daemon use"))


async def test_writes_land_only_in_the_write_scope(db, monkeypatch):
    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=db, embed=False)
    before = len(_rows_at(db, READ))
    n = await c.crystallize("- the build cache lives under the scratch directory today")
    assert n == 1
    c.store.close()
    assert len(_rows_at(db, READ)) == before
    assert any("build cache" in m.value for m in _rows_at(db, WRITE))


async def test_score_zero_rows_are_dropped(db, monkeypatch):
    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=db, embed=False)
    stats: dict = {}
    facts = await c.recall_facts("banana smoothie recipe ideas", stats=stats)
    assert facts == []
    assert stats["dropped"] >= 2
    assert await c.recall_block("banana smoothie recipe ideas") == ""


async def test_excluded_text_is_skipped(db, monkeypatch):
    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=db, embed=False)
    msg = "how does the fleet run podman quadlets"
    assert "podman quadlets" in await c.recall_block(msg)
    history = ["earlier I said: fleet.runtime: the fleet runs rootful podman quadlets "
               "on one host. ok?"]
    block = await c.recall_block(msg, exclude=history)
    assert "rootful podman" not in block
    assert c.last_recall["dropped"] >= 1


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def put(self, key, value, meta):
        self.rows.append((key, value, meta))

    def scan(self, limit):
        return self.rows[:limit]


class _Graph:
    def __init__(self, root, hits, delay=0.0, warm=True):
        self.root = root
        self.hits = hits
        self.delay = delay
        self.is_warm = warm
        self.calls = 0

    async def query(self, text, k):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.hits[:k]


async def test_graph_hits_survive_a_full_fact_block(tmp_path):
    rows = [(f"k{i}", f"the state cache module number {i} stores session state entries",
             {"ts": float(i)}) for i in range(60)]
    hits = [{"name": "StateCache", "path": os.path.join("pkg", "state.py"), "line": 12,
             "signature": "class StateCache"}]
    c = Crystal(scope=WRITE, store=_Store(rows), graph=_Graph(str(tmp_path), hits),
                max_chars=900)
    block = await c.recall_block("state cache session entries")
    assert len(block) <= 900
    assert "[CRYSTAL]" in block and "[CODE GRAPH]" in block
    assert c.last_recall["graph_hits"] == 1 and c.last_recall["facts"] >= 1
    # Graph hit paths are absolute (joined to the graph root), not root-relative.
    want = os.path.normpath(os.path.join(str(tmp_path), "pkg", "state.py"))
    assert f"{want}:12 class StateCache" in block


async def test_two_build_crystal_calls_share_one_index(db, tmp_path, monkeypatch):
    monkeypatch.delenv("ADK_CRYSTAL_READ_SCOPES", raising=False)
    a = build_crystal("aitherium:one:desk", db=db, graph_root=str(tmp_path), embed=False)
    b = build_crystal("aitherium:two:desk", db=db, graph_root=str(tmp_path), embed=False)
    assert isinstance(a.graph, AwgraphIndex)
    assert a.graph is b.graph
    assert crystal_mod.shared_graph(str(tmp_path)) is a.graph


async def test_concurrent_opens_hydrate_once(tmp_path):
    idx = AwgraphIndex(str(tmp_path))
    calls = []

    async def slow_hydrate():
        calls.append(1)
        await asyncio.sleep(0.05)
        idx._ok = True
        return True

    idx._hydrate = slow_hydrate  # type: ignore[method-assign]
    assert await asyncio.gather(idx._open(), idx._open(), idx._open()) == [True] * 3
    assert len(calls) == 1 and idx.is_warm


async def test_cold_graph_returns_within_the_timeout_and_is_marked_degraded(monkeypatch):
    monkeypatch.setattr(crystal_mod, "GRAPH_WAIT_S", 0.2)
    graph = _Graph("/repo", [{"name": "x", "path": "a.py", "line": 1}], delay=5.0,
                   warm=False)
    rows = [("k", "the state cache module stores session entries", {})]
    c = Crystal(scope=WRITE, store=_Store(rows), graph=graph)
    t0 = time.monotonic()
    block = await c.recall_block("state cache session")
    assert time.monotonic() - t0 < 1.5
    assert "[CRYSTAL]" in block and "[CODE GRAPH]" not in block  # the turn still recalls
    assert c.last_recall["graph"] == "cold"
    assert "awgraph:degraded:cold" in c.last_recall["degraded"]


async def test_warm_without_a_loop_is_a_noop(tmp_path):
    assert crystal_mod.warm.__name__ == "warm"
    n = await asyncio.get_running_loop().run_in_executor(
        None, crystal_mod.warm, str(tmp_path))
    assert n == 0


class _Agent:
    def __init__(self):
        self.name = "helper"
        self._tools = ToolRegistry()


async def test_remember_fact_stores_a_vector_when_an_embedder_is_bound(db, monkeypatch):
    monkeypatch.delenv("ADK_CRYSTAL_READ_SCOPES", raising=False)
    c = build_crystal(WRITE, db=db, embed=False)

    async def fake_embed(texts):
        return [[0.6, 0.8, 0.0] for _ in texts]

    c.embed = fake_embed
    agent = _Agent()
    register_crystal_memory_tools(agent, c)
    tool = {t.name: t for t in agent._tools.list_tools()}["remember_fact"]
    out = json.loads(await tool.fn("the release branch is cut every friday at noon"))
    assert out["ok"] is True
    c.store.close()
    saved = [m for m in _rows_at(db, WRITE) if "release branch" in m.value]
    assert saved and saved[0].meta.get("vec") == [0.6, 0.8, 0.0]


async def test_remember_fact_falls_back_to_keyword_when_embedding_fails(db, monkeypatch):
    monkeypatch.delenv("ADK_CRYSTAL_READ_SCOPES", raising=False)
    c = build_crystal(WRITE, db=db, embed=False)

    async def broken_embed(texts):
        raise RuntimeError("embedder down")

    c.embed = broken_embed
    agent = _Agent()
    register_crystal_memory_tools(agent, c)
    tool = {t.name: t for t in agent._tools.list_tools()}["remember_fact"]
    out = json.loads(await tool.fn("the nightly backup runs at two in the morning"))
    assert out["ok"] is True
    c.store.close()
    saved = [m for m in _rows_at(db, WRITE) if "nightly backup" in m.value]
    assert saved and "vec" not in saved[0].meta


# ── review fixes (adversarial pass on WP-B) ─────────────────────────────────


async def test_real_hydrate_does_not_block_the_loop_past_the_timeout(tmp_path, monkeypatch):
    """awgraph's ``_open_graph`` is async in name only: the chunk-cache load and the
    embedding hydrate are synchronous (~37 s). Awaited on the loop, it froze the turn
    and the GRAPH_WAIT_S timer could never fire; it must run off the loop."""
    import importlib.machinery
    import sys
    import types

    class _FakeCG:
        async def hybrid_query(self, text, max_results=10):
            return []

    async def blocking_open_graph(root, build, hydrate=True):
        time.sleep(0.8)  # blocking-ok: mimics the real synchronous awgraph load
        return _FakeCG(), True

    pkg = types.ModuleType("awgraph")
    pkg.__spec__ = importlib.machinery.ModuleSpec("awgraph", None)
    pkg.__path__ = []  # type: ignore[attr-defined]
    cli = types.ModuleType("awgraph.cli")
    cli._open_graph = blocking_open_graph  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "awgraph", pkg)
    monkeypatch.setitem(sys.modules, "awgraph.cli", cli)
    monkeypatch.setattr(crystal_mod, "GRAPH_WAIT_S", 0.1)

    idx = AwgraphIndex(str(tmp_path))
    rows = [("k", "the state cache module stores session entries", {})]
    c = Crystal(scope=WRITE, store=_Store(rows), graph=idx)
    t0 = time.monotonic()
    block = await c.recall_block("state cache session")
    assert time.monotonic() - t0 < 0.6
    assert "[CRYSTAL]" in block
    assert c.last_recall["graph"] == "cold"
    await asyncio.wait_for(idx._open(), 5.0)  # the open finished in the background
    assert idx.is_warm


async def test_landed_memory_files_are_not_recalled_as_facts(tmp_path, monkeypatch):
    """``awm land`` writes a memory file with no ``type`` as ``kind=fact``. Those rows
    belong to the [MEMORY] section (first line, nearest-scope cut); read as subject-less
    facts they landed whole -- description plus body -- in the [CRYSTAL] block too."""
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.remember(Scope.parse(READ), "deploy-notes",
                   "how the fleet deploys podman quadlets\n\nstep one ... step nine",
                   kind="fact", meta={"source": "memory-file", "name": "deploy-notes"})
    store.close()
    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=path, embed=False)
    facts = await c.recall_facts("how does the fleet deploy podman quadlets")
    assert not any("step nine" in f for f in facts)


async def test_one_oversized_fact_does_not_starve_the_rest():
    """Subject-less read-scope facts can be long. The best-ranked one not fitting must
    skip that one fact, not end the section with nothing in it."""
    long_fact = "state cache session entries " + "x" * 2000
    rows = [("big", long_fact, {"ts": 2.0}),
            ("small", "the state cache keeps session entries", {"ts": 1.0})]
    c = Crystal(scope=WRITE, store=_Store(rows), max_chars=600)
    block = await c.recall_block("state cache session entries")
    assert "the state cache keeps session entries" in block
    assert len(block) <= 600


async def test_awm_scans_do_not_block_the_event_loop(db, monkeypatch):
    """Every turn's recall reads the write scope AND each read scope (twice for facts,
    once more, unbounded, for landed memory) -- synchronous sqlite plus a JSON parse of
    every row's vector. Measured 283 ms of loop stall per turn on 1,400 vector rows.
    On the loop it froze every other turn and stream; it must run in a worker thread."""
    monkeypatch.setenv("ADK_CRYSTAL_READ_SCOPES", READ)
    c = build_crystal(WRITE, db=db, embed=False)
    real_recall = c.store._store.recall

    def slow_recall(*a, **kw):
        time.sleep(0.15)  # a big memory.db: sqlite + json.loads of every vector
        return real_recall(*a, **kw)

    monkeypatch.setattr(c.store._store, "recall", slow_recall)
    gaps: list = []
    stop = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.ensure_future(ticker())
    block = await c.recall_block("how does the fleet run podman quadlets")
    stop.set()
    await t
    assert "podman quadlets" in block  # the offloaded scan still recalls
    assert max(gaps) < 0.12, f"loop stalled {max(gaps):.3f}s on awm scans"


def test_landed_scan_is_cached_between_turns(tmp_path, monkeypatch):
    """scan_landed read every row of every read scope on every turn (~10 s on a 2.5k-row
    project scope, past the 3 s crystal budget). It is reused until it ages out."""
    import adk.crystal as crystal_mod

    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.remember(Scope.parse(READ), "deploy-notes", "how the fleet deploys",
                   kind="fact", meta={"source": "memory-file", "name": "deploy-notes"})
    store.close()
    fs = crystal_mod.AwmFactStore(WRITE, db=path)
    fs.read_scopes = [Scope.parse(READ)]
    calls = {"n": 0}
    real = fs._store.recall

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    fs._store.recall = counting
    first = fs.scan_landed(20)
    after_first = calls["n"]
    assert any(k == "deploy-notes" for k, _v, _m in first)
    assert fs.scan_landed(20) == first
    assert calls["n"] == after_first, "the second turn re-read the whole scope"
    monkeypatch.setattr(crystal_mod, "LANDED_CACHE_S", 0.0)
    fs.scan_landed(20)
    assert calls["n"] > after_first, "an aged-out cache reads again"


async def test_a_slow_graph_never_costs_the_turn_its_facts():
    """GRAPH_WAIT_S equalled the turn's 3 s crystal stage, so a slow graph query always
    lost the race and the stage dropped the facts it had recalled (every live turn,
    2026-10-14). The graph wait now sits inside the stage with room to spare."""
    assert crystal_mod.GRAPH_WAIT_S + 1.0 <= crystal_mod.CRYSTAL_STAGE_S
    graph = _Graph("/repo", [{"name": "x", "path": "a.py", "line": 1}], delay=30.0,
                   warm=True)
    rows = [("k", "the state cache module stores session entries", {})]
    c = Crystal(scope=WRITE, store=_Store(rows), graph=graph)
    block = await asyncio.wait_for(c.recall_block("state cache session"),
                                   crystal_mod.CRYSTAL_STAGE_S)
    assert "session entries" in block


async def test_a_graph_query_never_blocks_the_event_loop():
    """awgraph's hybrid_query is async in name only: 4-12 s of numpy on the loop froze
    it, and every other recall stage's timer fired late (2026-10-14). It must run in a
    worker thread so the loop keeps ticking."""
    idx = AwgraphIndex("/repo")

    class _SyncHeavy:
        async def hybrid_query(self, text, max_results=5):
            time.sleep(0.6)  # blocking-ok: simulates awgraph's synchronous scan
            return []

    idx._graph, idx._ok = _SyncHeavy(), True
    ticks: list = []

    async def ticker():
        for _ in range(10):
            t = time.perf_counter()
            await asyncio.sleep(0.02)
            ticks.append(time.perf_counter() - t)

    tk = asyncio.create_task(ticker())
    await asyncio.sleep(0.03)  # the ticker is running before the query starts
    await idx.query("q", 5)
    await tk
    assert max(ticks) < 0.3, f"the loop stalled {max(ticks):.2f}s during a graph query"


async def test_a_slow_embedder_still_recalls_facts_by_keyword():
    """The embedder shares the GPU with the chat models; it answered a query in 3-10 s
    (2026-10-14), past the turn's crystal stage, so the turn lost every fact."""
    async def slow_embed(texts):
        await asyncio.sleep(30)
        return [[1.0, 0.0]] * len(texts)

    rows = [("k", "awsh turns were slow because the orchestrator lane was saturated",
             {"vec": [0.0, 1.0]})]
    c = Crystal(scope=WRITE, store=_Store(rows), embed=slow_embed)
    t0 = time.monotonic()
    block = await asyncio.wait_for(c.recall_block("why were awsh turns slow"),
                                   crystal_mod.CRYSTAL_STAGE_S)
    assert time.monotonic() - t0 < crystal_mod.EMBED_WAIT_S + 1.0
    assert "orchestrator lane was saturated" in block
    assert "embed:slow" in c.telemetry["degraded"]


async def test_keyword_fallback_keeps_few_clear_matches():
    """With no query vector, 36 facts sharing one word each filled a 20k-char block and
    the model answered the greeting instead of the question (2026-10-14)."""
    rows = [(f"k{i}", f"fact {i} about turns in some unrelated board game round", {})
            for i in range(30)]
    rows.append(("hit", "awsh turns were slow because the orchestrator lane was saturated", {}))
    c = Crystal(scope=WRITE, store=_Store(rows), embed=None)
    facts = await c.recall_facts("why were awsh turns slow yesterday")
    assert facts[0].startswith("awsh turns were slow")
    assert len(facts) <= crystal_mod.KEYWORD_FALLBACK_LIMIT
