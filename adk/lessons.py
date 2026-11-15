"""Lessons: what an agent learned from an outcome, kept only while it provably helps.

One record shape (:class:`Lesson`) for four kinds of learned thing:

* ``memory``       -- a fact about this company, its people, its files
                      ("their roster sheets call the name column 'Emp Name'").
* ``knowledge``    -- a pointer into the document graph.
* ``skill``        -- a procedure: when to use it, steps, inputs, a worked example.
                      Exports and imports as an agentskills.io ``SKILL.md``.
* ``tool_request`` -- a capability the agent lacked. An agent cannot write code into
                      a customer's tenant, so it records the gap and the workaround
                      and the host files the request with engineering.

Four rules, each enforced here and each with a test in ``tests/test_lessons.py``:

1. **Evidence-gated.** :meth:`LessonStore.capture` refuses a lesson with no outcome
   behind it (a correction, a confirmed mapping, a failed-then-succeeded task, a
   thumbs-up/down, a verified answer, a capability gap). The source turn is stored.
2. **Replay-checked.** A lesson carries a replay case: the triggering situation is
   re-run without the lesson and with it, and the lesson is kept only if the result
   is better (:func:`replay_check`). The case is stored, so
   :meth:`LessonStore.consolidate` re-runs it later and retires a lesson that
   stopped helping. Three unhelpful uses in a row retire it too.
3. **Authority.** What is in use changes only on the authority of an admin's own
   confirmation, or of a passing check where the workspace chose to learn
   automatically. Everything else waits as a proposal BESIDE the lesson in use and
   never touches it; :meth:`LessonStore.approve` folds it in.
4. **Context first, tenant-local.** Lessons are applied by retrieval at the moment
   of use (:meth:`LessonStore.context_block`, :meth:`LessonStore.find_mapping`),
   never by training. They are typed-memory records in a :class:`LocalOnlyMemory`,
   which has no remote path, so nothing learned from a customer's content leaves
   the file. The only outbound shape is :func:`product_lesson`: closed-vocabulary
   slugs, default OFF, and it raises :class:`TenantTextLeakError` rather than send
   a word of tenant text.

Every write is compare-and-swap on the record's revision inside one SQLite
transaction, so a slow re-check can never overwrite an admin's "off" or bring back
a lesson that was forgotten while it ran.

Pure stdlib plus PyYAML (already a base dependency) for SKILL.md frontmatter.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from adk.memory import Memory, MemoryEntry
from adk.typed_memory import Role, Tier, make_metadata

logger = logging.getLogger("adk.lessons")

KINDS = ("memory", "knowledge", "skill", "tool_request")
#: proposed = waiting for an admin's OK; active = applied; off = kept, not applied;
#: retired = stopped helping (kept for the record, not applied).
STATUSES = ("proposed", "active", "off", "retired")
#: The outcomes a lesson may come from. ``imported`` is an admin bringing a SKILL.md
#: in by hand: it is evidence of intent, not of benefit, so it never auto-activates.
EVIDENCE_TYPES = (
    "user_correction", "retry_success", "thumbs_up", "thumbs_down",
    "confirmed_mapping", "verified_answer", "capability_gap", "imported",
)
MODES = ("auto", "ask", "off")
CONSOLIDATE = ("task", "nightly", "weekly")
APPLIES_TO = ("me", "workspace")

#: Unhelpful uses in a row before a lesson is retired without being asked.
RETIRE_AFTER_UNHELPFUL = 3
#: The visible cap of every lesson list: five, then "+N more", with the real total.
VISIBLE_CAP = 5

_ID_PREFIX = "ls_"
_TOMBSTONE_PREFIX = "lsx_"
_SETTINGS_PREFIX = "lscfg_"
_STAMP_PREFIX = "lsat_"
_TITLE_CHARS = 160
_BODY_CHARS = 4000
_CONSOLIDATE_SECONDS = {"nightly": 86400.0, "weekly": 7 * 86400.0}
_ROLE_FOR_KIND = {
    "memory": Role.FACT, "knowledge": Role.FACT,
    "skill": Role.PROCEDURE, "tool_request": Role.TASK,
}
#: How often a write is re-tried from a fresh read when someone else wrote first.
_MAX_ATTEMPTS = 4


class LessonError(ValueError):
    """A lesson operation that cannot be done; the message is safe to show a user."""


class LessonNotFoundError(LessonError):
    """No lesson with that id in the caller's workspace (or it is someone else's)."""


class NoEvidenceError(LessonError):
    """A lesson was offered with no outcome behind it."""


class TenantTextLeakError(LessonError):
    """An outbound product lesson would have carried tenant text. Nothing was sent."""


class _StaleWriteError(Exception):
    """The record changed (or was forgotten) since it was read. Internal: retried."""


# ── text ────────────────────────────────────────────────────────────────────

def _fold(text: Any) -> str:
    """One spelling per character (NFKC), lower case. ``lower``, not ``casefold``:
    casefold turns "ß" into "ss" and would make "Maße" and "Masse" one word."""
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def _word_tokens(text: Any) -> List[str]:
    """Runs of letters and digits in ANY script, with the marks that belong to them.

    A name in Cyrillic or a one-letter initial is a word too. Combining marks stay
    inside their word: dropping them makes two different Hindi or Thai words read
    alike ("दिन" and "दान").
    """
    out: List[str] = []
    current: List[str] = []
    for ch in _fold(text):
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _tokens(text: Any) -> set:
    return set(_word_tokens(text))


def _digest(parts: Sequence[str]) -> str:
    """A hash of the parts as a JSON list, so no separator inside a part can make
    two different scopes hash alike (``("a", "b|c")`` is not ``("a|b", "c")``)."""
    seed = json.dumps([str(p) for p in parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _clean(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit].rstrip()


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit].rstrip()


# ── scope ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """Who a lesson belongs to. Always built from the AUTHENTICATED caller."""

    tenant_id: str
    workspace_id: str = "default"
    user_id: str = ""  # "" = the whole workspace

    def __post_init__(self) -> None:
        if not str(self.tenant_id or "").strip():
            raise LessonError("a lesson scope needs a tenant")


# ── the record ──────────────────────────────────────────────────────────────


@dataclass
class Lesson:
    """One learned thing. The same shape for all four kinds."""

    id: str
    kind: str
    title: str  # what, in one line
    body: str  # the text put in context at the moment of use
    tenant_id: str
    workspace_id: str = "default"
    user_id: str = ""  # set = applies to that person only ("me")
    status: str = "proposed"
    #: Exact-match key for the situation the lesson applies to ("sheet-columns:roster").
    trigger: str = ""
    #: Structured part. memory: ``{"aliases": {...}}``; skill: ``{"when_to_use",
    #: "steps", "inputs", "example"}``; tool_request: ``{"product": {"surface",
    #: "capability"}, "workaround", "ticket"}``. ``extends`` is set by the store on a
    #: proposal: the id of the lesson it will be folded into (or become) on approval.
    data: Dict[str, Any] = field(default_factory=dict)
    #: ``{"type", "source_turn", "confirmed_by", "at", "note"}``
    evidence: Dict[str, Any] = field(default_factory=dict)
    #: ``{"case", "before", "after", "passed", "checked_at", "runs"}``
    replay: Dict[str, Any] = field(default_factory=dict)
    used_count: int = 0
    helped_count: int = 0
    unhelpful_streak: int = 0
    last_used: float = 0.0
    last_helped: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 1
    retired_reason: str = ""
    #: The state before the last change, for one-step undo.
    previous: Dict[str, Any] = field(default_factory=dict)
    #: Storage revision: 0 = never saved; every save adds one. A write is refused
    #: when the stored revision is not the one this copy was read at.
    rev: int = 0

    @property
    def applies_to(self) -> str:
        return "me" if self.user_id else "workspace"

    def scope(self) -> Scope:
        return Scope(self.tenant_id, self.workspace_id, self.user_id)

    def visible_to(self, scope: Scope, *, all_users: bool = False) -> bool:
        """Same tenant AND workspace; a personal lesson only to its person (or an admin)."""
        if self.tenant_id != scope.tenant_id:
            return False
        if (self.workspace_id or "default") != (scope.workspace_id or "default"):
            return False
        return all_users or not self.user_id or self.user_id == scope.user_id

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def view(self) -> Dict[str, Any]:
        """The row an admin panel shows: what, from what, when, by whom, how used."""
        ev = self.evidence or {}
        rp = self.replay or {}
        return {
            "id": self.id,
            "kind": self.kind,
            "what": self.title,
            "body": self.body,
            "status": self.status,
            "applies_to": self.applies_to,
            "from": {"evidence": ev.get("type", ""), "source_turn": ev.get("source_turn", "")},
            "when": self.created_at,
            "confirmed_by": ev.get("confirmed_by", ""),
            "used": self.used_count,
            "helped": self.helped_count,
            "last_used": self.last_used or None,
            "last_helped": self.last_helped or None,
            "replay": ({"before": rp.get("before"), "after": rp.get("after"),
                        "passed": bool(rp.get("passed")), "checked_at": rp.get("checked_at")}
                       if rp else None),
            "retired_reason": self.retired_reason,
            #: On a proposal: the id of the lesson it is folded into on approval, or
            #: that it becomes when nothing is in use for that situation yet.
            "extends": (self.data or {}).get("extends") or None,
            "can_undo": bool(self.previous),
            "version": self.version,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Lesson":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})


@dataclass
class LessonSettings:
    """What the workspace admin configures."""

    mode: str = "ask"  # auto = keep what the replay check proves; ask = OK first; off
    consolidate: str = "task"  # re-check after each task / nightly / weekly
    applies_to: str = "workspace"  # default audience of a new lesson
    #: Contribute CONTENT-FREE product lessons ("importer could not read .xlsx") to the
    #: vendor. Default off; see :func:`product_lesson`.
    share_product_lessons: bool = False
    updated_by: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureResult:
    kept: bool
    reason: str
    lesson: Optional[Lesson] = None


# ── replay check ────────────────────────────────────────────────────────────

#: ``run(lesson_or_None) -> score in [0, 1]`` (higher is better); sync or async.
ReplayRun = Callable[[Optional[Lesson]], Union[float, Awaitable[float]]]
#: ``runner(case, lesson_or_None) -> score``; registered per stored case ``type`` so a
#: lesson's check can be re-run long after the turn that produced it.
ReplayRunner = Callable[[Dict[str, Any], Optional[Lesson]], Union[float, Awaitable[float]]]

REPLAY_RUNNERS: Dict[str, ReplayRunner] = {}


def register_replay(case_type: str) -> Callable[[ReplayRunner], ReplayRunner]:
    """Register the runner for a stored replay case type (``case["type"]``)."""

    def deco(fn: ReplayRunner) -> ReplayRunner:
        REPLAY_RUNNERS[case_type] = fn
        return fn

    return deco


async def _score(run: ReplayRun, lesson: Optional[Lesson]) -> float:
    out = run(lesson)
    if inspect.isawaitable(out):
        out = await out
    value = float(out)
    if not math.isfinite(value):
        return 0.0  # min(1.0, nan) is 1.0: a broken grader must never read as perfect
    return max(0.0, min(1.0, value))


async def replay_check(lesson: Lesson, run: ReplayRun, *, baseline: Optional[Lesson] = None,
                       min_gain: float = 1e-9) -> Dict[str, Any]:
    """Re-run the triggering case WITHOUT the lesson, then WITH it.

    ``passed`` is true only when the result with the lesson is strictly better. A
    lesson that changes nothing has not earned a place in anyone's context.

    "Without" means what is in use today: ``baseline`` is the lesson already in use
    for the situation (None when there is none). A change that scores better than
    nothing but worse than the lesson it would replace does not pass.
    """
    before = await _score(run, baseline)
    after = await _score(run, lesson)
    return {
        "before": round(before, 4),
        "after": round(after, 4),
        "passed": (after - before) > min_gain,
        "checked_at": time.time(),
    }


def _run_for_case(case: Dict[str, Any]) -> Optional[ReplayRun]:
    runner = REPLAY_RUNNERS.get(str((case or {}).get("type") or ""))
    if runner is None:
        return None
    return lambda lesson: runner(case, lesson)


# ── worked pattern: a confirmed mapping ─────────────────────────────────────


def normalize_header(name: Any) -> str:
    """``" Emp. Name "`` -> ``"emp name"``: case, punctuation and spacing do not matter.

    Letters, digits and their marks are kept in every script (``"Имя 1"`` is not
    ``"Отдел 1"``, ``"Größe"`` is not ``"Grüße"``, ``"दिन"`` is not ``"दान"``); a
    header with none has the empty name.
    """
    return " ".join(_word_tokens(name))


def _aliases_of(lesson: Lesson) -> Dict[str, str]:
    """``normalised header -> field`` from a lesson; anything malformed is skipped."""
    raw = (lesson.data or {}).get("aliases")
    if not isinstance(raw, dict):
        return {}
    return {a: f for a, f in raw.items() if isinstance(a, str) and isinstance(f, str)}


def map_columns(
    headers: Sequence[str],
    required: Sequence[str],
    lessons: Iterable[Lesson] = (),
) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Resolve ``required`` fields against a sheet's ``headers``.

    What a person confirmed comes first: the aliases each lesson carries
    (``data["aliases"]``, normalised header -> field). Then the field's own name,
    which is all an importer with no lesson does. Column ORDER never matters: the
    match is by name. Anything ambiguous stays missing so a person decides: two
    headers that normalise to the same name, two columns both taught as one field,
    a column wanted by two fields, or the taught column sitting next to a free
    column that carries the field's own name.

    Returns ``(field -> header, missing fields, ids of lessons that resolved one)``.
    """
    required = list(dict.fromkeys(required))  # a field listed twice is one field
    by_norm: Dict[str, List[str]] = {}
    for h in headers:
        by_norm.setdefault(normalize_header(h), []).append(h)

    def only(norm: str) -> Optional[str]:
        """The one header with this normalised name; None when absent OR ambiguous.
        A header with no letters or digits has no name and matches nothing."""
        hits = (by_norm.get(norm) or []) if norm else []
        return hits[0] if len(hits) == 1 else None

    # 1. What a person confirmed: every header the lessons point at a field.
    candidates: Dict[str, Dict[str, List[str]]] = {}
    for lesson in lessons:
        for alias, fld in _aliases_of(lesson).items():
            if fld not in required:
                continue
            hit = only(normalize_header(alias))
            if hit is not None:
                candidates.setdefault(fld, {}).setdefault(hit, []).append(lesson.id)
    undecided: set = set()
    taught: Dict[str, str] = {}
    wanted_by: Dict[str, List[str]] = {}
    for fld in required:
        found = candidates.get(fld) or {}
        if len(found) > 1:
            undecided.add(fld)  # two of this sheet's columns were both taught as it
        elif found:
            header = next(iter(found))
            taught[fld] = header
            wanted_by.setdefault(header, []).append(fld)
    for header, flds in wanted_by.items():
        if len(flds) > 1:  # one column never fills two fields
            for fld in flds:
                undecided.add(fld)
                taught.pop(fld, None)
    claimed = set(taught.values())
    mapping: Dict[str, str] = {}
    used: List[str] = []
    for fld, header in taught.items():
        own = only(normalize_header(fld))
        if own is not None and own != header and own not in claimed:
            undecided.add(fld)  # the taught column AND one with the field's own name
            continue
        mapping[fld] = header
        for lesson_id in candidates[fld][header]:
            if lesson_id not in used:
                used.append(lesson_id)
    # 2. The field's own name, for what is left, unless a person already assigned
    #    that column to another field.
    for fld in required:
        if fld in mapping or fld in undecided:
            continue
        own = only(normalize_header(fld))
        if own is not None and own not in claimed:
            mapping[fld] = own
            claimed.add(own)
    missing = [f for f in required if f not in mapping]
    return mapping, missing, used


@register_replay("sheet-columns")
def _replay_sheet_columns(case: Dict[str, Any], lesson: Optional[Lesson]) -> float:
    """Share of the required fields the triggering sheet resolves, 0..1."""
    required = list(case.get("required") or [])
    if not required:
        return 0.0
    mapping, _missing, _used = map_columns(
        list(case.get("headers") or []), required, [lesson] if lesson else [])
    return len(mapping) / len(required)


# ── storage: typed memory that never leaves its file ────────────────────────


class LocalOnlyMemory(Memory):
    """A :class:`~adk.memory.Memory` with every remote path switched off.

    ``Memory.remember`` normally also teaches Spirit and pushes to the fleet vector
    store. A lesson learned from a customer's content must not: the four remote
    methods are overridden here, so no configuration or environment variable can
    turn the sync back on for a lesson store.
    """

    def __init__(self, db_path: Union[str, Path], agent_name: str = "lessons") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(db_path=db_path, agent_name=agent_name)
        self._spirit_enabled = False
        self._fleet_enabled = False
        self._fleet_url = ""

    async def _spirit_teach(self, content: str, category: str = "general") -> None:
        return None

    async def _spirit_recall(self, query: str, limit: int = 5) -> list:
        return []

    async def _fleet_push(self, key: str, content: str, category: str = "general",
                          content_type: str = "text") -> None:
        return None

    async def _fleet_search(self, query: str, limit: int = 5) -> list:
        return []

    async def entries(self, prefix: str) -> List[MemoryEntry]:
        """Every entry whose key starts with ``prefix`` (one query, newest first)."""
        rows = await self._db(lambda conn: conn.execute(
            "SELECT key, value, category, timestamp, metadata FROM kv_store "
            "WHERE substr(key, 1, ?) = ? ORDER BY timestamp DESC",
            (len(prefix), prefix),
        ).fetchall())
        return [
            MemoryEntry(key=r[0], value=r[1], category=r[2], timestamp=r[3],
                        metadata=json.loads(r[4]) if r[4] else None)
            for r in rows
        ]

    async def get_entry(self, key: str) -> Optional[MemoryEntry]:
        """One entry by key, read in a worker thread (the base reads on the loop)."""
        row = await self._db(lambda conn: conn.execute(
            "SELECT key, value, category, timestamp, metadata FROM kv_store WHERE key = ?",
            (key,),
        ).fetchone())
        if not row:
            return None
        return MemoryEntry(key=row[0], value=row[1], category=row[2], timestamp=row[3],
                           metadata=json.loads(row[4]) if row[4] else None)

    async def transact(self, fn: Callable[[Any], Any]) -> Any:
        """Run ``fn(conn)`` in a worker thread inside ONE write transaction.

        ``BEGIN IMMEDIATE`` takes the write lock before the first read, so a
        read-then-write in ``fn`` is atomic against every other connection: other
        coroutines, other threads and other processes on the same file.
        """
        def go(conn: Any) -> Any:
            conn.execute("BEGIN IMMEDIATE")
            return fn(conn)

        return await self._db(go)

    @staticmethod
    def put(conn: Any, key: str, value: str, category: str,
            metadata: Optional[Dict[str, Any]], timestamp: float) -> None:
        """Write one row on ``conn`` (inside :meth:`transact`)."""
        conn.execute(
            "INSERT OR REPLACE INTO kv_store (key, value, category, timestamp, metadata, synced) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (key, value, category, timestamp,
             json.dumps(metadata) if metadata is not None else None))

    async def drop(self, key: str) -> None:
        await self._db(lambda conn: conn.execute("DELETE FROM kv_store WHERE key = ?", (key,)))


def _lesson_id(scope: Scope, kind: str, trigger: str) -> str:
    """Deterministic per (scope, kind, trigger) so a re-learned situation UPDATES its
    lesson instead of piling up duplicates; random when there is no trigger."""
    if not trigger:
        return _ID_PREFIX + uuid.uuid4().hex[:16]
    return _ID_PREFIX + _digest(
        [scope.tenant_id, scope.workspace_id or "default", scope.user_id, kind, trigger])[:16]


def _addition_id(lesson_id: str) -> str:
    """The one slot where proposals for the lesson ``lesson_id`` wait."""
    return _ID_PREFIX + _digest([lesson_id, "addition"])[:16]


def _parent_of(lesson: Lesson) -> str:
    """The id a proposal extends, or ``""``. Honoured only when this record really
    is that lesson's addition slot, so a forged ``extends`` points nowhere."""
    ext = (lesson.data or {}).get("extends")
    if isinstance(ext, str) and ext.startswith(_ID_PREFIX) and _addition_id(ext) == lesson.id:
        return ext
    return ""


def _stored_lesson(row: Any) -> Optional[Lesson]:
    """The lesson in a ``(metadata,)`` row; None for no row or a row that is not one."""
    if row is None:
        return None
    try:
        raw = (json.loads(row[0] or "{}") or {}).get("lesson")
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return None
    lesson = Lesson.from_dict(raw)
    lesson.rev = max(1, int(lesson.rev or 0))  # it is stored, so it has a revision
    return lesson


def _row_for(lesson: Lesson, now: float) -> Tuple[str, str, Dict[str, Any]]:
    """``(content, category, metadata)``: the typed-memory row of a lesson."""
    content = f"{lesson.title}\n{lesson.body}".strip()
    role = _ROLE_FOR_KIND.get(lesson.kind, Role.FACT)
    passed = bool((lesson.replay or {}).get("passed"))
    metadata = make_metadata(content, role=role, confidence=0.9 if passed else 0.6, now=now)
    metadata["tier"] = Tier.PERMANENT  # a lesson is retired by outcome, never by age
    metadata.update({"lesson": lesson.to_dict(), "tenant_id": lesson.tenant_id,
                     "workspace_id": lesson.workspace_id})
    return content, role, metadata


_READ_LESSON = "SELECT metadata FROM kv_store WHERE key = ?"
_DELETE_ROW = "DELETE FROM kv_store WHERE key = ?"
#: Fields only :meth:`LessonStore.record_use` writes. Counting a use does not change
#: the revision, so a save takes them from the stored row, never from its own copy.
_USE_FIELDS = ("used_count", "helped_count", "unhelpful_streak", "last_used", "last_helped")


def _retrying(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Run a store operation again from a fresh read when someone else wrote first."""

    @functools.wraps(fn)
    async def inner(self: "LessonStore", *args: Any, **kwargs: Any) -> Any:
        for _ in range(_MAX_ATTEMPTS):
            try:
                return await fn(self, *args, **kwargs)
            except _StaleWriteError:
                continue
        raise LessonError(
            "this lesson was being changed at the same moment; nothing was saved, try again")

    return inner


_STOP = frozenset((
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "you", "your",
    "our", "how", "what", "when", "where", "which", "who", "can", "will", "please",
    "not", "use", "using", "here", "there", "have", "has", "into", "about",
))


def _words(text: str) -> set:
    """Content words for matching a lesson to a question: three characters or more."""
    return {word for word in _word_tokens(text) if len(word) >= 3 and word not in _STOP}


def _check_data(data: Dict[str, Any]) -> None:
    """Refuse a structured part that would break the code that applies it."""
    if not isinstance(data, dict):
        raise LessonError("a lesson's data must be an object")
    aliases = data.get("aliases")
    if aliases is not None and not (
            isinstance(aliases, dict)
            and all(isinstance(a, str) and isinstance(f, str) for a, f in aliases.items())):
        raise LessonError("aliases must map a column name to a field name")


_STATUS_ORDER = {"proposed": 0, "active": 1, "off": 2, "retired": 3}


class LessonStore:
    """The tenant's lessons: typed-memory records in one local SQLite file."""

    def __init__(self, db_path: Union[str, Path, None] = None, *,
                 memory: Optional[LocalOnlyMemory] = None,
                 clock: Callable[[], float] = time.time) -> None:
        if memory is None:
            if db_path is None:
                raise LessonError("LessonStore needs a db_path")
            memory = LocalOnlyMemory(db_path)
        if not isinstance(memory, LocalOnlyMemory):
            raise LessonError("a lesson store must sit on a LocalOnlyMemory")
        self._mem = memory
        self._now = clock

    # ----- settings -------------------------------------------------------

    @staticmethod
    def _settings_key(scope: Scope) -> str:
        return _SETTINGS_PREFIX + _digest(
            [scope.tenant_id, scope.workspace_id or "default"])[:24]

    @staticmethod
    def _stamp_key(scope: Scope) -> str:
        return _STAMP_PREFIX + _digest([scope.tenant_id, scope.workspace_id or "default"])[:24]

    @staticmethod
    def _scope_meta(scope: Scope) -> Dict[str, Any]:
        return {"tenant_id": scope.tenant_id, "workspace_id": scope.workspace_id or "default"}

    async def last_consolidated(self, scope: Scope) -> float:
        """When this workspace's lessons were last re-checked (0.0 = never).

        Kept in its own row, NOT in the settings: a re-check that wrote the
        settings back would undo a change an admin made while it was running.
        """
        entry = await self._mem.get_entry(self._stamp_key(scope))
        try:
            return float(entry.value) if entry is not None else 0.0
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_settings(value: Optional[str]) -> LessonSettings:
        try:
            raw = json.loads(value) if value else {}
        except ValueError:
            raw = {}
        known = {f.name for f in fields(LessonSettings)}
        return LessonSettings(**{k: v for k, v in (raw or {}).items() if k in known})

    async def settings(self, scope: Scope) -> LessonSettings:
        entry = await self._mem.get_entry(self._settings_key(scope))
        return self._parse_settings(entry.value if entry is not None else None)

    async def update_settings(self, scope: Scope, by: str, **changes: Any) -> LessonSettings:
        """Change settings. Read, change and write are one transaction, so two admins
        changing different settings at once both keep their change."""
        allowed = {"mode": MODES, "consolidate": CONSOLIDATE, "applies_to": APPLIES_TO}
        for key, value in changes.items():
            if value is None:
                continue
            if key in allowed:
                if value not in allowed[key]:
                    raise LessonError(f"{key} must be one of: {', '.join(allowed[key])}")
            elif key != "share_product_lessons":
                raise LessonError(f"unknown setting: {key}")
        key_ = self._settings_key(scope)
        now = self._now()

        def go(conn: Any) -> LessonSettings:
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key_,)).fetchone()
            cfg = self._parse_settings(row[0] if row else None)
            for name, value in changes.items():
                if value is None:
                    continue
                setattr(cfg, name, bool(value) if name == "share_product_lessons" else value)
            cfg.updated_by = by
            cfg.updated_at = now
            LocalOnlyMemory.put(conn, key_, json.dumps(cfg.to_dict()), "lesson_settings",
                                self._scope_meta(scope), now)
            return cfg

        return await self._mem.transact(go)

    # ----- read -----------------------------------------------------------

    async def _load(self, lesson_id: str) -> Optional[Lesson]:
        if not str(lesson_id or "").startswith(_ID_PREFIX):
            return None
        entry = await self._mem.get_entry(lesson_id)
        raw = (entry.metadata or {}).get("lesson") if entry is not None else None
        if not isinstance(raw, dict):
            return None
        lesson = Lesson.from_dict(raw)
        lesson.rev = max(1, int(lesson.rev or 0))  # it is stored, so it has a revision
        return lesson

    async def get(self, scope: Scope, lesson_id: str, *,
                  all_users: bool = False) -> Optional[Lesson]:
        """The lesson, or None when it does not exist OR belongs to another scope."""
        lesson = await self._load(lesson_id)
        if lesson is None or not lesson.visible_to(scope, all_users=all_users):
            return None
        return lesson

    async def list(self, scope: Scope, *, status: Optional[str] = None,
                   kind: Optional[str] = None, all_users: bool = False) -> List[Lesson]:
        """Every lesson the scope may see: waiting ones first, then most recently useful."""
        out: List[Lesson] = []
        for entry in await self._mem.entries(_ID_PREFIX):
            raw = (entry.metadata or {}).get("lesson")
            if not isinstance(raw, dict):
                continue
            lesson = Lesson.from_dict(raw)
            if not lesson.visible_to(scope, all_users=all_users):
                continue
            if status and lesson.status != status:
                continue
            if kind and lesson.kind != kind:
                continue
            lesson.rev = max(1, int(lesson.rev or 0))
            out.append(lesson)
        out.sort(key=lambda x: (_STATUS_ORDER.get(x.status, 9),
                                -(x.last_helped or 0.0), -(x.updated_at or 0.0)))
        return out

    async def active(self, scope: Scope, *, kind: Optional[str] = None,
                     trigger: Optional[str] = None) -> List[Lesson]:
        rows = await self.list(scope, status="active", kind=kind)
        return [r for r in rows if trigger is None or r.trigger == trigger]

    async def page(self, scope: Scope, *, limit: int = VISIBLE_CAP, offset: int = 0,
                   status: Optional[str] = None, kind: Optional[str] = None,
                   all_users: bool = False, agent_name: str = "The agent") -> Dict[str, Any]:
        """The panel payload: next action FIRST, five rows, ``more``, the real total."""
        cfg = await self.settings(scope)
        everything = await self.list(scope, all_users=all_users)
        rows = [r for r in everything
                if (not status or r.status == status) and (not kind or r.kind == kind)]
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        shown = rows[offset:offset + limit]
        counts = {s: 0 for s in STATUSES}
        for r in everything:
            counts[r.status] = counts.get(r.status, 0) + 1
        waiting = counts["proposed"]
        if waiting:
            line = f"{waiting} lesson{'s' if waiting != 1 else ''} waiting for your OK"
        elif cfg.mode == "off":
            line = f"Learning is off. Turn it on in settings to let {agent_name} learn."
        elif not everything:
            line = ("Nothing learned yet. Confirm a mapping or correct an answer and it "
                    "shows up here.")
        else:
            n = counts["active"]
            line = f"{n} lesson{'s' if n != 1 else ''} in use. Nothing needs your OK."
        return {
            "next_action": line,
            "items": [r.view() for r in shown],
            "total": len(rows),
            "more": max(0, len(rows) - offset - len(shown)),
            "waiting": waiting,
            "counts": counts,
            "settings": cfg.to_dict(),
            "last_checked": (await self.last_consolidated(scope)) or None,
        }

    # ----- write ----------------------------------------------------------

    async def _save(self, lesson: Lesson, *, consume: Optional[Lesson] = None,
                    reset_streak: bool = False) -> Lesson:
        """Write ``lesson`` if the store still holds the revision it was read at.

        A new lesson (``rev`` 0) is written only where no lesson exists; a loaded one
        only where the stored revision is the one it was loaded at. Otherwise
        :class:`_StaleWriteError`: the caller starts again from a fresh read. This
        is what stops a slow re-check from undoing an admin's "off", and a stale
        copy from bringing back a lesson that was forgotten.

        ``consume`` is a proposal this write uses up: it is deleted in the same
        transaction, and only if it is still the revision that was read. A
        proposal forgotten or changed in the meantime makes the write stale.

        The use counters belong to :meth:`record_use` (which does not change the
        revision): they are taken from the stored row, not from this copy.
        """
        expected = int(lesson.rev or 0)
        now = self._now()
        tombstone = _TOMBSTONE_PREFIX + lesson.id[len(_ID_PREFIX):]

        def write(conn: Any) -> bool:
            stored = _stored_lesson(conn.execute(_READ_LESSON, (lesson.id,)).fetchone())
            if (stored.rev if stored is not None else 0) != expected:
                return False
            if consume is not None:
                waiting = _stored_lesson(conn.execute(_READ_LESSON, (consume.id,)).fetchone())
                if waiting is None or waiting.rev != consume.rev:
                    return False
            if stored is not None:
                for name in _USE_FIELDS:
                    setattr(lesson, name, getattr(stored, name))
            if reset_streak:
                lesson.unhelpful_streak = 0
            lesson.rev = expected + 1
            lesson.updated_at = now
            content, category, metadata = _row_for(lesson, now)
            LocalOnlyMemory.put(conn, lesson.id, content, category, metadata, now)
            if expected == 0:  # learned again after being forgotten: no longer forgotten
                conn.execute(_DELETE_ROW, (tombstone,))
            if consume is not None:
                conn.execute(_DELETE_ROW, (consume.id,))
            return True

        if not await self._mem.transact(write):
            raise _StaleWriteError(lesson.id)
        return lesson

    @_retrying
    async def capture(
        self,
        scope: Scope,
        *,
        kind: str,
        title: str,
        body: str,
        evidence_type: str,
        source_turn: str,
        confirmed_by: str = "",
        confirmed_by_admin: bool = False,
        note: str = "",
        trigger: str = "",
        data: Optional[Dict[str, Any]] = None,
        case: Optional[Dict[str, Any]] = None,
        run: Optional[ReplayRun] = None,
        body_for: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> CaptureResult:
        """Offer a lesson. It is kept only with evidence and, where a replay case
        exists, only if the case does better with the lesson than without.

        ``case`` is a stored, re-runnable replay case (its ``type`` must be
        registered); ``run`` is a one-off callable for cases that cannot be stored.
        ``body_for`` rebuilds the body from the merged ``data`` when the same
        situation is taught again.

        A lesson goes IN USE only on authority: this call's own check passed AND
        the workspace learns automatically or an admin confirmed it. A capability
        gap has no before/after to check, so it goes in use only when an admin
        reported it. It is then merged into the lesson in use for that situation.
        The check compares with what is in use today, not with nothing: a change
        that would make the lesson in use worse is not kept.

        Everything else is a proposal. It waits in the one slot beside the lesson
        (``data["extends"]``), where further proposals for the same situation are
        merged, and it never changes the lesson in use. An admin's
        :meth:`approve` folds it in. A lesson an admin switched off is changed only
        by an admin's own confirmation.
        """
        if kind not in KINDS:
            raise LessonError(f"kind must be one of: {', '.join(KINDS)}")
        if evidence_type not in EVIDENCE_TYPES or not str(source_turn or "").strip():
            raise NoEvidenceError(
                "a lesson needs an outcome behind it: one of "
                + ", ".join(EVIDENCE_TYPES) + ", and the turn it came from")
        title = _one_line(title, _TITLE_CHARS)
        body = _clean(body, _BODY_CHARS)
        if not title or not body:
            raise LessonError("a lesson needs a title and a body")
        # ``extends`` is the store's own field; a caller cannot aim a proposal anywhere.
        data = {k: v for k, v in (data or {}).items() if k != "extends"}
        _check_data(data)
        cfg = await self.settings(scope)
        if cfg.mode == "off":
            return CaptureResult(False, "learning is off for this workspace")
        if kind == "tool_request" or cfg.applies_to == "workspace":
            scope = Scope(scope.tenant_id, scope.workspace_id, "")  # a gap is nobody's alone
        elif not scope.user_id:
            return CaptureResult(
                False, "learning is set to apply to each person, and this request has no "
                       "signed-in person to keep the lesson for")
        replay_run = run or (_run_for_case(case) if case else None)
        if case and replay_run is None:
            raise LessonError(f"no replay runner registered for case type "
                              f"{(case or {}).get('type')!r}")

        now = self._now()
        evidence = {"type": evidence_type, "source_turn": str(source_turn),
                    "confirmed_by": confirmed_by, "at": now, "note": _clean(note, 500)}

        def candidate(lesson_id: str, base: Optional[Lesson]) -> Lesson:
            """The lesson as it would be stored under ``lesson_id``, merged over ``base``."""
            merged = _merge_data(base.data if base is not None else {}, data)
            made = Lesson(
                id=lesson_id, kind=kind, title=title,
                body=_clean(body_for(merged), _BODY_CHARS) if body_for is not None else body,
                tenant_id=scope.tenant_id, workspace_id=scope.workspace_id or "default",
                user_id=scope.user_id, trigger=trigger, data=merged, evidence=evidence,
                created_at=now,
            )
            if base is not None:
                made.created_at = base.created_at
                made.version = base.version + 1
                made.used_count = base.used_count
                made.helped_count = base.helped_count
                made.unhelpful_streak = base.unhelpful_streak
                made.last_used = base.last_used
                made.last_helped = base.last_helped
                made.replay = dict(base.replay or {})  # the last check that ran
                made.previous = _snapshot(base)
                made.rev = base.rev
            return made

        async def check(made: Lesson, *, effective: Optional[Lesson] = None,
                        baseline: Optional[Lesson] = None) -> Optional[CaptureResult]:
            """Run the replay check and record it on ``made``; a result means: not
            kept, and why. ``effective`` is the lesson as it would be in use
            (``made`` itself unless it is a proposal for a lesson in use);
            ``baseline`` is what is in use today."""
            if replay_run is None:
                return None
            runs = int((made.replay or {}).get("runs", 0) or 0)
            result = await replay_check(effective or made, replay_run, baseline=baseline)
            result["runs"] = runs + 1
            if case:
                result["case"] = case
            made.replay = result
            if result["passed"]:
                return None
            return CaptureResult(
                False,
                "the replay check did not improve with this lesson "
                f"(before {result['before']}, after {result['after']}), so it was not kept",
                made)

        async def mine(lesson_id: str) -> Optional[Lesson]:
            found = await self._load(lesson_id)
            if found is not None and not found.visible_to(scope, all_users=True):
                raise LessonError(
                    "this lesson id belongs to another workspace; nothing was changed")
            return found

        # Proof is THIS call's own check (run below; a failed one returns early).
        proven = replay_run is not None
        activate = ((proven and (cfg.mode == "auto" or confirmed_by_admin))
                    or (kind == "tool_request" and confirmed_by_admin))
        main_id = _lesson_id(scope, kind, trigger)

        if not trigger:  # no situation key: nothing to merge with, it stands alone
            lesson = candidate(main_id, None)
            refused = await check(lesson)
            if refused is not None:
                return refused
            lesson.status = "active" if activate else "proposed"
            await self._save(lesson)
            return CaptureResult(True, "kept and in use" if activate
                                 else "kept, waiting for an admin's OK", lesson)

        main = await mine(main_id)
        if main is not None and main.status == "off" and not confirmed_by_admin:
            return CaptureResult(
                False, "an administrator switched this lesson off, so it was not changed", main)

        in_use = main if (main is not None and main.status == "active") else None

        if activate:
            lesson = candidate(main_id, main)
            refused = await check(lesson, baseline=in_use)
            if refused is not None:
                return refused
            lesson.status = "active"
            lesson.retired_reason = ""
            await self._save(lesson, reset_streak=True)
            return CaptureResult(True, "kept and in use", lesson)

        addition_id = _addition_id(main_id)
        pending = await mine(addition_id)
        if pending is not None and pending.status != "proposed":
            raise LessonError("the waiting proposal for this lesson is in an unexpected "
                              "state; forget it and teach again")
        lesson = candidate(addition_id, pending)
        effective = None
        if in_use is not None:  # judge it as it would be in use: folded into the lesson
            effective = Lesson.from_dict(in_use.to_dict())
            effective.data = _merge_data(in_use.data, lesson.data)
            effective.body = _clean(
                body_for(effective.data) if body_for is not None
                else f"{in_use.body}\n{lesson.body}", _BODY_CHARS)
        lesson.data["extends"] = main_id
        refused = await check(lesson, effective=effective, baseline=in_use)
        if refused is not None:
            return refused
        lesson.status = "proposed"
        await self._save(lesson)
        return CaptureResult(
            True, "kept as a proposed addition, waiting for an admin's OK"
            if in_use is not None else "kept, waiting for an admin's OK", lesson)

    async def capture_confirmed_mapping(
        self,
        scope: Scope,
        *,
        target: str,
        headers: Sequence[str],
        mapping: Dict[str, str],
        required: Sequence[str],
        confirmed_by: str,
        confirmed_by_admin: bool,
        source_turn: str,
    ) -> CaptureResult:
        """A person confirmed which of THEIR columns is which field.

        ``mapping`` is ``their header -> field``. Only the headers that are not
        already the field's own name are worth remembering; the replay case is the
        sheet that needed the confirmation.
        """
        aliases = {
            normalize_header(h): f for h, f in (mapping or {}).items()
            if isinstance(f, str) and f in required and normalize_header(h)
            and normalize_header(h) != normalize_header(f)
        }
        if not aliases:
            return CaptureResult(False, "the sheet already uses the expected column names; "
                                        "there is nothing to learn")

        def body_for(merged: Dict[str, Any]) -> str:
            pairs = "; ".join(
                f"'{h}' is {f}" for h, f in sorted((merged.get("aliases") or {}).items()))
            return f"In {target} spreadsheets from this workspace: {pairs}."

        return await self.capture(
            scope, kind="memory",
            title=f"How this workspace names the columns of its {target} spreadsheets",
            body=body_for({"aliases": aliases}),
            evidence_type="confirmed_mapping", source_turn=source_turn,
            confirmed_by=confirmed_by, confirmed_by_admin=confirmed_by_admin,
            trigger=f"sheet-columns:{target}", data={"aliases": aliases, "target": target},
            case={"type": "sheet-columns", "headers": list(headers), "required": list(required)},
            body_for=body_for,
        )

    async def capture_capability_gap(
        self,
        scope: Scope,
        *,
        surface: str,
        capability: str,
        workaround: str,
        source_turn: str,
        reported_by: str = "",
        reported_by_admin: bool = False,
        ticket: str = "",
    ) -> CaptureResult:
        """The agent lacked a capability. Record the gap and what to do meanwhile.

        ``surface`` and ``capability`` are product slugs chosen by CODE, never text
        from the customer; the customer's own words go in ``workaround`` only. The
        workaround is put in every colleague's context once the lesson is in use and
        nothing can check it, so in EVERY mode a gap a member hit waits for an
        admin's OK; a gap an admin reported is in use at once.
        """
        if not _SLUG_RE.match(surface or "") or not _SLUG_RE.match(capability or ""):
            raise LessonError("surface and capability must be product slugs (a-z, 0-9, '.', '-')")
        return await self.capture(
            scope, kind="tool_request",
            title=f"Cannot {capability} in {surface} yet",
            body=(f"This agent cannot {capability} in {surface}. Say so plainly instead of "
                  f"failing, and offer the workaround: {_clean(workaround, 600)}"),
            evidence_type="capability_gap", source_turn=source_turn,
            confirmed_by=reported_by, confirmed_by_admin=reported_by_admin,
            trigger=f"capability:{surface}:{capability}",
            data={"product": {"surface": surface, "capability": capability},
                  "workaround": _clean(workaround, 600),
                  **({"ticket": str(ticket)} if ticket else {})},  # never blank a ticket
        )

    # ----- the admin's controls ------------------------------------------

    async def _mutable(self, scope: Scope, lesson_id: str) -> Lesson:
        lesson = await self.get(scope, lesson_id, all_users=True)
        if lesson is None:
            raise LessonNotFoundError("no such lesson")
        return lesson

    @_retrying
    async def approve(self, scope: Scope, lesson_id: str, by: str) -> Lesson:
        """An admin's OK on a waiting lesson. Returns the lesson that is now in use.

        A proposal that waited beside a lesson is folded into that lesson (one undo
        takes it back out, and the lesson's on/off state is not touched). Where
        nothing is in use for the situation yet, the proposal becomes the lesson.
        Either way the returned id is the lesson's, not the proposal's. Writing the
        lesson and removing the proposal are one transaction, and only if the
        proposal is still the one that was read: a proposal forgotten or changed
        while the approval was running is not approved.
        """
        lesson = await self._mutable(scope, lesson_id)
        if lesson.status != "proposed":
            raise LessonError(
                f"only a waiting lesson can be approved (this one is {lesson.status})")
        confirmed = {"confirmed_by": by, "confirmed_at": self._now()}
        addition = {k: v for k, v in (lesson.data or {}).items() if k != "extends"}
        parent_id = _parent_of(lesson)
        if not parent_id:  # a lesson with no situation key: it is approved where it is
            lesson.data = addition
            lesson.previous = _snapshot(lesson)
            lesson.status = "active"
            lesson.evidence = {**lesson.evidence, **confirmed}
            return await self._save(lesson)
        parent = await self._load(parent_id)
        if parent is not None and (not parent.visible_to(scope, all_users=True)
                                   or parent.kind != lesson.kind):
            raise LessonError("this proposal can no longer be approved; forget it instead")
        if parent is None:
            promoted = Lesson.from_dict({
                **lesson.to_dict(), "id": parent_id, "rev": 0, "data": addition,
                "status": "active", "previous": {},
                "evidence": {**lesson.evidence, **confirmed}})
            await self._save(promoted, consume=lesson)
            return promoted
        parent.previous = _snapshot(parent)
        parent.data = _merge_data(parent.data, addition)
        if lesson.body not in parent.body:
            parent.body = _clean(f"{parent.body}\n{lesson.body}", _BODY_CHARS)
        parent.version += 1
        parent.evidence = {**parent.evidence, **confirmed}
        await self._save(parent, consume=lesson)
        return parent

    @_retrying
    async def set_enabled(self, scope: Scope, lesson_id: str, on: bool, by: str) -> Lesson:
        lesson = await self._mutable(scope, lesson_id)
        if lesson.status == "proposed":
            raise LessonError("this lesson is waiting for an OK; approve it or forget it")
        target = "active" if on else "off"
        if lesson.status == target:
            return lesson
        lesson.previous = _snapshot(lesson)
        lesson.status = target
        if on:
            lesson.retired_reason = ""
        return await self._save(lesson, reset_streak=on)

    @_retrying
    async def edit(self, scope: Scope, lesson_id: str, by: str, *,
                   title: Optional[str] = None, body: Optional[str] = None,
                   data: Optional[Dict[str, Any]] = None) -> Lesson:
        lesson = await self._mutable(scope, lesson_id)
        snap = _snapshot(lesson)
        if title is not None:
            if not _one_line(title, _TITLE_CHARS):
                raise LessonError("a lesson needs a title")
            lesson.title = _one_line(title, _TITLE_CHARS)
        if body is not None:
            if not _clean(body, _BODY_CHARS):
                raise LessonError("a lesson needs a body")
            lesson.body = _clean(body, _BODY_CHARS)
        if data is not None:
            _check_data(data)
            new = {k: v for k, v in data.items() if k != "extends"}
            if (lesson.data or {}).get("extends"):  # where a proposal goes is not editable
                new["extends"] = lesson.data["extends"]
            lesson.data = new
        lesson.previous = snap
        lesson.version += 1
        lesson.evidence = {**lesson.evidence, "edited_by": by, "edited_at": self._now()}
        return await self._save(lesson)

    @_retrying
    async def undo(self, scope: Scope, lesson_id: str, by: str) -> Lesson:
        """Put back the state before the last on / off / edit, or take a folded-in
        proposal back out."""
        lesson = await self._mutable(scope, lesson_id)
        prev = lesson.previous or {}
        if not prev:
            raise LessonError("nothing to undo on this lesson")
        for key in ("title", "body", "data", "status", "retired_reason"):
            if key in prev:
                setattr(lesson, key, prev[key])
        lesson.previous = {}
        lesson.version += 1
        return await self._save(lesson)

    async def forget(self, scope: Scope, lesson_id: str, by: str) -> bool:
        """Delete the lesson and its text for good. Not undoable, by design.

        A content-free tombstone (id, who, when) stays so an endpoint that syncs
        later drops its copy instead of bringing the lesson back. Deleting and
        writing the tombstone are one transaction, and no copy read before it can
        be saved after it.
        """
        lesson = await self.get(scope, lesson_id, all_users=True)
        if lesson is None:
            return False
        now = self._now()
        tombstone = _TOMBSTONE_PREFIX + lesson.id[len(_ID_PREFIX):]
        meta = {"tenant_id": lesson.tenant_id, "workspace_id": lesson.workspace_id,
                "forgotten_by": by, "at": now}

        def go(conn: Any) -> None:
            conn.execute("DELETE FROM kv_store WHERE key = ?", (lesson.id,))
            LocalOnlyMemory.put(conn, tombstone, "", "lesson_tombstone", meta, now)

        await self._mem.transact(go)
        return True

    async def forgotten(self, scope: Scope) -> List[str]:
        """Ids of lessons this workspace forgot (for a syncing endpoint)."""
        out = []
        for entry in await self._mem.entries(_TOMBSTONE_PREFIX):
            md = entry.metadata or {}
            if md.get("tenant_id") == scope.tenant_id and \
                    (md.get("workspace_id") or "default") == (scope.workspace_id or "default"):
                out.append(_ID_PREFIX + entry.key[len(_TOMBSTONE_PREFIX):])
        return out

    # ----- use, and retirement by outcome --------------------------------

    async def record_use(self, scope: Scope, lesson_id: str,
                         helped: Optional[bool] = None) -> Optional[Lesson]:
        """Count a use. ``helped`` True/False is the outcome; None = not known yet.

        One transaction on the stored row. Counting does not change the revision,
        so a lesson that is used on every request never invalidates a check or an
        edit that is under way; retiring it (three unhelpful uses in a row) does.
        """
        if not str(lesson_id or "").startswith(_ID_PREFIX):
            return None
        now = self._now()

        def go(conn: Any) -> Optional[Lesson]:
            lesson = _stored_lesson(conn.execute(_READ_LESSON, (lesson_id,)).fetchone())
            if lesson is None or not lesson.visible_to(scope, all_users=True):
                return None
            lesson.used_count += 1
            lesson.last_used = now
            if helped is True:
                lesson.helped_count += 1
                lesson.last_helped = now
                lesson.unhelpful_streak = 0
            elif helped is False:
                lesson.unhelpful_streak += 1
                if (lesson.unhelpful_streak >= RETIRE_AFTER_UNHELPFUL
                        and lesson.status == "active"):
                    lesson.previous = _snapshot(lesson)
                    lesson.status = "retired"
                    lesson.retired_reason = (
                        f"did not help {lesson.unhelpful_streak} times in a row")
                    lesson.rev += 1
                    lesson.updated_at = now
            content, category, metadata = _row_for(lesson, now)
            LocalOnlyMemory.put(conn, lesson.id, content, category, metadata, now)
            return lesson

        return await self._mem.transact(go)

    @_retrying
    async def recheck(self, scope: Scope, lesson_id: str,
                      run: Optional[ReplayRun] = None) -> Optional[Lesson]:
        """Re-run a lesson's replay case; retire the lesson if it no longer helps.

        The check may take a while. If the lesson was changed, switched off or
        forgotten while it ran, the result is thrown away and the check starts
        again from the lesson as it now is.
        """
        lesson = await self.get(scope, lesson_id, all_users=True)
        if lesson is None:
            return None
        case = (lesson.replay or {}).get("case")
        replay_run = run or (_run_for_case(case) if case else None)
        if replay_run is None:
            return lesson  # nothing re-runnable was stored for it
        result = await replay_check(lesson, replay_run)
        result["runs"] = int((lesson.replay or {}).get("runs", 0) or 0) + 1
        if case:
            result["case"] = case
        lesson.replay = result
        if not result["passed"] and lesson.status == "active":
            lesson.previous = _snapshot(lesson)
            lesson.status = "retired"
            lesson.retired_reason = (
                f"replay check stopped improving (before {result['before']}, "
                f"after {result['after']})")
        return await self._save(lesson)

    async def consolidate(self, scope: Scope, *, force: bool = False) -> Dict[str, Any]:
        """Re-check every active lesson with a stored case, when the schedule says so.

        ``task``: every call. ``nightly`` / ``weekly``: at most once per period, on
        the first call after it is due. Returns what it did. It reads the settings
        and never writes them; the time of the last run has its own row, and
        "is it due" plus "mark it done" is one transaction, so two callers arriving
        together do not both run. One broken case does not stop the others.
        """
        cfg = await self.settings(scope)
        now = self._now()
        period = _CONSOLIDATE_SECONDS.get(cfg.consolidate, 0.0)
        stamp_key = self._stamp_key(scope)

        def claim(conn: Any) -> Optional[float]:
            """None = ours to run (and stamped); else the seconds since the last run."""
            row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (stamp_key,)).fetchone()
            try:
                last = float(row[0]) if row else 0.0
            except ValueError:
                last = 0.0
            elapsed = now - last
            # A negative elapsed means the clock was set back: due, not "never again".
            if not force and period and 0 <= elapsed < period:
                return elapsed
            LocalOnlyMemory.put(conn, stamp_key, repr(float(now)), "lesson_stamp",
                                self._scope_meta(scope), now)
            return None

        not_due = await self._mem.transact(claim)
        if not_due is not None:
            return {"ran": False, "checked": 0, "retired": 0, "failed": 0,
                    "next_due_in_s": round(period - not_due)}
        checked = retired = failed = 0
        for lesson in await self.list(scope, status="active", all_users=True):
            if not (lesson.replay or {}).get("case"):
                continue
            try:
                after = await self.recheck(scope, lesson.id)
            except Exception as exc:  # noqa: BLE001 -- one broken case must not stop the rest
                failed += 1
                logger.warning("lesson %s: re-check failed (%s); it stays as it is",
                               lesson.id, type(exc).__name__)
                continue
            checked += 1
            if after is not None and after.status == "retired":
                retired += 1
        return {"ran": True, "checked": checked, "retired": retired, "failed": failed}

    # ----- apply at the moment of use ------------------------------------

    async def find_mapping(self, scope: Scope, *, target: str, headers: Sequence[str],
                           required: Sequence[str]) -> Optional[Dict[str, str]]:
        """``field -> header`` when this workspace's active lessons plus the fields'
        own names resolve EVERY required field; None when a person has to be asked."""
        lessons = await self.active(scope, trigger=f"sheet-columns:{target}")
        mapping, missing, used = map_columns(headers, required, lessons)
        if missing:
            return None
        for lesson_id in used:
            await self._count_use(scope, lesson_id, True)
        return mapping

    async def _count_use(self, scope: Scope, lesson_id: str, helped: Optional[bool]) -> None:
        """Count a use; bookkeeping never fails the caller who is applying a lesson."""
        try:
            await self.record_use(scope, lesson_id, helped=helped)
        except Exception as exc:  # noqa: BLE001 -- the lesson was applied; say so and go on
            logger.warning("lesson %s: a use was not counted (%s)", lesson_id,
                           type(exc).__name__)

    async def context_block(self, scope: Scope, query: str, *, limit: int = VISIBLE_CAP,
                            max_chars: int = 1500) -> str:
        """Active lessons that bear on ``query``, ready to put in the prompt.

        A lesson is included when it shares at least two content words with the
        query (one, for a query of one or two words). Bounded by ``max_chars``.
        """
        want = _words(query)
        if not want:
            return ""
        need = 1 if len(want) <= 2 else 2
        scored = []
        for lesson in await self.active(scope):
            have = _words(f"{lesson.title} {lesson.body} {lesson.trigger}")
            overlap = len(want & have)
            if overlap >= need:
                scored.append((overlap, lesson.last_helped or 0.0, lesson))
        if not scored:
            return ""
        scored.sort(key=lambda t: (-t[0], -t[1]))
        header = "# LESSONS learned in this workspace (follow them)"
        lines: List[str] = []
        used = len(header)
        for _overlap, _when, lesson in scored[:max(1, limit)]:
            line = f"- [{lesson.kind}] {lesson.title}: {' '.join(lesson.body.split())}"
            if len(line) > 500:
                line = line[:499].rstrip() + "…"
            if lines and used + 1 + len(line) > max_chars:
                break
            lines.append(line)
            used += 1 + len(line)
            await self._count_use(scope, lesson.id, None)
        return header + "\n" + "\n".join(lines)


def _snapshot(lesson: Lesson) -> Dict[str, Any]:
    return {"title": lesson.title, "body": lesson.body, "data": dict(lesson.data or {}),
            "status": lesson.status, "retired_reason": lesson.retired_reason}


def _merge_data(base: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``new`` over ``base``; a dict value (the aliases) is merged one level deep."""
    merged = dict(base or {})
    for key, value in (new or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# ── SKILL.md (agentskills.io) ───────────────────────────────────────────────

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: A SKILL.md larger than this is refused unread; so is a frontmatter block larger
#: than its own cap (the spec's fields fit in a fraction of it).
SKILL_MD_MAX_CHARS = 100_000
_FRONTMATTER_MAX_CHARS = 8_000
_FRONTMATTER_MAX_DEPTH = 8


def skill_name(title: str) -> str:
    """An agentskills.io name: 1-64 chars of a-z, 0-9 and single hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:64].strip("-")
    return slug or "unnamed-skill"


def to_skill_md(lesson: Lesson) -> Tuple[str, str]:
    """``(directory name, SKILL.md text)`` for a lesson of kind ``skill``.

    The name is the parent directory's name (the spec requires them to match);
    ``metadata`` is a string-to-string map carrying the evidence and the replay
    numbers, so the before/after test travels with the skill.
    """
    import yaml

    if lesson.kind != "skill":
        raise LessonError("only a lesson of kind 'skill' exports as SKILL.md")
    data = lesson.data or {}
    name = skill_name(str(data.get("name") or lesson.title))
    when = _one_line(data.get("when_to_use"), 600)
    description = _one_line(lesson.title, 400)
    if when:
        description = f"{description}. Use when {when[0].lower() + when[1:]}"
    replay = lesson.replay or {}
    metadata = {
        "source": "aither-lessons",
        "lesson-id": lesson.id,
        "evidence": str((lesson.evidence or {}).get("type", "")),
        "version": str(lesson.version),
    }
    if replay:
        metadata["replay-before"] = str(replay.get("before"))
        metadata["replay-after"] = str(replay.get("after"))
    front = yaml.safe_dump(
        {"name": name, "description": description[:1024], "metadata": metadata},
        sort_keys=False, allow_unicode=True, default_flow_style=False, width=10_000,
    ).strip()
    parts = [f"---\n{front}\n---", f"# {lesson.title}", lesson.body.strip()]
    if when:
        parts += ["## When to use", when]
    steps = [str(s).strip() for s in (data.get("steps") or []) if str(s).strip()]
    if steps:
        parts += ["## Steps", "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))]
    inputs = [str(s).strip() for s in (data.get("inputs") or []) if str(s).strip()]
    if inputs:
        parts += ["## Inputs", "\n".join(f"- {s}" for s in inputs)]
    example = str(data.get("example") or "").strip()
    if example:
        parts += ["## Worked example", example]
    return name, "\n\n".join(parts) + "\n"


def _split_frontmatter(text: str) -> Tuple[str, str]:
    """``(frontmatter, body)`` of a ``---`` fenced document, in one linear pass."""
    wrong = LessonError("SKILL.md must start with a YAML frontmatter block (--- ... ---)")
    first = text.find("\n")
    if first == -1 or text[:first].strip() != "---":
        raise wrong
    start = pos = first + 1
    while True:
        end = text.find("\n", pos)
        line = text[pos:] if end == -1 else text[pos:end]
        if line.strip() == "---":
            return text[start:pos], ("" if end == -1 else text[end + 1:])
        if end == -1:
            raise wrong
        pos = end + 1
        if pos - start > _FRONTMATTER_MAX_CHARS:
            raise LessonError(
                f"SKILL.md frontmatter is larger than {_FRONTMATTER_MAX_CHARS} characters")


def _load_frontmatter(raw: str) -> Any:
    """Parse frontmatter that is plain data: no anchors, aliases or tags, shallow.

    Checked on the TOKEN stream before anything is built. ``yaml.safe_load`` on its
    own expands chained merge keys (``<<: [*a, *a, ...]``) to gigabytes from a few
    hundred characters, and recurses once per nesting level.
    """
    import yaml

    depth = 0
    try:
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.AnchorToken, yaml.AliasToken, yaml.TagToken)):
                raise LessonError(
                    "SKILL.md frontmatter must be plain YAML: no anchors, aliases or tags")
            if isinstance(token, (yaml.FlowSequenceStartToken, yaml.FlowMappingStartToken,
                                  yaml.BlockSequenceStartToken, yaml.BlockMappingStartToken)):
                depth += 1
                if depth > _FRONTMATTER_MAX_DEPTH:
                    raise LessonError("SKILL.md frontmatter is nested too deeply")
            elif isinstance(token, (yaml.FlowSequenceEndToken, yaml.FlowMappingEndToken,
                                    yaml.BlockEndToken)):
                depth -= 1
        return yaml.safe_load(raw) or {}
    except LessonError:
        raise
    except RecursionError as exc:
        raise LessonError("SKILL.md frontmatter is nested too deeply") from exc
    except (yaml.YAMLError, ValueError, OverflowError) as exc:
        # ValueError / OverflowError: the loader building a date ("2024-13-45"), a
        # 5000-digit number or an escape outside Unicode.
        raise LessonError(
            f"SKILL.md frontmatter is not valid YAML ({type(exc).__name__})") from exc


def parse_skill_md(text: str) -> Dict[str, Any]:
    """Validate a SKILL.md against the agentskills.io required fields and split it.

    Returns ``{"name", "description", "metadata", "title", "body", "data"}``. Raises
    :class:`LessonError` naming the field that is wrong.
    """
    text = text or ""
    if len(text) > SKILL_MD_MAX_CHARS:
        raise LessonError(f"SKILL.md is larger than {SKILL_MD_MAX_CHARS} characters")
    raw_front, body = _split_frontmatter(text)
    front = _load_frontmatter(raw_front)
    if not isinstance(front, dict):
        raise LessonError("SKILL.md frontmatter must be a mapping")
    name = front.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 64) \
            or not _SKILL_NAME_RE.match(name):
        raise LessonError("SKILL.md name must be 1-64 chars of a-z, 0-9 and single hyphens")
    description = front.get("description")
    if not isinstance(description, str) or not (1 <= len(description.strip()) <= 1024):
        raise LessonError("SKILL.md description must be 1-1024 characters of text")
    description = description.strip()
    body = body.strip()
    sections: Dict[str, str] = {}
    current = ""
    lead: List[str] = []
    title = ""
    for line in body.splitlines():
        h1 = re.match(r"^#\s+(.+)$", line)
        h2 = re.match(r"^##\s+(.+)$", line)
        if h1 and not title and not current:
            title = h1.group(1).strip()
        elif h2:
            current = h2.group(1).strip().lower()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
        else:
            lead.append(line)

    def _items(block: str) -> List[str]:
        out = []
        for raw in (block or "").splitlines():
            item = re.sub(r"^\s*(?:\d+[.)]|[-*])\s+", "", raw).strip()
            if item:
                out.append(item)
        return out

    data = {
        "name": name,
        "when_to_use": " ".join(sections.get("when to use", "").split()),
        "steps": _items(sections.get("steps", "")),
        "inputs": _items(sections.get("inputs", "")),
        "example": sections.get("worked example", "").strip(),
    }
    raw_meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    return {
        "name": name,
        "description": description,
        "metadata": {k: str(v) for k, v in raw_meta.items()
                     if isinstance(k, str) and isinstance(v, (str, int, float, bool))},
        "title": title or name.replace("-", " ").capitalize(),
        "body": "\n".join(lead).strip() or description,
        "data": {k: v for k, v in data.items() if v},
    }


async def import_skill_md(store: LessonStore, scope: Scope, text: str, *,
                          imported_by: str) -> CaptureResult:
    """Bring a SKILL.md in as a lesson of kind ``skill``.

    It arrives ``proposed`` in ``ask`` mode AND in ``auto`` mode: an imported skill
    has no outcome in this workspace yet, so it waits for an admin's OK.
    """
    parsed = parse_skill_md(text)
    return await store.capture(
        scope, kind="skill", title=parsed["title"], body=parsed["body"],
        evidence_type="imported", source_turn=f"skill.md:{parsed['name']}",
        confirmed_by=imported_by, confirmed_by_admin=False,
        trigger=f"skill:{parsed['name']}", data=parsed["data"],
    )


# ── the one outbound shape: a content-free product lesson ───────────────────

#: Capabilities a workspace may report missing. A CLOSED vocabulary: an outbound
#: lesson is built from these slugs and nothing else, so it cannot carry a word of
#: a customer's content. Engineering extends the set; a gap outside it still becomes
#: a local lesson and a support request, it just is not contributed.
PRODUCT_CAPABILITIES = {
    "read-xlsx", "read-xls", "read-csv", "read-docx", "read-doc", "read-pptx",
    "read-pdf-scanned", "read-image", "read-email-archive", "read-zip",
    "write-xlsx", "write-docx", "write-pdf", "send-email", "create-calendar-event",
    "open-shared-drive", "detect-roster-by-content", "map-sheet-columns",
}
PRODUCT_SURFACES = {
    "chat", "documents.upload", "documents.search", "sheet.import", "mail", "calendar",
}
#: The products that may sign an outbound lesson (``source: product:<agent>``).
PRODUCT_AGENTS = {"agent"}


def register_product_vocabulary(*, surfaces: Iterable[str] = (),
                                capabilities: Iterable[str] = (),
                                agents: Iterable[str] = ()) -> None:
    """Add a host product's own slugs to the closed vocabulary.

    Called by CODE at import time with literals. Never call it with a value that
    came from a user, a file or a model: that would open the vocabulary, and the
    vocabulary being closed is what keeps an outbound lesson content-free.
    """
    surfaces, capabilities, agents = list(surfaces), list(capabilities), list(agents)
    for slug in surfaces + capabilities + agents:
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise LessonError(f"not a product slug: {slug!r}")
    PRODUCT_SURFACES.update(surfaces)
    PRODUCT_CAPABILITIES.update(capabilities)
    PRODUCT_AGENTS.update(agents)


#: The fixed fields of the outbound row. Ours, not a customer's.
_PRODUCT_ROW_FIXED = {
    "kind": "tool_request", "class": "knowledge",
    "verified_by": "capability_gap", "review": "pending",
}
_PRODUCT_ROW_KEYS = frozenset(("id", "lesson", "source", *_PRODUCT_ROW_FIXED))
_PRODUCT_TEXT = "{surface}: missing capability {capability}"


def _product_row(surface: str, capability: str, agent: str) -> Dict[str, Any]:
    text = _PRODUCT_TEXT.format(surface=surface, capability=capability)
    return {
        "id": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "lesson": text,
        "source": f"product:{agent}",
        **_PRODUCT_ROW_FIXED,
    }


def _registered(slug: Any, vocabulary: set) -> bool:
    """True only for a plain string that code registered. Anything else is not ours."""
    return isinstance(slug, str) and slug in vocabulary


def _product_slugs(lesson: Lesson) -> Tuple[Any, Any]:
    product = (lesson.data or {}).get("product")
    if not isinstance(product, dict):
        return None, None
    return product.get("surface"), product.get("capability")


def assert_content_free(payload: Dict[str, Any], lesson: Lesson) -> None:
    """Raise :class:`TenantTextLeakError` unless ``payload`` is provably content-free.

    An ALLOWLIST, not a search for known tenant words (a search misses a name in
    another script, a one-letter initial, or a phrase made of ordinary words):

    1. Shape. The row must be exactly the one :func:`product_lesson` builds from
       three registered slugs (a surface, a capability, an agent): the same seven
       keys, the fixed values, and nothing else. An extra field or a longer
       sentence is refused without looking at what it says.
    2. Words. Every word in the row must come from those three slugs, the fixed
       template or the row's own id.
    """
    if not isinstance(payload, dict) or set(payload) != _PRODUCT_ROW_KEYS:
        raise TenantTextLeakError("refused: the outbound row has fields it must not have")
    source = payload.get("source")
    agent = None
    if isinstance(source, str) and source.startswith("product:"):
        agent = source[len("product:"):]
    surface, capability = _product_slugs(lesson)
    if not (_registered(agent, PRODUCT_AGENTS) and _registered(surface, PRODUCT_SURFACES)
            and _registered(capability, PRODUCT_CAPABILITIES)):
        raise TenantTextLeakError(
            "refused: the outbound row is not built from the product vocabulary")
    if payload != _product_row(surface, capability, agent):
        raise TenantTextLeakError(
            "refused: the outbound row carries text the product vocabulary did not write")
    allowed = _tokens(_PRODUCT_TEXT.format(surface="", capability=""))
    allowed |= _tokens("product " + " ".join(_PRODUCT_ROW_FIXED.values()))
    allowed |= _tokens(payload["id"])
    for slug in (surface, capability, agent):
        allowed |= _tokens(slug)
    sent: set = set()
    for value in payload.values():
        sent |= _tokens(value)
    if sent - allowed:
        raise TenantTextLeakError(
            "refused: a word outside the product vocabulary would have left the workspace")


def product_lesson(lesson: Lesson, settings: LessonSettings, *,
                   agent: str = "agent") -> Dict[str, Any]:
    """The content-free row a workspace may contribute to the vendor.

    Refuses unless the workspace switched contribution ON, the lesson is a
    ``tool_request``, and the surface, the capability and the agent are all in the
    closed vocabulary. The keys are the platform harvest's candidate-row keys
    (``id, lesson, source, kind, class, verified_by, review``) so both sides speak
    one record.
    """
    if not settings.share_product_lessons:
        raise LessonError("contributing product lessons is off for this workspace")
    if lesson.kind != "tool_request":
        raise LessonError("only a tool request can be contributed; everything else "
                          "stays in the workspace")
    if not _registered(agent, PRODUCT_AGENTS):
        raise TenantTextLeakError("refused: this agent is not a registered product")
    surface, capability = _product_slugs(lesson)
    if not (_registered(surface, PRODUCT_SURFACES)
            and _registered(capability, PRODUCT_CAPABILITIES)):
        raise TenantTextLeakError(
            "refused: this gap is not in the product vocabulary yet, so it cannot be "
            "described without the workspace's own words")
    payload = _product_row(surface, capability, agent)
    assert_content_free(payload, lesson)
    return payload


__all__ = [
    "APPLIES_TO", "CONSOLIDATE", "EVIDENCE_TYPES", "KINDS", "MODES", "STATUSES",
    "PRODUCT_AGENTS", "PRODUCT_CAPABILITIES", "PRODUCT_SURFACES", "REPLAY_RUNNERS",
    "RETIRE_AFTER_UNHELPFUL", "SKILL_MD_MAX_CHARS", "VISIBLE_CAP", "CaptureResult",
    "Lesson", "LessonError", "LessonNotFoundError", "LessonSettings", "LessonStore",
    "LocalOnlyMemory", "NoEvidenceError", "Scope", "TenantTextLeakError",
    "assert_content_free", "import_skill_md", "map_columns", "normalize_header",
    "parse_skill_md", "product_lesson", "register_product_vocabulary", "register_replay",
    "replay_check", "skill_name", "to_skill_md",
]
