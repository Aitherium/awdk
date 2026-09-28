"""The game-client contract every Agent Home game adapter speaks.

A game is anything an agent can JOIN (by URL, optionally with a token), then
OBSERVE, ACT in, and CHAT in. Two implementations ship:

* :class:`adk.games.generic.GenericGameClient` -- the ``aither-game/1`` JSON
  protocol (join/state/act/chat/leave). A game author implements five routes and
  every Agent Home agent can play it. :mod:`adk.games.fake_server` is the
  reference server.
* :class:`adk.games.saga.SagaRoomClient` -- a Saga story room, over Saga's own
  play API (``/api/local/saga/*``).

Observations carry a ``signature``: a short, hashable abstraction of the state.
The learning loop (:mod:`adk.games.learning`) and the world-model pack key
transitions on it, so it must be STABLE for the same situation and must not
include free text that never repeats (a narrative paragraph, a timestamp).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "GameError",
    "Observation",
    "StepResult",
    "GameClient",
    "register_game_client",
    "open_game",
    "split_token",
    "known_game_kinds",
]


class GameError(RuntimeError):
    """A game could not be joined, observed or acted in. The message says why."""


@dataclass
class Observation:
    """What the agent sees at one moment."""

    text: str = ""
    signature: str = ""
    actions: List[str] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    reward: float = 0.0
    done: bool = False
    messages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "signature": self.signature,
            "actions": list(self.actions),
            "state": dict(self.state),
            "reward": self.reward,
            "done": self.done,
            "messages": list(self.messages),
        }


@dataclass
class StepResult:
    """The outcome of one action."""

    observation: Observation
    reward: float = 0.0
    done: bool = False
    info: Dict[str, Any] = field(default_factory=dict)


class GameClient(ABC):
    """One agent's seat in one game room."""

    #: Short protocol name, e.g. "saga" or "aither-game".
    kind: str = "game"

    def __init__(self, url: str, token: Optional[str] = None,
                 name: str = "agent") -> None:
        self.url = url
        self.token = token
        self.name = name
        self.joined = False
        self.room = ""

    @property
    def domain(self) -> str:
        """World-model domain tag: one per game kind + room."""
        return f"game:{self.kind}:{self.room or 'default'}"

    @abstractmethod
    def join(self) -> Observation:
        """Enter the room. Raises :class:`GameError` when the room refuses."""

    @abstractmethod
    def observe(self) -> Observation:
        """The current situation."""

    def actions(self) -> List[str]:
        """Legal actions right now (defaults to the observation's list)."""
        return list(self.observe().actions)

    @abstractmethod
    def act(self, action: str) -> StepResult:
        """Take one action."""

    @abstractmethod
    def chat(self, text: str) -> Dict[str, Any]:
        """Say something in the room."""

    def leave(self) -> None:
        self.joined = False

    def close(self) -> None:
        self.leave()

    def __enter__(self) -> "GameClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 -- leaving must never mask the real error
            pass


# ── URL handling ──────────────────────────────────────────────────────────────

_TOKEN_KEYS = ("token", "access_token", "invite")


def split_token(url: str) -> Tuple[str, Optional[str]]:
    """Strip a token out of a game URL so it is never logged or persisted.

    Tokens may ride as ``?token=``/``?access_token=``/``?invite=`` or as a
    ``#token=`` fragment. Returns ``(url_without_token, token_or_None)``.
    """
    parts = urlsplit(url)
    token: Optional[str] = None
    query = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k in _TOKEN_KEYS and v:
            token = v
        else:
            query.append((k, v))
    fragment = parts.fragment
    if fragment:
        frag_pairs = parse_qsl(fragment, keep_blank_values=True)
        rest = []
        for k, v in frag_pairs:
            if k in _TOKEN_KEYS and v:
                token = token or v
            else:
                rest.append((k, v))
        fragment = urlencode(rest) if frag_pairs else fragment
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path,
                        urlencode(query), fragment))
    return clean, token


Matcher = Callable[[str], bool]
Factory = Callable[..., GameClient]
_REGISTRY: List[Tuple[str, Matcher, Factory]] = []


def register_game_client(kind: str, matcher: Matcher, factory: Factory) -> None:
    """Teach :func:`open_game` a new game kind. Later registrations win."""
    _REGISTRY.insert(0, (kind, matcher, factory))


def known_game_kinds() -> List[str]:
    _ensure_builtins()
    return [k for k, _m, _f in _REGISTRY]


_BUILTINS_LOADED = False


def _ensure_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from . import generic, saga  # noqa: F401 -- import registers them


def open_game(url: str, token: Optional[str] = None, name: str = "agent",
              kind: Optional[str] = None, **kwargs: Any) -> GameClient:
    """Build the right client for ``url``. Does not join.

    ``kind`` forces a client ("saga", "aither-game"); otherwise the URL decides:
    ``saga+https://…`` or a path containing ``/saga`` is a Saga room;
    ``aither-game+https://…`` or anything else is the generic protocol.
    The token comes from ``token=``, the URL, or ``AITHER_GAME_TOKEN``.
    """
    _ensure_builtins()
    clean, url_token = split_token(url)
    tok = token or url_token or os.environ.get("AITHER_GAME_TOKEN") or None
    if kind:
        for k, _m, factory in _REGISTRY:
            if k == kind:
                return factory(clean, token=tok, name=name, **kwargs)
        raise GameError(f"unknown game kind {kind!r}; known: {known_game_kinds()}")
    for _k, matcher, factory in _REGISTRY:
        if matcher(clean):
            return factory(clean, token=tok, name=name, **kwargs)
    raise GameError(f"no game client understands {clean!r}")
