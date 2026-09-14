"""Reinforcement learning for memory — confidence scoring, promotion gates.

Ported from AitherOS lib/memory/MemoryReinforcement.py.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .tiers import (
    DECAY_RATES,
    PROMOTION_CONFIDENCE,
    PROMOTION_SOURCES,
    PROMOTION_THRESHOLDS,
    SOURCE_CREDIBILITY,
    MemoryTier,
    next_tier,
)


@dataclass
class ConfidenceScore:
    """Confidence breakdown for an observed memory match."""

    base: float  # similarity / direct match score (0..1)
    source_weight: float  # SOURCE_CREDIBILITY lookup
    age_decay: float  # 0..1 multiplier from temporal decay
    reinforcement: float  # hits-based boost (0..1)

    @property
    def composite(self) -> float:
        raw = (
            0.35 * self.base
            + 0.30 * self.source_weight
            + 0.15 * self.age_decay
            + 0.20 * self.reinforcement
        )
        return max(0.0, min(1.0, raw))


@dataclass
class ReinforcedMemory:
    """In-memory representation of a memory row plus reinforcement state."""

    memory_id: str
    content: str
    tier: MemoryTier
    hits: int = 0
    confidence: float = 0.5
    sources: set[str] = field(default_factory=set)
    credibility: float = 0.5
    created_at: float = field(default_factory=time.time)
    accessed_at: float = field(default_factory=time.time)
    auto_recall_blocked: bool = False

    def decay_score(self, now: float | None = None) -> float:
        now = now or time.time()
        elapsed = max(0.0, now - self.accessed_at)
        rate = DECAY_RATES[self.tier]
        return math.exp(-rate * elapsed)


def get_source_credibility(source: str) -> float:
    """Look up source credibility with a safe fallback."""
    if not source:
        return SOURCE_CREDIBILITY["unknown"]
    return SOURCE_CREDIBILITY.get(source.lower(), SOURCE_CREDIBILITY["unknown"])


def calculate_match_confidence(
    similarity: float,
    source: str,
    accessed_at: float,
    tier: MemoryTier,
    hits: int,
) -> ConfidenceScore:
    """Compute a ConfidenceScore from observed match metadata."""
    age = max(0.0, time.time() - accessed_at)
    rate = DECAY_RATES[tier]
    age_decay = math.exp(-rate * age)
    reinforcement = 1.0 - math.exp(-hits / 10.0)  # asymptotic 0..1
    return ConfidenceScore(
        base=max(0.0, min(1.0, similarity)),
        source_weight=get_source_credibility(source),
        age_decay=age_decay,
        reinforcement=reinforcement,
    )


class MemoryReinforcer:
    """Promote / demote / decay memories based on observed reinforcement.

    Stateless — operates on ReinforcedMemory instances passed in.
    """

    @staticmethod
    def should_promote(mem: ReinforcedMemory) -> bool:
        """True if memory has earned promotion to the next tier."""
        if mem.tier == MemoryTier.IDENTITY:
            return False
        threshold = PROMOTION_THRESHOLDS.get(mem.tier)
        conf_gate = PROMOTION_CONFIDENCE.get(mem.tier)
        src_gate = PROMOTION_SOURCES.get(mem.tier)
        if threshold is None or conf_gate is None or src_gate is None:
            return False
        return (
            mem.hits >= threshold
            and mem.confidence >= conf_gate
            and len(mem.sources) >= src_gate
        )

    @staticmethod
    def promote(mem: ReinforcedMemory) -> MemoryTier:
        """Move memory to next tier (caller persists the change)."""
        nt = next_tier(mem.tier)
        if nt is None:
            return mem.tier
        mem.tier = nt
        return nt

    @staticmethod
    def reinforce(
        mem: ReinforcedMemory,
        similarity: float,
        source: str,
        now: float | None = None,
    ) -> ConfidenceScore:
        """Register a new observation. Updates hits, confidence, sources."""
        now = now or time.time()
        score = calculate_match_confidence(
            similarity=similarity,
            source=source,
            accessed_at=mem.accessed_at,
            tier=mem.tier,
            hits=mem.hits,
        )
        mem.hits += 1
        mem.accessed_at = now
        mem.sources.add(source or "unknown")
        # Exponential moving average so confidence rises with consistent hits.
        alpha = 0.3
        mem.confidence = (1 - alpha) * mem.confidence + alpha * score.composite
        mem.credibility = max(mem.credibility, get_source_credibility(source))
        return score

    @staticmethod
    def should_evict(mem: ReinforcedMemory, now: float | None = None) -> bool:
        """True if a memory has decayed below recoverable threshold."""
        if mem.tier == MemoryTier.IDENTITY:
            return False
        if mem.auto_recall_blocked and mem.tier == MemoryTier.WORKING:
            # Tombstone-blocked working memories can be dropped quickly.
            return True
        return mem.decay_score(now) < 0.01 and mem.confidence < 0.30
