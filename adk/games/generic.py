"""``aither-game/1`` -- the smallest protocol a game needs for Agent Home agents.

A game server exposes five JSON routes under one base URL::

    POST {base}/join   {"name": str}          -> {"session": str, "room": str, ...state}
    GET  {base}/state                          -> state
    POST {base}/act    {"action": str}        -> state (+ "reward", "done")
    POST {base}/chat   {"text": str}          -> {"ok": true}
    POST {base}/leave                          -> {"ok": true}

``state`` is ``{"text", "state": {...}, "actions": [...], "reward", "done",
"messages": [...], "signature"?}``. Every call after join carries the session in
``X-Game-Session``; a bearer token (``Authorization: Bearer``) is sent when the
join URL carried one. ``signature`` is optional: without it the client hashes
the canonical JSON of ``state`` (so a game should keep free text OUT of
``state`` and in ``text``).

:mod:`adk.games.fake_server` implements this protocol and is the reference.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ._http import make_client, request_json
from .base import (
    GameClient,
    GameError,
    Observation,
    StepResult,
    register_game_client,
)

PROTOCOL = "aither-game/1"


def _strip_scheme_prefix(url: str) -> str:
    if url.startswith("aither-game+"):
        return url[len("aither-game+"):]
    return url


def observation_from(payload: Dict[str, Any]) -> Observation:
    state = payload.get("state") or {}
    signature = payload.get("signature")
    if not signature:
        signature = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return Observation(
        text=str(payload.get("text") or ""),
        signature=str(signature),
        actions=[str(a) for a in (payload.get("actions") or [])],
        state=state if isinstance(state, dict) else {"value": state},
        reward=float(payload.get("reward") or 0.0),
        done=bool(payload.get("done")),
        messages=list(payload.get("messages") or []),
    )


class GenericGameClient(GameClient):
    kind = "aither-game"

    def __init__(self, url: str, token: Optional[str] = None, name: str = "agent",
                 transport: Optional[httpx.BaseTransport] = None,
                 timeout: float = 30.0) -> None:
        super().__init__(_strip_scheme_prefix(url), token=token, name=name)
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https"):
            raise GameError(f"aither-game URL must be http(s), got {url!r}")
        self.base = urlunsplit((parts.scheme, parts.netloc,
                                parts.path.rstrip("/"), "", ""))
        headers = {"content-type": "application/json",
                   "x-game-protocol": PROTOCOL}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._http = make_client(self.base, headers, transport=transport,
                                 timeout=timeout)
        self.session = ""
        self._last: Optional[Observation] = None

    def _headers(self) -> Dict[str, str]:
        return {"x-game-session": self.session} if self.session else {}

    def join(self) -> Observation:
        data = request_json(self._http, "POST", "/join", what="join",
                            json={"name": self.name})
        self.session = str(data.get("session") or "")
        if not self.session:
            raise GameError("join: the game answered without a session id")
        self.room = str(data.get("room") or "default")
        self.joined = True
        self._last = observation_from(data)
        return self._last

    def _require_joined(self) -> None:
        if not self.joined:
            raise GameError("not joined -- call join() first")

    def observe(self) -> Observation:
        self._require_joined()
        data = request_json(self._http, "GET", "/state", what="observe",
                            headers=self._headers())
        self._last = observation_from(data)
        return self._last

    def act(self, action: str) -> StepResult:
        self._require_joined()
        data = request_json(self._http, "POST", "/act", what="act",
                            json={"action": str(action)}, headers=self._headers())
        obs = observation_from(data)
        self._last = obs
        return StepResult(observation=obs, reward=obs.reward, done=obs.done,
                          info={"action": str(action)})

    def chat(self, text: str) -> Dict[str, Any]:
        self._require_joined()
        return request_json(self._http, "POST", "/chat", what="chat",
                            json={"text": str(text)}, headers=self._headers())

    def leave(self) -> None:
        if self.joined:
            try:
                request_json(self._http, "POST", "/leave", what="leave",
                             headers=self._headers())
            except GameError:
                pass
        self.joined = False

    def close(self) -> None:
        self.leave()
        self._http.close()


def _matches(url: str) -> bool:
    if url.startswith("aither-game+"):
        return True
    return urlsplit(url).scheme in ("http", "https")


register_game_client("aither-game", _matches, GenericGameClient)
