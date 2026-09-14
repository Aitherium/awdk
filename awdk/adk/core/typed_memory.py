"""Typed-activation memory — the *lite* port of AitherOS's unified memory.

Agent memory is an **authority / activation** problem, not a storage one: a
stale guess should not come back with the same weight as a fresh correction,
temporary notes should expire, and decisions should constrain what the agent
does next.  The full implementation lives server-side in
``AitherOS/lib/memory/unified`` (delegating to a multi-backend graph).  That
stack is far too heavy for a standalone folder-agent, so this module
re-expresses the same ideas in the ``aither_adk`` world:

* **pure stdlib, zero new dependencies** (scoring is ``math.exp``);
* the :class:`~adk.core.memory.Memory` protocol stays byte-stable —
  :class:`TypedMemory` is a *wrapper* over any existing backend, not a rewrite;
* typed fields live in ``MemoryRecord.metadata`` under reserved keys, so
  :class:`~adk.core.persistence.FileStore` JSONL and the vector store
  round-trip unchanged.

What you get over a plain store:

1. **Authority-labelled recall** — results are re-ranked by
   ``authority(role) × confidence × freshness × supersession × (1+reinforce)``
   and carry labels the LLM reads (``[CORRECTION] [STALE] [SUPERSEDED:id]``).
2. **Decision/correction constraints** — surfaced as a prompt preamble the
   agent must respect (lite agents have no planner; they have the ReAct loop).
3. **Supersession + 1-hop cascade** — correcting a memory weakens it and its
   immediate ``related_ids`` neighbours.

Deliberately dropped vs. the server: the graph diffusion engine (here a 1-hop
``related_ids`` bonus), multi-backend persistence, and Chronicle audit.  Agents
that need the real graph reach it via the optional remote bridge.

The authority/decay constants below are a baked mirror of
``AitherOS/config/memory_unified.yaml``.  Keep them in sync.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from adk.core.logging import get_logger
from adk.core.memory import Memory, MemoryRecord

_log = get_logger("typed_memory")


# ---------------------------------------------------------------------------
# Roles & tiers (string constants — light; mirror the server enums)
# ---------------------------------------------------------------------------


class Role:
    """What a memory *is* — drives authority. String values, not an Enum, to
    keep the metadata round-trip trivial."""

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

    ALL = frozenset({
        FACT, DECISION, CORRECTION, TASK, TEMPORARY, PROCEDURE, INSIGHT,
        DISCOVERY, PREFERENCE, INTERACTION, TEACHING, FEEDBACK, IDENTITY,
    })


class Tier:
    """How long a memory lasts / how fast it decays."""

    EPHEMERAL = "ephemeral"
    SESSION = "session"
    PERSISTENT = "persistent"
    RELATIONAL = "relational"
    TRACE = "trace"
    PERMANENT = "permanent"

    ALL = frozenset({EPHEMERAL, SESSION, PERSISTENT, RELATIONAL, TRACE, PERMANENT})


# Authority multipliers — mirror config/memory_unified.yaml role_authority.
ROLE_AUTHORITY: dict[str, float] = {
    Role.CORRECTION: 1.3,
    Role.DECISION: 1.2,
    Role.IDENTITY: 1.15,
    Role.TEACHING: 1.1,
    Role.PREFERENCE: 1.1,
    Role.FEEDBACK: 1.1,
    Role.FACT: 1.0,
    Role.TASK: 1.0,
    Role.PROCEDURE: 0.95,
    Role.INSIGHT: 0.9,
    Role.DISCOVERY: 0.8,
    Role.INTERACTION: 0.7,
    Role.TEMPORARY: 0.5,
}

# Per-tier decay: freshness = exp(-ln2 * age / half_life_seconds).
_DAY = 86400.0
TIER_HALF_LIFE: dict[str, float] = {
    Tier.EPHEMERAL: 900.0,        # 15 min
    Tier.SESSION: 3600.0,         # 1 hour
    Tier.PERSISTENT: 7 * _DAY,
    Tier.RELATIONAL: 30 * _DAY,
    Tier.TRACE: 90 * _DAY,
    Tier.PERMANENT: math.inf,
}

TIER_TTL: dict[str, float] = {
    Tier.EPHEMERAL: 900.0,
    Tier.SESSION: 3600.0,
    Tier.PERSISTENT: 7 * _DAY,
    Tier.RELATIONAL: 30 * _DAY,
    Tier.TRACE: 90 * _DAY,
    Tier.PERMANENT: math.inf,
}

# Default tier per role.
_ROLE_TIER: dict[str, str] = {
    Role.IDENTITY: Tier.PERMANENT,
    Role.TEMPORARY: Tier.EPHEMERAL,
    Role.TASK: Tier.SESSION,
    Role.INTERACTION: Tier.SESSION,
    Role.CORRECTION: Tier.PERSISTENT,
    Role.DECISION: Tier.PERSISTENT,
    Role.TEACHING: Tier.PERSISTENT,
    Role.PREFERENCE: Tier.PERSISTENT,
    Role.FEEDBACK: Tier.PERSISTENT,
    Role.FACT: Tier.PERSISTENT,
    Role.PROCEDURE: Tier.PERSISTENT,
    Role.INSIGHT: Tier.PERSISTENT,
    Role.DISCOVERY: Tier.PERSISTENT,
}

STALE_FRESHNESS_THRESHOLD = 0.3
SUPERSEDED_FACTOR = 0.3
STALE_FACTOR = 0.5
REINFORCE_BONUS_CAP = 0.3
ACTIVATION_BONUS_CAP = 0.3
RANK_DECAY = 0.5  # retrieval-rank prior spans [1-RANK_DECAY, 1.0]

# Reserved metadata keys holding the typed fields.
_K_ROLE = "role"
_K_TIER = "tier"
_K_CONF = "confidence"
_K_REINF = "reinforcement_count"
_K_LAST_REINF = "last_reinforced"
_K_CREATED = "created_at"
_K_SUPERSEDED = "superseded_by"
_K_STALE = "stale"
_K_RELATED = "related_ids"


def default_tier_for(role: str) -> str:
    return _ROLE_TIER.get(role, Tier.PERSISTENT)


# ---------------------------------------------------------------------------
# Role inference (cheap regex; mirrors the server infer_role)
# ---------------------------------------------------------------------------

_CORRECTION_RE = re.compile(
    r"\b(actually|correction|not quite|that'?s wrong|i was wrong|instead of|"
    r"should be|use .* not|don'?t use)\b", re.I)
_DECISION_RE = re.compile(
    r"\b(we (?:decided|chose|will use|agreed)|let'?s use|going with|"
    r"the plan is|decision:)\b", re.I)
_PREF_RE = re.compile(r"\b(i (?:prefer|like|want|always)|please always|my preference)\b", re.I)
_TASK_RE = re.compile(r"\b(todo|to-do|task:|remember to|need to|follow up|next step)\b", re.I)
_TEMP_RE = re.compile(r"\b(for now|temporar|note to self|scratch|draft)\b", re.I)
_TEACH_RE = re.compile(r"\b(here'?s how|the way to|you should|to do this)\b", re.I)


def infer_role(content: str, metadata: dict[str, Any] | None = None) -> str:
    """Best-effort role from content + metadata category. Defaults to FACT."""
    md = metadata or {}
    cat = str(md.get("category") or md.get("type") or "").lower().strip()
    if cat in Role.ALL:
        return cat
    _alias = {
        "user_correction": Role.CORRECTION, "user_preference": Role.PREFERENCE,
        "user_said": Role.PREFERENCE, "note": Role.TEMPORARY, "todo": Role.TASK,
        "lesson": Role.TEACHING, "rule": Role.TEACHING,
    }
    if cat in _alias:
        return _alias[cat]
    text = content or ""
    if _CORRECTION_RE.search(text):
        return Role.CORRECTION
    if _DECISION_RE.search(text):
        return Role.DECISION
    if _PREF_RE.search(text):
        return Role.PREFERENCE
    if _TASK_RE.search(text):
        return Role.TASK
    if _TEMP_RE.search(text):
        return Role.TEMPORARY
    if _TEACH_RE.search(text):
        return Role.TEACHING
    return Role.FACT


# ---------------------------------------------------------------------------
# Typed view over a MemoryRecord (reads the reserved metadata keys)
# ---------------------------------------------------------------------------


def _md(rec: MemoryRecord) -> dict[str, Any]:
    return rec.metadata if isinstance(rec.metadata, dict) else {}


def role_of(rec: MemoryRecord) -> str:
    return str(_md(rec).get(_K_ROLE) or Role.FACT)


def tier_of(rec: MemoryRecord) -> str:
    md = _md(rec)
    return str(md.get(_K_TIER) or default_tier_for(role_of(rec)))


def created_at_of(rec: MemoryRecord) -> float:
    md = _md(rec)
    v = md.get(_K_LAST_REINF) or md.get(_K_CREATED)
    return float(v) if v else time.time()


def freshness(rec: MemoryRecord, now: float | None = None) -> float:
    """``exp(-ln2 · age / half_life)`` for the record's tier. 1.0 = brand new."""
    now = time.time() if now is None else now
    hl = TIER_HALF_LIFE.get(tier_of(rec), 7 * _DAY)
    if hl == math.inf:
        return 1.0
    age = max(0.0, now - created_at_of(rec))
    return math.exp(-math.log(2.0) * age / hl)


def is_expired(rec: MemoryRecord, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    ttl = TIER_TTL.get(tier_of(rec), math.inf)
    if ttl == math.inf:
        return False
    return (now - created_at_of(rec)) > ttl


@dataclass(slots=True)
class ScoreBreakdown:
    authority: float
    confidence: float
    freshness: float
    supersession: float
    reinforce_bonus: float
    activation_bonus: float
    combined: float


def score(
    rec: MemoryRecord,
    *,
    activation: float = 0.0,
    base: float = 1.0,
    now: float | None = None,
) -> ScoreBreakdown:
    """Combined authority score. ``base`` is the retrieval-relevance prior."""
    now = time.time() if now is None else now
    md = _md(rec)
    role = role_of(rec)
    authority = ROLE_AUTHORITY.get(role, 1.0)
    conf = float(md.get(_K_CONF, 0.7) or 0.7)
    fresh = freshness(rec, now)
    reinf = int(md.get(_K_REINF, 0) or 0)
    superseded = md.get(_K_SUPERSEDED)
    stale = bool(md.get(_K_STALE, False))

    if superseded:
        supersession = SUPERSEDED_FACTOR
    elif stale and fresh < STALE_FRESHNESS_THRESHOLD:
        supersession = STALE_FACTOR
    else:
        supersession = 1.0

    reinforce_bonus = min(REINFORCE_BONUS_CAP, reinf * 0.05)
    activation_bonus = min(ACTIVATION_BONUS_CAP, max(0.0, activation) * 0.1)

    combined = (
        base
        * authority
        * max(0.05, conf)
        * max(0.05, fresh)
        * supersession
        * (1.0 + reinforce_bonus)
        * (1.0 + activation_bonus)
    )
    return ScoreBreakdown(
        authority=authority, confidence=conf, freshness=fresh,
        supersession=supersession, reinforce_bonus=reinforce_bonus,
        activation_bonus=activation_bonus, combined=combined,
    )


_NOTABLE_ROLE_LABELS = {
    Role.CORRECTION, Role.DECISION, Role.TEACHING, Role.PREFERENCE,
    Role.TASK, Role.TEMPORARY, Role.FEEDBACK, Role.IDENTITY,
}


def labels(rec: MemoryRecord, now: float | None = None) -> list[str]:
    """Authority labels the LLM reads, e.g. ``['CORRECTION', 'REINFORCED:3']``."""
    now = time.time() if now is None else now
    md = _md(rec)
    out: list[str] = []
    role = role_of(rec)
    if role in _NOTABLE_ROLE_LABELS:
        out.append(role.upper())
    if md.get(_K_SUPERSEDED):
        out.append(f"SUPERSEDED:{md[_K_SUPERSEDED]}")
    fresh = freshness(rec, now)
    if bool(md.get(_K_STALE, False)) and fresh < STALE_FRESHNESS_THRESHOLD:
        out.append("STALE")
    reinf = int(md.get(_K_REINF, 0) or 0)
    if reinf > 0:
        out.append(f"REINFORCED:{reinf}")
    conf = float(md.get(_K_CONF, 0.7) or 0.7)
    if conf >= 0.9:
        out.append(f"CONFIDENT:{conf:.2f}")
    if not out:
        out.append("NEW")
    return out


def label_content(rec: MemoryRecord, now: float | None = None) -> str:
    """Bake the labels into the content string the LLM actually reads."""
    tags = "".join(f"[{lbl}] " for lbl in labels(rec, now))
    return f"{tags}{rec.content}".strip()


# ---------------------------------------------------------------------------
# RecalledItem
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecalledItem:
    """A recalled record with its authority score and labels."""

    record: MemoryRecord
    score: float
    labels: list[str] = field(default_factory=list)

    @property
    def content(self) -> str:
        return self.record.content

    @property
    def role(self) -> str:
        return role_of(self.record)

    def labelled_content(self) -> str:
        tags = "".join(f"[{lbl}] " for lbl in self.labels)
        return f"{tags}{self.record.content}".strip()


# ---------------------------------------------------------------------------
# Constraint extraction (decision/correction → prompt preamble)
# ---------------------------------------------------------------------------

_MUST_NOT_RE = re.compile(r"\b(?:must not|do not|don'?t|never|avoid)\s+(.+)", re.I)
_MUST_RE = re.compile(r"\b(?:must|always|should)\s+(.+)", re.I)
_PREFER_RE = re.compile(r"\b(?:prefer|use)\s+(.+?)(?:\s+(?:over|instead of)\s+(.+))?$", re.I)


def parse_constraint(content: str) -> str | None:
    """Turn a decision/correction into a short imperative constraint, or None."""
    text = (content or "").strip()
    if not text:
        return None
    m = _MUST_NOT_RE.search(text)
    if m:
        return f"AVOID: {m.group(1).strip().rstrip('.')}"
    m = _MUST_RE.search(text)
    if m:
        return f"REQUIRE: {m.group(1).strip().rstrip('.')}"
    m = _PREFER_RE.search(text)
    if m:
        return f"PREFER: {m.group(1).strip().rstrip('.')}"
    # generic-soft fallback: surface the whole thing
    return text.rstrip(".")


# ---------------------------------------------------------------------------
# TypedMemory — wraps any Memory backend
# ---------------------------------------------------------------------------


class TypedMemory(Memory):
    """Authority/activation layer over any :class:`Memory` backend.

    Delegates persistence to ``backing`` (defaults to a fresh
    :class:`~adk.core.memory.InMemoryStore`); adds typed roles, decay,
    authority-ranked recall, decision constraints, and supersession.
    """

    def __init__(self, backing: Memory | None = None) -> None:
        from adk.core.memory import InMemoryStore
        self._mem: Memory = backing if backing is not None else InMemoryStore()

    # ----- raw Memory protocol (kept stable; role inferred on write) -------

    async def put(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        return await self.remember(content, metadata=metadata)

    async def get(self, id_: str) -> MemoryRecord | None:
        return await self._mem.get(id_)

    async def search(self, query: str, *, limit: int = 10) -> list[MemoryRecord]:
        items = await self.recall(query, limit=limit)
        return [it.record for it in items]

    async def recent(self, *, limit: int = 10) -> list[MemoryRecord]:
        return await self._mem.recent(limit=limit)

    # ----- typed surface ---------------------------------------------------

    async def remember(
        self,
        content: str,
        *,
        role: str | None = None,
        tier: str | None = None,
        confidence: float = 0.7,
        related_ids: Sequence[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store ``content`` with a typed role/tier and activation metadata."""
        md = dict(metadata or {})
        resolved_role = role or md.get(_K_ROLE) or infer_role(content, md)
        if resolved_role not in Role.ALL:
            resolved_role = Role.FACT
        now = time.time()
        md.update({
            _K_ROLE: resolved_role,
            _K_TIER: tier or md.get(_K_TIER) or default_tier_for(resolved_role),
            _K_CONF: float(confidence),
            _K_REINF: int(md.get(_K_REINF, 0) or 0),
            _K_CREATED: float(md.get(_K_CREATED) or now),
            _K_LAST_REINF: now,
            _K_RELATED: list(related_ids) or list(md.get(_K_RELATED, []) or []),
        })
        return await self._mem.put(content, metadata=md)

    async def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        include_stale: bool = False,
        now: float | None = None,
    ) -> list[RecalledItem]:
        """Authority-ranked, labelled recall.

        Over-fetches from the backing store, re-ranks by the authority score,
        applies a 1-hop ``related_ids`` activation bonus, labels each result,
        and returns the top ``limit``.  Stale/superseded records are labelled,
        not silently dropped, unless ``include_stale=False``.
        """
        now = time.time() if now is None else now
        fetch = max(limit * 3, 15)
        try:
            records = await self._mem.search(query, limit=fetch)
        except Exception as exc:  # backend hiccup — degrade to empty
            _log.warning("typed_memory.recall backing search failed", extra={"err": str(exc)})
            return []

        # retrieval-rank prior: band [1-RANK_DECAY, 1.0], best-first
        n = max(1, len(records))
        rank_base = {
            id(rec): 1.0 - RANK_DECAY * (i / n) for i, rec in enumerate(records)
        }

        # 1-hop activation: a record gains a bonus if it's referenced by the
        # related_ids of a higher-ranked seed (cheap co-occurrence proxy).
        seed_related: set[str] = set()
        for rec in records[: max(3, limit)]:
            seed_related.update(_md(rec).get(_K_RELATED, []) or [])

        out: list[RecalledItem] = []
        for rec in records:
            md = _md(rec)
            if not include_stale:
                fresh = freshness(rec, now)
                if md.get(_K_SUPERSEDED):
                    continue
                if bool(md.get(_K_STALE, False)) and fresh < STALE_FRESHNESS_THRESHOLD:
                    continue
            activation = 1.0 if rec.id in seed_related else 0.0
            bd = score(rec, activation=activation, base=rank_base[id(rec)], now=now)
            out.append(RecalledItem(record=rec, score=bd.combined, labels=labels(rec, now)))

        out.sort(key=lambda it: (-it.score, rank_base.get(id(it.record), 0.0)))
        return out[:limit]

    async def reinforce(self, id_: str, *, now: float | None = None) -> bool:
        """Strengthen a record on confirmation (in-place; bumps reinforcement).

        Note: mutates the in-memory record. Append-only backends (FileStore)
        reflect this for the process lifetime but do not rewrite history.
        """
        now = time.time() if now is None else now
        rec = await self._mem.get(id_)
        if rec is None or not isinstance(rec.metadata, dict):
            return False
        rec.metadata[_K_REINF] = int(rec.metadata.get(_K_REINF, 0) or 0) + 1
        rec.metadata[_K_LAST_REINF] = now
        rec.metadata[_K_CONF] = min(1.0, float(rec.metadata.get(_K_CONF, 0.7) or 0.7) + 0.05)
        return True

    async def supersede(
        self,
        old_id: str,
        new_content: str,
        *,
        role: str = Role.CORRECTION,
        cascade: bool = True,
        now: float | None = None,
    ) -> str:
        """Replace ``old_id`` with ``new_content``.

        Marks the old record superseded (→ ×0.3) and stale, then decays its
        immediate ``related_ids`` neighbours one hop.  Returns the new id.
        """
        now = time.time() if now is None else now
        old = await self._mem.get(old_id)
        related: list[str] = []
        if old is not None and isinstance(old.metadata, dict):
            related = list(old.metadata.get(_K_RELATED, []) or [])

        new_id = await self.remember(
            new_content, role=role, related_ids=related, confidence=0.85,
        )

        if old is not None and isinstance(old.metadata, dict):
            old.metadata[_K_SUPERSEDED] = new_id
            old.metadata[_K_STALE] = True

        if cascade:
            for nid in related[:8]:  # 1 hop, bounded
                neighbour = await self._mem.get(nid)
                if neighbour is not None and isinstance(neighbour.metadata, dict):
                    # 0.7 decay on confidence; mark stale only if it bottoms out
                    c = float(neighbour.metadata.get(_K_CONF, 0.7) or 0.7) * 0.7
                    neighbour.metadata[_K_CONF] = c
                    if freshness(neighbour, now) < 0.2:
                        neighbour.metadata[_K_STALE] = True
        return new_id

    async def active_constraints(self, *, limit: int = 30) -> list[str]:
        """Decision/correction memories → imperative constraint strings."""
        try:
            recents = await self._mem.recent(limit=limit)
        except Exception:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for rec in recents:
            if role_of(rec) not in (Role.DECISION, Role.CORRECTION):
                continue
            if _md(rec).get(_K_SUPERSEDED):
                continue
            c = parse_constraint(rec.content)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # ----- prompt-injection helpers (consumed by ReActLoop) ----------------

    async def context_block(self, query: str, *, limit: int = 5) -> str:
        """A ready-to-inject ``# MEMORY`` block, or ``''`` if nothing recalled."""
        items = await self.recall(query, limit=limit)
        if not items:
            return ""
        lines = "\n".join(f"- {it.labelled_content()}" for it in items)
        return f"# MEMORY (ranked by authority)\n{lines}"

    async def constraints_block(self, *, limit: int = 20) -> str:
        """A ready-to-inject decisions/corrections block, or ``''`` if none."""
        constraints = await self.active_constraints(limit=limit)
        if not constraints:
            return ""
        lines = "\n".join(f"- {c}" for c in constraints)
        return f"# DECISIONS / CORRECTIONS — you MUST respect these\n{lines}"

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self._mem) if hasattr(self._mem, "__len__") else 0


def as_typed(memory: Memory | None) -> TypedMemory:
    """Wrap ``memory`` in a :class:`TypedMemory` if it isn't already one."""
    if isinstance(memory, TypedMemory):
        return memory
    return TypedMemory(memory)


__all__ = [
    "Role",
    "Tier",
    "ROLE_AUTHORITY",
    "TIER_HALF_LIFE",
    "RecalledItem",
    "ScoreBreakdown",
    "TypedMemory",
    "as_typed",
    "default_tier_for",
    "freshness",
    "infer_role",
    "is_expired",
    "label_content",
    "labels",
    "parse_constraint",
    "role_of",
    "score",
    "tier_of",
]
