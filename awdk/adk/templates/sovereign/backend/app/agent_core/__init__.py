"""agent_core — Portable memory + cognition kernel for standalone agent apps.

Vendor-friendly subset of AitherOS's 5-tier memory architecture and
6-pillar cognition. Designed to drop into any FastAPI + SQLAlchemy app
with zero AitherOS service dependencies.

When AitherOS services are reachable, the optional SpiritBridge promotes
high-confidence memories to AitherSpirit/MemoryHub. When offline, the
local SQLite-backed mini-tiers preserve continuity.

Public surface:
    from agent_core import (
        MemoryTier, DECAY_RATES, PROMOTION_THRESHOLDS,
        LocalMemoryStore, MemoryReinforcer, ConsolidationDaemon,
        UnifiedMemoryClient, SpiritBridge,
        MiniSixPillarsKernel,
        evict_cached_conversations_referencing,
    )
"""

from .tiers import (
    DECAY_RATES,
    DEFAULT_TTL,
    PROMOTION_CONFIDENCE,
    PROMOTION_SOURCES,
    PROMOTION_THRESHOLDS,
    SOURCE_CREDIBILITY,
    MemoryTier,
)
from .reinforcement import (
    ConfidenceScore,
    MemoryReinforcer,
    ReinforcedMemory,
    calculate_match_confidence,
    get_source_credibility,
)
from .store import LocalMemoryStore
from .consolidation import ConsolidationDaemon, ConsolidationReport
from .bridge import SpiritBridge
from .client import UnifiedMemoryClient, MemoryMode
from .pillars import MiniSixPillarsKernel, PillarResult
from .eviction import evict_cached_conversations_referencing, tombstone_document
from .admin import build_memory_router
from .integration import build_recall_context, record_chat_turn

__all__ = [
    "MemoryTier",
    "DECAY_RATES",
    "PROMOTION_THRESHOLDS",
    "PROMOTION_CONFIDENCE",
    "PROMOTION_SOURCES",
    "SOURCE_CREDIBILITY",
    "DEFAULT_TTL",
    "ConfidenceScore",
    "ReinforcedMemory",
    "MemoryReinforcer",
    "calculate_match_confidence",
    "get_source_credibility",
    "LocalMemoryStore",
    "ConsolidationDaemon",
    "ConsolidationReport",
    "SpiritBridge",
    "UnifiedMemoryClient",
    "MemoryMode",
    "MiniSixPillarsKernel",
    "PillarResult",
    "evict_cached_conversations_referencing",
    "tombstone_document",
    "build_memory_router",
    "build_recall_context",
    "record_chat_turn",
]

__version__ = "1.0.0"
