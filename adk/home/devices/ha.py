"""A Home Assistant client: its MCP server first, its REST API as the fallback.

Endpoints (Home Assistant core, ``homeassistant/components/mcp_server`` and the REST
API), all with ``Authorization: Bearer <long-lived access token>``:

* ``POST /api/mcp`` -- Streamable HTTP, STATELESS: one JSON-RPC request per POST,
  ``Accept: application/json, text/event-stream``, answered with one JSON body.
  ``tools/list`` names the Assist tools (``HassTurnOn`` ... or, on newer cores that
  merge LLM APIs, ``homeassistant__HassTurnOn``); ``tools/call`` runs one. Intents
  target by NAME (plus ``domain`` / ``device_class`` slots), and only entities exposed
  to Assist exist for it. ``GetLiveContext`` returns a text overview of those.
* ``GET /api/states`` -- every entity with its id, state and attributes.
* ``POST /api/services/<domain>/<service>`` -- a service call by exact ``entity_id``.

Which path runs:

* state reads use REST (ids and ``device_class`` are what the policy needs) and fall
  back to MCP ``GetLiveContext`` when REST is refused or down;
* an action uses an MCP intent when one maps, the target's friendly name is unique in
  its domain and the action is not ``guarded``; otherwise (and when MCP fails) the REST
  service call with the exact ``entity_id``. Guarded actions are always REST: a lock or
  a garage door is never chosen by a name match.

The token is read through a callable, by NAME, at call time (never stored here).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple, Union

logger = logging.getLogger("adk.home.devices.ha")

TokenProvider = Callable[[], Union[str, Awaitable[str]]]
MCP_PATH = "/api/mcp"
STATES_PATH = "/api/states"
SERVICES_PATH = "/api/services"
LIVE_CONTEXT = "GetLiveContext"
TIMEOUT_S = 10.0

#: (domain, action) -> (intent, extra slots built from params). Only these go over MCP.
INTENTS: Dict[Tuple[str, str], str] = {
    ("light", "turn_on"): "HassTurnOn", ("light", "turn_off"): "HassTurnOff",
    ("light", "toggle"): "HassToggle", ("fan", "turn_on"): "HassTurnOn",
    ("fan", "turn_off"): "HassTurnOff", ("fan", "toggle"): "HassToggle",
    ("switch", "turn_on"): "HassTurnOn", ("switch", "turn_off"): "HassTurnOff",
    ("scene", "turn_on"): "HassTurnOn", ("script", "turn_on"): "HassTurnOn",
    ("cover", "open_cover"): "HassTurnOn", ("cover", "close_cover"): "HassTurnOff",
    ("cover", "set_cover_position"): "HassSetPosition",
    ("climate", "set_temperature"): "HassClimateSetTemperature",
    ("media_player", "media_pause"): "HassMediaPause",
    ("media_player", "media_play"): "HassMediaUnpause",
    ("media_player", "media_next_track"): "HassMediaNext",
    ("media_player", "volume_set"): "HassSetVolume",
    ("vacuum", "start"): "HassVacuumStart",
    ("vacuum", "return_to_base"): "HassVacuumReturnToBase",
}
#: Attributes a status read passes on. Free text a stranger could write (a song title,
#: a camera caption) is left out: device state is the household's, not the internet's.
STATUS_ATTRS = ("friendly_name", "device_class", "brightness", "color_temp_kelvin",
                "current_temperature", "temperature", "target_temp_high",
                "target_temp_low", "hvac_mode", "hvac_action", "current_position",
                "volume_level", "is_volume_muted", "unit_of_measurement", "battery_level",
                "percentage")


class HAError(RuntimeError):
    """Home Assistant could not be reached, refused, or said the action failed."""


def _intent_slots(intent: str, name: str, domain: str, device_class: Optional[str],
                  params: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    slots: Dict[str, Any] = {"name": name, "domain": [domain]}
    if device_class:
        slots["device_class"] = [device_class]
    if intent == "HassLightSet" or (domain == "light" and intent == "HassTurnOn" and params):
        return None                      # brightness/colour: the REST call says it exactly
    if intent == "HassSetPosition":
        if "position" not in params:
            return None
        slots["position"] = int(params["position"])
    elif intent == "HassClimateSetTemperature":
        if set(params) != {"temperature"}:
            return None
        slots = {"name": name, "temperature": params["temperature"]}
    elif intent == "HassSetVolume":
        if "volume_level" not in params:
            return None
        slots["volume_level"] = int(round(float(params["volume_level"]) * 100))
    elif params:
        return None                      # an intent that cannot carry the params
    return slots


class HAClient:
    """``await client.states()``, ``await client.call(...)``; see the module docstring."""

    def __init__(self, base_url: str, token: TokenProvider, *, timeout: float = TIMEOUT_S,
                 transport: Any = None, use_mcp: bool = True):
        self.base = str(base_url or "").rstrip("/")
        self._token = token
        self.timeout = timeout
        self._transport = transport
        self.use_mcp = use_mcp
        self._mcp_tools: Optional[Dict[str, str]] = None   # bare name -> served name
        self.last_path = ""                               # "mcp" | "rest" (for receipts)

    @property
    def configured(self) -> bool:
        return bool(self.base)

    async def _bearer(self) -> str:
        tok = self._token()
        if hasattr(tok, "__await__"):
            tok = await tok          # type: ignore[misc]
        tok = str(tok or "").strip()
        if not tok:
            raise HAError("no Home Assistant token is configured")
        return tok

    def _client(self):
        import httpx

        kw: Dict[str, Any] = {"timeout": self.timeout, "follow_redirects": False}
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.AsyncClient(**kw)

    async def _rest(self, method: str, path: str, body: Any = None) -> Any:
        import httpx

        if not self.base:
            raise HAError("no Home Assistant URL is configured")
        headers = {"Authorization": f"Bearer {await self._bearer()}"}
        try:
            async with self._client() as c:
                r = await c.request(method, self.base + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant unreachable ({type(exc).__name__})") from exc
        if r.status_code in (401, 403):
            raise HAError("Home Assistant refused the token")
        if r.status_code >= 400:
            raise HAError(f"Home Assistant answered {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise HAError("Home Assistant sent an unreadable answer") from exc

    async def _rpc(self, method: str, params: Dict[str, Any]) -> Any:
        import httpx

        if not self.base:
            raise HAError("no Home Assistant URL is configured")
        headers = {"Authorization": f"Bearer {await self._bearer()}",
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with self._client() as c:
                r = await c.post(self.base + MCP_PATH, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant MCP unreachable ({type(exc).__name__})") from exc
        if r.status_code >= 400:
            raise HAError(f"Home Assistant MCP answered {r.status_code}")
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            text = datas[-1] if datas else ""
        try:
            msg = json.loads(text)
        except ValueError as exc:
            raise HAError("Home Assistant MCP sent an unreadable answer") from exc
        if not isinstance(msg, dict) or msg.get("error"):
            err = (msg or {}).get("error") if isinstance(msg, dict) else None
            raise HAError(f"Home Assistant MCP error: {(err or {}).get('message', err)}")
        return msg.get("result")

    async def mcp_tools(self) -> Dict[str, str]:
        """Bare tool name (``HassTurnOn``) -> the name the server serves it under."""
        if self._mcp_tools is None:
            res = await self._rpc("tools/list", {})
            names = [str(t.get("name")) for t in (res or {}).get("tools") or []
                     if isinstance(t, dict)]
            self._mcp_tools = {n.split("__")[-1]: n for n in names}
        return self._mcp_tools

    async def mcp_call(self, bare: str, arguments: Dict[str, Any]) -> Any:
        tools = await self.mcp_tools()
        name = tools.get(bare)
        if not name:
            raise HAError(f"Home Assistant MCP has no {bare} tool")
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = (res or {}).get("content") or []
        text = next((c.get("text") for c in content if isinstance(c, dict)
                     and c.get("type") == "text"), "")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"text": text}
        if (res or {}).get("isError"):
            raise HAError(f"Home Assistant: {str(data.get('error') or text)[:200]}")
        return data

    # ── reads ────────────────────────────────────────────────────────────────
    async def states(self) -> List[Dict[str, Any]]:
        """Every entity as ``{"entity_id", "state", "name", "device_class", "attributes"}``.

        REST first (exact ids); with REST refused or down, the MCP live context (names
        only: ids are synthesised as ``<domain>.<slug>``, marked ``"via": "mcp"``)."""
        try:
            rows = await self._rest("GET", STATES_PATH)
            self.last_path = "rest"
            return [self._row(r) for r in rows if isinstance(r, dict) and r.get("entity_id")]
        except HAError as rest_err:
            if not self.use_mcp:
                raise
            try:
                data = await self.mcp_call(LIVE_CONTEXT, {})
            except HAError:
                raise rest_err
            self.last_path = "mcp"
            return parse_live_context(str(data.get("result") or data.get("text") or ""))

    @staticmethod
    def _row(r: Mapping[str, Any]) -> Dict[str, Any]:
        attrs = r.get("attributes") or {}
        return {"entity_id": str(r["entity_id"]), "state": str(r.get("state", "")),
                "name": str(attrs.get("friendly_name") or r["entity_id"]),
                "device_class": attrs.get("device_class"),
                "attributes": {k: attrs[k] for k in STATUS_ATTRS if k in attrs},
                "via": "rest"}

    # ── actions ──────────────────────────────────────────────────────────────
    async def call(self, action: Mapping[str, Any], *, entity: Mapping[str, Any],
                   cls: str, unique_name: bool) -> Dict[str, Any]:
        """Run one checked action (``policy.normalize_action`` shape). Returns
        ``{"ok": True, "via": "mcp"|"rest", ...}`` or raises :class:`HAError`."""
        dom, act, params = action["domain"], action["action"], dict(action.get("params") or {})
        intent = INTENTS.get((dom, act))
        mcp_err = ""
        if self.use_mcp and intent and cls != "guarded" and unique_name:
            slots = _intent_slots(intent, str(entity.get("name") or ""), dom,
                                  entity.get("device_class"), params)
            if slots is not None and slots.get("name"):
                try:
                    data = await self.mcp_call(intent, slots)
                    failed = ((data.get("data") or {}).get("failed") or []) \
                        if isinstance(data, dict) else []
                    if isinstance(data, dict) and data.get("response_type") == "error":
                        raise HAError(str((data.get("speech") or {}).get("plain", {})
                                          .get("speech") or "the intent failed")[:200])
                    if failed:
                        raise HAError("Home Assistant could not do that for "
                                      f"{len(failed)} device(s)")
                    self.last_path = "mcp"
                    return {"ok": True, "via": "mcp", "intent": intent}
                except HAError as exc:
                    mcp_err = str(exc)
                    logger.info("HA MCP %s failed (%s); using REST", intent, exc)
        body = {"entity_id": action["target"], **params}
        out = await self._rest("POST", f"{SERVICES_PATH}/{dom}/{act}", body)
        self.last_path = "rest"
        changed = [s.get("entity_id") for s in out if isinstance(s, dict)] \
            if isinstance(out, list) else []
        res: Dict[str, Any] = {"ok": True, "via": "rest", "changed": changed[:10]}
        if mcp_err:
            res["mcp_fallback"] = mcp_err[:160]
        return res


_LC_NAME = re.compile(r"^- names: (.+)$")
_LC_KEY = re.compile(r"^\s+(domain|state|areas): (.*)$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:120] or "unnamed"


def parse_live_context(text: str) -> List[Dict[str, Any]]:
    """HA's ``GetLiveContext`` text -> rows (best effort: names, domain, state, area)."""
    rows: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        m = _LC_NAME.match(line)
        if m:
            name = m.group(1).split(",")[0].strip().strip("'\"")
            cur = {"name": name, "state": "", "device_class": None, "attributes": {},
                   "via": "mcp", "domain": ""}
            rows.append(cur)
            continue
        k = _LC_KEY.match(line)
        if k and cur is not None:
            val = k.group(2).strip().strip("'\"")
            if k.group(1) == "areas":
                cur["area"] = val
            else:
                cur[k.group(1)] = val
    out = []
    for r in rows:
        if r["domain"]:
            r["entity_id"] = f"{r.pop('domain')}.{_slug(r['name'])}"
            out.append(r)
    return out
