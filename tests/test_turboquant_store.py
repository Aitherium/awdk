"""TurboQuantVectorStore (adk/core/turboquant_store.py) + allowlist search on the
in-memory store. The turbovec-backed tests run only where ``awdk[turbovec]`` is
installed; the allowlist contract and the missing-dependency message run everywhere."""

from __future__ import annotations

import builtins
import random

import pytest

from adk.core.vector_memory import InMemoryVectorStore, VectorMemory

try:
    import turbovec  # noqa: F401

    HAVE_TURBOVEC = True
except ImportError:
    HAVE_TURBOVEC = False

needs_turbovec = pytest.mark.skipif(not HAVE_TURBOVEC, reason="awdk[turbovec] not installed")

DIM = 64


def _vec(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 1.0) for _ in range(DIM)]


def _near(v: list[float], seed: int, eps: float = 0.02) -> list[float]:
    rng = random.Random(seed)
    return [x + rng.gauss(0.0, eps) for x in v]


# ── in-memory store: the same allowlist contract ────────────────────────────


async def test_inmemory_allowlist_restricts_and_fails_closed():
    store = InMemoryVectorStore()
    for i in range(20):
        await store.add(f"r{i}", _vec(i), {"tenant": "a" if i < 10 else "b"})
    q = _near(_vec(15), 99)  # nearest row is r15, tenant b
    top = await store.search(q, k=1)
    assert top[0].id == "r15"
    scoped = await store.search(q, k=3, allow_ids=[f"r{i}" for i in range(10)])
    assert scoped and all(int(h.id[1:]) < 10 for h in scoped)
    assert await store.search(q, k=3, allow_ids=[]) == []
    assert await store.search(q, k=3, allow_ids=["nope"]) == []


async def test_vector_memory_passes_allowlist_through():
    async def embed(text: str) -> list[float]:
        return _vec(sum(map(ord, text)))

    mem = VectorMemory(embed)
    a = await mem.put("alpha")
    b = await mem.put("beta")
    hits = await mem.search("alpha", limit=5, allow_ids=[b])
    assert [h.id for h in hits] == [b]
    assert a != b


# ── missing dependency ──────────────────────────────────────────────────────


def test_missing_turbovec_names_the_extra(monkeypatch):
    from adk.core import turboquant_store

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "turbovec":
            raise ImportError("no turbovec")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"awdk\[turbovec\]"):
        turboquant_store.TurboQuantVectorStore()


def test_bad_bit_width_rejected():
    from adk.core.turboquant_store import TurboQuantVectorStore

    with pytest.raises(ValueError, match="bit_width"):
        TurboQuantVectorStore(bit_width=3)


# ── turbovec-backed ─────────────────────────────────────────────────────────


@needs_turbovec
async def test_add_search_finds_the_nearest():
    from adk.core.turboquant_store import TurboQuantVectorStore

    store = TurboQuantVectorStore()
    for i in range(200):
        await store.add(f"doc-{i}", _vec(i), {"n": i})
    assert len(store) == 200 and store.dim == DIM
    hits = await store.search(_near(_vec(42), 7), k=5)
    assert hits[0].id == "doc-42"
    assert hits[0].payload == {"n": 42}


@needs_turbovec
async def test_allowlist_is_enforced_in_the_index_and_fails_closed():
    from adk.core.turboquant_store import TurboQuantVectorStore

    store = TurboQuantVectorStore()
    await store.add_many((f"doc-{i}", _vec(i), {"tenant": i % 2}) for i in range(100))
    q = _near(_vec(43), 3)  # doc-43 is tenant 1
    even = [f"doc-{i}" for i in range(0, 100, 2)]
    hits = await store.search(q, k=10, allow_ids=even)
    assert len(hits) == 10
    assert all(h.payload["tenant"] == 0 for h in hits)
    assert await store.search(q, k=10, allow_ids=[]) == []
    assert await store.search(q, k=10, allow_ids=["not-held"]) == []
    # an allowlist mixing unknown ids still answers from the known ones
    mixed = await store.search(q, k=10, allow_ids=["not-held", "doc-8"])
    assert [h.id for h in mixed] == ["doc-8"]


@needs_turbovec
async def test_upsert_delete_and_clear():
    from adk.core.turboquant_store import TurboQuantVectorStore

    store = TurboQuantVectorStore()
    await store.add("x", _vec(1), {"v": 1})
    await store.add("x", _vec(2), {"v": 2})  # replace
    assert len(store) == 1
    assert (await store.get("x")).payload == {"v": 2}
    assert (await store.search(_vec(2), k=1))[0].id == "x"
    assert await store.delete("x") is True
    assert await store.delete("x") is False
    assert await store.get("x") is None and await store.search(_vec(2)) == []
    await store.add("y", _vec(3), {})
    await store.clear()
    assert len(store) == 0 and await store.search(_vec(3)) == []


@needs_turbovec
async def test_dim_mismatch_rejected():
    from adk.core.turboquant_store import TurboQuantVectorStore

    store = TurboQuantVectorStore()
    await store.add("a", _vec(1), {})
    with pytest.raises(ValueError, match="dim mismatch"):
        await store.add("b", [1.0, 2.0], {})
    with pytest.raises(ValueError, match="dim mismatch"):
        await store.search([1.0, 2.0])


@needs_turbovec
async def test_save_load_round_trip(tmp_path):
    from adk.core.turboquant_store import TurboQuantVectorStore

    store = TurboQuantVectorStore(bit_width=2)
    await store.add_many((f"d{i}", _vec(i), {"i": i}) for i in range(50))
    await store.delete("d7")
    index_path, side_path = store.save(tmp_path / "mem")
    assert index_path.is_file() and side_path.is_file()

    back = TurboQuantVectorStore.load(tmp_path / "mem")
    assert len(back) == 49 and back.bit_width == 2 and back.dim == DIM
    assert await back.get("d7") is None
    q = _near(_vec(21), 5)
    assert [h.id for h in await back.search(q, k=3)] == [h.id for h in await store.search(q, k=3)]
    await back.add("new", _vec(999), {})  # ids keep counting after a reload
    assert len(back) == 50


@needs_turbovec
async def test_quantized_index_is_smaller_than_float32():
    from adk.core.turboquant_store import TurboQuantVectorStore

    n, dim = 2000, 256
    store = TurboQuantVectorStore()
    rng = random.Random(0)
    await store.add_many(
        (str(i), [rng.gauss(0, 1) for _ in range(dim)], {}) for i in range(n)
    )
    float32_bytes = n * dim * 4
    # 4-bit codes are 1/8 of float32; the index also carries a fixed rotation/codebook
    # overhead, so at this small size the cut is ~3.4x (7x at 20k x 768).
    assert store.index_nbytes() * 3 < float32_bytes


@needs_turbovec
async def test_vector_memory_over_turboquant():
    from adk.core.turboquant_store import TurboQuantVectorStore

    async def embed(text: str) -> list[float]:
        return _vec(sum(map(ord, text)))

    mem = VectorMemory(embed, store=TurboQuantVectorStore())
    rid = await mem.put("the retry policy is exponential", metadata={"k": 1})
    await mem.put("unrelated text about lunch")
    got = await mem.search("the retry policy is exponential", limit=1)
    assert got[0].id == rid and got[0].metadata == {"k": 1}
