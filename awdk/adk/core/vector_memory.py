"""Pluggable vector memory.

Adds two protocols and one in-memory implementation:

* :class:`Embedder` — turns text into a fixed-dim float vector.
* :class:`VectorStore` — stores ``(id, vector, payload)`` triples and answers
  k-nearest-neighbour queries by cosine similarity.

The convenience class :class:`VectorMemory` glues them together behind the
existing :class:`~adk.core.memory.Memory` protocol so any agent can
swap ``InMemoryStore`` for vector retrieval without code changes.

Zero hard dependencies. Pure stdlib. If you want a production-grade store
(FAISS, Qdrant, pgvector) implement :class:`VectorStore` against it; the
:class:`Memory` surface stays identical.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol, runtime_checkable

from adk.core.memory import Memory, MemoryRecord


@dataclass(slots=True)
class VectorHit:
    """One scored neighbour from a vector search."""

    id: str
    score: float
    payload: dict[str, Any]


@runtime_checkable
class Embedder(Protocol):
    """Anything that maps text → vector. Async to allow remote backends."""

    @property
    def dim(self) -> int:
        """Embedding dimensionality. Must be stable for the lifetime."""

    async def embed(self, text: str) -> list[float]:
        """Return the embedding for ``text``."""


# A callable form is also accepted for ergonomics in tests.
EmbedFn = Callable[[str], Awaitable[list[float]]]


@runtime_checkable
class VectorStore(Protocol):
    """Vector index protocol. Implementations may be sync or async-friendly."""

    async def add(
        self, id_: str, vector: list[float], payload: dict[str, Any]
    ) -> None: ...

    async def search(self, vector: list[float], *, k: int = 10) -> list[VectorHit]: ...

    async def get(self, id_: str) -> VectorHit | None: ...

    async def delete(self, id_: str) -> bool: ...

    async def clear(self) -> None: ...

    def __len__(self) -> int: ...


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python. Returns 0.0 for zero vectors."""
    if len(a) != len(b):
        raise ValueError(f"dim mismatch: {len(a)} vs {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    id: str
    vector: list[float]
    payload: dict[str, Any]


class InMemoryVectorStore(VectorStore):
    """O(N·D) cosine-similarity store. Fine for ≤ a few thousand vectors.

    For larger corpora swap in a real ANN index. The :class:`VectorStore`
    protocol matches FAISS-style APIs intentionally.
    """

    def __init__(self, *, dim: int | None = None) -> None:
        self._dim = dim
        self._entries: dict[str, _Entry] = {}

    @property
    def dim(self) -> int | None:
        return self._dim

    async def add(
        self, id_: str, vector: list[float], payload: dict[str, Any]
    ) -> None:
        if self._dim is None:
            self._dim = len(vector)
        elif len(vector) != self._dim:
            raise ValueError(f"dim mismatch: got {len(vector)}, want {self._dim}")
        self._entries[id_] = _Entry(id=id_, vector=list(vector), payload=dict(payload))

    async def search(self, vector: list[float], *, k: int = 10) -> list[VectorHit]:
        if k <= 0:
            return []
        scored: list[VectorHit] = []
        for entry in self._entries.values():
            score = _cosine(vector, entry.vector)
            scored.append(VectorHit(id=entry.id, score=score, payload=entry.payload))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    async def get(self, id_: str) -> VectorHit | None:
        entry = self._entries.get(id_)
        if entry is None:
            return None
        return VectorHit(id=entry.id, score=1.0, payload=entry.payload)

    async def delete(self, id_: str) -> bool:
        return self._entries.pop(id_, None) is not None

    async def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def all_ids(self) -> Iterable[str]:
        return self._entries.keys()


# ---------------------------------------------------------------------------
# VectorMemory — implements the Memory protocol over a VectorStore + Embedder
# ---------------------------------------------------------------------------


def _as_embedder(embedder: Embedder | EmbedFn, *, dim: int | None = None) -> Embedder:
    if isinstance(embedder, Embedder):
        return embedder
    if not callable(embedder):
        raise TypeError(f"embedder must be Embedder or async callable, got {type(embedder)!r}")

    class _Adapter:
        def __init__(self) -> None:
            self._dim = dim

        @property
        def dim(self) -> int:
            if self._dim is None:
                raise RuntimeError("embedder dim is unknown until first call")
            return self._dim

        async def embed(self, text: str) -> list[float]:
            vec = await embedder(text)  # type: ignore[misc]
            if self._dim is None:
                self._dim = len(vec)
            return vec

    return _Adapter()


class VectorMemory(Memory):
    """Memory backend that embeds content and stores it in a vector index.

    Search runs the query through the embedder and returns the k nearest
    records. Falls through to recency for ties because the underlying store
    is stable-sorted by score.

    Parameters
    ----------
    embedder:
        :class:`Embedder` or async callable returning a vector for given text.
    store:
        Backing :class:`VectorStore`. Defaults to a fresh
        :class:`InMemoryVectorStore`.
    """

    def __init__(
        self,
        embedder: Embedder | EmbedFn,
        *,
        store: VectorStore | None = None,
    ) -> None:
        self._embedder = _as_embedder(embedder)
        self._store: VectorStore = store if store is not None else InMemoryVectorStore()
        # Recency + content cache keyed by id. Kept tiny — just enough to
        # rehydrate MemoryRecord without re-fetching from the store payload.
        self._order: list[str] = []

    @property
    def store(self) -> VectorStore:
        return self._store

    async def put(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        rid = uuid.uuid4().hex
        vec = await self._embedder.embed(content)
        payload = {
            "content": content,
            "metadata": dict(metadata or {}),
            "created_ns": time.perf_counter_ns(),
        }
        await self._store.add(rid, vec, payload)
        self._order.append(rid)
        return rid

    async def get(self, id_: str) -> MemoryRecord | None:
        hit = await self._store.get(id_)
        if hit is None:
            return None
        return _hit_to_record(hit)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        vec = await self._embedder.embed(query)
        hits = await self._store.search(vec, k=limit)
        return [_hit_to_record(h) for h in hits]

    async def recent(self, *, limit: int = 10) -> list[MemoryRecord]:
        ids = list(reversed(self._order))[:limit]
        out: list[MemoryRecord] = []
        for rid in ids:
            hit = await self._store.get(rid)
            if hit is not None:
                out.append(_hit_to_record(hit))
        return out

    def __len__(self) -> int:
        return len(self._store)


def _hit_to_record(hit: VectorHit) -> MemoryRecord:
    payload = hit.payload
    return MemoryRecord(
        id=hit.id,
        content=str(payload.get("content", "")),
        metadata=dict(payload.get("metadata", {})),
        created_ns=int(payload.get("created_ns", 0)),
    )


__all__ = [
    "Embedder",
    "EmbedFn",
    "InMemoryVectorStore",
    "VectorHit",
    "VectorMemory",
    "VectorStore",
]
