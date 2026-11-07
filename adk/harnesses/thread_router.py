"""thread_router — the SLOW path: who handles the FIRST event on an unowned thread.

WHY THIS EXISTS
---------------
``steer_dispatch._pass_through`` hands every follow-up on a SUBSCRIBED thread to its
subscribers' mailboxes and, for a thread nobody owns yet, deliberately does nothing --
its comment says "the router's slow path decides". Until this file there was no slow
path, so the first event on a new GitHub thread, Discord thread or mail chain was
dropped and nobody was ever asked to work it.

This module is that decision, kept DETERMINISTIC (no LLM). An event on an unowned thread
is a work request when ``payload.work_request`` is truthy or when its text addresses the
router (``@aither`` by default). The predicate is INJECTABLE
(``work_request_predicate``) so an LLM judge can replace it later without touching the
safety rules that sit around it.

WHAT IT DOES, AND ONLY THAT
---------------------------
On a work request it spawns ONE worker through an injected ``spawn`` callable, subscribes
that worker to the thread in :class:`adk.harnesses.thread_subs.ThreadSubscriptions`,
delivers the triggering text to the worker's steering mailbox, and publishes a
``thread.routed`` receipt into the room. The worker is an ordinary managed session: the
daemon's spawn callable uses the same ``SessionConfig`` -> ``SessionManager.create`` core
``POST /sessions`` takes, so it shows up in ``/sessions`` and can be attached to or
stopped like any other. The daemon helper also applies the entitlement and reasoning
doctrine pre-flight; it requests no connector, names no agent and takes no skill from the
event, so those route checks have nothing to decide here.

REGISTRATION ORDER IS LOAD-BEARING
----------------------------------
This router must be registered AFTER the ``SteerDispatcher``. Both listen to the same
room, and the router subscribes the worker it spawned, so if it ran first the
dispatcher's ``_pass_through`` would see that brand-new subscriber on the SAME event and
deliver the triggering text to the worker a second time. :func:`register` refuses to
register ahead of a supplied dispatcher (``must_follow``) -- the way ``daemon.py`` wires
it -- so a reordering cannot silently double every first work request.

GUARDS (each is a way this could spend compute on someone else's behalf)
------------------------------------------------------------------------
* A thread that already has a subscriber is the FAST path -- this router declines.
* One spawn per event id (an LRU, like ``SteerDispatcher``): a retrying producer cannot
  start a second worker for the same request.
* A live-worker cap (default 4): a burst cannot become an unbounded fleet.
* An event authored by a worker THIS router spawned is never routed again (loop guard),
  and the id stays remembered after that worker ends.
* An event must carry the daemon's OWNER-PLAN auth stamp (``auth.plan == "owner"``) --
  the stamp ``Room._normalise`` resolves from the presented bearer, never a payload field
  -- unless the caller opted in with ``allow_unstamped``. A stranger posting a room event
  must not be able to make this box spawn compute.
* A spawn failure, an unwired spawn callable or a raising subscription store degrade to
  a logged error plus a receipt. Nothing raises into ``Room.publish``.
* A spawn whose SUBSCRIPTION write failed keeps a claim on the thread: a follow-up
  retries the subscription and, if it still cannot be written, is refused naming the
  existing worker -- never a second worker for the same thread.

SESSION END
-----------
The manager exposes no "this session ended" callback, so :meth:`ThreadRouter.reap` drops
the subscriptions of spawned workers the injected session lister no longer reports, and
:meth:`handle` calls it on every event. A grace period stops a worker spawned seconds ago
(and not yet listed) from being reaped before it is ever seen. :meth:`session_ended` is
the same drop for one id, for a future hook.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from adk.decisions.store import write_steer
from adk.harnesses.thread_subs import thread_key_from_event

#: Default words that address the router when ``payload.work_request`` is absent.
DEFAULT_MENTION_TOKENS: Tuple[str, ...] = ("@aither",)

#: Concurrent workers this router may have running at once.
DEFAULT_MAX_WORKERS = 4

#: A worker younger than this is never reaped: ``mgr.list_sessions()`` can lag a spawn.
DEFAULT_REAP_GRACE_S = 60.0

#: How many processed event ids are remembered (idempotency), and how many spawned
#: worker ids are remembered for the loop guard. Bounded: the daemon is long-lived.
DEFAULT_LRU_SIZE = 512
SPAWNED_HISTORY_SIZE = 512

#: The plan the daemon must have stamped on the event for this router to act. Matches
#: ``daemon.OWNER_PRINCIPAL.plan`` and ``steer_dispatch.TIER1_REQUIRED_PLAN``.
ROUTER_REQUIRED_PLAN = "owner"

#: The receipt type. Deliberately outside the desk's chat vocabulary and NOT carrying
#: ``payload.thread``, so neither the pass-through nor this router can act on it.
ROUTED_EVENT_TYPE = "thread.routed"

#: The router's own actor id in the room, and the fallback ``kind`` for the mailbox file.
ROUTER_ACTOR_ID = "thread-router"

#: Session states that mean "this worker is gone"; anything else counts as live.
ENDED_SESSION_STATES = frozenset({"exited", "failed"})

#: Event types that are control traffic, never work: the (un)subscribe pair and our own
#: receipt. Handling one of these would be either a no-op or a loop.
_CONTROL_TYPES = frozenset({"thread.subscribe", "thread.unsubscribe", ROUTED_EVENT_TYPE})

#: How many ``recent`` entries :meth:`ThreadRouter.status` keeps.
STATUS_RECENT_CAP = 20

_TRUTHY_FLAGS = frozenset({"1", "true", "yes", "on"})


def status_path() -> Path:
    """Where the router's own trace lives (``AITHER_THREAD_ROUTER_STATUS`` overrides)."""
    env = os.environ.get("AITHER_THREAD_ROUTER_STATUS", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "thread-router.json"


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _auth_plan(event: Dict[str, Any]) -> str:
    """The plan the daemon stamped on this event, or ``""`` (never a payload field)."""
    auth = event.get("auth")
    if not isinstance(auth, dict):
        return ""
    return str(auth.get("plan") or "")


def _extract_text(event: Dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return f"({event.get('type') or 'thread event'}, no payload text)"


def _mentions(text: str, tokens: Sequence[str]) -> bool:
    """True when ``text`` addresses one of ``tokens`` as a word, case-insensitively.

    Not a substring test: ``someone@aither.io`` is an address, not an instruction, so a
    token preceded by a letter/digit/``._-`` does not count. A token inside prose
    ("@aither, please") and one at either end of the text do.
    """
    lowered = text.lower()
    for token in tokens:
        needle = str(token or "").strip().lower()
        if not needle:
            continue
        start = lowered.find(needle)
        while start != -1:
            before = lowered[start - 1] if start > 0 else ""
            after_at = start + len(needle)
            after = lowered[after_at] if after_at < len(lowered) else ""
            # 🚩 ``"" in "._-"`` is TRUE in Python, so the empty ''before'' case (a
            # mention at the very start of the text) must be short-circuited, not
            # tested as a substring.
            if not (before.isalnum() or (before and before in "._-")) and not (
                after.isalnum() or after == "_"
            ):
                return True
            start = lowered.find(needle, start + 1)
    return False


def _work_request_flag(value: Any) -> bool:
    """Is ``payload.work_request`` set? Booleans, and the usual string spellings."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_FLAGS
    return False


def is_work_request_event(
    event: Dict[str, Any], *, mention_tokens: Sequence[str] = DEFAULT_MENTION_TOKENS
) -> bool:
    """The default deterministic predicate: does this event ask for work?

    ``payload.work_request`` is an explicit statement of intent; addressing the router in
    the text is the fallback. Anything else is conversation, and conversation on an
    unowned thread is left for a human or a later event.
    """
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if _work_request_flag(payload.get("work_request")):
        return True
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    return _mentions(text, mention_tokens)


@dataclass(frozen=True)
class SpawnRequest:
    """Everything the spawn callable needs to start one worker for one thread.

    Deliberately a value object, not the raw event: the router hands the daemon the
    already-parsed ``(surface, thread_id)`` key and the text, so a spawn implementation
    never has to re-validate an event it did not receive.
    """

    surface: str
    thread_id: str
    title: str
    text: str
    sender: str
    sender_id: str
    event_id: str
    event_type: str
    #: Whether a person wrote the event (the room's ``actor.kind == "human"``). The
    #: admission gate needs it: a bot can make an inferred candidate, never an explicit
    #: request. Defaults True for callers that construct a request directly.
    human: bool = True
    #: Whether the event addressed the router by name. A producer's ``work_request``
    #: flag is intent, not a mention, so it is not "addressed"; the admission gate
    #: treats the two differently.
    addressed: bool = True


class ThreadRouter:
    """The slow path: one work request in, at most one worker spawned out.

    Every dependency is injected so this is testable with no daemon, no HTTP and no real
    harness process: ``spawn`` stands in for the daemon's ``mgr.create(config)`` path,
    ``list_sessions`` for ``mgr.list_sessions()``, ``write_steer_fn`` for
    :func:`adk.decisions.store.write_steer`. ``daemon.py`` constructs one with the real
    callables and registers :meth:`handle` on the room registry, behind
    ``AITHER_THREAD_ROUTER=1`` (default off, because this is the one listener that
    spawns compute).
    """

    def __init__(
        self,
        *,
        thread_subscriptions: Any,
        spawn: Optional[Callable[[SpawnRequest], Optional[str]]] = None,
        list_sessions: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        write_steer_fn: Callable[..., Optional[Path]] = write_steer,
        work_request_predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
        mention_tokens: Sequence[str] = DEFAULT_MENTION_TOKENS,
        max_workers: int = DEFAULT_MAX_WORKERS,
        allow_unstamped: bool = False,
        admission: Optional[Callable[[SpawnRequest], Any]] = None,
        on_routed: Optional[Callable[[SpawnRequest, str], None]] = None,
        reap_grace_s: float = DEFAULT_REAP_GRACE_S,
        lru_size: int = DEFAULT_LRU_SIZE,
        now: Callable[[], float] = time.time,
        status_file: Optional[Path] = None,
    ) -> None:
        if thread_subscriptions is None:
            raise ValueError("ThreadRouter needs a ThreadSubscriptions table")
        self._thread_subs = thread_subscriptions
        self._spawn = spawn
        self._list_sessions = list_sessions
        self._write_steer = write_steer_fn
        #: Optional second gate: a callable taking the built SpawnRequest and returning
        #: an object (or dict) with ``accepted``, ``observed`` and ``reason``. ``None``
        #: keeps today's behaviour (the owner-plan stamp alone). Consulted BEFORE the
        #: fleet cap, so an observed or refused candidate never consumes a slot.
        self._admission = admission
        #: Optional recorder called after a worker is spawned, subscribed and delivered
        #: to. The daemon uses it to mint the durable work item; observations never
        #: reach it, because they started nothing.
        self._on_routed = on_routed
        if work_request_predicate is not None:
            self._is_work_request = work_request_predicate
            self._mention_tokens = tuple(
                t for t in mention_tokens if str(t or "").strip())
        else:
            tokens = tuple(t for t in mention_tokens if str(t or "").strip())
            tokens = tokens or DEFAULT_MENTION_TOKENS
            self._mention_tokens = tokens

            def _predicate(event: Dict[str, Any]) -> bool:
                return is_work_request_event(event, mention_tokens=tokens)

            self._is_work_request = _predicate
        self._max_workers = max(1, int(max_workers))
        self._allow_unstamped = bool(allow_unstamped)
        self._reap_grace_s = max(0.0, float(reap_grace_s))
        self._lru_size = max(1, int(lru_size))
        self._now = now
        self._status_file = status_file or status_path()

        self._lock = threading.Lock()
        #: Serializes the check-spawn-subscribe sequence so two events racing on one
        #: empty thread cannot start two workers (the SQLite table has no such gate).
        self._decision_lock = threading.Lock()
        self._processed_ids: "OrderedDict[str, float]" = OrderedDict()
        #: Workers this router spawned and believes may still be running.
        self._spawned: "OrderedDict[str, float]" = OrderedDict()
        #: Every worker id this router ever spawned -- the loop guard outlives the
        #: worker, because a delayed event can still arrive from its thread.
        self._spawned_ever: "OrderedDict[str, float]" = OrderedDict()
        #: ``(surface, thread_id) -> worker_id`` for threads a spawn took but the
        #: subscription write for did NOT land. Without this the thread looks unowned
        #: on the next event and a burst would spawn a worker per event up to the cap.
        self._claimed_threads: Dict[Tuple[str, str], str] = {}

        self._routed_count = 0
        self._refused_count = 0
        self._spawn_failed_count = 0
        self._last_error = ""
        self._recent: List[Dict[str, Any]] = []

    # ── the listener entry point ───────────────────────────────────────────

    def handle(self, room: Any, event: Dict[str, Any]) -> None:
        """Registered on the room registry as ``fn(room, event)``.

        Wraps everything in try/except: a bug in here costs a stderr line and a
        ``last_error``, never the publish that is already durable when a listener runs.
        """
        try:
            self._dispatch(room, event)
        except Exception as exc:  # never raise into publish
            sys.stderr.write(f"[thread-router] handle failed: {type(exc).__name__}: {exc}\n")
            self._note_error(f"{type(exc).__name__}: {exc}")

    # ── internals ──────────────────────────────────────────────────────────

    def _dispatch(self, room: Any, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        key = thread_key_from_event(event)
        if key is None:
            return  # no thread on this event: the addressed path owns it
        # Every THREAD-carrying event reaps, not only work requests: a worker whose
        # session ended must stop collecting the thread's mail even during a quiet
        # stretch of conversation. With no spawned workers this is a dict check and
        # returns before the session listing is ever touched.
        self.reap()
        if str(event.get("type") or "") in _CONTROL_TYPES:
            return
        if event.get("to"):
            return  # addressed: the fast path's business, never the slow path's
        if not self._is_work_request(event):
            return
        with self._decision_lock:
            outcome = self._decide(event, key)
            if outcome is not None:
                event_id = str(event.get("id") or "")
                if event_id:
                    self._mark_processed(event_id)
        if outcome is not None:
            self._publish_receipt(room, event, outcome)

    def _decide(
        self, event: Dict[str, Any], key: Tuple[str, str]
    ) -> Optional[Dict[str, Any]]:
        """Route one work request. Returns a receipt outcome, or None for "not mine".

        Called with ``self._decision_lock`` held.
        """
        surface, thread_id = key
        title = f"thread {surface}:{thread_id}"[:120]
        event_id = str(event.get("id") or "")
        if event_id and self._already_processed(event_id):
            return None
        sender_id = str((event.get("actor") or {}).get("id") or "")

        # 1. Someone already owns this thread: that is exactly the fast path.
        try:
            subscribers = self._thread_subs.subscribers(surface, thread_id)
        except Exception as exc:  # a broken store must not crash the listener
            self._note_error(f"thread lookup failed: {type(exc).__name__}: {exc}")
            return None
        if subscribers:
            self._record_skip(event, surface, thread_id)
            return None

        # 1b. A previous event spawned a worker for this thread but the subscription
        # write failed, so the table still says "unowned". Without this claim every
        # follow-up would spawn another worker until the cap was reached. Retry the
        # subscription (self-healing); if it still will not land, refuse and keep the
        # existing worker -- never start a second one for the same thread.
        claimed = self._claimed_worker(key)
        if claimed:
            if self._is_live_worker(claimed):
                if self._try_subscribe(claimed, surface, thread_id, title):
                    self._release_claim(key)
                    self._record_skip(event, surface, thread_id)
                    return None
                return self._refuse(
                    event, surface, thread_id,
                    f"thread is claimed by worker {claimed}, whose subscription could "
                    "not be written; not spawning a second worker for it",
                )
            self._release_claim(key)  # the claimant ended: the thread is free again

        # 2. Loop guard: a worker this router started does not get to start more.
        if sender_id and self._was_spawned(sender_id):
            return self._refuse(
                event, surface, thread_id,
                "authored by a worker this router spawned; not routed again",
            )

        # 3. Authority: only the daemon's owner-plan stamp may spend compute.
        if not self._allow_unstamped and _auth_plan(event) != ROUTER_REQUIRED_PLAN:
            return self._refuse(
                event, surface, thread_id,
                "no owner-plan auth stamp on this event; a router will not spawn "
                "compute for an unauthenticated sender (owner ruling)",
            )

        # The value object is built here because the optional admission gate consumes
        # it, and the cap below must not be charged for a candidate that never spawns.
        request = SpawnRequest(
            surface=surface,
            thread_id=thread_id,
            title=title,
            text=_extract_text(event),
            sender=str((event.get("actor") or {}).get("name") or sender_id or "a peer"),
            sender_id=sender_id,
            event_id=event_id,
            event_type=str(event.get("type") or ""),
            human=str((event.get("actor") or {}).get("kind") or "") == "human",
            addressed=_mentions(
                _extract_text(event), self._mention_tokens),
        )

        # 3b. Admission: who may originate work on this surface, and whether inferred
        # work is accepted at all. None keeps the owner-plan stamp as the only gate.
        refusal = self._admission_refusal(request)
        if refusal is not None:
            return self._refuse(event, surface, thread_id, refusal)

        # 4. Fleet cap: a burst of work requests must not become a burst of workers.
        if self._live_worker_count() >= self._max_workers:
            return self._refuse(
                event, surface, thread_id,
                f"worker cap reached ({self._max_workers} live); not spawning",
            )

        # 5. Spawn.
        if self._spawn is None:
            return self._spawn_failed(
                event, surface, thread_id, "no spawn callable is wired on this router",
            )
        try:
            worker_id = self._spawn(request)
        except Exception as exc:  # noqa: BLE001 - a failed spawn is logged, never raised
            self._note_error(
                f"spawn failed for {surface}:{thread_id}: {type(exc).__name__}: {exc}"
            )
            return self._spawn_failed(
                event, surface, thread_id, f"spawn raised {type(exc).__name__}: {exc}",
            )
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return self._spawn_failed(
                event, surface, thread_id, "the spawn callable returned no session id",
            )
        # Track BEFORE subscribing: if the subscribe below fails, the worker still ran
        # and the loop guard must still recognise it.
        self._track_worker(worker_id)

        # 6. Subscribe the worker: every later event in the thread takes the fast path.
        # A failed write does NOT strand the thread: the claim above keeps this worker
        # as its owner so a follow-up cannot spawn a second one.
        subscribed = self._try_subscribe(worker_id, surface, thread_id, title)
        if not subscribed:
            self._claim_thread(key, worker_id)

        # 7. Deliver the triggering text to the worker's mailbox.
        delivered = self._deliver(event, worker_id, sender_id)
        detail = (
            f"spawned {worker_id} for {surface}:{thread_id}; "
            f"subscribed={subscribed} delivered={delivered}"
        )
        if not subscribed:
            detail += (
                f"; subscription could not be written, so {worker_id} holds this thread "
                "and no further worker will be spawned for it"
            )
        self._record_route(event, surface, thread_id, worker_id, detail)
        self._record_routed(request, worker_id)
        return {
            "status": "routed",
            "surface": surface,
            "thread_id": thread_id,
            "worker": worker_id,
            "event": event_id,
            "detail": detail,
        }

    # ── the optional admission gate ────────────────────────────────────────

    def _admission_refusal(self, request: SpawnRequest) -> Optional[str]:
        """Ask the injected admission callable; a string means refuse.

        The callable returns an object or dict with ``accepted`` (bool), ``observed``
        (bool, for an inferred candidate recorded under ``observe`` mode) and ``reason``
        (str). It must not spawn and must not raise; a raising callable is a refused
        request, because a broken policy is not permission.
        """
        if self._admission is None:
            return None
        try:
            decision = self._admission(request)
        except Exception as exc:  # noqa: BLE001 - fail closed, never into publish
            self._note_error(
                f"admission gate failed for {request.surface}:{request.thread_id}: "
                f"{type(exc).__name__}: {exc}")
            return "admission gate failed; refusing rather than assuming permission"
        if decision is None:
            return None
        if isinstance(decision, dict):
            accepted = decision.get("accepted")
            observed = bool(decision.get("observed"))
            reason = str(decision.get("reason") or "")
        else:
            accepted = getattr(decision, "accepted", None)
            observed = bool(getattr(decision, "observed", False))
            reason = str(getattr(decision, "reason", "") or "")
        if accepted is True:
            return None
        suffix = " (observed only; nothing accepted)" if observed else ""
        return f"refused by the admission policy: {reason or 'not admitted'}{suffix}"

    def _record_routed(self, request: SpawnRequest, worker_id: str) -> None:
        """Hand the accepted request to the recorder; a broken recorder never raises."""
        if self._on_routed is None:
            return
        try:
            self._on_routed(request, worker_id)
        except Exception as exc:  # noqa: BLE001 - recording never undoes a route
            self._note_error(
                f"on_routed failed for {request.surface}:{request.thread_id}: "
                f"{type(exc).__name__}: {exc}")

    # ── the mailbox ────────────────────────────────────────────────────────

    def _deliver(
        self, event: Dict[str, Any], worker_id: str, sender_id: str
    ) -> bool:
        """Write the triggering text to the worker's steering mailbox. Never raises."""
        try:
            path = self._write_steer(
                worker_id,
                [_extract_text(event)],
                suffix="steer",
                sender=str((event.get("actor") or {}).get("name") or sender_id or "a peer"),
                authority="peer",
                origin_id=str(event.get("id") or ""),
                kind="thread_worker",
            )
        except Exception as exc:  # noqa: BLE001 - the mailbox writer is fail-soft
            self._note_error(
                f"mailbox write failed for {worker_id}: {type(exc).__name__}: {exc}"
            )
            return False
        if path is None:
            self._note_error(
                f"mailbox write for {worker_id} refused (invalid session id)"
            )
            return False
        return True

    def _try_subscribe(
        self, worker_id: str, surface: str, thread_id: str, title: str
    ) -> bool:
        """Write one subscription; True on success, logging (never raising) on failure."""
        try:
            self._thread_subs.subscribe(surface, thread_id, worker_id, title=title)
        except Exception as exc:  # noqa: BLE001 - a broken store is logged, not raised
            self._note_error(
                f"subscribe failed for worker {worker_id}: {type(exc).__name__}: {exc}"
            )
            return False
        return True

    # ── orphan claims: a spawn whose subscription write failed ──────────────

    def _claim_thread(self, key: Tuple[str, str], worker_id: str) -> None:
        with self._lock:
            self._claimed_threads[key] = worker_id

    def _claimed_worker(self, key: Tuple[str, str]) -> str:
        with self._lock:
            return self._claimed_threads.get(key, "")

    def _release_claim(self, key: Tuple[str, str]) -> None:
        with self._lock:
            self._claimed_threads.pop(key, None)

    def _is_live_worker(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self._spawned

    # ── session end: reap / session_ended ──────────────────────────────────

    def reap(self) -> int:
        """Drop subscriptions of spawned workers the lister no longer reports.

        Returns how many workers were reaped. A lister that RAISES is "cannot judge",
        not "all are dead", so nothing is reaped and the error is recorded. Workers
        younger than ``reap_grace_s`` are skipped: a just-spawned session may not be in
        the listing yet, and unsubscribing it would silently cut off its thread.
        """
        if self._list_sessions is None:
            return 0
        now = float(self._now())
        with self._lock:
            candidates = [
                worker_id
                for worker_id, spawned_at in self._spawned.items()
                if now - spawned_at >= self._reap_grace_s
            ]
        if not candidates:
            return 0
        try:
            rows = list(self._list_sessions() or [])
        except Exception as exc:  # noqa: BLE001 - cannot judge is not "all are dead"
            self._note_error(f"session listing failed during reap: {type(exc).__name__}: {exc}")
            return 0
        live = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            if str(row.get("state") or "") in ENDED_SESSION_STATES:
                continue
            live.add(session_id)
        dropped = 0
        for worker_id in candidates:
            if worker_id in live:
                continue
            self._drop_worker(worker_id)
            dropped += 1
        return dropped

    def session_ended(self, session_id: str) -> int:
        """Unsubscribe one spawned worker (idempotent; 0 for an id this router never made).

        The manager has no session-ended callback yet; this is the method a future hook
        would call. :meth:`reap` is what covers today's daemon, on every event.
        """
        worker_id = str(session_id or "").strip()
        if not worker_id or worker_id not in self._spawned_ever:
            return 0
        return self._drop_worker(worker_id)

    def _drop_worker(self, worker_id: str) -> int:
        with self._lock:
            self._spawned.pop(worker_id, None)
            # A worker that is gone no longer owns any thread it claimed, so the
            # thread becomes spawnable again instead of pointing at a dead id.
            for key, claimed in list(self._claimed_threads.items()):
                if claimed == worker_id:
                    self._claimed_threads.pop(key, None)
        try:
            dropped = int(self._thread_subs.unsubscribe_all(worker_id))
        except Exception as exc:  # noqa: BLE001
            self._note_error(
                f"unsubscribe_all failed for {worker_id}: {type(exc).__name__}: {exc}"
            )
            return 0
        with self._lock:
            self._append_recent(
                {}, target=worker_id, channel="reap",
                detail=f"worker ended; dropped {dropped} subscription(s)",
            )
        self._write_status()
        return dropped

    # ── worker bookkeeping ────────────────────────────────────────────────

    def _track_worker(self, worker_id: str) -> None:
        now = float(self._now())
        with self._lock:
            self._spawned[worker_id] = now
            self._spawned_ever[worker_id] = now
            self._spawned_ever.move_to_end(worker_id)
            while len(self._spawned_ever) > SPAWNED_HISTORY_SIZE:
                self._spawned_ever.popitem(last=False)

    def _was_spawned(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._spawned_ever

    def _live_worker_count(self) -> int:
        with self._lock:
            return len(self._spawned)

    # ── receipts ───────────────────────────────────────────────────────────

    def _refuse(
        self, event: Dict[str, Any], surface: str, thread_id: str, reason: str
    ) -> Dict[str, Any]:
        with self._lock:
            self._refused_count += 1
            self._append_recent(
                event, target=f"{surface}:{thread_id}", channel="none", detail=reason,
            )
        self._write_status()
        return {
            "status": "refused",
            "surface": surface,
            "thread_id": thread_id,
            "worker": None,
            "event": str(event.get("id") or ""),
            "detail": reason,
        }

    def _spawn_failed(
        self, event: Dict[str, Any], surface: str, thread_id: str, reason: str
    ) -> Dict[str, Any]:
        with self._lock:
            self._spawn_failed_count += 1
            self._append_recent(
                event, target=f"{surface}:{thread_id}", channel="none", detail=reason,
            )
        self._write_status()
        return {
            "status": "spawn_failed",
            "surface": surface,
            "thread_id": thread_id,
            "worker": None,
            "event": str(event.get("id") or ""),
            "detail": reason,
        }

    def _record_route(
        self,
        event: Dict[str, Any],
        surface: str,
        thread_id: str,
        worker_id: str,
        detail: str,
    ) -> None:
        with self._lock:
            self._routed_count += 1
            self._append_recent(event, target=worker_id, channel="spawn", detail=detail)
        self._write_status()

    def _record_skip(
        self, event: Dict[str, Any], surface: str, thread_id: str
    ) -> None:
        """Fast-path skip: not a refusal, just not this router's business."""
        with self._lock:
            self._append_recent(
                event, target=f"{surface}:{thread_id}", channel="fast-path",
                detail="the thread already has a subscriber; pass-through owns it",
            )
        self._write_status()

    def _publish_receipt(
        self, room: Any, event: Dict[str, Any], outcome: Dict[str, Any]
    ) -> None:
        """Publish ``thread.routed`` -- correlated AND causated to the original event.

        No ``to`` and no ``payload.thread`` on purpose: a receipt is not addressable, and
        both listeners key on those fields, so the receipt can never be re-dispatched or
        re-routed.
        """
        if room is None:
            return
        receipt = {
            "type": ROUTED_EVENT_TYPE,
            "pillar": "orchestration",  # thread.routed has no pillar_for() mapping
            "actor": {
                "kind": "service", "id": ROUTER_ACTOR_ID, "name": ROUTER_ACTOR_ID,
            },
            "correlation_id": str(event.get("correlation_id") or event.get("id") or ""),
            "causation_id": str(event.get("id") or ""),
            "payload": {
                "status": outcome.get("status"),
                "surface": outcome.get("surface"),
                "thread_id": outcome.get("thread_id"),
                "worker": outcome.get("worker"),
                "event": outcome.get("event"),
                "detail": outcome.get("detail"),
            },
        }
        try:
            room.publish(receipt)
        except Exception as exc:  # a receipt that cannot publish must not undo the route
            self._note_error(f"receipt publish failed: {type(exc).__name__}: {exc}")

    # ── the processed-event LRU (retry guard) ─────────────────────────────

    def _already_processed(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._processed_ids:
                self._processed_ids.move_to_end(event_id)
                return True
            return False

    def _mark_processed(self, event_id: str) -> None:
        with self._lock:
            self._processed_ids[event_id] = float(self._now())
            self._processed_ids.move_to_end(event_id)
            while len(self._processed_ids) > self._lru_size:
                self._processed_ids.popitem(last=False)

    # ── status / observability ─────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "at": _iso(self._now()),
                "routed": self._routed_count,
                "refused": self._refused_count,
                "spawn_failed": self._spawn_failed_count,
                "workers": len(self._spawned),
                "max_workers": self._max_workers,
                "last_error": self._last_error,
                "recent": list(self._recent),
            }

    def _note_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self._write_status()

    def _append_recent(
        self, event: Dict[str, Any], *, target: Any, channel: str, detail: str
    ) -> None:
        # Called with ``self._lock`` already held.
        self._recent.append(
            {
                "at": _iso(self._now()),
                "event": str(event.get("id") or "") if isinstance(event, dict) else "",
                "target": target,
                "channel": channel,
                "detail": detail,
            }
        )
        if len(self._recent) > STATUS_RECENT_CAP:
            del self._recent[: len(self._recent) - STATUS_RECENT_CAP]

    def _write_status(self) -> None:
        """Best-effort, never throws: a silent router must not look like a quiet room."""
        try:
            data = self.status()
            target = self._status_file
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            sys.stderr.write(f"[thread-router] status write failed: {exc}\n")


def _registered_callables(registry: Any) -> Optional[List[Any]]:
    """The listener callables a registry currently holds, or None if it does not say.

    ``RoomRegistry`` keeps plain callables in ``_listeners``; ``Room`` keeps
    ``(listener, wants_room)`` tuples. Anything else (a test double that only offers
    ``add_listener``) returns None, which means "cannot judge" and never a false refusal.
    """
    raw = getattr(registry, "_listeners", None)
    if raw is None:
        raw = getattr(registry, "listeners", None)
    if not isinstance(raw, (list, tuple)):
        return None
    out: List[Any] = []
    for item in raw:
        if isinstance(item, tuple) and item:
            item = item[0]
        if callable(item):
            out.append(item)
    return out


def register(
    registry: Any, *, must_follow: Any = None, **deps: Any
) -> ThreadRouter:
    """Build a :class:`ThreadRouter` and register it on a room registry.

    Call this ONCE per process (``daemon.py`` does, behind ``AITHER_THREAD_ROUTER=1``).
    Two instances do not share the processed-event LRU, and the one-spawn-per-event
    guarantee would then be a lock each, not a lock between them.

    ``must_follow`` is the fast-path listener that must ALREADY be on ``registry`` --
    ``daemon.py`` passes the ``SteerDispatcher``. Ordering is load-bearing here: this
    router subscribes the worker it spawns, so if it ran first the dispatcher's
    ``_pass_through`` would see that new subscriber on the SAME event and deliver the
    triggering text to the worker twice. When the registry exposes its listener list, a
    wrong order raises ``ValueError`` at startup instead of double-delivering at run
    time; a registry that does not expose one is left to the caller's discipline.
    """
    if must_follow is not None:
        handle = getattr(must_follow, "handle", must_follow)
        present = _registered_callables(registry)
        if present is not None and not any(fn is handle or fn == handle for fn in present):
            raise ValueError(
                "ThreadRouter must be registered AFTER the SteerDispatcher: the router "
                "subscribes the worker it spawns, so if it ran first the dispatcher's "
                "pass-through would deliver the same triggering event to that worker a "
                "second time"
            )
    router = ThreadRouter(**deps)
    registry.add_listener(router.handle)
    return router
