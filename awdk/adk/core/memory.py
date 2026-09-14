"""Memory primitives.

Protocol with a tiny surface (``put``, ``get``, ``search``, ``recent``).
Implementations:

* :class:`InMemoryStore` — process-local. For tests and ephemeral agents.

Larger stores (SQLite, AitherMemoryStore, RagMemory with graphs) land in
slice D. The protocol stays stable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable


@dataclass(slots=True)
class MemoryRecord:
    """One stored item."""

    id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_ns: int = field(default_factory=time.perf_counter_ns)


@runtime_checkable
class Memory(Protocol):
    """Storage protocol every agent memory backend implements."""

    async def put(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        """Store ``content`` and return its id."""

    async def get(self, id_: str) -> MemoryRecord | None:
        """Fetch by id, or ``None`` if absent."""

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        """Return up to ``limit`` records relevant to ``query``."""

    async def recent(self, *, limit: int = 10) -> list[MemoryRecord]:
        """Return up to ``limit`` most-recent records."""


class InMemoryStore(Memory):
    """Process-local memory. Linear-scan search by substring + metadata.

    Don't ship this to production. It's for tests and short-lived agents.
    """

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._order: list[str] = []

    async def put(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        rid = uuid.uuid4().hex
        self._records[rid] = MemoryRecord(id=rid, content=content, metadata=dict(metadata or {}))
        self._order.append(rid)
        return rid

    async def get(self, id_: str) -> MemoryRecord | None:
        return self._records.get(id_)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        needle = query.casefold()
        hits: list[MemoryRecord] = []
        # Walk newest-first so ties resolve in recency order.
        for rid in reversed(self._order):
            rec = self._records[rid]
            if needle in rec.content.casefold() or any(
                needle in str(v).casefold() for v in rec.metadata.values()
            ):
                hits.append(rec)
                if len(hits) >= limit:
                    break
        return hits

    async def recent(self, *, limit: int = 10) -> list[MemoryRecord]:
        ids = list(reversed(self._order))[:limit]
        return [self._records[i] for i in ids]

    def __len__(self) -> int:
        return len(self._records)

    def all(self) -> Iterable[MemoryRecord]:
        return (self._records[i] for i in self._order)
