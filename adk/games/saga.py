"""SagaRoomClient -- an Agent Home agent playing in a Saga story room.

Speaks Saga's play API exactly as ``.PRODUCTS/.SAGA/saga_engine/play_routes.py``
serves it (prefix ``/api/local/saga``):

    GET  /status                  engine check ({"engine": "saga", ...})
    GET  /worlds                  {"default", "worlds": [{"id", ...}]}
    POST /worlds/{id}/seed        seed an empty story
    POST /turn   {"message", "prose"?}   advance the story one turn
    GET  /turns?limit=N           recent turns
    GET  /continuity              continuity issues

The room (world) is chosen by the ``X-Saga-World`` header, taken from the URL's
``?world=`` (or the last path segment of ``/play/<world>``). A token in the URL
rides as ``Authorization: Bearer`` and is never stored in config or memory.

URL forms accepted::

    saga+http://127.0.0.1:8793?world=elysium
    http://127.0.0.1:8793/api/local/saga?world=cyber-feudalism
    https://saga.example.com/play/elysium#token=...

Saga has no score, so reward is shaped from what the engine reports: a turn
that advances earns a little, each story node the agent has not seen before in
this session earns more (discovery), and each NEW continuity issue costs
(the agent contradicted the story). The state signature is location + mood +
time of day + the set of activated story nodes -- stable for the same scene,
free of prose.

When the Saga server has no model configured (503), the client can write the
turn's prose itself with ``prose_fn`` (the agent's own model), which is exactly
the ``prose`` field Saga's TurnRequest accepts.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from ._http import make_client, request_json
from .base import (
    GameClient,
    GameError,
    Observation,
    StepResult,
    register_game_client,
)

SAGA_PREFIX = "/api/local/saga"
BASE_ACTIONS = [
    "look around",
    "ask who is here",
    "investigate the nearest clue",
    "move on",
    "rest for a while",
]
MAX_ACTIONS = 8
TURN_REWARD = 0.05
DISCOVERY_REWARD = 0.1
CONTINUITY_PENALTY = 0.3
_WORLD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

ProseFn = Callable[[str, str], str]


def parse_saga_url(url: str) -> Dict[str, str]:
    """-> {"base": scheme://host[:port]<prefix>, "world": id-or-""}."""
    if url.startswith("saga+"):
        url = url[len("saga+"):]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise GameError(f"Saga URL must be http(s) (or saga+http(s)), got {url!r}")
    query = dict(parse_qsl(parts.query))
    frag = dict(parse_qsl(parts.fragment))
    world = query.get("world") or frag.get("world") or ""
    path = parts.path.rstrip("/")
    prefix = SAGA_PREFIX
    m = re.search(r"(/api/[\w/-]*saga)", path)
    if m:
        prefix = m.group(1)
        tail = path[m.end():].strip("/")
    else:
        tail = path.strip("/")
    if not world and tail:
        seg = tail.split("/")[-1]
        if seg != "play" and _WORLD_ID.match(seg):
            world = seg
    if world and not _WORLD_ID.match(world):
        raise GameError(f"invalid Saga world id {world!r}")
    base = urlunsplit((parts.scheme, parts.netloc, prefix, "", ""))
    return {"base": base, "world": world}


class SagaRoomClient(GameClient):
    kind = "saga"

    def __init__(self, url: str, token: Optional[str] = None, name: str = "agent",
                 transport: Optional[httpx.BaseTransport] = None,
                 prose_fn: Optional[ProseFn] = None, seed: bool = True,
                 timeout: float = 120.0) -> None:
        super().__init__(url, token=token, name=name)
        parsed = parse_saga_url(url)
        self.base = parsed["base"]
        self.world = parsed["world"]
        self.prose_fn = prose_fn
        self.seed = seed
        self._transport = transport
        self._timeout = timeout
        self._http = self._make_http()
        self._seen_nodes: Set[str] = set()
        self._continuity_count = 0
        self._last_turn: Dict[str, Any] = {}
        self._last_obs: Optional[Observation] = None

    def _make_http(self) -> httpx.Client:
        headers = {"content-type": "application/json"}
        if self.world:
            headers["x-saga-world"] = self.world
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return make_client(self.base, headers, transport=self._transport,
                           timeout=self._timeout)

    # ── join / observe ────────────────────────────────────────────────────
    def join(self) -> Observation:
        status = request_json(self._http, "GET", "/status", what="join")
        if status.get("engine") != "saga":
            raise GameError(f"join: {self.base} is not a Saga engine "
                            f"(status said engine={status.get('engine')!r})")
        worlds = request_json(self._http, "GET", "/worlds", what="join")
        ids = [w.get("id") for w in worlds.get("worlds", []) if isinstance(w, dict)]
        if not self.world:
            self.world = str(worlds.get("default") or status.get("default_world") or "")
            self._http.close()
            self._http = self._make_http()
        if ids and self.world and self.world not in ids:
            raise GameError(f"join: world {self.world!r} is not on this Saga "
                            f"server; it has {ids}")
        self.room = self.world or "default"
        self.joined = True
        self._seen_nodes = set()  # discovery is per session
        turns = request_json(self._http, "GET", "/turns", what="join",
                             params={"limit": 1}).get("turns", [])
        if not turns and self.seed and self.world:
            request_json(self._http, "POST", f"/worlds/{self.world}/seed",
                         what="seed")
        self._continuity_count = len(self._continuity())
        return self.observe()

    def _continuity(self) -> List[Any]:
        try:
            return list(request_json(self._http, "GET", "/continuity",
                                     what="continuity").get("issues", []))
        except GameError:
            return []

    def observe(self) -> Observation:
        if not self.joined:
            raise GameError("not joined -- call join() first")
        turns = request_json(self._http, "GET", "/turns", what="observe",
                             params={"limit": 1}).get("turns", [])
        text = str(turns[-1].get("text") or "") if turns else ""
        if self._last_turn:
            text = str(self._last_turn.get("text") or text)
        obs = self._build_observation(text, self._last_turn)
        self._last_obs = obs
        return obs

    def _build_observation(self, text: str, turn: Dict[str, Any],
                           reward: float = 0.0) -> Observation:
        ws = turn.get("world_state") or {}
        activated = (turn.get("context") or {}).get("activated") or []
        names = sorted({str(a.get("name") or a.get("id")) for a in activated
                        if isinstance(a, dict)})[:6]
        signature = "|".join([
            str(ws.get("location") or "?"),
            str(ws.get("mood") or "?"),
            str(ws.get("time_of_day") or "?"),
            ",".join(names),
        ])
        return Observation(
            text=text,
            signature=signature,
            actions=self._actions_for(activated),
            state={"world": self.world, "turn_number": turn.get("turn_number", 0),
                   "location": ws.get("location"), "mood": ws.get("mood"),
                   "time_of_day": ws.get("time_of_day"), "present": names},
            reward=reward,
            done=False,
        )

    @staticmethod
    def _actions_for(activated: List[Any]) -> List[str]:
        acts: List[str] = []
        for a in activated:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip()
            kind = str(a.get("type") or "").lower()
            if not name:
                continue
            if kind == "character":
                acts.append(f"talk to {name}")
            elif kind == "location":
                acts.append(f"go to {name}")
            elif kind in ("item", "object", "artifact"):
                acts.append(f"examine {name}")
        out: List[str] = []
        for a in acts + BASE_ACTIONS:
            if a not in out:
                out.append(a)
        return out[:MAX_ACTIONS]

    # ── act / chat ────────────────────────────────────────────────────────
    def _post_turn(self, message: str) -> Dict[str, Any]:
        try:
            return request_json(self._http, "POST", "/turn", what="act",
                                json={"message": message})
        except GameError as exc:
            if getattr(exc, "status_code", None) != 503 or self.prose_fn is None:
                raise
        # The server has no model: our own model writes the continuation.
        last = self._last_obs.text if self._last_obs else ""
        prose = self.prose_fn(last, message)
        if not prose or not prose.strip():
            raise GameError("act: the Saga server has no model and prose_fn "
                            "returned nothing")
        return request_json(self._http, "POST", "/turn", what="act",
                            json={"message": message, "prose": prose})

    def act(self, action: str) -> StepResult:
        if not self.joined:
            raise GameError("not joined -- call join() first")
        turn = self._post_turn(str(action))
        activated = (turn.get("context") or {}).get("activated") or []
        ids = {str(a.get("id") or a.get("name")) for a in activated
               if isinstance(a, dict)}
        new_nodes = ids - self._seen_nodes
        self._seen_nodes |= ids
        cont = turn.get("continuity")
        n_issues = len(cont) if isinstance(cont, list) else self._continuity_count
        new_issues = max(0, n_issues - self._continuity_count)
        self._continuity_count = n_issues
        reward = (TURN_REWARD + DISCOVERY_REWARD * len(new_nodes)
                  - CONTINUITY_PENALTY * new_issues)
        self._last_turn = turn
        obs = self._build_observation(str(turn.get("text") or ""), turn,
                                      reward=reward)
        self._last_obs = obs
        return StepResult(observation=obs, reward=reward, done=False, info={
            "turn_id": turn.get("turn_id"), "turn_number": turn.get("turn_number"),
            "new_nodes": sorted(new_nodes), "new_continuity_issues": new_issues,
            "prose_source": turn.get("prose_source"), "action": str(action)})

    def chat(self, text: str) -> Dict[str, Any]:
        step = self.act(f'I say: "{text}"')
        return {"ok": True, "reply": step.observation.text,
                "turn_id": step.info.get("turn_id")}

    def close(self) -> None:
        self.leave()
        self._http.close()


def _matches(url: str) -> bool:
    if url.startswith("saga+"):
        return True
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and (
        "/saga" in parts.path or parts.netloc.startswith("saga."))


register_game_client("saga", _matches, SagaRoomClient)
