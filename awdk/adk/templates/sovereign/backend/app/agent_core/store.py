"""LocalMemoryStore — async SQLite-backed storage for the 5-tier kernel.

Uses aiosqlite for zero extra deps. Additive tables only; never touches
the host app's existing conversation/document tables. Vendored apps
provide their own DB path so each tenant's memory persists alongside
the rest of its data.

Schema:
    memory_entries(id, tenant_id, conversation_id, tier, content,
                   embedding_blob, hits, confidence, sources_json,
                   credibility, created_at, accessed_at, auto_recall_blocked,
                   metadata_json)
    memory_citations(id, memory_id, document_id, span_start, span_end,
                     status, created_at)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from typing import Any, Iterable

try:
    import aiosqlite
except ImportError:  # pragma: no cover - aiosqlite is the only hard dep
    aiosqlite = None  # type: ignore

from .reinforcement import ReinforcedMemory
from .tiers import MemoryTier

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT,
    tier TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding_blob BLOB,
    hits INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    sources_json TEXT NOT NULL DEFAULT '[]',
    credibility REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    accessed_at REAL NOT NULL,
    auto_recall_blocked INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_mem_tenant_tier ON memory_entries(tenant_id, tier);
CREATE INDEX IF NOT EXISTS idx_mem_conv ON memory_entries(conversation_id);
CREATE INDEX IF NOT EXISTS idx_mem_recall ON memory_entries(tenant_id, auto_recall_blocked);

CREATE TABLE IF NOT EXISTS memory_citations (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    span_start INTEGER NOT NULL DEFAULT 0,
    span_end INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memory_entries(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cit_doc ON memory_citations(document_id, status);
CREATE INDEX IF NOT EXISTS idx_cit_mem ON memory_citations(memory_id);
"""


class LocalMemoryStore:
    """Async SQLite store for the agent_core memory kernel."""

    def __init__(self, db_path: str):
        if aiosqlite is None:
            raise RuntimeError(
                "aiosqlite is required for LocalMemoryStore. "
                "Install with: pip install aiosqlite"
            )
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._initialized = False

    async def init(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._initialized = True

    # ── Write path ─────────────────────────────────────────────────────────

    async def store(
        self,
        tenant_id: str,
        content: str,
        *,
        tier: MemoryTier = MemoryTier.WORKING,
        conversation_id: str | None = None,
        source: str = "conversation",
        confidence: float = 0.5,
        credibility: float = 0.5,
        metadata: dict[str, Any] | None = None,
        document_citations: Iterable[str] | None = None,
    ) -> str:
        """Insert a new memory and return its id."""
        await self.init()
        mem_id = str(uuid.uuid4())
        now = time.time()
        sources_json = json.dumps([source] if source else [])
        meta_json = json.dumps(metadata or {})
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO memory_entries
                   (id, tenant_id, conversation_id, tier, content,
                    hits, confidence, sources_json, credibility,
                    created_at, accessed_at, auto_recall_blocked, metadata_json)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    mem_id, tenant_id, conversation_id, tier.value, content,
                    confidence, sources_json, credibility, now, now, meta_json,
                ),
            )
            if document_citations:
                for doc_id in document_citations:
                    if not doc_id:
                        continue
                    await db.execute(
                        """INSERT INTO memory_citations
                           (id, memory_id, document_id, span_start, span_end,
                            status, created_at)
                           VALUES (?, ?, ?, 0, 0, 'active', ?)""",
                        (str(uuid.uuid4()), mem_id, doc_id, now),
                    )
            await db.commit()
        return mem_id

    async def add_citation(
        self, memory_id: str, document_id: str,
        span_start: int = 0, span_end: int = 0,
    ) -> None:
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO memory_citations
                   (id, memory_id, document_id, span_start, span_end, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (str(uuid.uuid4()), memory_id, document_id,
                 span_start, span_end, time.time()),
            )
            await db.commit()

    # ── Read path ──────────────────────────────────────────────────────────

    async def recall(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 10,
        include_blocked: bool = False,
        min_confidence: float = 0.0,
    ) -> list[ReinforcedMemory]:
        """Naive LIKE-based recall. Embedding-aware recall can layer on top.

        Skips `auto_recall_blocked=1` rows unless explicitly requested.
        """
        await self.init()
        like = f"%{query[:120]}%" if query else "%"
        clauses = ["tenant_id = ?", "confidence >= ?"]
        params: list[Any] = [tenant_id, min_confidence]
        if not include_blocked:
            clauses.append("auto_recall_blocked = 0")
        if query:
            clauses.append("content LIKE ?")
            params.append(like)
        sql = (
            "SELECT id, tier, content, hits, confidence, sources_json, "
            "       credibility, created_at, accessed_at, auto_recall_blocked "
            "FROM memory_entries WHERE " + " AND ".join(clauses) +
            " ORDER BY accessed_at DESC LIMIT ?"
        )
        params.append(limit)
        out: list[ReinforcedMemory] = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cur:
                async for row in cur:
                    out.append(_row_to_memory(row))
        return out

    async def recent(
        self, tenant_id: str, *, limit: int = 20,
        conversation_id: str | None = None,
    ) -> list[ReinforcedMemory]:
        await self.init()
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        sql = (
            "SELECT id, tier, content, hits, confidence, sources_json, "
            "       credibility, created_at, accessed_at, auto_recall_blocked "
            "FROM memory_entries WHERE " + " AND ".join(clauses) +
            " ORDER BY accessed_at DESC LIMIT ?"
        )
        params.append(limit)
        out: list[ReinforcedMemory] = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, params) as cur:
                async for row in cur:
                    out.append(_row_to_memory(row))
        return out

    # ── Update path ────────────────────────────────────────────────────────

    async def update(self, mem: ReinforcedMemory) -> None:
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE memory_entries
                   SET tier = ?, hits = ?, confidence = ?, sources_json = ?,
                       credibility = ?, accessed_at = ?, auto_recall_blocked = ?
                   WHERE id = ?""",
                (
                    mem.tier.value, mem.hits, mem.confidence,
                    json.dumps(sorted(mem.sources)),
                    mem.credibility, mem.accessed_at,
                    1 if mem.auto_recall_blocked else 0,
                    mem.memory_id,
                ),
            )
            await db.commit()

    async def block_recall_for_document(
        self, tenant_id: str, document_id: str,
    ) -> int:
        """Tombstone-side effect: stop auto-recalling memories citing a deleted doc.

        Returns the number of memory_entries flipped to auto_recall_blocked=1.
        """
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """UPDATE memory_entries SET auto_recall_blocked = 1
                   WHERE tenant_id = ?
                     AND id IN (
                         SELECT memory_id FROM memory_citations
                         WHERE document_id = ? AND status = 'active'
                     )""",
                (tenant_id, document_id),
            )
            n = cur.rowcount
            await db.execute(
                """UPDATE memory_citations SET status = 'stale'
                   WHERE document_id = ? AND status = 'active'""",
                (document_id,),
            )
            await db.commit()
        return int(n or 0)

    async def conversations_citing_document(
        self, tenant_id: str, document_id: str,
    ) -> list[str]:
        """Return the set of conversation_ids whose memories cite the doc."""
        await self.init()
        sql = """
            SELECT DISTINCT m.conversation_id
            FROM memory_entries m
            JOIN memory_citations c ON c.memory_id = m.id
            WHERE m.tenant_id = ?
              AND c.document_id = ?
              AND m.conversation_id IS NOT NULL
        """
        out: list[str] = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, (tenant_id, document_id)) as cur:
                async for row in cur:
                    if row[0]:
                        out.append(row[0])
        return out

    # ── Maintenance ────────────────────────────────────────────────────────

    async def all_for_consolidation(self, batch: int = 500) -> list[ReinforcedMemory]:
        """Stream memories in batches for the consolidation daemon."""
        await self.init()
        sql = (
            "SELECT id, tier, content, hits, confidence, sources_json, "
            "       credibility, created_at, accessed_at, auto_recall_blocked "
            "FROM memory_entries ORDER BY accessed_at ASC LIMIT ?"
        )
        out: list[ReinforcedMemory] = []
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(sql, (batch,)) as cur:
                async for row in cur:
                    out.append(_row_to_memory(row))
        return out

    async def delete(self, memory_id: str) -> None:
        await self.init()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM memory_entries WHERE id = ?", (memory_id,)
            )
            await db.commit()

    async def stats(self, tenant_id: str | None = None) -> dict[str, Any]:
        await self.init()
        where, params = ("WHERE tenant_id = ?", [tenant_id]) if tenant_id else ("", [])
        out: dict[str, Any] = {"by_tier": {}, "total": 0, "blocked": 0}
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT tier, COUNT(*) FROM memory_entries {where} GROUP BY tier",
                params,
            ) as cur:
                async for row in cur:
                    out["by_tier"][row[0]] = row[1]
                    out["total"] += row[1]
            async with db.execute(
                f"SELECT COUNT(*) FROM memory_entries "
                f"{where + (' AND ' if where else 'WHERE ')}auto_recall_blocked = 1",
                params,
            ) as cur:
                row = await cur.fetchone()
                out["blocked"] = row[0] if row else 0
        return out


def _row_to_memory(row: Any) -> ReinforcedMemory:
    (
        mid, tier, content, hits, confidence, sources_json,
        credibility, created_at, accessed_at, auto_recall_blocked,
    ) = row
    try:
        sources = set(json.loads(sources_json or "[]"))
    except Exception:
        sources = set()
    try:
        tier_enum = MemoryTier(tier)
    except ValueError:
        tier_enum = MemoryTier.WORKING
    return ReinforcedMemory(
        memory_id=mid,
        content=content,
        tier=tier_enum,
        hits=int(hits or 0),
        confidence=float(confidence or 0.0),
        sources=sources,
        credibility=float(credibility or 0.0),
        created_at=float(created_at or 0.0),
        accessed_at=float(accessed_at or 0.0),
        auto_recall_blocked=bool(auto_recall_blocked),
    )
