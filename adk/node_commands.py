"""The device half of the control plane's command channel.

Identity (``/v1/nodes``) can queue a command for an enrolled device; the device's
own heartbeat collects it. This module decides whether to run it, runs it, and
signs what it did. The language is CLOSED and is enforced HERE, on the device,
whatever the server sent:

* :data:`VERBS` is every verb this device will run, each with its allowed
  arguments and values. There is no shell, no path, no URL and no free text.
* A command runs only when its HMAC-SHA256 signature checks out against this
  device's key (handed over once at registration, stored owner-only), it names
  THIS device, it has not expired, and its id has not been run before.
* Results are signed with the same key, so Identity can tell a result from this
  device apart from anything else holding the owner's session.

The canonical bytes MUST match ``services/security/identity_node_commands.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("adk.node_commands")

__all__ = ["VERBS", "SIGNED_FIELDS", "RESULT_FIELDS", "canonical", "sign", "verify",
           "load_key", "save_key", "run_commands", "sign_result"]

#: verb -> {argument: allowed values}. Keep in step with identity_node_commands.VERBS;
#: a verb the server adds is refused here until this copy learns it.
VERBS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "collect-diagnostics": {},
    "update": {},
    "lend-on": {"via": ("lan", "tunnel", "local")},
    "lend-off": {},
}
SIGNED_FIELDS = ("id", "tenant_id", "node_id", "verb", "args", "issued_by",
                 "issued_at", "expires_at")
RESULT_FIELDS = ("id", "node_id", "ok", "output")
MAX_OUTPUT_CHARS = 8000
_SEEN_MAX = 200


def _aither_dir() -> Path:
    return Path(os.environ.get("AITHER_HOME") or (Path.home() / ".aither"))


def _key_path() -> Path:
    return _aither_dir() / "node_command_key.json"


def _seen_path() -> Path:
    return _aither_dir() / "node_commands_seen.json"


def canonical(record: Dict[str, Any], fields: Tuple[str, ...] = SIGNED_FIELDS) -> bytes:
    return json.dumps({f: record.get(f) for f in fields}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode()


def sign(key_hex: str, payload: bytes) -> str:
    return hmac.new(key_hex.encode(), payload, hashlib.sha256).hexdigest()


def load_key(node_id: str) -> str:
    """This device's command key for ``node_id``, or ''."""
    try:
        data = json.loads(_key_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("node_id") != node_id:
        return ""
    return str(data.get("key") or "")


def save_key(node_id: str, key_hex: str) -> bool:
    """Store the key owner-only. False (and nothing written) when that is impossible."""
    if not key_hex or not node_id:
        return False
    try:
        from adk._private_file import write_private_text
        _key_path().parent.mkdir(parents=True, exist_ok=True)
        write_private_text(_key_path(), json.dumps({"node_id": node_id, "key": key_hex}))
        return True
    except Exception as exc:  # noqa: BLE001 - no key stored means commands are refused
        log.warning("node command key not stored: %s", exc)
        return False


def _load_seen() -> List[str]:
    try:
        data = json.loads(_seen_path().read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _remember_seen(cmd_id: str) -> None:
    seen = [s for s in _load_seen() if s != cmd_id] + [cmd_id]
    try:
        _seen_path().parent.mkdir(parents=True, exist_ok=True)
        _seen_path().write_text(json.dumps(seen[-_SEEN_MAX:]), encoding="utf-8")
    except OSError as exc:
        log.warning("could not record command %s as run: %s", cmd_id, exc)


def verify(cmd: Any, key_hex: str, node_id: str, *, now: Optional[float] = None,
           seen: Optional[List[str]] = None) -> str:
    """'' when this device may run ``cmd``; otherwise the reason it may not."""
    if not isinstance(cmd, dict):
        return "not an object"
    if not key_hex:
        return "no command key on this device"
    sig = str(cmd.get("sig") or "")
    if not sig or not hmac.compare_digest(sign(key_hex, canonical(cmd)), sig):
        return "bad signature"
    if cmd.get("node_id") != node_id:
        return "addressed to another device"
    t = time.time() if now is None else now
    try:
        if float(cmd.get("expires_at") or 0) <= t:
            return "expired"
    except (TypeError, ValueError):
        return "expired"
    if str(cmd.get("id") or "") in (seen if seen is not None else _load_seen()):
        return "already run"
    verb = cmd.get("verb")
    if verb not in VERBS:
        return f"verb {str(verb)[:40]!r} is not allowed on this device"
    args = cmd.get("args") or {}
    if not isinstance(args, dict):
        return "bad args"
    for k, v in args.items():
        if k not in VERBS[verb] or str(v) not in VERBS[verb][k]:
            return f"argument {str(k)[:40]!r} is not allowed for {verb}"
    return ""


# ── the verbs ────────────────────────────────────────────────────────────────


def _diagnostics(_args: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"python": sys.version.split()[0], "platform": platform.platform(),
                           "machine": platform.machine()}
    try:
        from adk import __version__
        out["adk"] = __version__
    except Exception:  # noqa: BLE001
        out["adk"] = "unknown"
    for name, fn in (("heartbeat", "adk.enrollment:heartbeat_status"),
                     ("lend", "adk.lend_routes:status")):
        mod, attr = fn.split(":")
        try:
            out[name] = getattr(__import__(mod, fromlist=[attr]), attr)()
        except Exception as exc:  # noqa: BLE001 - one section failing is reported, not fatal
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        from adk import self_update
        out["code"] = {"running_root": str(self_update.RUNNING_ROOT),
                       "auto_update": self_update.auto_source()[0]}
    except Exception as exc:  # noqa: BLE001
        out["code"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return out


def _update(_args: Dict[str, str]) -> Dict[str, Any]:
    from adk import self_update
    return {"requested_for": self_update.request_apply(),
            "note": "each daemon adopts new code at its next check, only if validated and idle"}


def _lend_on(args: Dict[str, str]) -> Dict[str, Any]:
    from adk import lend_routes
    return lend_routes.start(args.get("via", "lan"))


def _lend_off(_args: Dict[str, str]) -> Dict[str, Any]:
    from adk import lend_routes
    return lend_routes.stop()


_HANDLERS: Dict[str, Callable[[Dict[str, str]], Any]] = {
    "collect-diagnostics": _diagnostics,
    "update": _update,
    "lend-on": _lend_on,
    "lend-off": _lend_off,
}


def sign_result(key_hex: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {**result, "sig": sign(key_hex, canonical(result, RESULT_FIELDS))}


def run_commands(cmds: Any, node_id: str, key_hex: str,
                 *, handlers: Optional[Dict[str, Callable[[Dict[str, str]], Any]]] = None
                 ) -> List[Dict[str, Any]]:
    """Run every command this device accepts; return one signed result per command
    it RAN. A refused command runs nothing and reports nothing (a forged command
    must not get a signed answer out of this device); the refusal is logged."""
    table = handlers or _HANDLERS
    results: List[Dict[str, Any]] = []
    for cmd in cmds if isinstance(cmds, list) else []:
        why = verify(cmd, key_hex, node_id)
        if why:
            log.warning("node command %s refused: %s",
                        str(cmd.get("id") if isinstance(cmd, dict) else "?")[:40], why)
            continue
        _remember_seen(str(cmd["id"]))  # before running: a crash must not re-run it
        try:
            output: Any = table[cmd["verb"]](dict(cmd.get("args") or {}))
            ok = not (isinstance(output, dict) and output.get("ok") is False)
        except Exception as exc:  # noqa: BLE001 - reported to the owner, never raised
            output, ok = {"error": f"{type(exc).__name__}: {exc}"[:500]}, False
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        log.info("node command %s (%s) ran ok=%s", cmd["id"], cmd["verb"], ok)
        results.append(sign_result(key_hex, {"id": cmd["id"], "node_id": node_id,
                                             "ok": ok, "output": text[:MAX_OUTPUT_CHARS]}))
    return results
