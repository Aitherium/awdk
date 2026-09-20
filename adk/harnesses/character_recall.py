"""Character recall hook -- every room utterance becomes a party member's memory.

A :class:`~adk.harnesses.rooms.RoomRegistry` publish listener (registered the way
``steer_dispatch`` is: ``register(rooms)`` from ``daemon.py``) that indexes each spoken
event under its actor's ``persona_id`` through ``lib.avatars.character_recall`` -- awembed's
micro-embedder behind two append-only files per persona. The persona is resolved through
the party manifest awdesk exports (``%APPDATA%\\Desk\\party.json``, ``origin_key`` = room
actor id -> ``persona_id``); an agent the manifest does not name falls back to a
persona-shaped form of its actor id, so nothing spoken by a body is lost while the cast
catches up.

WHAT COUNTS AS AN UTTERANCE
---------------------------
Event types in :data:`UTTERANCE_TYPES` whose payload carries text (``text`` | ``content`` |
``message`` | ``reply`` -- the same keys the desk's ``room-publisher.shapeChat`` reads).
``tool_call``, ``thinking``, ``classify`` and the rest are not speech and are never indexed.

WHOSE MEMORY IT LANDS IN
------------------------
* ``self`` -- the actor's own persona, when the actor is a party member or an
  ``adk_agent``. A ``claude_code`` tab or a ``human`` with no party row is NOT given a
  memory: the transcript bridge emits every tab's assistant text as ``message`` events, and
  one memory directory per session id is an index of the owner's terminals, not a
  character's recall.
* ``heard`` -- each ``to`` target that is a party member or a ``relay:`` actor (the room's
  named agents). Unaddressed speech is not fanned out to every persona: a memory that
  contains everything anyone said is not a memory of what was said to *you*.

NEVER ON THE PUBLISH THREAD
---------------------------
Listeners run synchronously at the end of ``Room.publish``. The embedder is an HTTP call
with a timeout measured in seconds, so :meth:`CharacterRecallHook.handle` only classifies
and enqueues; one daemon thread resolves personas and drains the queue. A full queue DROPS the
oldest and counts it -- ``status()`` exposes ``dropped`` -- because blocking a publish
would wedge the room the way the 554 MB transcript once did. Off switch:
``AITHER_CHARACTER_RECALL=0``.

THE LIB IS IMPORTED OFF THE BOOT PATH
------------------------------------
``import lib`` costs ~12 s on this box (measured 2026-09-20: ``lib/__init__.py`` pulls the
agent toolkit). The daemon's bind-first boot grace cannot afford that, so the hook
constructs in microseconds, the worker thread imports the lib on its first tick, and
events published before then wait in the queue. A missing ``lib`` (the daemon runs from a
checkout; a wheel install has no ``lib/``) makes the hook a documented no-op:
``status()["available"]`` is False with the reason, and one stderr line says so. Silence
is not an option; a quiet room and a dead hook must not look the same.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The party manifest's persona-id grammar. Restated here rather than imported, because an
#: installed awdk has no party-manifest reader beside it and an actor must still be
#: classifiable without one. The manifest's own copy is the contract: an id it allows while
#: this refuses is a party member who can never be remembered, so a checker in the platform
#: repo parses both and fails if they diverge. Change one, change both.
PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PERSONA_ID_MAX = 128

#: Event types that are speech. Everything else is telemetry.
UTTERANCE_TYPES: Tuple[str, ...] = ("agent_message", "message", "command_reply", "steering")
#: Payload keys that carry the spoken text, first non-empty wins (room-publisher.shapeChat).
TEXT_KEYS: Tuple[str, ...] = ("text", "content", "message", "reply")
#: Actor kinds whose own speech is indexed even without a party row.
SELF_KINDS: Tuple[str, ...] = ("adk_agent",)
#: ``to`` targets indexed as ``heard`` without a party row.
HEARD_PREFIXES: Tuple[str, ...] = ("relay:",)

ENABLED_ENV = "AITHER_CHARACTER_RECALL"
PARTY_FILE_ENV = "DESK_PARTY_FILE"
QUEUE_MAX = 512
PARTY_RECHECK_SECONDS = 30.0
MAX_TEXT = 4000


def _party_file_default() -> Path:
    override = os.environ.get(PARTY_FILE_ENV)
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Desk" / "party.json"


def _repo_aitheros_dir() -> Optional[Path]:
    """``<repo>/AitherOS`` when this file lives in the monorepo checkout, else None."""
    candidate = Path(__file__).resolve().parents[3] / "AitherOS"
    return candidate if (candidate / "lib" / "avatars").is_dir() else None


def _import_lib() -> Tuple[Any, Optional[Callable[[Any], Any]], Optional[str]]:
    """(character_recall module, a ``load_party(path)`` callable, error).

    Two independent halves on purpose: the STORE is what makes the hook work at all, and
    the party READER only improves the persona it picks. Tries the ambient ``lib`` first,
    then the checkout this file sits in -- and an ambient ``lib`` from ANOTHER checkout
    (an editable install pointing elsewhere) can supply one half and not the other, which
    is what a plain "import both or neither" got wrong.
    """
    recall_mod: Any = None
    load_party: Optional[Callable[[Any], Any]] = None
    reason: Optional[str] = None
    for attempt in (0, 1):
        if attempt == 1:
            root = _repo_aitheros_dir()
            if root is None or str(root) in sys.path:
                break
            sys.path.insert(0, str(root))
        if recall_mod is None:
            try:
                from lib.avatars import character_recall as mod  # type: ignore[import-not-found]
                recall_mod = mod
            except ImportError as exc:
                reason = str(exc)
        if load_party is None:
            try:
                from lib.avatars.party_manifest import load_party as fn  # type: ignore
                load_party = fn
            except ImportError as exc:
                reason = reason or str(exc)
        if recall_mod is not None and load_party is not None:
            return recall_mod, load_party, None
    return recall_mod, load_party, None if recall_mod is not None else reason


def sanitize_actor_id(actor_id: Any) -> Optional[str]:
    """A room actor id (``relay:#agents:lyra``, ``service:awdesk``) as a persona-id-shaped
    key, or None when nothing usable remains. Kept byte-identical to the store-side function
    of the same name (see the grammar note at the top of this module): the party manifest is
    the real mapping, and this is what a persona is called when no row claims its actor."""
    if not isinstance(actor_id, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", actor_id.strip()).strip("._-")
    cleaned = cleaned[:PERSONA_ID_MAX]
    return cleaned if cleaned and PERSONA_ID_RE.match(cleaned) else None


def extract_utterance(event: Dict[str, Any]) -> Optional[str]:
    """The spoken text of an event, or None when it is not speech."""
    if not isinstance(event, dict) or str(event.get("type") or "") not in UTTERANCE_TYPES:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            if cleaned:
                return cleaned[:MAX_TEXT]
    return None


class PersonaResolver:
    """Room actor id -> persona_id via the party manifest, re-read when the file changes
    (checked at most every :data:`PARTY_RECHECK_SECONDS`). ``load_party`` is injected --
    the monorepo's reader in the fleet, a stub in a test, None on a box with no ``lib``,
    where every lookup falls through to :func:`sanitize_actor_id`."""

    def __init__(self, party_file: Optional[Path],
                 load_party: Optional[Callable[[Any], Any]] = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.party_file = party_file
        self._load_party = load_party
        self._now = now
        self._checked_at = -1e9
        self._mtime: Optional[float] = None
        self._by_origin: Dict[str, str] = {}
        self._members: set = set()
        self.party_error: Optional[str] = None
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        now = self._now()
        if now - self._checked_at < PARTY_RECHECK_SECONDS:
            return
        self._checked_at = now
        if self.party_file is None or self._load_party is None:
            return
        try:
            mtime = self.party_file.stat().st_mtime
        except OSError:
            if self._mtime is not None:
                self._mtime, self._by_origin, self._members = None, {}, set()
            self.party_error = None  # absent is a state, not an error
            return
        if mtime == self._mtime:
            return
        try:
            party = self._load_party(self.party_file)
        except Exception as exc:  # a manifest error, OSError, ValueError
            self.party_error = f"{type(exc).__name__}: {exc}"
            return
        by_origin: Dict[str, str] = {}
        members = set()
        for member in getattr(party, "members", ()):
            members.add(member.persona_id)
            if member.origin_key:
                by_origin[member.origin_key] = member.persona_id
        self._mtime, self._by_origin, self._members = mtime, by_origin, members
        self.party_error = None

    def resolve(self, actor_id: str) -> Tuple[Optional[str], bool]:
        """(persona_id or None, is_party_member)."""
        with self._lock:
            self._refresh()
            mapped = self._by_origin.get(actor_id)
            if mapped:
                return mapped, True
            if actor_id in self._members:
                return actor_id, True
        return sanitize_actor_id(actor_id), False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "party_file": str(self.party_file) if self.party_file else None,
                "party_loaded": self._mtime is not None,
                "party_members": len(self._members),
                "party_error": self.party_error,
            }


class CharacterRecallHook:
    """The listener. Construct once per process, ``register`` it, keep the instance."""

    def __init__(
        self,
        *,
        store: Any = None,
        party_file: Optional[Path] = None,
        load_party: Optional[Callable[[Any], Any]] = None,
        worker: bool = True,
        queue_max: int = QUEUE_MAX,
        enabled: Optional[bool] = None,
        log: Callable[[str], None] = lambda line: sys.stderr.write(line + "\n"),
    ) -> None:
        self._log = log
        self.enabled = (os.environ.get(ENABLED_ENV, "1").strip().lower()
                        not in ("0", "false", "no", "off")) if enabled is None else enabled
        self._party_file = party_file if party_file is not None else _party_file_default()
        self._store = store
        self._load_party = load_party
        self._recall_mod: Any = None
        self.unavailable: Optional[str] = None
        self._store_error: Optional[str] = None
        self.resolver: Optional[PersonaResolver] = None
        self._booted = threading.Event()
        self._queue: "queue.Queue[Tuple[str, str, str, Any, Dict[str, Any]]]" = queue.Queue(
            maxsize=max(1, queue_max))
        self._lock = threading.Lock()
        self._counts = {"seen": 0, "utterances": 0, "queued": 0, "indexed": 0,
                        "unembedded": 0, "dropped": 0, "failed": 0, "skipped": 0}
        self._last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if not self.enabled:
            self._booted.set()
            self._log(f"[character-recall] hook disabled: {self.reason}")
        elif worker:
            self._thread = threading.Thread(target=self._drain, name="character-recall",
                                            daemon=True)
            self._thread.start()

    # ── boot (off the publish thread and off the daemon's boot path) ──

    def _bootstrap(self) -> None:
        """Import the lib, build the store and the resolver. Idempotent; runs once."""
        if self._booted.is_set():
            return
        try:
            imported_load_party: Optional[Callable[[Any], Any]] = None
            if self._store is None or self._load_party is None:
                self._recall_mod, imported_load_party, self.unavailable = _import_lib()
            if self._store is None and self._recall_mod is not None:
                try:
                    self._store = self._recall_mod.default_store()
                except Exception as exc:
                    self._store_error = f"{type(exc).__name__}: {exc}"
            self.resolver = PersonaResolver(self._party_file,
                                            self._load_party or imported_load_party)
        finally:
            self._booted.set()
        if not self.available:
            self._log(f"[character-recall] hook disabled: {self.reason}")

    # ── state ──

    @property
    def available(self) -> Optional[bool]:
        """True once booted with a store, False when booted without one, None while the
        worker has not imported the lib yet (events queue meanwhile)."""
        if not self.enabled:
            return False
        if not self._booted.is_set():
            return None
        return self._store is not None

    @property
    def reason(self) -> Optional[str]:
        if not self.enabled:
            return f"{ENABLED_ENV} is off"
        if not self._booted.is_set():
            return "booting: the worker has not imported lib.avatars yet"
        if self.unavailable:
            return f"lib.avatars.character_recall not importable: {self.unavailable}"
        if self._store_error:
            return f"store construction failed: {self._store_error}"
        return None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            last_error = self._last_error
        out: Dict[str, Any] = {
            "available": self.available,
            "reason": self.reason,
            "queue_depth": self._queue.qsize(),
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "last_error": last_error,
            **counts,
        }
        if self.resolver is not None:
            out.update(self.resolver.status())
        else:
            out["party_file"] = str(self._party_file)
        if self._store is not None:
            out["root"] = str(getattr(self._store, "root", ""))
            out["model"] = getattr(self._store, "model_tag", None)
        return out

    # ── the listener ──

    def handle(self, room: Any, event: Dict[str, Any]) -> None:
        """``fn(room, event)`` -- cheap: classify and enqueue. Never raises, never imports,
        never touches the network. Resolution and indexing happen on the worker."""
        if self.available is False:
            return
        with self._lock:
            self._counts["seen"] += 1
        text = extract_utterance(event)
        if text is None:
            return
        with self._lock:
            self._counts["utterances"] += 1
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
        actor_id = str(actor.get("id") or "").strip()
        actor_kind = str(actor.get("kind") or "").strip()
        actor_name = str(actor.get("name") or actor_id)[:128]
        meta = {
            "speaker": actor_name or None,
            "ts": event.get("ts"),
            "room": getattr(room, "id", None) or event.get("room"),
            "event_id": event.get("id"),
        }
        to = event.get("to")
        self._enqueue((actor_id, actor_kind, text, list(to) if isinstance(to, (list, tuple))
                       else None, meta))

    def targets_for(self, actor_id: str, actor_kind: str,
                    to: Any) -> List[Tuple[str, str]]:
        """Which (persona_id, role) pairs one utterance lands in. Pure, for the test;
        needs the resolver, i.e. a booted hook."""
        out: List[Tuple[str, str]] = []
        resolver = self.resolver
        if resolver is None:
            return out
        if actor_id:
            persona, member = resolver.resolve(actor_id)
            if persona and (member or actor_kind in SELF_KINDS):
                out.append((persona, "self"))
        if isinstance(to, (list, tuple)):
            for target in to:
                if not isinstance(target, str) or not target.strip():
                    continue
                target = target.strip()
                persona, member = resolver.resolve(target)
                if not persona:
                    continue
                if member or target.startswith(HEARD_PREFIXES):
                    if all(p != persona for p, _ in out):
                        out.append((persona, "heard"))
        return out

    # ── the worker ──

    def _enqueue(self, item: Tuple[str, str, str, Any, Dict[str, Any]]) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                with self._lock:
                    self._counts["queued"] += 1
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    with self._lock:
                        self._counts["dropped"] += 1
                except queue.Empty:
                    continue

    def _drain(self) -> None:
        self._bootstrap()
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._index(item)
            self._queue.task_done()

    def _index(self, item: Tuple[str, str, str, Any, Dict[str, Any]]) -> None:
        if self._store is None:
            with self._lock:
                self._counts["skipped"] += 1
            return
        actor_id, actor_kind, text, to, meta = item
        targets = self.targets_for(actor_id, actor_kind, to)
        if not targets:
            with self._lock:
                self._counts["skipped"] += 1
            return
        for persona_id, role in targets:
            try:
                result = self._store.remember(
                    persona_id, text, role, speaker=meta.get("speaker"), ts=meta.get("ts"),
                    room=meta.get("room"), event_id=meta.get("event_id"),
                )
            except Exception as exc:
                with self._lock:
                    self._counts["failed"] += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                continue
            with self._lock:
                self._counts["indexed"] += 1
                if not getattr(result, "embedded", True):
                    self._counts["unembedded"] += 1
                    if getattr(result, "error", None):
                        self._last_error = f"{result.error_kind}: {result.error}"

    def flush(self, timeout: float = 5.0) -> bool:
        """Boot if needed and drain synchronously (tests; shutdown). True when the queue
        emptied in time."""
        deadline = time.monotonic() + timeout
        if self._thread is None:
            self._bootstrap()
            while not self._queue.empty():
                self._index(self._queue.get_nowait())
            return True
        while time.monotonic() < deadline:
            if self._booted.is_set() and self._queue.empty():
                return True
            time.sleep(0.02)
        return self._queue.empty()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def register(registry: Any, **deps: Any) -> CharacterRecallHook:
    """Build a :class:`CharacterRecallHook` and register it on a room registry (daemon.py).
    ``RoomRegistry.add_listener`` is idempotent per callable; call this once per process."""
    hook = CharacterRecallHook(**deps)
    registry.add_listener(hook.handle)
    return hook


__all__ = [
    "CharacterRecallHook",
    "PERSONA_ID_MAX",
    "PERSONA_ID_RE",
    "HEARD_PREFIXES",
    "PersonaResolver",
    "SELF_KINDS",
    "TEXT_KEYS",
    "UTTERANCE_TYPES",
    "extract_utterance",
    "register",
    "sanitize_actor_id",
]
