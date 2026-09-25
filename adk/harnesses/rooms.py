"""Rooms — the AitherAeon spine: many producers, one ordered, replayable stream.

A ``Session`` is one agent doing one thing. A ``Room`` is the place several of them --
Claude Code tabs, adk agent loops, sovereign Aither agents, ACP clients, remote A2A
peers and humans -- appear together, with every event attributed to one of the six
pillars so the cognition is watchable rather than merely happening.

WHY THIS IS NOT A SECOND EVENT PATH
-----------------------------------
It deliberately copies ``session.py``'s ``_emit``: stamp under the lock, append to the
JSONL transcript OUTSIDE the lock, keep a bounded in-memory buffer for reconnects, and
serve ``events_since(seq)``. Inventing a second event mechanism here would mean two
things to reason about when a stream goes quiet, and the daemon already has the one
that works.

REPLAY SURVIVES A RESTART
-------------------------
A room HYDRATES from its transcript on construction: the tail is read back into the
buffer and ``_seq`` resumes from the highest seq on disk. Without this, "durable" and
"replayable" were different claims and only the first was true — a client reconnecting
with ``?since=5`` after a daemon restart would silently receive only what happened
after the restart, which looks identical to "nothing happened" and is exactly the class
of silence this spine exists to end. Hydration is bounded (:data:`HYDRATE_TAIL_BYTES`)
so a long-lived room does not re-read a huge file at startup.

WHY ORDERING IS THE ROOM'S JOB
------------------------------
Producers are concurrent and independent -- a hook in a Claude Code tab, a kernel tick
in a container, a neuron in the worker. They cannot agree on an order, so they do not
try: ``seq`` is 0 on the wire and the room stamps it on arrival. A client that has seen
seq N asks for ``?since=N`` and gets exactly what it missed, whatever produced it.

FAIL-SOFT, NEVER FAIL-SILENT
----------------------------
An unknown pillar is REJECTED rather than coerced to a default lane. Silently filing a
mystery event under ``orchestration`` is how a lane stops meaning anything, and the
producer that sent it would never learn it was wrong.

ADDRESSING (``to``/``hops``) IS A ROUTING HINT, NEVER AUTHORIZATION
------------------------------------------------------------------
An event may name up to :data:`MAX_TO_TARGETS` recipients so something downstream can
carry it to a specific body. That is all it is. ``actor.kind``/``id``/``name`` are echoed
verbatim from the payload the producer sent, so an addressed event proves only that
*somebody* asked — authority travels in the mailbox file's ``aither-steer v1
authority="owner|peer"`` header, not here. A dispatcher that read ``to`` as permission
would let any producer put text in front of any session.

``actor.*`` IS THE CLAIM; ``auth`` IS THE DAEMON'S OWN FINDING
---------------------------------------------------------------
A stamped event carries a second block, ``auth = {"principal", "plan"}``, built ONLY
from the ``auth=`` argument :meth:`Room.publish` was called with -- the ``Principal`` the
daemon's ``POST /events`` route resolved from the bearer the producer actually presented.
A producer that writes ``auth`` into its own payload gets it dropped on the fixed-key-set
line like any other undeclared field, so a non-empty ``auth`` was never typed by a
producer. The two blocks must never be read interchangeably: ``actor.kind == "human"`` is
what the sender SAID, ``auth.plan == "owner"`` is what the daemon FOUND. The in-process
producers (spool tailer, transcript bridge) call ``publish`` with no ``auth`` and land
with ``auth == {}``, which every consumer must treat as "unvouched" -- never as owner.

Validation lives in :meth:`Room._normalise` and NOT in the HTTP route, because
``_normalise`` returns a FIXED key set: a field added anywhere else is silently dropped
on the way in, and the two loudest producers (the spool tailer and the transcript bridge)
never cross the route at all — a rule on the route would read as enforced and miss them.

LISTENERS
---------
:meth:`Room.add_listener` / :meth:`RoomRegistry.add_listener` fan out at the END of
``publish``, OUTSIDE the lock and after the transcript append. Both are required: a
listener that raised into ``publish`` would turn one bad consumer into a lost event, and a
listener that ran inside the lock would turn the already-25-s publish on a huge room into
a wedge.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.aither_events_generated import (
    ACTOR_KINDS,
    PILLARS,
    PROTOCOL_VERSION,
    TIERS,
    pillar_for,
)

#: Events kept in memory per room for reconnecting clients. The transcript on disk is
#: the durable record; this is only the fast path for a client that dropped briefly.
MAX_BUFFERED_EVENTS = 2000

#: How much of the transcript tail to re-read when a room is constructed. Bounded so a
#: room with a months-long history does not stall daemon startup re-parsing all of it;
#: 4 MB comfortably covers MAX_BUFFERED_EVENTS at typical event size.
HYDRATE_TAIL_BYTES = 4 * 1024 * 1024

#: 🚩 THE TRANSCRIPT IS ROTATED, BECAUSE AN UNBOUNDED ONE STOPS THE ROOM.
#: Measured 2026-09-18 on the owner's box: room "main" had grown to 554 MB of
#: JSONL (every Claude Code tab emits tool_call rows into it) and a single
#: ``POST /events`` to that room took OVER 25 SECONDS, while the same request
#: against a fresh room answered in 0.23 s — same route, same lock, same code.
#: Every producer with a client timeout (the desk's room publisher gives up at
#: 4 s) was therefore silently failing to reach the room, which reads exactly
#: like "the spine is down". The in-memory buffer was capped from the start;
#: the file behind it never was. Rotation keeps the ACTIVE file small, which is
#: the only thing append speed depends on. Old segments are renamed, never
#: deleted: this is the audit record.
MAX_TRANSCRIPT_BYTES = int(os.environ.get("AITHER_HARNESS_ROOM_MAX_BYTES", 64 * 1024 * 1024))

#: Room ids are used as directory names, so they are constrained rather than sanitised.
#: Sanitising invites two different ids collapsing onto one directory.
_ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_ROOM = "main"

#: How many actors one event may address. Small on purpose: ``to`` exists to steer a
#: named body, and a producer that wants to reach everyone already has the room.
MAX_TO_TARGETS = 8

#: 🚩 A ROUND TRIP, NOT A CHAIN. ``hops`` counts how many times an addressed event has
#: been re-emitted by something that received one. 2 is the ceiling because the shapes
#: this spine actually needs are "owner asks an agent" (0) and "that agent answers" (1);
#: anything deeper is a loop, and a loop here lands text in interactive sessions.
MAX_HOPS = 2

#: Actor ids that may appear in ``to``. Constrained rather than sanitised for the same
#: reason room ids are: sanitising invites two different ids collapsing onto one target.
#: Colon is allowed because the namespaced forms (``relay:<nick>``) are already in use.
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: A publish listener, called as ``fn(room, event)`` or ``fn(event)`` — whichever its
#: signature takes (see :func:`_listener_wants_room`).
RoomListener = Callable[..., Any]


class RoomError(ValueError):
    """A malformed room id or event. Always carries the reason, never a bare refusal."""


def rooms_root() -> Path:
    base = os.environ.get(
        "AITHER_HARNESS_ROOMS_ROOT", Path.home() / ".aither" / "harness-rooms"
    )
    return Path(base)


def validate_room_id(room_id: str) -> str:
    room_id = (room_id or "").strip().lower()
    if not _ROOM_ID_RE.match(room_id):
        raise RoomError(
            f"invalid room id {room_id!r}: lowercase letters, digits, dot, dash and "
            "underscore only, 1-64 chars"
        )
    return room_id


def _normalise_actor(kind: str, actor_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """The actor as the room stores it: kind/id/name, plus `title` when set.

    Rebuilding the actor from three literal keys silently DROPPED every other
    field a producer sent -- measured 2026-09-19: the transcript bridge set
    `title` (what the session is working on) on every event and every stored
    event served `title: None`, with producer and consumer both looking right.
    `title` rides only when non-empty so the stored shape is unchanged for
    every producer that does not carry one; it is clipped, because a room row
    is a line the owner reads, not a document.
    """
    out = {"kind": kind, "id": actor_id, "name": str(raw.get("name") or actor_id)}
    title = str(raw.get("title") or "").strip()
    if title:
        out["title"] = title[:120]
    return out


def _normalise_addressing(event: Dict[str, Any], sender_id: str) -> Dict[str, Any]:
    """Validate ``to``/``hops`` and return ONLY the keys that belong on the event.

    Both default ABSENT: an event with neither field comes back as ``{}``, so every
    producer shape that existed before addressing (spool, transcript bridge, desk, relay)
    produces a byte-identical envelope.

    🚩 AN EMPTY ``to`` IS ABSENT, NOT AN ERROR. The daemon's ``PublishEvent`` model
    defaults ``to`` to ``[]`` and ``hops`` to ``0``, so every HTTP publish carries them
    whether the producer meant to address anyone or not. Refusing an empty list would
    400 every existing HTTP producer; echoing ``to: []`` would mark every event as
    "addressed to nobody". So ``[]`` and ``hops=0`` with no targets both collapse to
    nothing. ``hops`` is still VALIDATED first, so a bad value is refused either way.

    ``to`` is a routing hint, never authorization — see the module docstring.
    """
    raw_to = event.get("to")
    raw_hops = event.get("hops")

    hops = 0
    if raw_hops is not None:
        # bool is an int subclass: ``hops: true`` must not read as 1.
        if isinstance(raw_hops, bool) or not isinstance(raw_hops, int):
            raise RoomError(f"hops must be an integer 0-{MAX_HOPS}, got {raw_hops!r}")
        if raw_hops < 0:
            raise RoomError(f"hops {raw_hops} is negative; expected 0-{MAX_HOPS}")
        if raw_hops > MAX_HOPS:
            raise RoomError(f"hops {raw_hops} exceeds the limit {MAX_HOPS}")
        hops = raw_hops

    if raw_to is None or (isinstance(raw_to, (list, tuple)) and not raw_to):
        return {"hops": hops} if hops else {}

    # A bare string is the likeliest producer mistake, and iterating it would address
    # one actor per CHARACTER — refuse it by name rather than by accident.
    if isinstance(raw_to, str) or not isinstance(raw_to, (list, tuple)):
        raise RoomError(
            f"to must be a list of 1-{MAX_TO_TARGETS} actor ids, got {type(raw_to).__name__}"
        )
    if len(raw_to) > MAX_TO_TARGETS:
        raise RoomError(f"to lists {len(raw_to)} targets; at most {MAX_TO_TARGETS}")

    targets: List[str] = []
    for index, value in enumerate(raw_to):
        target = value.strip() if isinstance(value, str) else ""
        if not _ACTOR_ID_RE.match(target):
            raise RoomError(
                f"to[{index}] is not a valid actor id ({value!r}): letters, digits, dot, "
                "dash, underscore and colon only, 1-128 chars"
            )
        if target == sender_id:
            raise RoomError(f"to[{index}] names the sender; an actor cannot address itself")
        # Deduped, order kept: a retrying producer that lists a target twice must not
        # get it delivered twice by a dispatcher that trusts the list.
        if target not in targets:
            targets.append(target)
    return {"to": targets, "hops": hops}


def _listener_wants_room(fn: RoomListener) -> bool:
    """Does this listener take ``(room, event)`` or only ``(event)``?

    Decided ONCE, at registration, from the signature. A dispatcher needs the Room (to
    resolve a target against the room's own participants); a counter or a probe needs
    only the event. Guessing by calling and catching ``TypeError`` would misread a
    TypeError raised INSIDE the listener as a signature mismatch and call it twice.
    Only REQUIRED positionals count: ``def on_event(event, sink=None)`` is a one-arg
    listener with an option, not a request for the room. ``*args`` and anything
    uninspectable get ``(room, event)`` — the richer form.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    required = 0
    for param in params:
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.default is inspect.Parameter.empty and param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            required += 1
    return required >= 2


def _listener_name(fn: RoomListener) -> str:
    return str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or fn)


class Room:
    """One ordered, durable, replayable event stream with a participant roster."""

    def __init__(self, room_id: str, title: str = "") -> None:
        self.id = validate_room_id(room_id)
        self.title = title or self.id
        self.created_at = time.time()
        self._lock = threading.Lock()
        self._seq = 0
        self._events: List[Dict[str, Any]] = []
        #: actor id -> last-seen record. Presence is DERIVED from traffic rather than
        #: declared, so a producer that dies stops being present without having to
        #: announce it -- the same reason the cockpit derives session status from the
        #: transcript rather than trusting a status field.
        self._participants: Dict[str, Dict[str, Any]] = {}
        #: (listener, wants_room). A SEPARATE lock from ``_lock``: registration must never
        #: contend with the publish hot path, and fan-out snapshots this list and then
        #: runs with NO lock held.
        self._listeners: List[Tuple[RoomListener, bool]] = []
        self._listener_lock = threading.Lock()

        self.dir = rooms_root() / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._transcript = self.dir / "events.jsonl"
        #: Bytes in the ACTIVE transcript segment, tracked so the rotation check
        #: costs no syscall per publish. Seeded by _hydrate from the file on disk.
        self._transcript_bytes = 0
        self.hydrated = self._hydrate()

    def _hydrate(self) -> int:
        """Reload the transcript tail so ``?since=`` survives a daemon restart.

        Reads only the last :data:`HYDRATE_TAIL_BYTES`, discards the first (possibly
        partial) line, and keeps the most recent :data:`MAX_BUFFERED_EVENTS` entries.
        ``_seq`` resumes from the highest seq found, so a restarted room never reissues
        a sequence number a client has already seen — a duplicate seq would make
        ``events_since`` skip real events.

        A corrupt or unreadable transcript degrades to an empty buffer and says so on
        stderr. Refusing to construct the room would take the whole spine down over one
        bad line; pretending it was empty silently would hide it.
        """
        self._transcript_bytes = 0
        if not self._transcript.is_file():
            return 0
        try:
            size = self._transcript.stat().st_size
            #: Seed the rotation counter: a room that starts up already over the
            #: cap rotates on its FIRST publish, which is how an existing oversized
            #: transcript (the 554 MB "main" that started this) heals itself.
            self._transcript_bytes = size
            with self._transcript.open("rb") as handle:
                if size > HYDRATE_TAIL_BYTES:
                    handle.seek(size - HYDRATE_TAIL_BYTES)
                    handle.readline()  # drop the partial first line
                raw = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write(f"[room {self.id}] could not hydrate: {exc}\n")
            return 0

        events: List[Dict[str, Any]] = []
        dropped = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                dropped += 1
                continue
            if isinstance(event, dict) and isinstance(event.get("seq"), int):
                events.append(event)
            else:
                dropped += 1

        if dropped:
            sys.stderr.write(
                f"[room {self.id}] hydrate skipped {dropped} unreadable transcript line(s)\n"
            )
        if not events:
            return 0

        events.sort(key=lambda e: e["seq"])
        self._events = events[-MAX_BUFFERED_EVENTS:]
        self._seq = max(e["seq"] for e in events)
        for event in self._events:
            actor = event.get("actor")
            if not isinstance(actor, dict) or not actor.get("id"):
                continue
            record = self._participants.setdefault(
                actor["id"],
                {"kind": actor.get("kind", "service"), "id": actor["id"],
                 "name": actor.get("name") or actor["id"],
                 "first_seen": event.get("ts", 0.0), "events": 0},
            )
            record["last_seen"] = event.get("ts", 0.0)
            record["events"] += 1
        return len(self._events)

    # ── ingest ──────────────────────────────────────────────────────────────

    def publish(self, event: Dict[str, Any], *, auth: tuple = ()) -> Dict[str, Any]:
        """Validate, stamp, persist and buffer one event. Safe from any thread.

        Returns the stamped event. Raises :class:`RoomError` on a malformed envelope --
        an ingest endpoint that accepted anything would turn a producer bug into a
        stream that is merely confusing.

        ``auth`` is ``(principal_id, plan)`` as the DAEMON resolved it from the caller's
        bearer, or empty for a producer nobody authenticated (the in-process tailer and
        bridge). It is the only source of the stamped event's ``auth`` block -- see the
        module docstring's "``actor.*`` IS THE CLAIM" section.
        """
        stamped = self._normalise(event, auth)

        with self._lock:
            self._seq += 1
            stamped["seq"] = self._seq
            stamped["room"] = self.id
            self._events.append(stamped)
            if len(self._events) > MAX_BUFFERED_EVENTS:
                del self._events[: len(self._events) - MAX_BUFFERED_EVENTS]
            actor = stamped["actor"]
            record = self._participants.setdefault(
                actor["id"],
                {"kind": actor["kind"], "id": actor["id"], "name": actor["name"],
                 "first_seen": stamped["ts"], "events": 0},
            )
            record["last_seen"] = stamped["ts"]
            record["events"] += 1
            record["name"] = actor["name"] or record["name"]

        # Outside the lock: disk I/O must not serialize concurrent producers. A failed
        # append is reported inline rather than swallowed -- losing the audit record
        # silently is worse than a noisy room.
        line = json.dumps(stamped) + "\n"
        try:
            self._rotate_if_large(len(line.encode("utf-8")))
            with self._transcript.open("a", encoding="utf-8") as handle:
                handle.write(line)
            self._transcript_bytes += len(line.encode("utf-8"))
        except OSError as exc:
            sys.stderr.write(f"[room {self.id}] transcript write failed: {exc}\n")
        self._notify(stamped)
        return stamped

    # ── listeners ───────────────────────────────────────────────────────────

    def add_listener(self, fn: RoomListener) -> None:
        """Call ``fn`` once for every event published to this room from now on.

        Called as ``fn(room, event)`` or ``fn(event)`` per :func:`_listener_wants_room`.
        IDEMPOTENT: registering the same callable twice is a no-op, because a steer
        dispatcher registered twice would deliver every addressed event twice — and an
        LRU downstream only catches that if both calls land inside its window.
        """
        if not callable(fn):
            raise RoomError(f"listener must be callable, got {type(fn).__name__}")
        wants_room = _listener_wants_room(fn)
        with self._listener_lock:
            if any(existing is fn or existing == fn for existing, _ in self._listeners):
                return
            self._listeners.append((fn, wants_room))

    def _notify(self, event: Dict[str, Any]) -> None:
        """Fan out one stamped event. Runs with NO lock held, after the transcript write.

        A listener that raises costs one stderr line and nothing else: the event is
        already buffered and on disk, and the NEXT listener still runs. Listeners get the
        same dict the buffer holds — they must treat it as read-only.
        """
        with self._listener_lock:
            listeners = list(self._listeners)
        for fn, wants_room in listeners:
            try:
                if wants_room:
                    fn(self, event)
                else:
                    fn(event)
            except Exception as exc:  # a consumer bug must never become a lost event
                sys.stderr.write(
                    f"[room {self.id}] publish listener {_listener_name(fn)} failed on "
                    f"seq {event.get('seq')}: {type(exc).__name__}: {exc}\n"
                )

    def _rotate_if_large(self, incoming: int) -> None:
        """Retire the active transcript once it passes the cap.

        Size is TRACKED, not stat()ed per event: a syscall per publish on the
        hot path is exactly the kind of cost that only shows up under the load
        this guard exists for. The counter is seeded from the file at startup
        (``_hydrate``) and corrected here if a rename races another process.

        Rotation renames, so the segment keeps its bytes and its name says when
        it closed. Nothing is deleted: a room transcript is the audit record,
        and a spine that quietly drops history is worse than a large directory.
        """
        if MAX_TRANSCRIPT_BYTES <= 0:
            return
        if self._transcript_bytes + incoming <= MAX_TRANSCRIPT_BYTES:
            return
        try:
            if not self._transcript.exists():
                self._transcript_bytes = 0
                return
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            target = self.dir / f"events-{stamp}.jsonl"
            # A second rotation inside the same second must not clobber the first.
            suffix = 1
            while target.exists():
                target = self.dir / f"events-{stamp}-{suffix}.jsonl"
                suffix += 1
            self._transcript.replace(target)
            self._transcript_bytes = 0
            sys.stderr.write(f"[room {self.id}] transcript rotated to {target.name}\n")
        except OSError as exc:
            # A rotation that cannot happen must never stop the room from
            # recording. Keep appending to the large file and say so once.
            sys.stderr.write(f"[room {self.id}] transcript rotation failed: {exc}\n")
            self._transcript_bytes = 0

    def _normalise(self, event: Dict[str, Any], auth: tuple = ()) -> Dict[str, Any]:
        """Coerce an inbound payload into a valid AitherEvent, or refuse with a reason."""
        if not isinstance(event, dict):
            raise RoomError("event must be an object")

        event_type = str(event.get("type") or "").strip()
        if not event_type:
            raise RoomError("event.type is required")

        actor_raw = event.get("actor")
        if not isinstance(actor_raw, dict):
            raise RoomError("event.actor is required and must be an object")
        actor_kind = str(actor_raw.get("kind") or "").strip()
        actor_id = str(actor_raw.get("id") or "").strip()
        if actor_kind not in ACTOR_KINDS:
            raise RoomError(
                f"unknown actor.kind {actor_kind!r}; expected one of {', '.join(ACTOR_KINDS)}"
            )
        if not actor_id:
            raise RoomError("actor.id is required")

        # An absent pillar is DERIVED from the vocabulary; a present one must be real.
        # Deriving is what makes a new producer a one-line change -- it emits the event
        # type it already had and lands in the right lane.
        pillar = event.get("pillar")
        if pillar is None:
            pillar = pillar_for(event_type)
        elif pillar not in PILLARS:
            raise RoomError(
                f"unknown pillar {pillar!r}; expected one of {', '.join(PILLARS)} or null"
            )

        tier = str(event.get("tier") or "host")
        if tier not in TIERS:
            raise RoomError(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")

        ts = event.get("ts")
        if not isinstance(ts, (int, float)) or ts <= 0:
            ts = time.time()

        # 🚩 MUST be here, not on the route: the dict below is a FIXED key set, so a field
        # validated anywhere else is dropped on this line without a word.
        addressing = _normalise_addressing(event, actor_id)

        normalised = {
            "v": int(event.get("v") or PROTOCOL_VERSION),
            "id": str(event.get("id") or uuid.uuid4().hex),
            "seq": 0,
            "ts": float(ts),
            "room": self.id,
            "session": str(event.get("session") or ""),
            "actor": _normalise_actor(actor_kind, actor_id, actor_raw),
            "pillar": pillar,
            "tier": tier,
            "type": event_type,
            "stage": str(event.get("stage") or ""),
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            "correlation_id": str(event.get("correlation_id") or ""),
            "causation_id": str(event.get("causation_id") or ""),
            # Built ONLY from the ``publish(auth=...)`` argument, never from ``event`` --
            # a producer that puts ``auth`` in its own payload is dropped on this line
            # exactly like any other key the fixed set does not declare.
            "auth": (
                {"principal": str(auth[0]), "plan": str(auth[1])}
                if auth and len(auth) >= 2 else {}
            ),
        }
        normalised.update(addressing)
        return normalised

    # ── read ────────────────────────────────────────────────────────────────

    def events_since(self, seq: int = 0, limit: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            out = [e for e in self._events if e["seq"] > seq]
        return out[-limit:] if limit else out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def last_event_ts(self) -> Optional[float]:
        """``ts`` of the newest buffered event, or None for an empty room.

        Why this exists beside ``last_seq``: seq is a monotonic counter, so "is the room
        still receiving?" took TWO samples and a wait. A timestamp answers it from ONE
        GET. A hydrated room reports its newest ON-DISK event, which is the honest answer
        after a restart: nothing new has arrived yet.
        """
        with self._lock:
            if not self._events:
                return None
            ts = self._events[-1].get("ts")
        return float(ts) if isinstance(ts, (int, float)) and ts > 0 else None

    def participants(self, idle_after: float = 300.0) -> List[Dict[str, Any]]:
        """Roster with liveness derived from traffic, newest first."""
        now = time.time()
        with self._lock:
            records = [dict(r) for r in self._participants.values()]
        for record in records:
            record["idle_seconds"] = round(now - record.get("last_seen", now), 1)
            record["active"] = record["idle_seconds"] <= idle_after
        records.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
        return records

    def pillar_counts(self) -> Dict[str, int]:
        """Events per lane. Reported so an empty lane is a fact, not a guess."""
        counts = {name: 0 for name in PILLARS}
        with self._lock:
            for event in self._events:
                pillar = event.get("pillar")
                # A malformed transcript line may carry an unhashable pillar.
                if isinstance(pillar, str) and pillar in counts:
                    counts[pillar] += 1
        return counts

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "last_seq": self.last_seq,
            "last_event_ts": self.last_event_ts,
            "participants": self.participants(),
            "pillars": self.pillar_counts(),
            "transcript": str(self._transcript),
        }


class RoomRegistry:
    """Process-wide room set. Rooms are created on first use, never 404 on publish."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rooms: Dict[str, Room] = {}
        #: Listeners every room gets, including rooms that do not exist yet.
        self._listeners: List[RoomListener] = []

    def add_listener(self, fn: RoomListener) -> None:
        """Register ``fn`` on every current room AND every room created later.

        🚩 Registering on the rooms that exist at startup is not enough: rooms are created
        lazily on first publish, and a room reconstructed from disk after a restart is
        "created" the first time anything touches it — a per-room registration made at
        boot would silently miss both. Idempotent, like :meth:`Room.add_listener`.
        """
        if not callable(fn):
            raise RoomError(f"listener must be callable, got {type(fn).__name__}")
        with self._lock:
            if any(existing is fn or existing == fn for existing in self._listeners):
                return
            self._listeners.append(fn)
            rooms = list(self._rooms.values())
        for room in rooms:
            room.add_listener(fn)

    def get_or_create(self, room_id: str = DEFAULT_ROOM, title: str = "") -> Room:
        room_id = validate_room_id(room_id)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                room = Room(room_id, title=title)
                # Attached BEFORE the room is published into the registry, so no event
                # can reach a new room ahead of its listeners.
                for fn in self._listeners:
                    room.add_listener(fn)
                self._rooms[room_id] = room
            return room

    def get(self, room_id: str) -> Optional[Room]:
        """In-memory room, or one reconstructed from its transcript.

        The disk fallback matters after a restart: rooms are created lazily, so a room
        with a full transcript on disk would 404 until something happened to publish to
        it again — durable, replayable, and invisible. That is the same defect
        hydration exists to fix, one level up.
        """
        room_id = validate_room_id(room_id)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is not None:
                return room
        if (rooms_root() / room_id / "events.jsonl").is_file():
            return self.get_or_create(room_id)
        return None

    def known_room_ids(self) -> List[str]:
        """Rooms in memory plus rooms persisted on disk, deduped."""
        with self._lock:
            ids = set(self._rooms)
        root = rooms_root()
        if root.is_dir():
            for child in root.iterdir():
                if (child / "events.jsonl").is_file():
                    try:
                        ids.add(validate_room_id(child.name))
                    except RoomError:
                        # A directory that is not a valid room id was not made by us.
                        continue
        return sorted(ids)

    def list_rooms(self) -> List[Dict[str, Any]]:
        return [info for info in (
            (self.get(room_id).info() if self.get(room_id) else None)
            for room_id in self.known_room_ids()
        ) if info]


_registry: Optional[RoomRegistry] = None
_registry_lock = threading.Lock()


def default_registry() -> RoomRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = RoomRegistry()
        return _registry
