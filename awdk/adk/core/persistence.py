"""Persistent file-backed memory store.

Drop-in replacement for :class:`InMemoryStore`. Records persist as JSON
on disk and survive process restarts. No external services required.

Use :class:`InMemoryStore` for tests and ephemeral agents; use
:class:`FileStore` for any agent that should remember across runs.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from adk.core.memory import MemoryRecord


class FileStore:
    """Append-only JSON record store.

    Format: one record per line (JSONL). Loaded fully into memory on init,
    so suitable for thousands-of-records workloads, not millions.

    Args:
        path: File path. Parent directory is created if missing.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._records: dict[str, MemoryRecord] = {}
        self._order: list[str] = []
        if self._path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Memory protocol
    # ------------------------------------------------------------------

    async def put(
        self, content: str, *, metadata: dict[str, Any] | None = None
    ) -> str:
        rec = MemoryRecord(
            id=uuid.uuid4().hex,
            content=content,
            metadata=metadata or {},
            created_ns=time.time_ns(),
        )
        async with self._lock:
            self._records[rec.id] = rec
            self._order.append(rec.id)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_record_to_dict(rec)) + "\n")
        return rec.id

    async def get(self, record_id: str) -> MemoryRecord | None:
        return self._records.get(record_id)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        q = query.lower()
        hits: list[MemoryRecord] = []
        for rid in reversed(self._order):
            rec = self._records[rid]
            if q in rec.content.lower() or any(
                q in str(v).lower() for v in rec.metadata.values()
            ):
                hits.append(rec)
                if len(hits) >= limit:
                    break
        return hits

    async def recent(self, *, limit: int = 10) -> list[MemoryRecord]:
        return [self._records[rid] for rid in reversed(self._order[-limit:])]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()
            self._order.clear()
            if self._path.exists():
                self._path.unlink()

    def _load(self) -> None:
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec = _record_from_dict(obj)
                self._records[rec.id] = rec
                self._order.append(rec.id)


def _record_to_dict(rec: MemoryRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "content": rec.content,
        "metadata": rec.metadata,
        "created_ns": rec.created_ns,
    }


def _record_from_dict(obj: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=obj["id"],
        content=obj["content"],
        metadata=obj.get("metadata") or {},
        created_ns=obj.get("created_ns") or time.time_ns(),
    )
