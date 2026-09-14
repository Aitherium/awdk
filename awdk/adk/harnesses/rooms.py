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
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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

#: Room ids are used as directory names, so they are constrained rather than sanitised.
#: Sanitising invites two different ids collapsing onto one directory.
_ROOM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

DEFAULT_ROOM = "main"


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

        self.dir = rooms_root() / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._transcript = self.dir / "events.jsonl"
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
        if not self._transcript.is_file():
            return 0
        try:
            size = self._transcript.stat().st_size
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

    def publish(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Validate, stamp, persist and buffer one event. Safe from any thread.

        Returns the stamped event. Raises :class:`RoomError` on a malformed envelope --
        an ingest endpoint that accepted anything would turn a producer bug into a
        stream that is merely confusing.
        """
        stamped = self._normalise(event)

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
        try:
            with self._transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stamped) + "\n")
        except OSError as exc:
            sys.stderr.write(f"[room {self.id}] transcript write failed: {exc}\n")
        return stamped

    def _normalise(self, event: Dict[str, Any]) -> Dict[str, Any]:
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

        return {
            "v": int(event.get("v") or PROTOCOL_VERSION),
            "id": str(event.get("id") or uuid.uuid4().hex),
            "seq": 0,
            "ts": float(ts),
            "room": self.id,
            "session": str(event.get("session") or ""),
            "actor": {
                "kind": actor_kind,
                "id": actor_id,
                "name": str(actor_raw.get("name") or actor_id),
            },
            "pillar": pillar,
            "tier": tier,
            "type": event_type,
            "stage": str(event.get("stage") or ""),
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            "correlation_id": str(event.get("correlation_id") or ""),
            "causation_id": str(event.get("causation_id") or ""),
        }

    # ── read ────────────────────────────────────────────────────────────────

    def events_since(self, seq: int = 0, limit: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            out = [e for e in self._events if e["seq"] > seq]
        return out[-limit:] if limit else out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

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
                if pillar in counts:
                    counts[pillar] += 1
        return counts

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "last_seq": self.last_seq,
            "participants": self.participants(),
            "pillars": self.pillar_counts(),
            "transcript": str(self._transcript),
        }


class RoomRegistry:
    """Process-wide room set. Rooms are created on first use, never 404 on publish."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rooms: Dict[str, Room] = {}

    def get_or_create(self, room_id: str = DEFAULT_ROOM, title: str = "") -> Room:
        room_id = validate_room_id(room_id)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                room = Room(room_id, title=title)
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
