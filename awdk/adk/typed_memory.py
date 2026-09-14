"""Typed-activation memory for the public ADK.

Plain agent memory treats every stored fact equally.  ``TypedMemory`` adds the
idea that memory is an **authority / activation** problem: a stale guess should
not resurface with the same weight as a fresh correction, temporary notes should
expire, and decisions should constrain what the agent does next.

It is a thin wrapper over the standard :class:`adk.memory.Memory` (local SQLite)
— no new dependency, no new database.  Typed fields live in the existing
``kv_store.metadata`` JSON column, so storage round-trips unchanged.  What you
get over a plain store:

1. **Authority-labelled recall** — results are re-ranked by
   ``authority(role) × confidence × freshness × supersession × (1+reinforce)``
   and carry labels the LLM reads (``[CORRECTION] [STALE] [SUPERSEDED:id]``).
2. **Decision / correction constraints** — surfaced as a prompt preamble the
   agent should respect.
3. **Supersession + 1-hop cascade** — correcting a memory weakens it and its
   immediate ``related_ids`` neighbours.

The authority/decay constants mirror the AitherOS server-side memory config; the
heavy graph-diffusion engine and multi-backend persistence are deliberately
dropped (here a 1-hop ``related_ids`` bonus).  Pure stdlib — ``math``/``re``/``time``.
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from adk.memory import Memory, MemoryEntry

logger = logging.getLogger("adk.typed_memory")


# ---------------------------------------------------------------------------
# Roles & tiers (string constants — light; metadata round-trips trivially)
# ---------------------------------------------------------------------------


class Role:
    """What a memory *is* — drives authority."""

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

_DAY = 86400.0
# freshness = exp(-ln2 * age / half_life_seconds)
TIER_HALF_LIFE: dict[str, float] = {
    Tier.EPHEMERAL: 900.0,        # 15 min
    Tier.SESSION: 3600.0,         # 1 hour
    Tier.PERSISTENT: 7 * _DAY,
    Tier.RELATIONAL: 30 * _DAY,
    Tier.TRACE: 90 * _DAY,
    Tier.PERMANENT: math.inf,
}

TIER_TTL: dict[str, float] = dict(TIER_HALF_LIFE)

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


def make_metadata(
    content: str,
    *,
    category: str | None = None,
    role: str | None = None,
    confidence: float = 0.7,
    now: float | None = None,
) -> dict[str, Any]:
    """Build typed-memory metadata for a plain KV write (role inferred if absent).

    Lets ``Memory.remember(key, value, metadata=make_metadata(value))`` entries
    participate in authority-ranked recall and constraint extraction without the
    full :class:`TypedMemory` wrapper.
    """
    now = time.time() if now is None else now
    md = {"category": category} if category else {}
    resolved = role or infer_role(content, md)
    if resolved not in Role.ALL:
        resolved = Role.FACT
    return {
        _K_ROLE: resolved,
        _K_TIER: default_tier_for(resolved),
        _K_CONF: float(confidence),
        _K_REINF: 0,
        _K_CREATED: now,
        _K_LAST_REINF: now,
        _K_RELATED: [],
    }


# ---------------------------------------------------------------------------
# Role inference (cheap regex)
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
# Typed view over a MemoryEntry (reads the reserved metadata keys)
# ---------------------------------------------------------------------------


def _md(entry: MemoryEntry) -> dict[str, Any]:
    return entry.metadata if isinstance(entry.metadata, dict) else {}


def role_of(entry: MemoryEntry) -> str:
    return str(_md(entry).get(_K_ROLE) or Role.FACT)


def tier_of(entry: MemoryEntry) -> str:
    md = _md(entry)
    return str(md.get(_K_TIER) or default_tier_for(role_of(entry)))


def created_at_of(entry: MemoryEntry) -> float:
    md = _md(entry)
    v = md.get(_K_LAST_REINF) or md.get(_K_CREATED) or entry.timestamp
    return float(v) if v else time.time()


def freshness(entry: MemoryEntry, now: float | None = None) -> float:
    """``exp(-ln2 · age / half_life)`` for the entry's tier. 1.0 = brand new."""
    now = time.time() if now is None else now
    hl = TIER_HALF_LIFE.get(tier_of(entry), 7 * _DAY)
    if hl == math.inf:
        return 1.0
    age = max(0.0, now - created_at_of(entry))
    return math.exp(-math.log(2.0) * age / hl)


def is_expired(entry: MemoryEntry, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    ttl = TIER_TTL.get(tier_of(entry), math.inf)
    if ttl == math.inf:
        return False
    return (now - created_at_of(entry)) > ttl


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
    entry: MemoryEntry,
    *,
    activation: float = 0.0,
    base: float = 1.0,
    now: float | None = None,
) -> ScoreBreakdown:
    """Combined authority score. ``base`` is the retrieval-relevance prior."""
    now = time.time() if now is None else now
    md = _md(entry)
    role = role_of(entry)
    authority = ROLE_AUTHORITY.get(role, 1.0)
    conf = float(md.get(_K_CONF, 0.7) or 0.7)
    fresh = freshness(entry, now)
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


def labels(entry: MemoryEntry, now: float | None = None) -> list[str]:
    """Authority labels the LLM reads, e.g. ``['CORRECTION', 'REINFORCED:3']``."""
    now = time.time() if now is None else now
    md = _md(entry)
    out: list[str] = []
    role = role_of(entry)
    if role in _NOTABLE_ROLE_LABELS:
        out.append(role.upper())
    if md.get(_K_SUPERSEDED):
        out.append(f"SUPERSEDED:{md[_K_SUPERSEDED]}")
    fresh = freshness(entry, now)
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


# ---------------------------------------------------------------------------
# RecalledItem
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RecalledItem:
    """A recalled entry with its authority score and labels."""

    entry: MemoryEntry
    score: float
    labels: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.entry.key

    @property
    def content(self) -> str:
        return self.entry.value

    @property
    def role(self) -> str:
        return role_of(self.entry)

    def labelled_content(self) -> str:
        tags = "".join(f"[{lbl}] " for lbl in self.labels)
        return f"{tags}{self.entry.value}".strip()


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
    return text.rstrip(".")


# ---------------------------------------------------------------------------
# TypedMemory — wraps the public KV Memory
# ---------------------------------------------------------------------------


class TypedMemory:
    """Authority/activation layer over :class:`adk.memory.Memory`.

    Delegates persistence to ``backing`` (a fresh :class:`~adk.memory.Memory` by
    default); adds typed roles, decay, authority-ranked recall, decision
    constraints, and supersession.  Typed records are stored under generated ids
    so they coexist with plain ``remember(key, value)`` entries.
    """

    def __init__(self, backing: Memory | None = None) -> None:
        self._mem: Memory = backing if backing is not None else Memory()

    # ----- write ----------------------------------------------------------

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
        """Store ``content`` with a typed role/tier and activation metadata.

        Returns the generated record id (use it for :meth:`reinforce` /
        :meth:`supersede`).
        """
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
        rec_id = f"tm_{uuid.uuid4().hex[:16]}"
        await self._mem.remember(rec_id, content, category=resolved_role, metadata=md)
        return rec_id

    # ----- read -----------------------------------------------------------

    async def get(self, id_: str) -> MemoryEntry | None:
        return await self._mem.get_entry(id_)

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
        applies a 1-hop ``related_ids`` activation bonus, labels each result, and
        returns the top ``limit``.  Stale/superseded entries are dropped unless
        ``include_stale=True``.
        """
        now = time.time() if now is None else now
        fetch = max(limit * 3, 15)
        try:
            entries = await self._mem.search(query, limit=fetch)
        except Exception as exc:  # backend hiccup — degrade to empty
            logger.warning("typed_memory.recall backing search failed: %s", exc)
            return []

        n = max(1, len(entries))
        rank_base = {e.key: 1.0 - RANK_DECAY * (i / n) for i, e in enumerate(entries)}

        # 1-hop activation: an entry gains a bonus if a higher-ranked seed lists
        # it in related_ids (cheap co-occurrence proxy).
        seed_related: set[str] = set()
        for e in entries[: max(3, limit)]:
            seed_related.update(_md(e).get(_K_RELATED, []) or [])

        out: list[RecalledItem] = []
        for e in entries:
            md = _md(e)
            if not include_stale:
                if md.get(_K_SUPERSEDED):
                    continue
                if bool(md.get(_K_STALE, False)) and freshness(e, now) < STALE_FRESHNESS_THRESHOLD:
                    continue
            activation = 1.0 if e.key in seed_related else 0.0
            bd = score(e, activation=activation, base=rank_base[e.key], now=now)
            out.append(RecalledItem(entry=e, score=bd.combined, labels=labels(e, now)))

        out.sort(key=lambda it: (-it.score, rank_base.get(it.entry.key, 0.0)))
        return out[:limit]

    # ----- update ---------------------------------------------------------

    async def reinforce(self, id_: str, *, now: float | None = None) -> bool:
        """Strengthen a record on confirmation (bumps reinforcement + confidence)."""
        now = time.time() if now is None else now
        entry = await self._mem.get_entry(id_)
        if entry is None:
            return False
        md = dict(entry.metadata or {})
        md[_K_REINF] = int(md.get(_K_REINF, 0) or 0) + 1
        md[_K_LAST_REINF] = now
        md[_K_CONF] = min(1.0, float(md.get(_K_CONF, 0.7) or 0.7) + 0.05)
        await self._mem.remember(id_, entry.value, category=entry.category, metadata=md)
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
        old = await self._mem.get_entry(old_id)
        related: list[str] = []
        if old is not None and isinstance(old.metadata, dict):
            related = list(old.metadata.get(_K_RELATED, []) or [])

        new_id = await self.remember(
            new_content, role=role, related_ids=related, confidence=0.85,
        )

        if old is not None:
            md = dict(old.metadata or {})
            md[_K_SUPERSEDED] = new_id
            md[_K_STALE] = True
            await self._mem.remember(old_id, old.value, category=old.category, metadata=md)

        if cascade:
            for nid in related[:8]:  # 1 hop, bounded
                neighbour = await self._mem.get_entry(nid)
                if neighbour is None:
                    continue
                nmd = dict(neighbour.metadata or {})
                nmd[_K_CONF] = float(nmd.get(_K_CONF, 0.7) or 0.7) * 0.7
                if freshness(neighbour, now) < 0.2:
                    nmd[_K_STALE] = True
                await self._mem.remember(nid, neighbour.value, category=neighbour.category, metadata=nmd)
        return new_id

    # ----- constraints ----------------------------------------------------

    async def active_constraints(self, *, limit: int = 30) -> list[str]:
        """Decision/correction memories → imperative constraint strings."""
        try:
            recents = await self._mem.recent(limit=limit)
        except Exception:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for e in recents:
            if role_of(e) not in (Role.DECISION, Role.CORRECTION):
                continue
            if _md(e).get(_K_SUPERSEDED):
                continue
            c = parse_constraint(e.value)
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # ----- prompt-injection helpers (consumed by the agent loop) ----------

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


def as_typed(memory: Memory | TypedMemory | None) -> TypedMemory:
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
    "labels",
    "parse_constraint",
    "role_of",
    "score",
    "tier_of",
]
