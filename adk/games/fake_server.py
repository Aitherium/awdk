"""Reference game servers for local testing -- no network, no model, deterministic.

* :class:`TreasureLineGame` implements ``aither-game/1`` (see
  :mod:`adk.games.generic`). It is also the protocol's reference: a game author
  can read this file and have a working server in an afternoon.
* :class:`FakeSaga` answers the subset of Saga's play API the Saga client uses,
  with a tiny scripted world, so the Saga adapter is testable without Saga.

Both are plain request handlers. Use them in-process through
``httpx.MockTransport(server.handle_httpx)``, or on a real socket with
:func:`serve_in_thread` (``python -m adk.games.fake_server --port 8799``).
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

import httpx

Response = Tuple[int, Dict[str, Any]]


class _Base:
    """Dispatch (method, path, headers, body) -> (status, json)."""

    prefix = ""

    def handle(self, method: str, path: str, headers: Dict[str, str],
               body: Optional[Dict[str, Any]], query: Dict[str, str]) -> Response:
        raise NotImplementedError

    def handle_httpx(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                return httpx.Response(400, json={"detail": "bad json"})
        headers = {k.lower(): v for k, v in request.headers.items()}
        query = dict(request.url.params)
        status, payload = self.handle(request.method, request.url.path, headers,
                                      body, query)
        return httpx.Response(status, json=payload)


# ── aither-game/1 reference: a one-dimensional treasure hunt ─────────────────

class TreasureLineGame(_Base):
    """Walk a line of ``length`` cells; the treasure is buried at the far end.

    Actions: ``left``, ``right``, ``dig``, ``wait``. Every step costs 0.01;
    digging on the treasure pays 1.0 and ends the episode; digging anywhere else
    costs 0.2. Deterministic, so an agent that remembers across sessions walks
    straight there -- which is what the learning tests assert.
    """

    ACTIONS = ["left", "right", "dig", "wait"]

    def __init__(self, length: int = 5, room: str = "treasure-line",
                 require_token: Optional[str] = None) -> None:
        self.length = length
        self.room = room
        self.require_token = require_token
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.chat_log: List[Dict[str, str]] = []

    def _state(self, sess: Dict[str, Any], reward: float = 0.0) -> Dict[str, Any]:
        pos = sess["pos"]
        return {
            "text": f"You stand on cell {pos} of {self.length}. The ground is "
                    f"{'freshly turned' if sess['dug'] else 'undisturbed'}.",
            "state": {"pos": pos},
            "signature": f"pos={pos}",
            "actions": [] if sess["done"] else list(self.ACTIONS),
            "reward": reward,
            "done": sess["done"],
            "messages": self.chat_log[-5:],
            "session": sess["id"],
            "room": self.room,
        }

    def handle(self, method: str, path: str, headers: Dict[str, str],
               body: Optional[Dict[str, Any]], query: Dict[str, str]) -> Response:
        if self.require_token and headers.get("authorization") != \
                f"Bearer {self.require_token}":
            return 401, {"detail": "bad or missing token"}
        route = path.rstrip("/").rsplit("/", 1)[-1]
        if method == "POST" and route == "join":
            sid = uuid.uuid4().hex[:12]
            sess = {"id": sid, "name": (body or {}).get("name", "agent"),
                    "pos": 0, "done": False, "dug": False}
            self.sessions[sid] = sess
            return 200, self._state(sess)
        sess = self.sessions.get(headers.get("x-game-session", ""))
        if sess is None:
            return 404, {"detail": "unknown session; join first"}
        if method == "GET" and route == "state":
            return 200, self._state(sess)
        if method == "POST" and route == "act":
            if sess["done"]:
                return 409, {"detail": "episode is over"}
            action = str((body or {}).get("action", ""))
            if action not in self.ACTIONS:
                return 400, {"detail": f"unknown action {action!r}"}
            reward = -0.01
            if action == "left":
                sess["pos"] = max(0, sess["pos"] - 1)
            elif action == "right":
                sess["pos"] = min(self.length - 1, sess["pos"] + 1)
            elif action == "dig":
                sess["dug"] = True
                if sess["pos"] == self.length - 1:
                    reward += 1.0
                    sess["done"] = True
                else:
                    reward -= 0.2
            return 200, self._state(sess, reward)
        if method == "POST" and route == "chat":
            self.chat_log.append({"from": sess["name"],
                                  "text": str((body or {}).get("text", ""))})
            return 200, {"ok": True}
        if method == "POST" and route == "leave":
            self.sessions.pop(sess["id"], None)
            return 200, {"ok": True}
        return 404, {"detail": f"no route {method} {path}"}


# ── a scripted Saga ──────────────────────────────────────────────────────────

class FakeSaga(_Base):
    """The Saga play routes the SagaRoomClient uses, over a scripted world.

    ``has_model=False`` makes ``POST /turn`` without ``prose`` answer 503, the
    way a real Saga with no SAGA_LLM_BASE_URL does.
    """

    prefix = "/api/local/saga"
    NODES = {
        "mira": {"id": "n-mira", "name": "Mira", "type": "character"},
        "market": {"id": "n-market", "name": "Market", "type": "location"},
        "gate": {"id": "n-gate", "name": "Old Gate", "type": "location"},
        "lantern": {"id": "n-lantern", "name": "Lantern", "type": "item"},
    }

    def __init__(self, worlds: Tuple[str, ...] = ("elysium", "cyber-feudalism"),
                 has_model: bool = True) -> None:
        self.worlds = list(worlds)
        self.has_model = has_model
        self.turns: Dict[str, List[Dict[str, Any]]] = {w: [] for w in worlds}
        self.seeded: Dict[str, bool] = {w: False for w in worlds}
        self.location: Dict[str, str] = {w: "Old Gate" for w in worlds}
        self.issues: Dict[str, List[str]] = {w: [] for w in worlds}
        self.auth_seen: List[str] = []

    def handle(self, method: str, path: str, headers: Dict[str, str],
               body: Optional[Dict[str, Any]], query: Dict[str, str]) -> Response:
        if not path.startswith(self.prefix):
            return 404, {"detail": "not a saga route"}
        route = path[len(self.prefix):].rstrip("/") or "/"
        if headers.get("authorization"):
            self.auth_seen.append(headers["authorization"])
        if method == "GET" and route == "/status":
            return 200, {"engine": "saga", "prefix": self.prefix,
                         "default_world": self.worlds[0]}
        if method == "GET" and route == "/worlds":
            return 200, {"default": self.worlds[0],
                         "worlds": [{"id": w, "name": w.title()} for w in self.worlds]}
        world = headers.get("x-saga-world") or query.get("world") or self.worlds[0]
        if world not in self.turns:
            return 404, {"detail": "world not found"}
        if method == "POST" and route.startswith("/worlds/") and route.endswith("/seed"):
            self.seeded[world] = True
            return 200, {"seeded": True, "world_id": world}
        if method == "GET" and route == "/turns":
            limit = int(query.get("limit", 20))
            return 200, {"turns": self.turns[world][-limit:]}
        if method == "GET" and route == "/continuity":
            return 200, {"issues": list(self.issues[world])}
        if method == "POST" and route == "/turn":
            message = str((body or {}).get("message", ""))
            prose = (body or {}).get("prose")
            if not prose and not self.has_model:
                return 503, {"detail": {"error": "no model is configured for Saga"}}
            low = message.lower()
            activated = [n for key, n in self.NODES.items() if key in low]
            for n in activated:
                if n["type"] == "location" and low.startswith("go to"):
                    self.location[world] = n["name"]
            if "dragon" in low:  # the scripted world has no dragons
                self.issues[world].append(f"turn mentions a dragon: {message}")
            if not activated:
                activated = [self.NODES["gate"] if self.location[world] == "Old Gate"
                             else self.NODES["market"]]
            if self.location[world] == "Market" and self.NODES["mira"] not in activated:
                activated.append(self.NODES["mira"])
            text = prose or f"[{self.location[world]}] You {message}. The wind answers."
            n = len(self.turns[world]) + 1
            turn = {"turn_id": f"t{n}", "turn_number": n, "message": message,
                    "prose": text, "text": text}
            self.turns[world].append(turn)
            return 200, {
                **turn,
                "prose_source": "client" if prose else "upstream",
                "context": {"activated": [
                    {**a, "reason": "keyword", "relevance": 1.0} for a in activated]},
                "world_state": {"story_name": world, "location": self.location[world],
                                "mood": "calm", "time_of_day": "dusk"},
                "continuity": list(self.issues[world]),
            }
        return 404, {"detail": f"no route {method} {route}"}


# ── real socket ──────────────────────────────────────────────────────────────

def _make_handler(server: _Base) -> type:
    class Handler(BaseHTTPRequestHandler):
        def _go(self, method: str) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = None
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    body = None
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = server.handle(method, parts.path, headers, body,
                                            dict(parse_qsl(parts.query)))
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 -- http.server API
            self._go("GET")

        def do_POST(self) -> None:  # noqa: N802 -- http.server API
            self._go("POST")

        def log_message(self, *args: Any) -> None:
            return

    return Handler


def serve_in_thread(server: _Base, host: str = "127.0.0.1",
                    port: int = 0) -> Tuple[ThreadingHTTPServer, str]:
    """Start ``server`` on a real socket in a daemon thread -> (httpd, base_url)."""
    httpd = ThreadingHTTPServer((host, port), _make_handler(server))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m adk.games.fake_server")
    p.add_argument("--game", choices=["treasure", "saga"], default="treasure")
    p.add_argument("--port", type=int, default=8799)
    args = p.parse_args(argv)
    srv: _Base = TreasureLineGame() if args.game == "treasure" else FakeSaga()
    httpd, base = serve_in_thread(srv, port=args.port)
    print(f"{args.game} fake game server on {base}"
          + ("/api/local/saga" if args.game == "saga" else ""))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
