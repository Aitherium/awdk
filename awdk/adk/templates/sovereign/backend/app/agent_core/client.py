"""UnifiedMemoryClient — façade that combines LocalMemoryStore + SpiritBridge.

`MEMORY_MODE` env: local | remote | hybrid (default).

- local:  only the SQLite-backed local store (offline-safe, fast).
- remote: only the SpiritBridge (apps that just want a thin proxy to
          AitherSpirit / MemoryHub; rarely used).
- hybrid: write locally always; mirror high-confidence writes upstream;
          read locally first, blend remote recall when local is sparse.

Apps should construct one of these and pass it around as a singleton.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .bridge import SpiritBridge
from .reinforcement import MemoryReinforcer, ReinforcedMemory
from .store import LocalMemoryStore
from .tiers import MemoryTier

log = logging.getLogger("agent_core.client")


class MemoryMode(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    HYBRID = "hybrid"


@dataclass
class RecallResult:
    memory_id: str
    content: str
    tier: MemoryTier
    confidence: float
    source: str  # "local" or "remote"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.memory_id,
            "content": self.content,
            "tier": self.tier.value,
            "confidence": self.confidence,
            "source": self.source,
        }


def _read_mode() -> MemoryMode:
    raw = (os.environ.get("MEMORY_MODE") or "hybrid").strip().lower()
    try:
        return MemoryMode(raw)
    except ValueError:
        return MemoryMode.HYBRID


class UnifiedMemoryClient:
    """One-stop client used by chat handlers and admin endpoints."""

    def __init__(
        self,
        store: LocalMemoryStore | None,
        bridge: SpiritBridge | None = None,
        mode: MemoryMode | None = None,
    ):
        self.mode = mode or _read_mode()
        if self.mode in (MemoryMode.LOCAL, MemoryMode.HYBRID) and store is None:
            raise ValueError(f"mode={self.mode} requires a LocalMemoryStore")
        self.store = store
        self.bridge = bridge or SpiritBridge()

    async def init(self) -> None:
        if self.store is not None:
            await self.store.init()

    # ── Write ──────────────────────────────────────────────────────────────

    async def store_memory(
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
    ) -> str | None:
        mem_id: str | None = None
        if self.store is not None and self.mode in (MemoryMode.LOCAL, MemoryMode.HYBRID):
            mem_id = await self.store.store(
                tenant_id, content,
                tier=tier, conversation_id=conversation_id,
                source=source, confidence=confidence, credibility=credibility,
                metadata=metadata, document_citations=document_citations,
            )
        if self.mode in (MemoryMode.REMOTE, MemoryMode.HYBRID):
            # Only mirror non-trivial, non-blocked content upstream.
            if confidence >= 0.5 and len(content) >= 24:
                await self.bridge.push(
                    tenant_id, content,
                    tier=tier, confidence=confidence,
                    sources=[source] if source else None,
                    metadata=metadata,
                )
        return mem_id

    # ── Read ───────────────────────────────────────────────────────────────

    async def recall(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 10,
        min_confidence: float = 0.30,
    ) -> list[RecallResult]:
        out: list[RecallResult] = []
        if self.store is not None and self.mode in (MemoryMode.LOCAL, MemoryMode.HYBRID):
            local = await self.store.recall(
                tenant_id, query, limit=limit, min_confidence=min_confidence,
            )
            for mem in local:
                # Reinforce on read.
                MemoryReinforcer.reinforce(mem, similarity=0.6, source="recall")
                try:
                    await self.store.update(mem)
                except Exception as e:
                    log.debug("recall reinforce update failed: %s", e)
                out.append(
                    RecallResult(
                        memory_id=mem.memory_id,
                        content=mem.content,
                        tier=mem.tier,
                        confidence=mem.confidence,
                        source="local",
                    )
                )
        # Blend remote when local is thin or in pure-remote mode.
        if self.mode == MemoryMode.REMOTE or (
            self.mode == MemoryMode.HYBRID and len(out) < limit
        ):
            remaining = max(0, limit - len(out))
            remote = await self.bridge.pull(tenant_id, query, limit=remaining or limit)
            seen = {r.memory_id for r in out}
            for m in remote:
                rid = str(m.get("id") or m.get("memory_id") or "")
                content = str(m.get("content") or "").strip()
                if not content or rid in seen:
                    continue
                try:
                    tier_enum = MemoryTier(str(m.get("tier") or "working"))
                except ValueError:
                    tier_enum = MemoryTier.WORKING
                out.append(
                    RecallResult(
                        memory_id=rid or f"remote-{len(out)}",
                        content=content,
                        tier=tier_enum,
                        confidence=float(m.get("confidence") or 0.5),
                        source="remote",
                    )
                )
                if len(out) >= limit:
                    break
        return out

    async def recent(
        self, tenant_id: str, *, limit: int = 20,
        conversation_id: str | None = None,
    ) -> list[ReinforcedMemory]:
        if self.store is None:
            return []
        return await self.store.recent(
            tenant_id, limit=limit, conversation_id=conversation_id,
        )

    # ── Admin ──────────────────────────────────────────────────────────────

    async def stats(self, tenant_id: str | None = None) -> dict[str, Any]:
        local_stats: dict[str, Any] = {}
        if self.store is not None:
            local_stats = await self.store.stats(tenant_id=tenant_id)
        return {
            "mode": self.mode.value,
            "local": local_stats,
            "bridge": await self.bridge.health(),
        }

    async def block_recall_for_document(
        self, tenant_id: str, document_id: str,
    ) -> int:
        if self.store is None:
            return 0
        return await self.store.block_recall_for_document(tenant_id, document_id)

    async def conversations_citing_document(
        self, tenant_id: str, document_id: str,
    ) -> list[str]:
        if self.store is None:
            return []
        return await self.store.conversations_citing_document(tenant_id, document_id)
