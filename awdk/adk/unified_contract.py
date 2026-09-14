"""Unified typed-activation memory contract.

Defines the ONE canonical record format (:class:`MemoryRecord`) that subsumes
the record types of all six AitherOS memory systems, plus the typed weighted
edge (:class:`MemoryEdgeRecord`) and the planning constraint
(:class:`DecisionConstraint`) derived from decision/correction memories.

The central design idea is two *orthogonal* axes that the legacy enums
conflated (Spirit ``MemoryType`` / Reinforcement ``MemoryTier`` / Integration
``MemoryType``):

- :class:`Role`  — what the memory *is*.  Drives **authority**: a correction
  outranks a teaching outranks a discovery.
- :class:`Tier`  — how long the memory *lasts* and how fast it decays.

Converters map each existing backend's record onto :class:`MemoryRecord` (and
back), so the rest of the system can speak one language while persistence stays
delegated to the existing stores.

Python 3.14 note: instance ``@property`` is fine; ``@classmethod @property``
stacking is NOT (removed in 3.14) — none is used here.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ═════════════════════════════════════════════════════════════════════════
# AXES — orthogonal: Role (what it is) × Tier (how long it lasts)
# ═════════════════════════════════════════════════════════════════════════

class Role(str, Enum):
    """What a memory *is* — drives authority during recall.

    Authority ordering (see ``config/memory_unified.yaml`` for the tunable
    multipliers) roughly: correction > decision > teaching/preference >
    fact/procedure > insight > discovery > interaction/task > temporary.
    """
    FACT = "fact"
    DECISION = "decision"
    CORRECTION = "correction"
    TASK = "task"
    TEMPORARY = "temporary"
    PROCEDURE = "procedure"
    INSIGHT = "insight"
    DISCOVERY = "discovery"
    PREFERENCE = "preference"
    INTERACTION = "interaction"
    TEACHING = "teaching"
    FEEDBACK = "feedback"
    IDENTITY = "identity"


class Tier(str, Enum):
    """How long a memory lasts / how fast it decays.

    Mirrors ``ComposableMemoryGraph.Durability`` so the two map 1:1, but
    additionally carries a per-second decay rate (see :data:`TIER_DECAY_RATES`)
    so freshness can be computed as ``e^(-λ·Δt)`` the way
    ``MemoryReinforcement`` does.
    """
    EPHEMERAL = "ephemeral"      # ~15 min
    SESSION = "session"          # ~1 hour
    PERSISTENT = "persistent"    # ~7 days
    RELATIONAL = "relational"    # ~30 days
    TRACE = "trace"              # ~90 days
    PERMANENT = "permanent"      # never decays


# Per-second decay constant λ for freshness = e^(-λ·Δt).
# Defaults chosen so freshness ≈ 0.5 at roughly the tier's nominal lifetime.
# These are overridable from config/memory_unified.yaml (loaded by the scorer).
TIER_DECAY_RATES: Dict[Tier, float] = {
    Tier.EPHEMERAL: math.log(2) / 900.0,        # 15 min half-life
    Tier.SESSION: math.log(2) / 3600.0,         # 1 hour
    Tier.PERSISTENT: math.log(2) / 604800.0,    # 7 days
    Tier.RELATIONAL: math.log(2) / 2592000.0,   # 30 days
    Tier.TRACE: math.log(2) / 7776000.0,        # 90 days
    Tier.PERMANENT: 0.0,                        # no decay
}

# Nominal TTL (seconds) per tier — used to compute expires_at when unset.
TIER_TTL_SECONDS: Dict[Tier, Optional[float]] = {
    Tier.EPHEMERAL: 900.0,
    Tier.SESSION: 3600.0,
    Tier.PERSISTENT: 604800.0,
    Tier.RELATIONAL: 2592000.0,
    Tier.TRACE: 7776000.0,
    Tier.PERMANENT: None,
}


# ═════════════════════════════════════════════════════════════════════════
# EDGES — typed, weighted, time-decaying
# ═════════════════════════════════════════════════════════════════════════

class EdgeType(str, Enum):
    """Typed relationships between memories.

    Superset of ``adk.graph_memory.EdgeType`` so existing graph edges
    map directly.
    """
    SUPERSEDES = "supersedes"
    SUPERSEDED_BY = "superseded_by"
    DERIVED_FROM = "derived_from"
    ELABORATES = "elaborates"
    RELATED = "related"
    TAG_SIBLING = "tag_sibling"
    REINFORCED_BY = "reinforced_by"
    SAME_AGENT = "same_agent"
    SAME_SESSION = "same_session"
    TEMPORAL = "temporal"
    PART_OF = "part_of"


@dataclass
class MemoryEdgeRecord:
    """A typed, weighted edge between two :class:`MemoryRecord` ids.

    Edge weight decays over time (``weight_decay_rate``) so that, per the
    activation model, *reinforced paths stay strong while stale links fade*.
    """
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    weight_decay_rate: float = 1e-7   # per second; 0 = never fades
    created_at: float = field(default_factory=time.time)
    id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.edge_type, str):
            self.edge_type = EdgeType(self.edge_type)
        if not self.id:
            self.id = f"edge_{uuid.uuid4().hex[:12]}"

    def current_weight(self, now: Optional[float] = None) -> float:
        """Edge weight after time-decay (``weight·e^(-λ·Δt)``)."""
        if self.weight_decay_rate <= 0:
            return self.weight
        now = now if now is not None else time.time()
        age = max(0.0, now - self.created_at)
        return self.weight * math.exp(-self.weight_decay_rate * age)


# ═════════════════════════════════════════════════════════════════════════
# THE CANONICAL RECORD
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class MemoryRecord:
    """Canonical unified memory representation across all six systems.

    Fields are grouped: identity, classification (the two axes), authority &
    confidence, temporal/freshness, supersession, graph connectivity, and
    scoping.  Converters below map each backend's native record onto this.
    """

    # ── Identity ────────────────────────────────────────────────────────
    id: str = ""
    content: str = ""
    content_hash: str = ""

    # ── Classification (the two orthogonal axes) ────────────────────────
    role: Role = Role.FACT
    tier: Tier = Tier.PERSISTENT
    domain: str = "general"                 # ComposableMemoryGraph domain
    tags: List[str] = field(default_factory=list)

    # ── Authority & confidence ──────────────────────────────────────────
    source: str = "system"
    source_credibility: float = 0.5
    confidence: float = 0.5
    temporal_consistency: float = 1.0       # 1.0 = uncontradicted; 0.3 = superseded
    reinforcement_count: int = 0
    judge_confidence: float = 0.0           # 0 = judge not consulted
    judge_approved: bool = True

    # ── Temporal / freshness ────────────────────────────────────────────
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    last_reinforced: float = field(default_factory=time.time)
    access_count: int = 0
    valid_from: Optional[float] = None
    valid_until: Optional[float] = None
    expires_at: Optional[float] = None

    # ── Supersession / staleness ────────────────────────────────────────
    stale: bool = False
    superseded_by: Optional[str] = None
    supersedes: List[str] = field(default_factory=list)

    # ── Graph connectivity ──────────────────────────────────────────────
    edges: List[MemoryEdgeRecord] = field(default_factory=list)

    # ── Entity identity (additive; opt-in via EntityResolver) ───────────
    entity_id: Optional[str] = None         # canonical entity this fact is about
    entity_aliases: List[str] = field(default_factory=list)

    # ── Vector / scope / extra ──────────────────────────────────────────
    embedding: Optional[List[float]] = None
    scope_namespace: str = "platform"
    relevance: float = 0.0                  # set by retrieval scoring (transient)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.role, str):
            self.role = Role(self.role)
        if isinstance(self.tier, str):
            self.tier = Tier(self.tier)
        if not self.id:
            self.id = f"mem_{uuid.uuid4().hex[:12]}"
        if not self.content_hash:
            self.content_hash = hashlib.sha256(
                self.content.encode(errors="replace")
            ).hexdigest()[:16]
        if self.expires_at is None:
            ttl = TIER_TTL_SECONDS.get(self.tier)
            if ttl is not None:
                self.expires_at = self.created_at + ttl

    # ── Derived activation properties (instance @property is 3.14-safe) ──

    def freshness(self, now: Optional[float] = None) -> float:
        """``e^(-λ·Δt)`` since last reinforcement, λ from the tier."""
        rate = TIER_DECAY_RATES.get(self.tier, TIER_DECAY_RATES[Tier.PERSISTENT])
        if rate <= 0:
            return 1.0
        now = now if now is not None else time.time()
        elapsed = max(0.0, now - self.last_reinforced)
        return math.exp(-rate * elapsed)

    def reinforcement_bonus(self) -> float:
        """Log-scaled bonus: 2 reinforcements ≈ +0.3, 10 ≈ +1.0 (capped)."""
        if self.reinforcement_count <= 1:
            return 0.0
        return min(1.0, math.log2(self.reinforcement_count) / 3.32)

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if self.valid_until is not None and now > self.valid_until:
            return True
        if self.expires_at is not None and now > self.expires_at:
            return True
        return False

    def is_valid_now(self, now: Optional[float] = None) -> bool:
        """Within the [valid_from, valid_until] window, if any is set."""
        now = now if now is not None else time.time()
        if self.valid_from is not None and now < self.valid_from:
            return False
        if self.valid_until is not None and now > self.valid_until:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "content_hash": self.content_hash,
            "role": self.role.value,
            "tier": self.tier.value,
            "domain": self.domain,
            "tags": list(self.tags),
            "source": self.source,
            "source_credibility": self.source_credibility,
            "confidence": self.confidence,
            "temporal_consistency": self.temporal_consistency,
            "reinforcement_count": self.reinforcement_count,
            "judge_confidence": self.judge_confidence,
            "judge_approved": self.judge_approved,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "last_reinforced": self.last_reinforced,
            "access_count": self.access_count,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "expires_at": self.expires_at,
            "stale": self.stale,
            "superseded_by": self.superseded_by,
            "supersedes": list(self.supersedes),
            "entity_id": self.entity_id,
            "entity_aliases": list(self.entity_aliases),
            "scope_namespace": self.scope_namespace,
            "relevance": self.relevance,
            "metadata": dict(self.metadata),
        }


# ═════════════════════════════════════════════════════════════════════════
# PLANNING CONSTRAINT (derived from DECISION / CORRECTION memories)
# ═════════════════════════════════════════════════════════════════════════

class ConstraintType(str, Enum):
    HARD = "hard"      # must be satisfied — violating plans are rejected
    SOFT = "soft"      # should be satisfied — surfaced to planner
    AVOID = "avoid"    # must not be done
    PREFER = "prefer"  # preferred but optional


@dataclass
class DecisionConstraint:
    """A constraint extracted from a DECISION/CORRECTION memory, consumed by
    the planner so past decisions actually constrain future plans."""
    id: str
    memory_id: str
    description: str
    constraint_type: ConstraintType
    scope: Optional[str] = None
    applies_to: Optional[str] = None    # e.g. a tool/agent name
    priority: float = 0.5

    def __post_init__(self) -> None:
        if isinstance(self.constraint_type, str):
            self.constraint_type = ConstraintType(self.constraint_type)

    def to_planner_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "type": self.constraint_type.value,
            "scope": self.scope,
            "applies_to": self.applies_to,
            "priority": self.priority,
            "source_memory_id": self.memory_id,
        }


# ═════════════════════════════════════════════════════════════════════════
# ROLE / TIER INFERENCE
# ═════════════════════════════════════════════════════════════════════════

# Map the various legacy type strings onto the unified Role.
_LEGACY_ROLE_ALIASES: Dict[str, Role] = {
    # MemoryIntegration.MemoryType
    "user_interaction": Role.INTERACTION,
    "user_preference": Role.PREFERENCE,
    "user_correction": Role.CORRECTION,
    "correction": Role.CORRECTION,
    "fact": Role.FACT,
    "codebase": Role.FACT,
    "decision": Role.DECISION,
    "teaching": Role.TEACHING,
    "discovery": Role.DISCOVERY,
    # Spirit MemoryType
    "insight": Role.INSIGHT,
    "procedure": Role.PROCEDURE,
    "context": Role.INTERACTION,
    "identity": Role.IDENTITY,
    "emotional": Role.INTERACTION,
    "neuron_feedback": Role.FEEDBACK,
    "feedback": Role.FEEDBACK,
    # misc
    "preference": Role.PREFERENCE,
    "interaction": Role.INTERACTION,
    "task": Role.TASK,
    "temporary": Role.TEMPORARY,
}

# Map ComposableMemoryGraph.Durability onto the unified Tier (1:1).
_DURABILITY_TO_TIER: Dict[str, Tier] = {
    "ephemeral": Tier.EPHEMERAL,
    "session": Tier.SESSION,
    "persistent": Tier.PERSISTENT,
    "relational": Tier.RELATIONAL,
    "trace": Tier.TRACE,
    "permanent": Tier.PERMANENT,
}

_TIER_TO_DURABILITY: Dict[Tier, str] = {t: d for d, t in _DURABILITY_TO_TIER.items()}


def infer_role(value: Optional[str], default: Role = Role.FACT) -> Role:
    """Best-effort map a legacy type string onto a unified :class:`Role`."""
    if not value:
        return default
    key = str(value).strip().lower()
    if key in _LEGACY_ROLE_ALIASES:
        return _LEGACY_ROLE_ALIASES[key]
    try:
        return Role(key)
    except ValueError:
        return default


# Content-based role inference — for legacy records that were stored WITHOUT an
# explicit role/type.  Without this every untyped record defaults to FACT, so
# authority can't differentiate and recall labels collapse to ``[NEW]``.
# Inferring at read time types legacy data on recall with zero re-store /
# duplication.  Best-effort and order-sensitive (most authoritative first).
_CONTENT_ROLE_PATTERNS: list[tuple[Role, re.Pattern[str]]] = [
    (Role.CORRECTION, re.compile(
        r"\b(actually|correction|not quite|that'?s wrong|i was wrong|"
        r"instead of|should be|use .+ not |don'?t use)\b", re.I)),
    (Role.DECISION, re.compile(
        r"\b(we (?:decided|chose|will use|agreed)|let'?s use|going with|"
        r"the plan is|decision:|we'?re using)\b", re.I)),
    (Role.PREFERENCE, re.compile(
        r"\b(i (?:prefer|like|want|always)|please always|my preference|"
        r"user prefers)\b", re.I)),
    (Role.TASK, re.compile(
        r"\b(todo|to-do|task:|remember to|need to|follow up|next step)\b", re.I)),
    (Role.TEMPORARY, re.compile(
        r"\b(for now|temporar|note to self|scratch|draft)\b", re.I)),
    (Role.TEACHING, re.compile(
        r"\b(here'?s how|the way to|you should|to do this|lesson:)\b", re.I)),
]


def infer_role_from_content(content: Optional[str]) -> Optional[Role]:
    """Infer a role from message content, or ``None`` if nothing matches."""
    if not content:
        return None
    text = str(content)
    for role, pat in _CONTENT_ROLE_PATTERNS:
        if pat.search(text):
            return role
    return None


def tier_from_durability(durability: Optional[str], default: Tier = Tier.PERSISTENT) -> Tier:
    if not durability:
        return default
    return _DURABILITY_TO_TIER.get(str(durability).strip().lower(), default)


def durability_from_tier(tier: Tier) -> str:
    return _TIER_TO_DURABILITY.get(tier, "persistent")


# ═════════════════════════════════════════════════════════════════════════
# CONVERTERS — backend record ⇄ MemoryRecord  (no field loss for round-trips)
# ═════════════════════════════════════════════════════════════════════════

def from_memory_entry(entry: Any) -> MemoryRecord:
    """Convert a ``ComposableMemoryGraph.MemoryEntry`` → :class:`MemoryRecord`.

    Accepts either the dataclass or a plain dict (duck-typed) so this works
    without importing ComposableMemoryGraph (avoids an import cycle).
    """
    g = _getter(entry)
    metadata = dict(g("metadata", {}) or {})
    # Explicit role/type wins; else infer from CONTENT (legacy records carry no
    # role); else fall back to the domain string (usually → FACT).
    _explicit = metadata.get("role") or metadata.get("memory_type")
    if _explicit:
        role = infer_role(_explicit)
    else:
        role = infer_role_from_content(g("content", "")) or infer_role(g("domain"))
    tier = tier_from_durability(_enum_value(g("durability")))
    scope = g("scope")
    scope_ns = (
        getattr(scope, "namespace", None)
        or metadata.get("_scope_ns")
        or "platform"
    )
    rec = MemoryRecord(
        id=g("id", "") or "",
        content=g("content", "") or "",
        content_hash=g("content_hash", "") or "",
        role=role,
        tier=tier,
        domain=_enum_value(g("domain")) or "general",
        tags=list(g("tags", []) or []),
        source=g("source", "system") or "system",
        confidence=float(metadata.get("confidence", g("confidence", 0.5)) or 0.5),
        temporal_consistency=float(metadata.get("temporal_consistency", 1.0)),
        reinforcement_count=int(metadata.get("reinforcement_count", 0)),
        created_at=float(g("created_at", time.time()) or time.time()),
        last_accessed=float(g("last_accessed", time.time()) or time.time()),
        last_reinforced=float(
            metadata.get("last_reinforced", g("last_accessed", time.time())) or time.time()
        ),
        access_count=int(g("access_count", 0) or 0),
        stale=bool(metadata.get("stale", False) or not metadata.get("active", True)),
        superseded_by=metadata.get("superseded_by"),
        supersedes=([metadata["supersedes"]] if metadata.get("supersedes") else []),
        entity_id=metadata.get("entity_id"),
        entity_aliases=list(metadata.get("entity_aliases", []) or []),
        embedding=g("embedding"),
        scope_namespace=scope_ns,
        relevance=float(g("relevance", 0.0) or 0.0),
        metadata=metadata,
    )
    return rec


def from_context_chunk(chunk: Dict[str, Any]) -> MemoryRecord:
    """Convert a ContextPipeline chunk dict (``MemoryEntry.to_context_chunk``)
    → :class:`MemoryRecord`, for re-ranking an EXISTING candidate set by
    authority without issuing a fresh backend query.

    The chunk carries ``content``/``relevance``/``confidence``/``domain`` but
    drops the stored ``metadata`` (so an explicit role is gone) — role is
    therefore inferred from content, with the domain as last resort.
    """
    md = dict(chunk.get("metadata", {}) or {})
    content = str(chunk.get("content", "") or "")
    explicit = md.get("role") or md.get("memory_type")
    if explicit:
        role = infer_role(explicit)
    else:
        role = infer_role_from_content(content) or infer_role(chunk.get("domain"))
    return MemoryRecord(
        id=str(chunk.get("memory_id") or chunk.get("id") or ""),
        content=content,
        role=role,
        tier=tier_from_durability(md.get("durability")),
        domain=str(chunk.get("domain") or "general"),
        tags=list(chunk.get("tags", []) or []),
        source=str(chunk.get("source", "system") or "system"),
        confidence=float(md.get("confidence", chunk.get("confidence", 0.5)) or 0.5),
        reinforcement_count=int(md.get("reinforcement_count", 0) or 0),
        last_reinforced=float(md.get("last_reinforced", time.time()) or time.time()),
        stale=bool(md.get("stale", False)),
        superseded_by=md.get("superseded_by"),
        scope_namespace=str(chunk.get("scope_namespace") or "platform"),
        relevance=float(chunk.get("relevance", 0.0) or 0.0),
        metadata=md,
    )


def to_memory_entry_kwargs(rec: MemoryRecord) -> Dict[str, Any]:
    """Produce kwargs for ``ComposableMemoryGraph.store(...)`` from a record.

    Activation fields that ``MemoryEntry`` lacks are round-tripped through
    ``metadata`` so no information is lost.
    """
    metadata = dict(rec.metadata)
    metadata.update({
        "role": rec.role.value,
        "confidence": rec.confidence,
        "temporal_consistency": rec.temporal_consistency,
        "reinforcement_count": rec.reinforcement_count,
        "last_reinforced": rec.last_reinforced,
        "stale": rec.stale,
    })
    if rec.superseded_by:
        metadata["superseded_by"] = rec.superseded_by
    if rec.supersedes:
        metadata["supersedes"] = rec.supersedes[0]
    if rec.entity_id:
        metadata["entity_id"] = rec.entity_id
    if rec.entity_aliases:
        metadata["entity_aliases"] = list(rec.entity_aliases)
    return {
        "content": rec.content,
        "domain": rec.domain,
        "durability": durability_from_tier(rec.tier),
        "source": rec.source,
        "tags": list(rec.tags),
        "metadata": metadata,
        "embedding": rec.embedding,
    }


def from_spirit_memory(mem: Any) -> MemoryRecord:
    """Convert a Spirit ``Memory`` (or dict) → :class:`MemoryRecord`."""
    g = _getter(mem)
    mtype = _enum_value(g("memory_type")) or g("type")
    half_life_days = float(g("half_life_days", 30.0) or 30.0)
    tier = _tier_from_half_life(half_life_days)
    strength = float(g("strength", 0.5) or 0.5)
    rec = MemoryRecord(
        id=g("id", "") or "",
        content=g("content", "") or "",
        role=infer_role(mtype),
        tier=tier,
        domain=_enum_value(g("domain")) or "general",
        tags=list(g("tags", []) or []),
        source=g("source", "spirit") or "spirit",
        confidence=strength,
        access_count=int(g("access_count", 0) or 0),
        created_at=float(g("created_at", time.time()) or time.time()),
        last_accessed=float(g("last_accessed", time.time()) or time.time()),
        last_reinforced=float(g("last_accessed", time.time()) or time.time()),
        stale=bool(g("archived", False)),
        superseded_by=g("superseded_by"),
        embedding=g("embedding"),
        metadata=dict(g("metadata", {}) or {}),
    )
    return rec


def from_reinforced_memory(mem: Any) -> MemoryRecord:
    """Convert a ``MemoryReinforcement.ReinforcedMemory`` → :class:`MemoryRecord`."""
    g = _getter(mem)
    tier_val = _enum_value(g("tier")) or "semantic"
    rec = MemoryRecord(
        id=g("id", "") or "",
        content=g("content", "") or "",
        content_hash=g("content_hash", "") or "",
        role=infer_role(g("category")),
        tier=_reinforce_tier_to_tier(tier_val),
        domain=g("category", "general") or "general",
        source=g("source", "system") or "system",
        confidence=float(g("confidence_score", 0.5) or 0.5),
        temporal_consistency=float(g("temporal_consistency", 1.0) or 1.0),
        reinforcement_count=int(g("reinforcement_count", 0) or 0),
        access_count=int(g("access_count", 0) or 0),
        created_at=float(g("created_at", time.time()) or time.time()),
        last_accessed=float(g("last_accessed", time.time()) or time.time()),
        last_reinforced=float(g("last_reinforced", time.time()) or time.time()),
        expires_at=g("expires_at"),
        superseded_by=g("superseded_by"),
        metadata=dict(g("metadata", {}) or {}),
    )
    return rec


# ── conversion helpers ───────────────────────────────────────────────────

# MemoryReinforcement.MemoryTier → unified Tier
_REINFORCE_TIER_MAP: Dict[str, Tier] = {
    "working": Tier.EPHEMERAL,
    "episodic": Tier.SESSION,
    "semantic": Tier.PERSISTENT,
    "archival": Tier.RELATIONAL,
    "identity": Tier.PERMANENT,
}


def _reinforce_tier_to_tier(tier_val: str) -> Tier:
    return _REINFORCE_TIER_MAP.get(str(tier_val).strip().lower(), Tier.PERSISTENT)


def _tier_from_half_life(half_life_days: float) -> Tier:
    """Map a Spirit half-life (days) onto the closest unified Tier."""
    if half_life_days >= 3650:      # ~identity (36500d in Spirit)
        return Tier.PERMANENT
    if half_life_days >= 60:        # teachings/procedures (90-180d)
        return Tier.TRACE
    if half_life_days >= 20:        # default 30d
        return Tier.RELATIONAL
    if half_life_days >= 1:
        return Tier.PERSISTENT
    return Tier.SESSION


def _getter(obj: Any):
    """Return a ``get(key, default)`` that works for dicts and objects."""
    if isinstance(obj, dict):
        return lambda k, d=None: obj.get(k, d)
    return lambda k, d=None: getattr(obj, k, d)


def _enum_value(v: Any) -> Optional[str]:
    if v is None:
        return None
    return getattr(v, "value", v)
