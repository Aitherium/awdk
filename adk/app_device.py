"""This PC as a device of a company app: registered once, then kept beating.

A company app (a tenant appliance) keeps its own device list -- "My Hardware" -- and
reads a device as online only while that device heartbeats
(``POST <api>/api/devices/<id>/heartbeat`` with the device's own secret). Until
2026-10-14 the ONLY thing that sent that beat was a curl the customer had to paste
by hand, so a PC set up through the setup page read "enrolling" forever, and the
Identity node registration (``adk enroll``) does not help a tenant whose plan
Identity refuses (402).

This module needs nothing but the person's ``adk login`` and the app's API URL:

* :func:`ensure_registered` -- the first time, ``POST <api>/api/devices/enroll`` with
  the login bearer (the app binds the device to the AUTHENTICATED caller, never to
  anything this sends) and keep the device id and the once-shown secret in
  ``~/.aither/app_devices.json`` (mode 600). Idempotent: an app already on record is
  not registered again.
* :func:`beat` -- one heartbeat with the device's own secret, so the PC stays live
  when the person's sign-in lapses.
* :class:`AppDeviceBeat` -- the throttled pair, driven by ``adk tenant watch``, which
  the setup page already starts at logon for exactly that app (AitherZero 3267).

Failures are said, never swallowed into "fine": a refused login, an app that cannot
serve (5xx), and a device the owner REMOVED in the app (404 -- not re-registered
behind their back; ``adk tenant watch --reset-device`` registers it again).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("adk.app_device")

ENROLL_PATH = "/api/devices/enroll"
HEARTBEAT_PATH = "/api/devices/{device_id}/heartbeat"

#: Seconds between two beats (the app reads a device online for 5 minutes).
BEAT_INTERVAL_S = 60.0

#: Seconds before a failed registration is tried again.
REGISTER_RETRY_S = 300.0


def default_state_path() -> Path:
    return Path.home() / ".aither" / "app_devices.json"


def _os_name() -> str:
    s = platform.system().lower()
    return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(s, "unknown")


def _machine_name() -> str:
    try:
        return (socket.gethostname() or "").strip()[:80] or "This PC"
    except OSError:
        return "This PC"


def _key(api_url: str) -> str:
    return api_url.strip().rstrip("/").lower()


def load(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or default_state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Write the record (it holds device secrets) readable by this user only."""
    p = path or default_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:  # Windows ACLs ignore the mode; the profile dir is private
        log.debug("chmod 600 on %s failed: %s", tmp, exc)
    os.replace(tmp, p)


def record_for(api_url: str, path: Optional[Path] = None) -> Dict[str, Any]:
    rec = load(path).get(_key(api_url))
    return rec if isinstance(rec, dict) else {}


def forget(api_url: str, path: Optional[Path] = None) -> bool:
    data = load(path)
    if data.pop(_key(api_url), None) is None:
        return False
    _save(data, path)
    return True


def _json(r: Any) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def ensure_registered(client: Any, api_url: str, bearer: str, *,
                      path: Optional[Path] = None,
                      name: Optional[str] = None) -> Dict[str, Any]:
    """This PC's device in the app, registered with ``bearer`` if not on record.

    Returns ``{"ok", "status", "device_id", "detail"}``; status is ``on-record``,
    ``registered``, ``removed`` (the owner removed it in the app; left alone),
    ``auth`` (401/403), ``no-login`` or ``error`` (network, 5xx, a bad answer)."""
    rec = record_for(api_url, path)
    if rec.get("removed"):
        return {"ok": False, "status": "removed", "device_id": rec.get("device_id", ""),
                "detail": "this PC was removed in the app"}
    if rec.get("device_id") and rec.get("token"):
        return {"ok": True, "status": "on-record", "device_id": rec["device_id"], "detail": ""}
    if not bearer:
        return {"ok": False, "status": "no-login", "device_id": "", "detail": "not signed in"}
    try:
        r = client.post(f"{_key(api_url)}{ENROLL_PATH}",
                        headers={"Authorization": f"Bearer {bearer}"},
                        json={"name": name or _machine_name(), "os": _os_name()})
    except Exception as exc:  # noqa: BLE001 - network: said, retried later
        return {"ok": False, "status": "error", "device_id": "",
                "detail": f"{type(exc).__name__}: {exc}"[:200]}
    code = getattr(r, "status_code", 0)
    body = _json(r)
    if code in (401, 403):
        return {"ok": False, "status": "auth", "device_id": "",
                "detail": f"the app refused the saved login (HTTP {code})"}
    dev = body.get("device") if isinstance(body, dict) else None
    secret = body.get("device_secret") if isinstance(body, dict) else None
    if code != 200 or not isinstance(dev, dict) or not dev.get("id") or not secret:
        return {"ok": False, "status": "error", "device_id": "",
                "detail": f"device registration answered HTTP {code}"}
    data = load(path)
    data[_key(api_url)] = {"device_id": str(dev["id"]), "token": str(secret),
                           "name": str(dev.get("name") or ""),
                           "registered_at": int(time.time())}
    _save(data, path)
    return {"ok": True, "status": "registered", "device_id": str(dev["id"]), "detail": ""}


def beat(client: Any, api_url: str, *, path: Optional[Path] = None) -> Dict[str, Any]:
    """One heartbeat as the device. Returns ``{"ok", "status", "detail"}``; status is
    ``live``, ``removed`` (404: the app no longer has it -- recorded, beats stop),
    ``unregistered`` or ``error`` (network, 5xx: the next beat retries)."""
    rec = record_for(api_url, path)
    device_id, token = str(rec.get("device_id") or ""), str(rec.get("token") or "")
    if rec.get("removed"):
        return {"ok": False, "status": "removed", "detail": "this PC was removed in the app"}
    if not device_id or not token:
        return {"ok": False, "status": "unregistered", "detail": "no device on record"}
    try:
        r = client.post(f"{_key(api_url)}{HEARTBEAT_PATH.format(device_id=device_id)}",
                        headers={"X-Device-Token": token})
    except Exception as exc:  # noqa: BLE001 - network: said, retried next beat
        return {"ok": False, "status": "error", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    code = getattr(r, "status_code", 0)
    if 200 <= code < 300:
        return {"ok": True, "status": "live", "detail": ""}
    if code == 404:
        data = load(path)
        entry = data.get(_key(api_url))
        if isinstance(entry, dict):
            entry["removed"] = True
            _save(data, path)
        return {"ok": False, "status": "removed", "detail": "the app no longer has this PC"}
    return {"ok": False, "status": "error", "detail": f"heartbeat answered HTTP {code}"}


class AppDeviceBeat:
    """Register-then-beat on a clock: :meth:`tick` does at most one request pair per
    BEAT_INTERVAL_S and returns the outcome, or None when it was not yet time."""

    def __init__(self, api_url: str, *, path: Optional[Path] = None,
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = BEAT_INTERVAL_S) -> None:
        self.api_url = api_url
        self.path = path
        self.clock = clock
        self.interval = interval
        self._next = 0.0
        self.last: Dict[str, Any] = {}

    def tick(self, client: Any, bearer: str) -> Optional[Dict[str, Any]]:
        now = self.clock()
        if now < self._next:
            return None
        reg = ensure_registered(client, self.api_url, bearer, path=self.path)
        if not reg["ok"]:
            self._next = now + (REGISTER_RETRY_S if reg["status"] != "removed"
                                else REGISTER_RETRY_S * 12)
            self.last = reg
            return reg
        out = beat(client, self.api_url, path=self.path)
        out["device_id"] = reg["device_id"]
        if reg["status"] == "registered":
            out["registered"] = True
        self._next = now + self.interval
        self.last = out
        return out


def say(res: Dict[str, Any], app: str) -> str:
    """One human line for a tick that changed something worth saying."""
    label = app.upper()
    status = res.get("status")
    if res.get("registered"):
        return f"  [+] This PC is now listed in {label} (My Hardware) and shows online"
    if status == "live":
        return ""
    if status == "removed":
        return (f"  [x] This PC was removed from {label}'s devices; it is no longer kept "
                f"online. To list it again: adk tenant watch --app {app} --reset-device")
    if status in ("auth", "no-login"):
        return f"  [x] This PC is not listed in {label}: {res.get('detail')}. Run: adk login"
    return f"  [!] This PC could not check in with {label} ({res.get('detail')}); retrying"
