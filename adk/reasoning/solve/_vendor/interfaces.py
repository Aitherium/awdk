# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/interfaces.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""The three seams of the h30 reasoning core.

The core (everything in ``agent/repl/core``) is game-agnostic.  It talks to
the world only through these interfaces, so it can be promoted into awdk and
backed by other environments, model clients and memory stores.

* ``Environment`` -- observe / act / available_actions / done, plus optional
  perception hooks (``DomainHooks``) that turn states into prompt text and
  offer domain tools.  ``act`` is BLOCKING: it returns the next observation.
* ``LLM``         -- one chat completion (OpenAI-shaped messages).
* ``MemoryBackend`` -- namespaced JSON key/value persistence.  Files today;
  adk ``typed_memory`` / ``crystal`` later.

Stdlib + numpy only.  3.10-compatible.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import numpy as np

Action = Tuple[int, int, int]  # (action_id, x, y); x = y = -1 when unparameterised


@dataclass
class Obs:
    """One observation.  ``state`` is any numpy array the domain chooses."""

    state: np.ndarray
    level: int = 0
    level_up: bool = False      # this observation started a new level
    died: bool = False          # the action ended in a loss (the env already restarted)
    done: bool = False          # the episode is over (won or stopped)
    win_state: Optional[np.ndarray] = None  # the winning state, when the domain can show it
    info: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Environment(Protocol):
    def observe(self) -> Obs: ...

    def act(self, action: Action, source: str = "model") -> Obs: ...

    def available_actions(self) -> List[int]: ...

    def done(self) -> bool: ...


class DomainHooks(Protocol):
    """Optional perception + tools.  Every method has a core fallback."""

    def primer(self) -> str: ...                       # domain API + knowledge for the system prompt

    def render(self, obs: Obs, last: Any) -> str: ...  # the SITUATION text

    def describe(self, t: Any) -> str: ...             # one transition as text

    def tools(self) -> Dict[str, Tuple[Callable[..., Any], str]]: ...  # name -> (fn, one-line doc)

    def handoff(self, n: int) -> Dict[str, Any]: ...   # n actions chosen by a non-LLM policy

    def candidates(self) -> List[Action]: ...          # actions worth a systematic scan

    def state_key(self, state: np.ndarray) -> str: ...  # novelty key (masks clocks/HUD)


@dataclass
class ChatReply:
    content: str
    usage: Dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0


class LLM(Protocol):
    def chat(self, messages: List[Dict[str, Any]], max_tokens: int = 1000,
             temperature: float = 0.4, extra: Optional[Dict[str, Any]] = None) -> Any: ...


class LLMFailure(RuntimeError):
    """Raised by an LLM adapter on any transport/HTTP failure."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class ModelMismatch(LLMFailure):
    """The endpoint answered with a different model than requested (a
    cross-model route).  FATAL: the loop stops calling the model for the rest
    of the episode and the episode is flagged, because its numbers would
    measure the wrong model."""

    fatal = True


# ============================================================================
# memory backends
# ============================================================================
class MemoryBackend(Protocol):
    def get(self, namespace: str, key: str, default: Any = None) -> Any: ...

    def put(self, namespace: str, key: str, value: Any) -> None: ...

    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any: ...


class InMemoryBackend:
    """Process-local backend (tests, single-game runs)."""

    def __init__(self) -> None:
        self._d: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        with self._lock:
            v = self._d.get((namespace, key), default)
            return json.loads(json.dumps(v)) if v is not None else default

    def put(self, namespace: str, key: str, value: Any) -> None:
        with self._lock:
            self._d[(namespace, key)] = json.loads(json.dumps(value))

    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            new = fn(self._d.get((namespace, key)))
            self._d[(namespace, key)] = json.loads(json.dumps(new))
            return new


class FileMemoryBackend:
    """``<dir>/<namespace>.json`` holding ``{key: value}``.  ``update`` is a
    read-merge-write with an atomic replace, so two games of one run that
    save at nearly the same time keep each other's entries."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self._lock = threading.Lock()

    def _path(self, namespace: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in namespace)
        return os.path.join(self.directory, safe + ".json")

    def _read(self, namespace: str) -> Dict[str, Any]:
        p = self._path(namespace)
        if not os.path.exists(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, namespace: str, data: Dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        p = self._path(namespace)
        tmp = "%s.%d.%d.tmp" % (p, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, p)

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        return self._read(namespace).get(key, default)

    def put(self, namespace: str, key: str, value: Any) -> None:
        with self._lock:
            data = self._read(namespace)
            data[key] = value
            self._write(namespace, data)

    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            data = self._read(namespace)
            data[key] = fn(data.get(key))
            self._write(namespace, data)
            return data[key]


__all__ = ["Action", "Obs", "Environment", "DomainHooks", "LLM", "ChatReply", "LLMFailure", "ModelMismatch",
           "MemoryBackend", "InMemoryBackend", "FileMemoryBackend"]
