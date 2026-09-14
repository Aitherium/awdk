"""ConsolidationDaemon — 60s background loop for promotion + decay.

Mirrors AitherOS lib/memory/MemoryConsolidationDaemon.py at small scale.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .reinforcement import MemoryReinforcer
from .store import LocalMemoryStore
from .tiers import MemoryTier

log = logging.getLogger("agent_core.consolidation")


@dataclass
class ConsolidationReport:
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    scanned: int = 0
    promoted: int = 0
    evicted: int = 0
    errors: int = 0

    @property
    def duration_ms(self) -> float:
        if not self.completed_at:
            return 0.0
        return (self.completed_at - self.started_at) * 1000.0


class ConsolidationDaemon:
    """Runs periodic promotion/decay sweeps over the local store."""

    def __init__(
        self,
        store: LocalMemoryStore,
        interval_seconds: float = 60.0,
        batch_size: int = 500,
    ):
        self.store = store
        self.interval = max(5.0, interval_seconds)
        self.batch = batch_size
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_report: ConsolidationReport | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agent_core.consolidation")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _run(self) -> None:
        log.info("ConsolidationDaemon started (interval=%.1fs)", self.interval)
        while not self._stop.is_set():
            try:
                self.last_report = await self.tick()
            except Exception as e:  # broad catch: daemon must never crash
                log.warning("Consolidation tick failed: %s", e)
                if self.last_report:
                    self.last_report.errors += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue
        log.info("ConsolidationDaemon stopped")

    async def tick(self) -> ConsolidationReport:
        report = ConsolidationReport()
        now = time.time()
        memories = await self.store.all_for_consolidation(batch=self.batch)
        report.scanned = len(memories)
        for mem in memories:
            try:
                # Eviction first — keeps the store bounded.
                if MemoryReinforcer.should_evict(mem, now=now):
                    if mem.tier != MemoryTier.IDENTITY:
                        await self.store.delete(mem.memory_id)
                        report.evicted += 1
                        continue
                # Promotion
                if MemoryReinforcer.should_promote(mem):
                    MemoryReinforcer.promote(mem)
                    await self.store.update(mem)
                    report.promoted += 1
            except Exception as e:
                log.debug("consolidation row failed: %s", e)
                report.errors += 1
        report.completed_at = time.time()
        log.debug(
            "Consolidation: scanned=%d promoted=%d evicted=%d errors=%d (%.0fms)",
            report.scanned, report.promoted, report.evicted, report.errors,
            report.duration_ms,
        )
        return report
