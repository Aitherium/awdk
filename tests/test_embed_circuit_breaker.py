"""2026-10-14: the embedder answered /health while every embedding hung 30 s; every adk
memory stage embeds the question, so every turn waited on it and lost its recall. One
failure opens a shared breaker; until it closes, callers take their fallback at once."""

import asyncio
import time

from adk import crystal as crystal_mod
from adk import embeddings as emb


def _reset():
    emb._BREAKER["until"] = 0.0


def test_one_failure_skips_the_backend_for_the_breaker_window(monkeypatch):
    _reset()
    calls = {"n": 0}

    class _E:
        _backend = "microscheduler"
        _dim = emb.CANONICAL_DIM
        _resolved = True

        async def _resolve(self):
            return None

        async def _embed_openai(self, texts):
            calls["n"] += 1
            raise RuntimeError("HTTP 502: timed out")

    for name in ("embed_texts", "_no_vectors"):
        setattr(_E, name, getattr(emb.__dict__[_owner_name()], name))
    inst = _E()
    asyncio.run(inst.embed_texts(["a"]))
    assert calls["n"] == 1 and emb.embed_breaker_open()
    t0 = time.monotonic()
    vecs, _dim = asyncio.run(inst.embed_texts(["b"]))
    assert calls["n"] == 1, "the open breaker still called the backend"
    assert time.monotonic() - t0 < 0.1 and vecs == [[]]
    _reset()


def _owner_name():
    for name, obj in vars(emb).items():
        if isinstance(obj, type) and hasattr(obj, "embed_texts") and hasattr(obj, "_no_vectors"):
            return name
    raise AssertionError("no embedder class with embed_texts")


async def _slow(texts):
    await asyncio.sleep(30)
    return [[1.0, 0.0] for _ in texts]


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def scan(self, limit):
        return list(self.rows)


def test_crystal_trips_the_breaker_and_then_skips_the_wait(monkeypatch):
    _reset()
    monkeypatch.setattr(crystal_mod, "EMBED_WAIT_S", 0.3)
    rows = [("k", "awsh turns were slow because the orchestrator lane was saturated", {})]
    c = crystal_mod.Crystal(scope="aitherium:t:t", store=_Store(rows), embed=_slow)
    asyncio.run(c.recall_facts("why were awsh turns slow"))
    assert emb.embed_breaker_open()
    t0 = time.monotonic()
    facts = asyncio.run(c.recall_facts("why were awsh turns slow"))
    assert time.monotonic() - t0 < 0.2, "the open breaker still waited on the embedder"
    assert facts and "orchestrator lane" in facts[0]
    _reset()


def test_a_slow_query_embedding_counts_as_a_failure(monkeypatch):
    """The embedder answered in 8-14 s on a busy GPU; slow successes never tripped the
    breaker, so every memory stage waited out its budget (2026-10-14)."""
    monkeypatch.setattr(emb, "EMBED_QUERY_DEADLINE_S", 0.3)
    calls = {"n": 0}

    class _E:
        _backend = "microscheduler"
        _dim = emb.CANONICAL_DIM
        _resolved = True

        async def _resolve(self):
            return None

        async def _embed_openai(self, texts):
            calls["n"] += 1
            await asyncio.sleep(1.0)
            return [[0.0] * emb.CANONICAL_DIM for _ in texts]

    owner = emb.__dict__[_owner_name()]
    for name in ("embed_texts", "_no_vectors"):
        setattr(_E, name, getattr(owner, name))
    t0 = time.monotonic()
    vecs, _dim = asyncio.run(_E().embed_texts(["why slow"]))
    assert time.monotonic() - t0 < 0.8 and vecs == [[]]
    assert emb.embed_breaker_open()
    _reset()
    many = [f"doc {i}" for i in range(emb.EMBED_QUERY_MAX_TEXTS + 1)]
    vecs, _dim = asyncio.run(_E().embed_texts(many))
    assert len(vecs[0]) == emb.CANONICAL_DIM, "bulk ingestion keeps the full timeout"
