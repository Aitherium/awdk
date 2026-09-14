"""Activation scoring for unified typed-activation memory.

This module is the brain of "authority": it combines a memory's **role**,
**confidence**, **freshness**, **supersession status**, and **graph activation**
into one comparable score, computes the human-readable **labels** the LLM sees,
and implements **spreading activation** and **supersession cascade** over the
typed weighted graph.

It deliberately *re-implements* (not imports) the confidence/freshness formulas
from ``MemoryReinforcement`` so the scorer is a standalone, well-tested unit;
the source-credibility table is reused from the canonical quality config when
available.  All weights/multipliers are config-driven via
``config/memory_unified.yaml`` with conservative code defaults (start near 1.0,
tune from logged score breakdowns — see the plan's risk notes).
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.unified_contract import (
    EdgeType,
    MemoryEdgeRecord,
    MemoryRecord,
    Role,
    Tier,
)

import logging
logger = logging.getLogger("adk.graph_rag.activation_scoring")


# ═════════════════════════════════════════════════════════════════════════
# DEFAULTS (overridable from config/memory_unified.yaml)
# ═════════════════════════════════════════════════════════════════════════

# Role authority multipliers — start conservative.  A correction outranks a
# decision outranks a teaching/preference, etc.
DEFAULT_ROLE_AUTHORITY: Dict[str, float] = {
    Role.CORRECTION.value: 1.3,
    Role.DECISION.value: 1.2,
    Role.TEACHING.value: 1.1,
    Role.PREFERENCE.value: 1.1,
    Role.IDENTITY.value: 1.15,
    Role.FACT.value: 1.0,
    Role.PROCEDURE.value: 1.0,
    Role.INSIGHT.value: 0.9,
    Role.DISCOVERY.value: 0.8,
    Role.INTERACTION.value: 0.7,
    Role.FEEDBACK.value: 0.6,
    Role.TASK.value: 0.8,
    Role.TEMPORARY.value: 0.5,
}

# Tier priority weights — permanent/persistent memories weigh more than
# ephemeral scratch.
DEFAULT_TIER_WEIGHT: Dict[str, float] = {
    Tier.PERMANENT.value: 1.5,
    Tier.PERSISTENT.value: 1.0,
    Tier.RELATIONAL.value: 0.9,
    Tier.TRACE.value: 0.6,
    Tier.SESSION.value: 0.7,
    Tier.EPHEMERAL.value: 0.4,
}

# How much each role *spreads* activation to neighbours (decision/correction
# light up their neighbourhood; temporary memory barely spreads).
DEFAULT_ROLE_SPREAD: Dict[str, float] = {
    Role.DECISION.value: 1.3,
    Role.CORRECTION.value: 1.2,
    Role.TEACHING.value: 1.1,
    Role.IDENTITY.value: 1.1,
    Role.FACT.value: 1.0,
    Role.PROCEDURE.value: 1.0,
    Role.INSIGHT.value: 0.9,
    Role.DISCOVERY.value: 0.8,
    Role.PREFERENCE.value: 0.7,
    Role.INTERACTION.value: 0.6,
    Role.FEEDBACK.value: 0.4,
    Role.TASK.value: 0.5,
    Role.TEMPORARY.value: 0.3,
}

# Per-edge-type activation multiplier (spreading) and cascade-decay weight.
DEFAULT_EDGE_SPREAD: Dict[str, float] = {
    EdgeType.SUPERSEDED_BY.value: 1.2,
    EdgeType.REINFORCED_BY.value: 1.1,
    EdgeType.DERIVED_FROM.value: 1.0,
    EdgeType.PART_OF.value: 1.0,
    EdgeType.ELABORATES.value: 0.9,
    EdgeType.RELATED.value: 0.8,
    EdgeType.TAG_SIBLING.value: 0.7,
    EdgeType.SAME_SESSION.value: 0.6,
    EdgeType.SAME_AGENT.value: 0.5,
    EdgeType.TEMPORAL.value: 0.5,
    EdgeType.SUPERSEDES.value: 0.3,
}

DEFAULT_CASCADE_EDGE_WEIGHT: Dict[str, float] = {
    EdgeType.RELATED.value: 1.0,
    EdgeType.ELABORATES.value: 1.0,
    EdgeType.PART_OF.value: 0.9,
    EdgeType.DERIVED_FROM.value: 0.8,
    EdgeType.REINFORCED_BY.value: 0.6,
    EdgeType.TAG_SIBLING.value: 0.5,
    EdgeType.SAME_AGENT.value: 0.4,
    EdgeType.SAME_SESSION.value: 0.4,
    EdgeType.TEMPORAL.value: 0.3,
}

# Supersession demotion / staleness factors applied to the combined score.
SUPERSEDED_FACTOR = 0.3
STALE_FACTOR = 0.5
STALE_FRESHNESS_THRESHOLD = 0.3   # below this + stale flag → [STALE] label

# Spreading-activation bounds (token-budget guard).
ACTIVATION_THRESHOLD = 0.1
ACTIVATION_MAX_RECORDS = 50
ACTIVATION_MAX_HOPS = 2
ACTIVATION_DECAY_PER_HOP = 0.7
ACTIVATION_BONUS_CAP = 0.3

# Supersession cascade bounds.
CASCADE_DECAY_FACTOR = 0.7
CASCADE_MAX_HOPS = 2
CASCADE_STALE_FRESHNESS = 0.2
CASCADE_EDGE_TYPES = (
    EdgeType.RELATED.value,
    EdgeType.ELABORATES.value,
    EdgeType.DERIVED_FROM.value,
    EdgeType.PART_OF.value,
)


# ═════════════════════════════════════════════════════════════════════════
# CONFIG LOADING
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class ScoringConfig:
    role_authority: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ROLE_AUTHORITY))
    tier_weight: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIER_WEIGHT))
    role_spread: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ROLE_SPREAD))
    edge_spread: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_EDGE_SPREAD))
    cascade_edge_weight: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CASCADE_EDGE_WEIGHT))
    superseded_factor: float = SUPERSEDED_FACTOR
    stale_factor: float = STALE_FACTOR
    activation_threshold: float = ACTIVATION_THRESHOLD
    activation_max_records: int = ACTIVATION_MAX_RECORDS
    activation_max_hops: int = ACTIVATION_MAX_HOPS
    activation_decay_per_hop: float = ACTIVATION_DECAY_PER_HOP
    activation_bonus_cap: float = ACTIVATION_BONUS_CAP
    cascade_decay_factor: float = CASCADE_DECAY_FACTOR
    cascade_max_hops: int = CASCADE_MAX_HOPS
    cascade_stale_freshness: float = CASCADE_STALE_FRESHNESS

    @staticmethod
    def from_yaml(path: Optional[str] = None) -> "ScoringConfig":
        cfg = ScoringConfig()
        path = path or _default_config_path()
        if not path or not os.path.exists(path):
            return cfg
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:  # pragma: no cover - config is optional
            logger.debug("[scorer] config load failed (%s); using defaults", exc)
            return cfg

        def _merge(attr: str, key: str) -> None:
            block = data.get(key)
            if isinstance(block, dict):
                getattr(cfg, attr).update({str(k): float(v) for k, v in block.items()})

        _merge("role_authority", "role_authority")
        _merge("tier_weight", "tier_weight")
        _merge("role_spread", "role_spread")
        _merge("edge_spread", "edge_spread")
        _merge("cascade_edge_weight", "cascade_edge_weight")
        for scalar in (
            "superseded_factor", "stale_factor", "activation_threshold",
            "activation_decay_per_hop", "activation_bonus_cap",
            "cascade_decay_factor", "cascade_stale_freshness",
        ):
            if scalar in data:
                setattr(cfg, scalar, float(data[scalar]))
        for int_scalar in ("activation_max_records", "activation_max_hops", "cascade_max_hops"):
            if int_scalar in data:
                setattr(cfg, int_scalar, int(data[int_scalar]))
        return cfg


def _default_config_path() -> Optional[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    # lib/memory/unified -> AitherOS/config/memory_unified.yaml
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    candidate = os.path.join(root, "config", "memory_unified.yaml")
    return candidate


# ═════════════════════════════════════════════════════════════════════════
# THE SCORER
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class ScoreBreakdown:
    """Transparent, loggable breakdown of how a record's score was computed."""
    record_id: str
    authority: float
    confidence: float
    freshness: float
    supersession_factor: float
    activation_bonus: float
    tier_weight: float
    combined: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "authority": round(self.authority, 4),
            "confidence": round(self.confidence, 4),
            "freshness": round(self.freshness, 4),
            "supersession_factor": round(self.supersession_factor, 4),
            "activation_bonus": round(self.activation_bonus, 4),
            "tier_weight": round(self.tier_weight, 4),
            "combined": round(self.combined, 4),
        }


class MemoryScorer:
    """Computes authority-aware scores, labels, spreading activation, and
    supersession cascade for :class:`MemoryRecord` objects."""

    def __init__(self, config: Optional[ScoringConfig] = None):
        self.config = config or ScoringConfig.from_yaml()

    # ── confidence (mirrors MemoryReinforcement.ConfidenceScore.total) ──

    def effective_confidence(self, rec: MemoryRecord) -> float:
        """Blend stored confidence with reinforcement + temporal consistency.

        Mirrors the multi-factor idea in ``MemoryReinforcement`` without
        importing it: a heavily reinforced, uncontradicted memory gains
        confidence; a contradicted (superseded) one loses it.  A consulted
        judge that rejected caps the result.
        """
        base = rec.confidence * rec.temporal_consistency
        base = min(1.0, base + 0.2 * rec.reinforcement_bonus())
        if rec.judge_confidence > 0:
            if not rec.judge_approved:
                return min(0.5, base * 0.5)
            return min(1.0, base * 0.6 + rec.judge_confidence * 0.4)
        return max(0.0, min(1.0, base))

    # ── the combined authority score ────────────────────────────────────

    def score(
        self,
        rec: MemoryRecord,
        activation: float = 0.0,
        now: Optional[float] = None,
    ) -> ScoreBreakdown:
        now = now if now is not None else time.time()
        authority = self.config.role_authority.get(rec.role.value, 1.0)
        confidence = self.effective_confidence(rec)
        freshness = rec.freshness(now)
        tier_w = self.config.tier_weight.get(rec.tier.value, 1.0)

        if rec.superseded_by:
            supersession = self.config.superseded_factor
        elif rec.stale and freshness < STALE_FRESHNESS_THRESHOLD:
            supersession = self.config.stale_factor
        else:
            supersession = 1.0

        activation_bonus = min(self.config.activation_bonus_cap, max(0.0, activation) * 0.1)

        # base relevance (semantic/lexical match) flows in via rec.relevance;
        # default 1.0 if the caller hasn't set it so pure-authority ordering works.
        base_relevance = rec.relevance if rec.relevance > 0 else 1.0
        combined = (
            base_relevance
            * authority
            * max(0.05, confidence)
            * max(0.05, freshness)
            * supersession
            * tier_w
            * (1.0 + activation_bonus)
        )
        return ScoreBreakdown(
            record_id=rec.id,
            authority=authority,
            confidence=confidence,
            freshness=freshness,
            supersession_factor=supersession,
            activation_bonus=activation_bonus,
            tier_weight=tier_w,
            combined=combined,
        )

    # ── labels the LLM sees ─────────────────────────────────────────────

    def labels(self, rec: MemoryRecord, now: Optional[float] = None) -> List[str]:
        now = now if now is not None else time.time()
        out: List[str] = []
        if rec.role in (Role.CORRECTION, Role.DECISION, Role.TEACHING):
            out.append(rec.role.value.upper())
        if rec.superseded_by:
            out.append(f"SUPERSEDED:{rec.superseded_by}")
        if rec.stale and rec.freshness(now) < STALE_FRESHNESS_THRESHOLD:
            out.append("STALE")
        if rec.supersedes:
            out.append("DEPRECATING")
        if rec.reinforcement_count >= 5:
            out.append(f"REINFORCED:{rec.reinforcement_count}")
        conf = self.effective_confidence(rec)
        if conf >= 0.9:
            out.append(f"CONFIDENT:{conf:.2f}")
        if now - rec.created_at < 3600:
            out.append("NEW")
        if rec.judge_confidence > 0 and rec.judge_approved:
            out.append("JUDGE_OK")
        return out

    def label_content(self, rec: MemoryRecord, now: Optional[float] = None) -> str:
        """The record's content prefixed with its authority labels."""
        labels = self.labels(rec, now)
        prefix = " ".join(f"[{l}]" for l in labels)
        return f"{prefix} {rec.content}".strip() if prefix else rec.content

    # ── spreading activation ────────────────────────────────────────────

    def spread_activation(
        self,
        seed_ids: List[str],
        edges_of: Callable[[str], List[MemoryEdgeRecord]],
        role_of: Callable[[str], Optional[Role]],
        now: Optional[float] = None,
    ) -> Dict[str, float]:
        """Iterative activation diffusion over the typed weighted graph.

        ``edges_of(id)`` returns outgoing edges from a node; ``role_of(id)``
        returns the target's role (or None).  Returns ``{node_id: activation}``
        for nodes reached beyond the seeds, bounded by config caps.

        Each hop multiplies::

            source_activation × edge.current_weight × role_spread(target)
            × edge_spread(type) × decay_per_hop
        """
        now = now if now is not None else time.time()
        cfg = self.config
        activated: Dict[str, float] = {sid: 1.0 for sid in seed_ids}
        frontier: Dict[str, float] = dict(activated)

        for hop in range(1, cfg.activation_max_hops + 1):
            decay = cfg.activation_decay_per_hop ** hop
            next_frontier: Dict[str, float] = {}
            for node_id, act in frontier.items():
                for edge in edges_of(node_id) or []:
                    tgt = edge.target_id
                    if tgt in activated:
                        continue
                    role = role_of(tgt)
                    role_mult = cfg.role_spread.get(role.value, 1.0) if role else 1.0
                    edge_mult = cfg.edge_spread.get(edge.edge_type.value, 0.5)
                    new_act = (
                        act
                        * edge.current_weight(now)
                        * role_mult
                        * edge_mult
                        * decay
                    )
                    if new_act < cfg.activation_threshold:
                        continue
                    if new_act > next_frontier.get(tgt, 0.0):
                        next_frontier[tgt] = new_act
            # merge, respecting the global record cap
            for tgt, act in sorted(next_frontier.items(), key=lambda kv: -kv[1]):
                if tgt in activated:
                    continue
                if len(activated) >= cfg.activation_max_records:
                    break
                activated[tgt] = act
            frontier = next_frontier
            if not frontier or len(activated) >= cfg.activation_max_records:
                break

        # drop the seeds — caller already has them
        for sid in seed_ids:
            activated.pop(sid, None)
        return activated

    # ── supersession cascade ────────────────────────────────────────────

    def cascade_plan(
        self,
        superseded_ids: List[str],
        edges_of: Callable[[str], List[MemoryEdgeRecord]],
        freshness_of: Callable[[str], float],
        now: Optional[float] = None,
    ) -> Dict[str, Dict[str, float]]:
        """Compute which neighbours to decay when ``superseded_ids`` are
        corrected, and by how much — *pure* (no mutation), so it can be unit
        tested and audited before applying.

        Returns ``{node_id: {"decay": d, "stale": 0/1}}`` where ``decay`` is the
        multiplicative factor to apply to the neighbour's ``temporal_consistency``
        and freshness (``new = old × (1 - decay)``).
        """
        now = now if now is not None else time.time()
        cfg = self.config
        plan: Dict[str, Dict[str, float]] = {}
        visited = set(superseded_ids)
        # BFS queue of (node_id, hops)
        queue: List[Tuple[str, int]] = [(nid, 0) for nid in superseded_ids]

        while queue:
            cur, hops = queue.pop(0)
            if hops >= cfg.cascade_max_hops:
                continue
            for edge in edges_of(cur) or []:
                if edge.edge_type.value not in CASCADE_EDGE_TYPES:
                    continue
                tgt = edge.target_id
                if tgt in visited:
                    continue
                visited.add(tgt)
                edge_w = cfg.cascade_edge_weight.get(edge.edge_type.value, 0.5)
                decay = (cfg.cascade_decay_factor ** (hops + 1)) * edge_w
                decay = max(0.0, min(1.0, decay))
                resulting_freshness = freshness_of(tgt) * (1.0 - decay)
                plan[tgt] = {
                    "decay": decay,
                    "stale": 1.0 if resulting_freshness < cfg.cascade_stale_freshness else 0.0,
                }
                queue.append((tgt, hops + 1))
        return plan


# ── module singleton ──────────────────────────────────────────────────────

_scorer: Optional[MemoryScorer] = None


def get_scorer() -> MemoryScorer:
    global _scorer
    if _scorer is None:
        _scorer = MemoryScorer()
    return _scorer
