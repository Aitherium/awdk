"""Memory backends for the reasoning loop (the ``Memory`` protocol).

* :class:`InMemory` -- process-local; values are JSON round-tripped on the way in
  and out, so a caller cannot mutate stored state by aliasing.
* :class:`FileMemory` -- ``<dir>/<namespace>.json`` with an atomic
  read-merge-write ``update``.
* :class:`TypedMemoryBackend` -- the same ``get``/``put``/``update`` contract
  stored in adk's typed memory (``adk.typed_memory.TypedMemory`` over the local
  SQLite ``adk.memory.Memory``), every record keyed by a scope
  (domain / game / run), so procedural skills and PRISM scores carry across
  runs and semantic hypotheses carry across restarts of one game.

``InMemory`` and ``FileMemory`` are the vendored h30 backends under their adk
names. ``TypedMemoryBackend`` is duck-typed: this module never imports
``adk.memory`` or ``adk.typed_memory`` (the core stays hermetic); the caller
builds the store and passes it in::

    from adk.memory import Memory
    from adk.typed_memory import TypedMemory
    backend = TypedMemoryBackend(TypedMemory(Memory(db_path)), domain="arc", game="ls20")

It refuses a store whose Spirit bridge or fleet sync is switched on (build it
with ``AITHER_SPIRIT_BRIDGE=false`` and ``AITHER_FLEET_SYNC=false``) unless
``allow_network=True``: a solver's memory must not leave the machine by accident.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.parse
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from ._vendor.interfaces import FileMemoryBackend as FileMemory
from ._vendor.interfaces import InMemoryBackend as InMemory

__all__ = [
    "InMemory",
    "FileMemory",
    "TypedMemoryBackend",
    "SCOPE_LEVELS",
    "DEFAULT_SCOPES",
    "DEFAULT_ROLES",
]

#: Scope levels, widest first. A namespace stored at ``domain`` is shared by
#: every game and run of that domain; at ``game`` by every run of one game; at
#: ``run`` by nothing outside this backend's run.
SCOPE_LEVELS = ("global", "domain", "game", "run")

#: Where each h30 namespace lives. ``procedural`` holds the skill library (key
#: ``skills``) and PRISM scores (key ``prism``): reused across runs of a domain.
#: ``hypotheses`` holds hypotheses-as-code with their evidence
#: (``adk.reasoning.solve.hypotheses``): one game's rules, so per game.
#: Anything else is per run. Episodic memory is never stored here.
DEFAULT_SCOPES: Dict[str, str] = {"procedural": "domain", "hypotheses": "game"}

#: The typed-memory role each namespace is stored under (authority on recall).
DEFAULT_ROLES: Dict[str, str] = {"procedural": "procedure", "hypotheses": "insight"}

# One lock per backing store, shared by every backend instance over it in this
# process, so two games of one run that update at nearly the same time keep
# each other's entries (FileMemory's guarantee).
_STORE_LOCKS: Dict[str, threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(key: str) -> threading.Lock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = _STORE_LOCKS[key] = threading.Lock()
        return lock


def _broken() -> bool:
    """``SOLVE_MEMORY_BREAK=1``: ``TypedMemoryBackend`` forgets (``get`` returns
    the default, ``update`` sees ``None``) and persisted refutations are not
    loaded. Exists so the round-trip, merge, cross-run and restart tests can be
    shown to fail."""
    return os.environ.get("SOLVE_MEMORY_BREAK") == "1"


class _LoopThread:
    """A private event loop on a daemon thread. Typed memory is async; the
    ``Memory`` protocol is sync and is called from the solver's worker thread
    (and from tests on the main thread)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def run(self, coro: Any, timeout: float) -> Any:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                t = threading.Thread(
                    target=loop.run_forever, name="solve-typed-memory", daemon=True
                )
                t.start()
                self._loop, self._thread = loop, t
            loop = self._loop
        if threading.current_thread() is self._thread:  # pragma: no cover - misuse
            coro.close()
            raise RuntimeError("TypedMemoryBackend called from its own loop thread")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    def close(self) -> None:
        with self._lock:
            loop, t = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None and t is not None:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=5)
            loop.close()


class TypedMemoryBackend:
    """``get``/``put``/``update`` over ``adk.typed_memory.TypedMemory``.

    Merge semantics are FileMemory's: ``get`` returns a fresh JSON copy of the
    stored value, or ``default`` when the key was never written; ``put``
    replaces; ``update(ns, key, fn)`` calls ``fn`` with the stored value (or
    ``None``), stores the result and returns it, under a lock shared by every
    backend over the same store in this process. A value that is not JSON
    raises ``TypeError`` and stores nothing.

    Each record is a typed memory: ``role`` from ``roles`` (``procedure`` for
    skills and PRISM, ``insight`` for hypotheses, ``fact`` otherwise),
    ``tier=persistent``, ``reinforcement_count`` bumped on every rewrite, and
    the scope under ``metadata["solve_scope"]``. Record ids are
    ``solve/<domain>/<game>/<run>/<namespace>/<key>`` with ``*`` for the parts
    a namespace's scope level leaves out.
    """

    def __init__(
        self,
        typed: Any,
        *,
        domain: str = "default",
        game: Optional[str] = None,
        run: Optional[str] = None,
        scopes: Optional[Dict[str, str]] = None,
        roles: Optional[Dict[str, str]] = None,
        allow_network: bool = False,
        timeout_s: float = 30.0,
    ) -> None:
        if not (
            callable(getattr(typed, "remember", None)) and callable(getattr(typed, "get", None))
        ):
            raise TypeError("typed must be an adk.typed_memory.TypedMemory (remember/get)")
        backing = getattr(typed, "_mem", None)
        if not allow_network:
            live = [
                env
                for env, attr in (
                    ("AITHER_SPIRIT_BRIDGE", "_spirit_enabled"),
                    ("AITHER_FLEET_SYNC", "_fleet_enabled"),
                )
                if bool(getattr(backing, attr, False))
            ]
            if live:
                raise ValueError(
                    "the typed memory store would sync over the network (%s on); build it with "
                    "%s=false, or pass allow_network=True"
                    % (", ".join(live), "=false and ".join(live))
                )
        self.typed = typed
        self.domain = str(domain)
        self.game = None if game is None else str(game)
        self.run = str(run) if run is not None else uuid.uuid4().hex[:12]
        self.scopes = dict(DEFAULT_SCOPES if scopes is None else scopes)
        for ns, level in self.scopes.items():
            if level not in SCOPE_LEVELS:
                raise ValueError(
                    "scope for %r must be one of %s, not %r" % (ns, SCOPE_LEVELS, level)
                )
        self.roles = dict(DEFAULT_ROLES if roles is None else roles)
        self.timeout_s = float(timeout_s)
        store_key = str(getattr(backing, "_db_path", "") or "id:%d" % id(typed))
        self._lock = _store_lock(store_key)
        self._runner = _LoopThread()

    # -- keys --------------------------------------------------------------
    def scope_of(self, namespace: str) -> str:
        return self.scopes.get(namespace, "run")

    def record_id(self, namespace: str, key: str) -> str:
        level = SCOPE_LEVELS.index(self.scope_of(namespace))
        q = lambda p: urllib.parse.quote(p, safe="")  # noqa: E731 - "*" is never a quoted part
        parts = [
            q(self.domain) if level >= 1 else "*",
            q(self.game if self.game is not None else "_") if level >= 2 else "*",
            q(self.run) if level >= 3 else "*",
            q(namespace),
            q(key),
        ]
        return "solve/" + "/".join(parts)

    # -- storage -----------------------------------------------------------
    def _read(self, namespace: str, key: str) -> Tuple[bool, Any, Dict[str, Any]]:
        entry = self._runner.run(self.typed.get(self.record_id(namespace, key)), self.timeout_s)
        if entry is None:
            return False, None, {}
        md = dict(getattr(entry, "metadata", None) or {})
        return True, json.loads(entry.value), md

    def _write(self, namespace: str, key: str, value: Any, prev_md: Dict[str, Any]) -> None:
        text = json.dumps(value)  # TypeError on a non-JSON value, before anything is stored
        md: Dict[str, Any] = {
            "solve_scope": {
                "level": self.scope_of(namespace),
                "domain": self.domain,
                "game": self.game,
                "run": self.run,
                "namespace": namespace,
                "key": key,
            },
        }
        if prev_md:
            md["created_at"] = prev_md.get("created_at")
            md["reinforcement_count"] = int(prev_md.get("reinforcement_count", 0) or 0) + 1
        self._runner.run(
            self.typed.remember(
                text,
                role=self.roles.get(namespace, "fact"),
                tier="persistent",
                confidence=0.7,
                metadata=md,
                id_=self.record_id(namespace, key),
            ),
            self.timeout_s,
        )

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        found, value, _md = self._read(namespace, key)
        return value if found and not _broken() else default

    def put(self, namespace: str, key: str, value: Any) -> None:
        with self._lock:
            _found, _value, md = self._read(namespace, key)
            self._write(namespace, key, value, md)

    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            found, value, md = self._read(namespace, key)
            new = fn(value if found and not _broken() else None)
            self._write(namespace, key, new, md)
            return new

    def close(self) -> None:
        self._runner.close()

    def __enter__(self) -> "TypedMemoryBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
