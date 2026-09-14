"""Client for the awdesk bridge (default http://127.0.0.1:47931).

One control plane, many surfaces: the awdesk Fleet/Command windows, ``awsh /fleet``,
``awsh /command``, the Desk MCP tools and these ``adk`` verbs all land on the same
bridge routes, so no two of them can hold a different idea of whether the fleet is
up or what the owner asked for.

Routes (contract shared with awsh and the desk app):

- ``GET  /fleet/status``            -> the fleet verdict (``ok``, ``fleet``, ``held`` ...)
- ``POST /fleet/<verb>``            -> ``down | up | gaming | resume | adopt | open``
- ``POST /command`` ``{"text": ...}`` -> ``200 {id, reply}`` when it finished quickly,
  ``202 {id}`` when the reply lands in history later
- ``GET  /command/history?limit=N`` -> ``{"items": [{id, at, source, text, reply, kind}]}``

Exit codes: 0 ok · 1 refused (the verdict says ``ok: false``) · 2 could not judge (the
bridge is not running and no fallback answered). Silence is never a pass.

Fallback: when the bridge is unreachable for a FLEET verb and ``AWDESK_FLEET_FALLBACK``
names a command (the host's distro-side quiesce script), it is run with the verb's
argv appended and ``--json``; the result says which lane answered (``source``).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import Any, Callable, Optional, Tuple

FLEET_VERBS = ("status", "down", "up", "gaming", "resume", "adopt", "panel")

#: verb -> (method, path). ``panel`` is the owner-facing name of the bridge's ``open``.
_FLEET_ROUTES = {
    "status": ("GET", "/fleet/status"),
    "down": ("POST", "/fleet/down"),
    "up": ("POST", "/fleet/up"),
    "gaming": ("POST", "/fleet/gaming"),
    "resume": ("POST", "/fleet/resume"),
    "adopt": ("POST", "/fleet/adopt"),
    "panel": ("POST", "/fleet/open"),
}

#: verb -> argv for the fallback script (the distro-side implementation speaks
#: quiesce/resume; ``down``/``up`` are the owner-facing names).
_FALLBACK_ARGV = {
    "status": ["status"],
    "down": ["quiesce", "--all"],
    "up": ["resume"],
    "gaming": ["quiesce", "--deep"],
    "resume": ["resume"],
    "adopt": ["adopt"],
}

#: seconds. ``up`` after ``down`` reloads models one at a time -- minutes, not seconds.
TIMEOUTS = {
    "status": 90.0,
    "adopt": 120.0,
    "gaming": 600.0,
    "down": 900.0,
    "up": 1800.0,
    "resume": 1800.0,
    "panel": 10.0,
    "command": 1800.0,
    "history": 15.0,
    "desktop": 10.0,
}

Transport = Callable[[str, str, Optional[str], float], Tuple[int, str]]


def build_request(verb: str) -> Tuple[str, str, Optional[str]]:
    """Pure: fleet verb -> (method, path, body). ``status`` is a GET (the bridge
    answers 405 to a POST there); everything else POSTs with no body."""
    key = verb.lower().strip()
    if key not in _FLEET_ROUTES:
        raise ValueError(f"unknown fleet verb {verb!r} (one of {', '.join(FLEET_VERBS)})")
    method, path = _FLEET_ROUTES[key]
    return (method, path, None)


def build_command_request(text: str) -> Tuple[str, str, str]:
    """Pure: -> ("POST", "/command", json body)."""
    return ("POST", "/command", json.dumps({"text": text}))


def build_history_request(limit: int = 20) -> Tuple[str, str, Optional[str]]:
    """Pure: -> ("GET", "/command/history?limit=N", None)."""
    return ("GET", f"/command/history?limit={int(limit)}", None)


DESKTOP_SURFACES = ("overlay", "app", "status")


def build_desktop_request(surface: str) -> Tuple[str, str, Optional[str]]:
    """Pure: desktop surface -> (method, path, body). ``status`` is a GET; ``overlay``
    (the aitherium.com Living Desktop over the Windows desktop) and ``app`` (the full
    AitherDesktop window) POST with no body. No bearer: they raise a window on the
    owner's own screen, the same class as ``fleet panel``."""
    key = (surface or "status").lower().strip()
    if key not in DESKTOP_SURFACES:
        raise ValueError(
            f"unknown desktop surface {surface!r} (one of {', '.join(DESKTOP_SURFACES)})")
    return ("GET" if key == "status" else "POST", f"/desktop/{key}", None)


def build_fallback_argv(verb: str, fallback_cmd: str) -> list[str]:
    """Pure: the fallback command line for a fleet verb. Empty when the verb has no
    fallback (``panel`` needs the desk) or no fallback command is configured."""
    key = verb.lower().strip()
    if not fallback_cmd or key not in _FALLBACK_ARGV:
        return []
    return [*shlex.split(fallback_cmd), *_FALLBACK_ARGV[key], "--json"]


def exit_code_for(status: int, doc: Any) -> int:
    """Pure: HTTP status + verdict -> process exit code (0 ok, 1 refused, 2 cannot judge)."""
    if status in (200, 202):
        if isinstance(doc, dict) and doc.get("cannotJudge"):
            return 2
        if isinstance(doc, dict) and doc.get("ok") is False:
            return 1
        return 0
    if status in (404, 405, 409):
        return 1
    return 2


def get_bridge_url() -> str:
    url = (
        os.environ.get("AWDESK_URL")
        or os.environ.get("AITHERSHELL_AWDESK_URL")
        or "http://127.0.0.1:47931"
    )
    return url.rstrip("/")


def bridge_token(env: Optional[dict] = None, home: Optional[str] = None) -> str:
    """The bearer the bridge's mutating routes require (POST /fleet/<verb>, POST /command).

    Same resolution as the adk daemon and awsh: ``AITHER_HARNESS_TOKEN``, else
    ``~/.aither/harness_token``. Empty string when neither exists -- the caller then
    sends no header and the bridge answers 401, which ``explain_status`` turns into
    the one-line fix.
    """
    env = os.environ if env is None else env
    from_env = (env.get("AITHER_HARNESS_TOKEN") or "").strip()
    if from_env:
        return from_env
    try:
        path = os.path.join(home or os.path.expanduser("~"), ".aither", "harness_token")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def explain_status(status: int) -> Optional[str]:
    """Pure: the bridge's auth refusals as a sentence naming the fix, else None."""
    if status == 401:
        return ("desk bridge refused: bearer required -- set AITHER_HARNESS_TOKEN or start "
                "the adk daemon once so ~/.aither/harness_token exists")
    if status == 503:
        return "desk bridge refused: no bridge token configured on the awdesk side (HTTP 503)"
    return None


def _http_transport(method: str, url: str, body: Optional[str], timeout: float) -> Tuple[int, str]:
    """Default transport: (status, text). Raises on connection failure."""
    import httpx  # local import: keeps the pure builders importable without it

    headers = {"content-type": "application/json"} if body is not None else {}
    if method.upper() == "POST":
        token = bridge_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
    resp = httpx.request(method, url, content=body, headers=headers or None, timeout=timeout)
    return resp.status_code, resp.text


def _parse(text: str) -> Any:
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"error": (text or "")[:200]}


class DeskBridgeClient:
    """Talks to the awdesk bridge; falls back to the configured fleet script."""

    def __init__(self, url: Optional[str] = None, transport: Optional[Transport] = None,
                 fallback_cmd: Optional[str] = None,
                 runner: Optional[Callable[[list[str], float], Tuple[int, str]]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.url = url or get_bridge_url()
        self.transport = transport or _http_transport
        self.fallback_cmd = (fallback_cmd if fallback_cmd is not None
                             else os.environ.get("AWDESK_FLEET_FALLBACK", ""))
        self.runner = runner or self._run
        self.sleep = sleep

    @staticmethod
    def _run(argv: list[str], timeout: float) -> Tuple[int, str]:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, p.stdout

    # -- fleet ------------------------------------------------------------------
    def call_fleet(self, verb: str) -> Tuple[int, dict]:
        key = verb.lower().strip()
        method, path, body = build_request(key)
        try:
            status, text = self.transport(method, self.url + path, body, TIMEOUTS.get(key, 60.0))
        except Exception as exc:  # connection refused / timeout: the bridge is not there
            fallback = self._fallback_fleet(key)
            if fallback is not None:
                return fallback
            return (2, {"ok": False, "cannotJudge": True, "source": "none",
                        "error": f"desk bridge unreachable ({exc.__class__.__name__}) "
                                 f"and no AWDESK_FLEET_FALLBACK configured"})
        doc = _parse(text)
        if not isinstance(doc, dict):
            doc = {"ok": False, "error": "non-object verdict"}
        doc["source"] = "bridge"
        refused = explain_status(status)
        if refused and not (status == 503 and doc.get("cannotJudge")):
            doc.update({"ok": False, "cannotJudge": True, "error": refused})
        return (exit_code_for(status, doc), doc)

    def _fallback_fleet(self, key: str) -> Optional[Tuple[int, dict]]:
        argv = build_fallback_argv(key, self.fallback_cmd)
        if not argv:
            return None
        try:
            rc, out = self.runner(argv, TIMEOUTS.get(key, 600.0))
        except Exception as exc:
            return (2, {"ok": False, "cannotJudge": True, "source": "fallback",
                        "error": f"fallback failed: {exc}"})
        start = out.find("{")
        doc = _parse(out[start:]) if start >= 0 else {}
        if not isinstance(doc, dict) or not doc:
            return (2, {"ok": False, "cannotJudge": True, "source": "fallback",
                        "error": f"fallback rc={rc} with no JSON verdict"})
        doc["source"] = "fallback"
        if doc.get("verdict") == "CANNOT_JUDGE":
            doc["cannotJudge"] = True
            return (2, doc)
        return (0 if (rc == 0 and doc.get("ok", True)) else 1, doc)

    # -- command ----------------------------------------------------------------
    def call_command(self, text: str) -> Tuple[int, dict]:
        method, path, body = build_command_request(text)
        try:
            status, raw = self.transport(method, self.url + path, body, TIMEOUTS["command"])
        except Exception as exc:
            return (2, {"ok": False, "cannotJudge": True,
                        "error": f"desk bridge unreachable ({exc.__class__.__name__})"})
        doc = _parse(raw)
        if not isinstance(doc, dict):
            doc = {"error": "non-object reply"}
        refused = explain_status(status)
        if refused:
            doc.update({"ok": False, "cannotJudge": True, "error": refused})
        doc.setdefault("pending", status == 202)
        return (exit_code_for(status, doc), doc)

    # -- desktop surfaces -------------------------------------------------------
    def call_desktop(self, surface: str = "status") -> Tuple[int, dict]:
        """Raise the overlay / the AitherDesktop app, or read which are open."""
        method, path, body = build_desktop_request(surface)
        try:
            status, raw = self.transport(method, self.url + path, body, TIMEOUTS["desktop"])
        except Exception as exc:
            return (2, {"ok": False, "cannotJudge": True,
                        "error": f"desk bridge unreachable ({exc.__class__.__name__}) -- "
                                 "awdesk is not running (desk-start)"})
        doc = _parse(raw)
        if not isinstance(doc, dict):
            doc = {"ok": False, "error": "non-object reply"}
        if status == 404 and "unknown desktop surface" not in str(doc.get("error", "")):
            # A 404 with no verdict body is an awdesk built before the /desktop
            # routes existed -- could-not-judge (2), not a refusal (1).
            return (2, {"ok": False, "cannotJudge": True,
                        "error": "this awdesk has no /desktop routes (older build) -- update Desk"})
        return (exit_code_for(status, doc), doc)

    def get_history(self, limit: int = 20) -> Tuple[int, list]:
        method, path, body = build_history_request(limit)
        try:
            status, raw = self.transport(method, self.url + path, body, TIMEOUTS["history"])
        except Exception:
            return (2, [])
        doc = _parse(raw)
        items = doc.get("items", []) if isinstance(doc, dict) else []
        return (0 if status == 200 else 2, items)

    def poll_command_reply(self, cmd_id: str, max_wait_s: float = 1800.0,
                           interval_s: float = 2.0) -> Tuple[int, dict]:
        """Poll history until the item carries a reply. Returns (1, ...) on timeout."""
        waited = 0.0
        while True:
            rc, items = self.get_history(50)
            for item in items:
                if item.get("id") == cmd_id and item.get("reply"):
                    return (0, item)
            if waited >= max_wait_s:
                return (1, {"id": cmd_id, "error": f"no reply after {int(max_wait_s)} s"})
            self.sleep(interval_s)
            waited += interval_s


__all__ = [
    "FLEET_VERBS", "TIMEOUTS", "DeskBridgeClient", "build_request", "build_command_request",
    "build_history_request", "build_fallback_argv", "exit_code_for", "get_bridge_url",
    "bridge_token", "explain_status", "build_desktop_request", "DESKTOP_SURFACES",
]
