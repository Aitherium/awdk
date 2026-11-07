"""work_items — one durable work item per routed request: who may ask, what was
granted, how far it got, and whether a person must act now.

WHY THIS EXISTS
---------------
``thread_subs`` keeps the fast path (a follow-up on an owned thread goes straight to its
worker) and ``thread_router`` keeps the slow path (the FIRST request on an unowned thread
spawns one worker). Between them a routed request becomes a session and is then forgotten:
there is no record of *why* compute was spent on it, what the sender was allowed to ask
for, whether it is still working, and which requests are stalled waiting for a human.

This module is the missing middle, adapted from the work-item model in DeepSeek's
``dsh-superagent`` reference prototype (MIT, github.com/wizzense/dsh-superagent-prototype,
docs/001-003). It reimplements the ideas on primitives this daemon already has — room
events, thread keys and the steering mailbox — and adds no dependency: stdlib only, so it
imports on Python 3.10 exactly like ``thread_subs``.

THREE SEPARATIONS THE PROTOTYPE GOT RIGHT
-----------------------------------------
1. **Authorization is not routing.** A subscription or a delivered message proves only
   that a message arrived. Authority lives in a :class:`Mandate` minted by the gate, and
   a :class:`WorkItem` carries it for its whole life. ``thread_subs`` says the same thing
   about its rows ("a routing hint, never authorization"); this module is where the other
   half lives.
2. **Proactive work is observed before it is accepted.** ``accept_mode`` ``off`` admits
   nothing inferred; ``observe`` records the decision it *would* take and spends no
   compute; ``auto`` accepts it only when at least one written rule matched.
3. **A work item has its own stage.** The stage says whom it waits for and what it needs
   (a person, a queue slot, or a trigger) instead of copying a session's internal status.
   ``needs_person()`` is the only list a panel should lead with.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No model call, no timer, no second scheduler: a gate is a pure function of the event,
the policy and the matched rules. The caller decides what to do with an accept; this
module only records that it happened.

STORE
-----
One SQLite file, one row per work item, the mutable fields as JSON in a ``payload``
column. Idempotent on ``(surface, thread_id, event_id)``: a retrying producer cannot mint
a second item for the same request. ``AITHER_WORK_ITEMS_DB`` overrides the path.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

#: ``off``: nothing inferred is admitted. ``observe``: inferred candidates are judged and
#: recorded as ``would_accept``, and nothing starts. ``auto``: a positive judgement with a
#: matched rule is accepted.
MODES: Tuple[str, ...] = ("off", "observe", "auto")
STRENGTHS: Tuple[str, ...] = ("explicit", "inferred")
REPORT_STATES: Tuple[str, ...] = (
    "progress", "waiting", "ready", "blocked", "failed", "dropped",
)
ASSIGNMENT_STATES: Tuple[str, ...] = ("pending", "running", "ended")
OUTCOMES: Tuple[str, ...] = ("completed", "merged", "closed", "dropped", "stopped")

#: How many observations (``would_accept`` decisions) are kept. They are audit trail, not
#: work: a bounded ring keeps a panel honest without growing the database forever.
MAX_OBSERVATIONS = 500

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class WorkItemError(ValueError):
    """A malformed work item, mandate or policy."""


def default_db_path() -> Path:
    env = os.environ.get("AITHER_WORK_ITEMS_DB", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "work-items.db"


# ── the policy ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """One kind of proactive work a team has written down and enabled."""

    id: str
    name: str
    instruction: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class AdmissionPolicy:
    """Who may originate work on each surface, and what proactive work is accepted.

    ``allowed`` and ``bots`` are keyed by surface (``github``, ``discord``, ``mail``)
    and hold the sender ids the team listed. Identities on different surfaces are never
    mapped to each other: a GitHub login and a Discord snowflake are different people
    until a team says otherwise in its prompts.
    """

    allowed: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    bots: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    accept_mode: str = "observe"
    rules: Tuple[Rule, ...] = ()
    #: True when an unlisted sender who *addresses* the agent may make an explicit
    #: request (the prototype's ``unlistedAddressed: explicit`` admission cell). It is
    #: off by default: an unlisted sender addressing the agent is ignored.
    explicit_for_unlisted: bool = False
    version: str = ""

    def __post_init__(self) -> None:
        if self.accept_mode not in MODES:
            raise WorkItemError(
                f"accept_mode {self.accept_mode!r} is not one of {', '.join(MODES)}")
        for surface, senders in list(self.allowed.items()) + list(self.bots.items()):
            if not isinstance(surface, str) or not surface:
                raise WorkItemError(f"bad surface {surface!r}")
            for sender in senders:
                if not str(sender or "").strip():
                    raise WorkItemError(f"blank sender listed for {surface}")

    @property
    def enabled_rule_ids(self) -> FrozenSet[str]:
        return frozenset(rule.id for rule in self.rules if rule.enabled)

    def _is_allowed(self, surface: str, sender_id: str) -> bool:
        return sender_id in self.allowed.get(surface, frozenset())

    def _is_bot(self, surface: str, sender_id: str) -> bool:
        return sender_id in self.bots.get(surface, frozenset())

    def gate(
        self,
        *,
        surface: str,
        sender_id: str,
        human: bool,
        addressed: bool,
    ) -> "GateResult":
        """The deterministic first layer: may this sender originate work here?

        ``addressed`` means the event named the agent (a mention or a direct message).
        A bot can never make an explicit request, and a bot that is not on the surface's
        bot list is not admitted at all.
        """
        sid = str(sender_id or "").strip()
        if not sid:
            return GateResult(False, reason="unknown_sender")
        if not human:
            if self._is_bot(surface, sid):
                return GateResult(True, "inferred", "bot", "")
            return GateResult(False, reason="bot_not_listed")
        if self._is_allowed(surface, sid):
            if addressed:
                return GateResult(True, "explicit", "addressed", "")
            return GateResult(True, "inferred", "listed", "")
        if addressed and self.explicit_for_unlisted:
            return GateResult(True, "explicit", "admission", "")
        return GateResult(False, reason="sender_not_allowed")

    def decide(
        self,
        gate: "GateResult",
        *,
        sender: str = "",
        matched_rules: Sequence[str] = (),
        scope: str = "",
    ) -> "Decision":
        """The second layer: accept, observe, or ignore, with the Mandate an accept mints.

        An inferred candidate needs ``auto`` mode AND at least one enabled matched rule;
        a rule an operator disabled cannot carry an accept even if the assessor matched
        it.
        """
        if not gate.admitted:
            return Decision("ignore", gate.reason)
        matched = tuple(
            rule for rule in matched_rules if rule in self.enabled_rule_ids)
        mandate = Mandate(
            strength=gate.strength,
            basis=gate.basis,
            sender=str(sender or ""),
            policy_version=self.version,
            scope=scope,
            rules=matched,
        )
        if gate.strength == "explicit":
            return Decision("accept", "explicit_request", mandate)
        if self.accept_mode == "off":
            return Decision("ignore", "inferred_not_admitted")
        if self.accept_mode == "observe":
            return Decision("would_accept", "observed_inferred", mandate, matched)
        if not matched:
            return Decision("ignore", "no_matching_rule")
        return Decision("accept", "proactive_rule", mandate, matched)

    def revalidate(self, mandate: "Mandate") -> bool:
        """Whether this policy still holds a Mandate, e.g. before an ended item resumes.

        A person's explicit request keeps its authority unless the sender was removed
        from the list. Inferred work needs ``auto`` mode and every matched rule still
        enabled; a bot's Mandate also needs that bot still listed. A table-cell Mandate
        must still receive at least its original strength.
        """
        surface = mandate.surface
        sender = mandate.sender
        if mandate.strength == "explicit":
            if mandate.basis == "addressed":
                return True
            return self._is_allowed(surface, sender) or self.explicit_for_unlisted
        if self.accept_mode != "auto":
            return False
        if not set(mandate.rules) <= self.enabled_rule_ids:
            return False
        if mandate.basis == "bot":
            return self._is_bot(surface, sender)
        if mandate.basis == "admission":
            return self._is_allowed(surface, sender) or self.explicit_for_unlisted
        return self._is_allowed(surface, sender)


@dataclass(frozen=True)
class GateResult:
    admitted: bool
    strength: str = ""
    basis: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Mandate:
    """What an accept grants: strength, basis, policy version and scope. Immutable."""

    strength: str
    basis: str
    sender: str
    policy_version: str
    scope: str
    rules: Tuple[str, ...] = ()
    surface: str = ""

    def __post_init__(self) -> None:
        if self.strength not in STRENGTHS:
            raise WorkItemError(f"bad mandate strength {self.strength!r}")


@dataclass(frozen=True)
class Decision:
    """The recorded outcome of the gate and assessment for one candidate."""

    kind: str
    reason: str
    mandate: Optional[Mandate] = None
    rules: Tuple[str, ...] = ()


# ── the work item ──────────────────────────────────────────────────────────


@dataclass
class WorkItem:
    """One authorized piece of work and the last thing known about it."""

    id: str
    surface: str
    thread_id: str
    event_id: str
    title: str
    sender: str
    mandate: Mandate
    created_at: float
    updated_at: float
    worker_id: str = ""
    status: str = "open"                 # open | ended
    ended_by: str = ""                  # executor | human
    outcome: str = ""                   # completed | merged | closed | dropped | stopped
    report_state: str = ""              # one of REPORT_STATES
    report_summary: str = ""
    assignment_state: str = "pending"   # one of ASSIGNMENT_STATES
    attention: str = ""                 # a question a person must answer
    delivery_error: str = ""            # an input could not reach the worker
    human_since_report: bool = False    # a person's message arrived after the report

    def touch(self, now: Optional[float] = None) -> None:
        self.updated_at = time.time() if now is None else float(now)


STAGE_PERSON = "person"
STAGE_WORKING = "working"
STAGE_IDLE = "idle"
STAGE_ENDED = "ended"


def stage(item: WorkItem) -> Tuple[str, str]:
    """Classify an item by whom it waits for; first matching rule wins (docs/003).

    The returned pair is ``(stage, action)``: ``person`` with ``delivery``, ``answer``,
    ``review``, ``blocked`` or ``failed``; ``working`` with ``queued``; ``idle``; or
    ``ended``. A ``person`` stage is the only one that needs a human, and a new human
    message releases a blocked/failed item back to ``working``.
    """
    if item.status == "ended":
        return STAGE_ENDED, ""
    if item.delivery_error:
        return STAGE_PERSON, "delivery"
    if item.attention:
        return STAGE_PERSON, "answer"
    if item.report_state == "ready":
        return STAGE_PERSON, "review"
    if item.report_state in ("blocked", "failed") and not item.human_since_report:
        return STAGE_PERSON, item.report_state
    if item.assignment_state == "pending":
        return STAGE_WORKING, "queued"
    if item.report_state == "waiting" or item.assignment_state == "ended":
        return STAGE_IDLE, ""
    return STAGE_WORKING, ""


def needs_person(item: WorkItem) -> bool:
    return stage(item)[0] == STAGE_PERSON


# ── the store ──────────────────────────────────────────────────────────────


class WorkItems:
    """Durable work items plus the policy checks that mint them.

    Every method is safe to call from several threads: the connection is opened with
    ``check_same_thread=False`` and every statement runs under one lock, the same shape
    ``thread_subs`` uses.
    """

    def __init__(self, path: Optional[Path] = None, *, now: Any = time.time) -> None:
        self._path = Path(path) if path is not None else default_db_path()
        self._now = now
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as exc:
                sys.stderr.write(
                    f"[work_items] WAL unavailable, using rollback: {exc}\n")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS work_items ("
                " id TEXT PRIMARY KEY, surface TEXT NOT NULL, thread_id TEXT NOT NULL,"
                " event_id TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,"
                " updated REAL NOT NULL, payload TEXT NOT NULL)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS work_items_event"
                " ON work_items(surface, thread_id, event_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS work_items_status"
                " ON work_items(status, created)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS observations ("
                " id TEXT PRIMARY KEY, surface TEXT NOT NULL, thread_id TEXT NOT NULL,"
                " event_id TEXT NOT NULL, reason TEXT NOT NULL, rules TEXT NOT NULL,"
                " at REAL NOT NULL)")

    @property
    def path(self) -> Path:
        return self._path

    # -- admission ------------------------------------------------------

    def check(
        self,
        policy: AdmissionPolicy,
        *,
        surface: str,
        thread_id: str,
        event_id: str,
        sender: str,
        sender_id: str,
        human: bool,
        addressed: bool,
        matched_rules: Sequence[str] = (),
        scope: str = "",
    ) -> Decision:
        """Gate and decide one candidate, recording an observation when it observes.

        Returns the :class:`Decision`. A ``would_accept`` decision is stored in
        ``observations`` and creates no work item: that is the whole point of
        ``observe`` mode. An accept creates (or returns) the item.
        """
        gate = policy.gate(
            surface=surface, sender_id=sender_id, human=human, addressed=addressed)
        decision = policy.decide(
            gate, sender=str(sender_id or ""), matched_rules=matched_rules,
            scope=scope)
        if decision.kind == "would_accept":
            self._record_observation(
                surface, thread_id, event_id, decision.reason, decision.rules)
            return decision
        if decision.kind == "accept" and decision.mandate is not None:
            mandate = replace(
                decision.mandate, surface=surface, sender=str(sender_id or ""))
            self.record(
                surface=surface, thread_id=thread_id, event_id=event_id,
                title=str(thread_id), sender=str(sender or sender_id or ""),
                mandate=mandate)
            return decision
        return decision

    def record(
        self,
        *,
        surface: str,
        thread_id: str,
        event_id: str,
        title: str,
        sender: str,
        mandate: Mandate,
        worker_id: str = "",
        now: Optional[float] = None,
    ) -> WorkItem:
        """Create the item for an accepted request, or return the existing one.

        Idempotent on ``(surface, thread_id, event_id)``, so a router that checks
        admission and later records the spawned worker cannot mint two items.
        """
        for value, label in (
            (surface, "surface"), (thread_id, "thread_id"), (event_id, "event_id"),
        ):
            if not str(value or "").strip():
                raise WorkItemError(f"{label} is required")
        at = float(self._now() if now is None else now)
        item_id = _item_id(surface, thread_id, event_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT payload FROM work_items WHERE id=?", (item_id,)).fetchone()
            if row is not None:
                item = _load(row["payload"])
                if worker_id and not item.worker_id:
                    item.worker_id = str(worker_id)
                    item.touch(at)
                    self._save(item)
                return item
            item = WorkItem(
                id=item_id,
                surface=str(surface),
                thread_id=str(thread_id),
                event_id=str(event_id),
                title=str(title or thread_id)[:200],
                sender=str(sender or ""),
                mandate=mandate,
                created_at=at,
                updated_at=at,
                worker_id=str(worker_id or ""),
            )
            self._save(item)
            return item

    def get(self, item_id: str) -> Optional[WorkItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM work_items WHERE id=?", (str(item_id),)).fetchone()
        return _load(row["payload"]) if row else None

    def by_event(self, surface: str, thread_id: str, event_id: str) -> Optional[WorkItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM work_items WHERE surface=? AND thread_id=?"
                " AND event_id=?", (surface, thread_id, event_id)).fetchone()
        return _load(row["payload"]) if row else None

    def open_items(self) -> List[WorkItem]:
        """Open items, oldest first: the order in which work was accepted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM work_items WHERE status='open'"
                " ORDER BY created").fetchall()
        return [_load(row["payload"]) for row in rows]

    def all_items(self) -> List[WorkItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM work_items ORDER BY created DESC").fetchall()
        return [_load(row["payload"]) for row in rows]

    def needs_person(self) -> List[WorkItem]:
        """Open items whose stage is ``person``, oldest first -- what a panel leads with."""
        return [item for item in self.open_items() if needs_person(item)]

    # -- lifecycle ------------------------------------------------------

    def start(self, item_id: str, worker_id: str) -> WorkItem:
        """Bind a worker and mark the assignment running."""
        item = self._require(item_id)
        item.worker_id = str(worker_id or "")
        item.assignment_state = "running"
        item.delivery_error = ""
        item.touch(self._now())
        self._update(item)
        return item

    def report(self, item_id: str, state: str, summary: str = "") -> WorkItem:
        """Record what the worker last said. A report is not a ticket state."""
        if state not in REPORT_STATES:
            raise WorkItemError(
                f"report state {state!r} is not one of {', '.join(REPORT_STATES)}")
        item = self._require(item_id)
        item.report_state = state
        item.report_summary = str(summary or "")[:2000]
        item.human_since_report = False
        if state == "waiting":
            item.assignment_state = "ended"
        item.touch(self._now())
        self._update(item)
        return item

    def ask(self, item_id: str, question: str) -> WorkItem:
        """One concrete question for a person; clears the blocked hold on answer."""
        question = str(question or "").strip()
        if not question:
            raise WorkItemError("a question is required")
        item = self._require(item_id)
        item.attention = question[:1000]
        item.touch(self._now())
        self._update(item)
        return item

    def answer(self, item_id: str) -> WorkItem:
        item = self._require(item_id)
        item.attention = ""
        item.touch(self._now())
        self._update(item)
        return item

    def note_human_message(self, item_id: str) -> WorkItem:
        """A person's message reached the item: it releases a blocked/failed hold."""
        item = self._require(item_id)
        item.human_since_report = True
        item.touch(self._now())
        self._update(item)
        return item

    def fail_delivery(self, item_id: str, error: str) -> WorkItem:
        item = self._require(item_id)
        item.delivery_error = str(error or "delivery failed")[:500]
        item.touch(self._now())
        self._update(item)
        return item

    def clear_delivery(self, item_id: str) -> WorkItem:
        item = self._require(item_id)
        item.delivery_error = ""
        item.touch(self._now())
        self._update(item)
        return item

    def end(self, item_id: str, *, by: str, outcome: str, now: Optional[float] = None) -> WorkItem:
        """End an open item. The ending fact, not the worker, chooses ``outcome``."""
        if by not in ("executor", "human"):
            raise WorkItemError(f"bad ender {by!r}")
        if outcome not in OUTCOMES:
            raise WorkItemError(
                f"outcome {outcome!r} is not one of {', '.join(OUTCOMES)}")
        item = self._require(item_id)
        if item.status == "ended":
            return item
        item.status = "ended"
        item.ended_by = by
        item.outcome = outcome
        item.attention = ""
        item.delivery_error = ""
        item.assignment_state = "ended"
        item.touch(self._now() if now is None else now)
        self._update(item)
        return item

    def stop(self, item_id: str, reason: str = "") -> WorkItem:
        """A person's stop: durable, and it withdraws the Mandate (a stop is final)."""
        item = self.end(item_id, by="human", outcome="stopped")
        if reason:
            item.report_summary = str(reason)[:2000]
            self._update(item)
        return item

    def resume(self, item_id: str, policy: AdmissionPolicy,
               now: Optional[float] = None) -> bool:
        """Reopen an ended item on a person's message, under its unchanged Mandate.

        False when a person stopped it, when the Mandate no longer revalidates, or when
        the item is already open. No new Eval and no new Decision: nothing new is
        authorized, so nothing new is minted.
        """
        item = self._require(item_id)
        if item.status != "ended" or item.ended_by == "human":
            return False
        if not policy.revalidate(item.mandate):
            return False
        item.status = "open"
        item.ended_by = ""
        item.outcome = ""
        item.assignment_state = "pending"
        item.report_state = ""
        item.human_since_report = True
        item.touch(self._now() if now is None else now)
        self._update(item)
        return True

    # -- observations ---------------------------------------------------

    def observations(self) -> List[Dict[str, Any]]:
        """``would_accept`` decisions recorded under ``observe`` mode, newest last."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observations ORDER BY at").fetchall()
        return [dict(row) for row in rows]

    def _record_observation(
        self, surface: str, thread_id: str, event_id: str,
        reason: str, rules: Sequence[str],
    ) -> None:
        at = float(self._now())
        obs_id = _item_id(surface, thread_id, event_id)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO observations"
                " (id, surface, thread_id, event_id, reason, rules, at)"
                " VALUES (?,?,?,?,?,?,?)",
                (obs_id, surface, thread_id, event_id, reason,
                 json.dumps(list(rules)), at))
            self._conn.execute(
                "DELETE FROM observations WHERE id IN ("
                " SELECT id FROM observations ORDER BY at DESC"
                " LIMIT -1 OFFSET ?)", (MAX_OBSERVATIONS,))

    # -- internals ------------------------------------------------------

    def _require(self, item_id: str) -> WorkItem:
        item = self.get(item_id)
        if item is None:
            raise WorkItemError(f"unknown work item {item_id!r}")
        return item

    def _save(self, item: WorkItem) -> None:
        # Called with the lock and connection held.
        self._conn.execute(
            "INSERT OR REPLACE INTO work_items"
            " (id, surface, thread_id, event_id, status, created, updated, payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (item.id, item.surface, item.thread_id, item.event_id, item.status,
             item.created_at, item.updated_at, _dump(item)))

    def _update(self, item: WorkItem) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE work_items SET status=?, updated=?, payload=? WHERE id=?",
                (item.status, item.updated_at, _dump(item), item.id))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _item_id(surface: str, thread_id: str, event_id: str) -> str:
    return f"{surface}:{thread_id}:{event_id}"


def _dump(item: WorkItem) -> str:
    data = asdict(item)
    data["mandate"]["rules"] = list(item.mandate.rules)
    return json.dumps(data, sort_keys=True)


def _load(payload: str) -> WorkItem:
    data = json.loads(payload)
    mandate = Mandate(
        strength=data["mandate"]["strength"],
        basis=data["mandate"]["basis"],
        sender=data["mandate"].get("sender", ""),
        policy_version=data["mandate"].get("policy_version", ""),
        scope=data["mandate"].get("scope", ""),
        rules=tuple(data["mandate"].get("rules") or ()),
        surface=data["mandate"].get("surface", data.get("surface", "")),
    )
    known = {
        "id", "surface", "thread_id", "event_id", "title", "sender", "created_at",
        "updated_at", "worker_id", "status", "ended_by", "outcome", "report_state",
        "report_summary", "assignment_state", "attention", "delivery_error",
        "human_since_report",
    }
    fields = {key: data[key] for key in known if key in data}
    fields.setdefault("sender", "")
    return WorkItem(mandate=mandate, **fields)


_DEFAULT: Optional[WorkItems] = None
_DEFAULT_LOCK = threading.Lock()


def default_work_items() -> WorkItems:
    """The process-wide store at :func:`default_db_path` (one per daemon process)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = WorkItems()
        return _DEFAULT


def policy_from_env(environ: Optional[Mapping[str, str]] = None) -> AdmissionPolicy:
    """Build a policy from operator environment variables, failing loudly on garbage.

    ``AITHER_WORK_ITEMS_ALLOWED`` / ``AITHER_WORK_ITEMS_BOTS`` are comma-separated
    ``surface:sender`` pairs. ``AITHER_WORK_ITEMS_ACCEPT_MODE`` is ``off``, ``observe``
    (default) or ``auto``; an invalid mode raises rather than silently widening
    authority. ``AITHER_WORK_ITEMS_EXPLICIT_FOR_UNLISTED=1`` opens the admission cell
    that lets an unlisted sender who mentions the agent make an explicit request.
    """
    env = os.environ if environ is None else environ

    def pairs(name: str) -> Dict[str, FrozenSet[str]]:
        out: Dict[str, FrozenSet[str]] = {}
        raw = str(env.get(name) or "").strip()
        for part in (item.strip() for item in raw.split(",")):
            if not part:
                continue
            surface, _, sender = part.partition(":")
            if not surface or not sender:
                raise WorkItemError(
                    f"{name} entry {part!r} is not surface:sender")
            out[surface] = out.get(surface, frozenset()) | {sender}
        return out

    mode = str(env.get("AITHER_WORK_ITEMS_ACCEPT_MODE") or "observe").strip()
    if mode not in MODES:
        raise WorkItemError(
            f"AITHER_WORK_ITEMS_ACCEPT_MODE={mode!r} is not one of {', '.join(MODES)}")
    rules: List[Rule] = []
    raw_rules = str(env.get("AITHER_WORK_ITEMS_RULES") or "").strip()
    for part in (item.strip() for item in raw_rules.split(",")):
        if part:
            rules.append(Rule(id=part, name=part))
    return AdmissionPolicy(
        allowed=pairs("AITHER_WORK_ITEMS_ALLOWED"),
        bots=pairs("AITHER_WORK_ITEMS_BOTS"),
        accept_mode=mode,
        rules=tuple(rules),
        explicit_for_unlisted=(
            str(env.get("AITHER_WORK_ITEMS_EXPLICIT_FOR_UNLISTED") or "")
            .strip().lower() in ("1", "true", "yes", "on")),
        version=str(env.get("AITHER_WORK_ITEMS_POLICY_VERSION") or "env"),
    )
