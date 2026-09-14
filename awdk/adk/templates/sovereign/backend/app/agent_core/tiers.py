"""Memory tier definitions — ported from AitherOS MemoryReinforcement.

The 5-tier lifecycle (working -> identity) with decay rates, promotion
gates, and source credibility. These constants are the canonical values
used by AitherOS's MemoryReinforcer; keeping them identical means
memories crossing the SpiritBridge use compatible math on both sides.
"""

from __future__ import annotations

from enum import Enum


class MemoryTier(str, Enum):
    """5-tier lifecycle. Lower index = shorter retention, lower confidence."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    ARCHIVAL = "archival"
    IDENTITY = "identity"


# Decay rate per second. Identity = 0 (never decays).
DECAY_RATES: dict[MemoryTier, float] = {
    MemoryTier.WORKING: 0.01,
    MemoryTier.EPISODIC: 0.001,
    MemoryTier.SEMANTIC: 0.0001,
    MemoryTier.ARCHIVAL: 0.00001,
    MemoryTier.IDENTITY: 0.0,
}

# Hits required for promotion to the NEXT tier.
PROMOTION_THRESHOLDS: dict[MemoryTier, int] = {
    MemoryTier.WORKING: 5,
    MemoryTier.EPISODIC: 10,
    MemoryTier.SEMANTIC: 25,
    MemoryTier.ARCHIVAL: 50,
}

# Confidence required for promotion to the NEXT tier.
PROMOTION_CONFIDENCE: dict[MemoryTier, float] = {
    MemoryTier.WORKING: 0.90,
    MemoryTier.EPISODIC: 0.95,
    MemoryTier.SEMANTIC: 0.98,
    MemoryTier.ARCHIVAL: 0.99,
}

# Unique source count required for promotion.
PROMOTION_SOURCES: dict[MemoryTier, int] = {
    MemoryTier.WORKING: 1,
    MemoryTier.EPISODIC: 2,
    MemoryTier.SEMANTIC: 3,
    MemoryTier.ARCHIVAL: 4,
}

# Default TTL in seconds for newly-stored memories.
DEFAULT_TTL: dict[MemoryTier, int] = {
    MemoryTier.WORKING: 300,
    MemoryTier.EPISODIC: 3600,
    MemoryTier.SEMANTIC: 86400,
    MemoryTier.ARCHIVAL: 604800,
    MemoryTier.IDENTITY: 0,  # 0 = no expiry
}

# Per-source credibility weight (0..1).
SOURCE_CREDIBILITY: dict[str, float] = {
    "identity": 1.00,
    "system": 0.99,
    "consolidation": 0.95,
    "verified_document": 0.90,
    "rag": 0.85,
    "user": 0.80,
    "user_confirmed": 0.95,
    "tool_output": 0.75,
    "agent": 0.70,
    "assistant": 0.65,
    "conversation": 0.60,
    "inference": 0.50,
    "speculation": 0.30,
    "unknown": 0.40,
}


def next_tier(tier: MemoryTier) -> MemoryTier | None:
    """Return the tier above `tier`, or None if already IDENTITY."""
    order = [
        MemoryTier.WORKING,
        MemoryTier.EPISODIC,
        MemoryTier.SEMANTIC,
        MemoryTier.ARCHIVAL,
        MemoryTier.IDENTITY,
    ]
    try:
        idx = order.index(tier)
    except ValueError:
        return None
    if idx >= len(order) - 1:
        return None
    return order[idx + 1]
