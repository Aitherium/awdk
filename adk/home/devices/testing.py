"""A fake Home Assistant on a real local port, for tests and dry runs (no live HA).

Speaks what :mod:`.ha` uses: ``GET /api/states``, ``POST /api/services/<d>/<s>`` and
the stateless Streamable HTTP MCP endpoint ``POST /api/mcp`` (``tools/list``,
``tools/call``) with bearer auth. Every call is recorded in ``calls``.

    with FakeHomeAssistant(states=[...]) as ha:
        client = HAClient(ha.url, lambda: ha.token)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

INTENT_NAMES = ("HassTurnOn", "HassTurnOff", "HassToggle", "HassSetPosition",
                "HassClimateSetTemperature", "HassMediaPause", "HassMediaUnpause",
                "HassMediaNext", "HassSetVolume", "HassVacuumStart",
                "HassVacuumReturnToBase", "GetLiveContext")


def state(entity_id: str, st: str = "off", name: str = "", device_class: Optional[str] = None,
          **attrs: Any) -> Dict[str, Any]:
    a = {"friendly_name": name or entity_id.split(".", 1)[1].replace("_", " ").title(), **attrs}
    if device_class:
        a["device_class"] = device_class
    return {"entity_id": entity_id, "state": st, "attributes": a}


class FakeHomeAssistant:
    def __init__(self, states: List[Dict[str, Any]], token: str = "test-token",
                 namespace: str = "homeassistant", mcp: bool = True, rest: bool = True):
        self.states = states
        self.token = token
        self.namespace = namespace
        self.mcp = mcp
        self.rest = rest
        self.calls: List[Dict[str, Any]] = []
        self.fail_intents = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _json(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    self._json(401, {"message": "Unauthorized"})
                    return False
                return True

            def _body(self) -> Any:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return
                if self.path == "/api/states" and fake.rest:
                    fake.calls.append({"path": "rest", "method": "GET", "url": self.path})
                    return self._json(200, fake.states)
                self._json(404, {"message": "not found"})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                body = self._body()
                if self.path.startswith("/api/services/") and fake.rest:
                    _, _, _, dom, svc = self.path.split("/", 4)
                    fake.calls.append({"path": "rest", "domain": dom, "service": svc,
                                       "body": body})
                    eid = body.get("entity_id")
                    changed = [s for s in fake.states if s["entity_id"] == eid]
                    return self._json(200, changed)
                if self.path == "/api/mcp" and fake.mcp:
                    if "application/json" not in self.headers.get("Accept", ""):
                        return self._json(400, {"message": "must accept json"})
                    return self._json(200, fake._rpc(body))
                self._json(404, {"message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _served(self, bare: str) -> str:
        return f"{self.namespace}__{bare}" if self.namespace else bare

    def _rpc(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": self._served(n), "inputSchema": {"type": "object"}}
                for n in INTENT_NAMES]}}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            self.calls.append({"path": "mcp", "tool": name, "arguments": args})
            if str(name).endswith("GetLiveContext"):
                lines = ["Live Context: An overview of the areas and the devices in this "
                         "smart home:"]
                for s in self.states:
                    lines += [f"- names: {s['attributes'].get('friendly_name')}",
                              f"  domain: {s['entity_id'].split('.')[0]}",
                              f"  state: '{s['state']}'"]
                data = {"success": True, "result": "\n".join(lines)}
            elif self.fail_intents:
                data = {"response_type": "error", "speech": {"plain": {"speech": "no"}}}
            else:
                data = {"response_type": "action_done", "data": {"success": [
                    {"name": args.get("name"), "type": "entity"}], "failed": []}}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(data)}], "isError": False}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601,
                                                        "message": "Method not found"}}

    def __enter__(self) -> "FakeHomeAssistant":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()


def household_states() -> List[Dict[str, Any]]:
    """A small home with one device of every class."""
    return [
        state("light.kitchen", "on", "Kitchen"),
        state("light.ada_room", "off", "Ada's Room"),
        state("scene.bedtime", "scening", "Bedtime"),
        state("fan.office", "off", "Office Fan"),
        state("media_player.living_room", "idle", "Living Room Speaker"),
        state("climate.hall", "heat", "Hall", temperature=20, current_temperature=19),
        state("cover.blinds", "closed", "Blinds", device_class="blind"),
        state("cover.garage", "closed", "Garage Door", device_class="garage"),
        state("vacuum.roomba", "docked", "Roomba"),
        state("lock.front_door", "locked", "Front Door"),
        state("alarm_control_panel.home", "armed_away", "Alarm"),
        state("switch.oven", "off", "Oven"),
        state("camera.porch", "idle", "Porch Camera"),
    ]
