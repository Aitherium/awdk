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
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("adk.node_commands")

__all__ = ["VERBS", "VERSION_RE", "SIGNED_FIELDS", "RESULT_FIELDS", "canonical", "sign", "verify",
           "load_key", "save_key", "run_commands", "sign_result"]

#: verb -> {argument: allowed values}. Keep in step with identity_node_commands.VERBS;
#: a verb the server adds is refused here until this copy learns it.
VERBS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "collect-diagnostics": {},
    "update": {},
    "lend-on": {"via": ("lan", "tunnel", "local")},
    "lend-off": {},
    # Restart ONE unit from this host's own restartable list (adk.restartable_units);
    # its single argument is checked against that list, not a fixed tuple.
    "restart-lane": {},
    # Install ONE exact awdk release (``version`` matches VERSION_RE) into the
    # interpreter running this daemon, then restart onto it. Never a URL, a path or a
    # pip option: the argv is fixed and only the version string is filled in.
    "upgrade": {},
    # Set (or, with '' / 'auto', clear) the inference URL this device advertises in
    # its heartbeat. AITHER_NODE_INFERENCE_URL on the host still wins.
    "advertise-inference": {},
    # Install ONE exact release of ONE allow-listed companion package (COMPONENTS) into
    # this interpreter -- the same fixed argv as `upgrade`, a different package name.
    # Never restarts anything itself: the owner follows with `restart-lane` for the
    # component's unit, so a bad release never takes the heartbeat down with it.
    "upgrade-component": {},
}
#: Companion packages `upgrade-component` may install. Closed: a package not named
#: here is refused here and in identity_node_commands.UPGRADE_COMPONENTS.
COMPONENTS: Tuple[str, ...] = ("awnode",)
#: verb -> {argument: check}. An argument whose allowed values live on this host.
_HOST_ARGS: Dict[str, Dict[str, Callable[[str], bool]]] = {
    "restart-lane": {"unit": lambda v: v in _restartable()},
}
#: An exact release: three dot-separated integers, nothing else (no specifier, no
#: extra pip argument). Same rule as identity_node_commands.UPGRADE_VERSION_RE.
VERSION_RE = re.compile(r"\A[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}\Z")
#: verb -> {argument: check}. An open-ended argument whose FORM is checked.
_FORM_ARGS: Dict[str, Dict[str, Callable[[str], bool]]] = {
    "upgrade": {"version": lambda v: bool(VERSION_RE.match(v))},
    "advertise-inference": {"url": lambda v: _valid_inference_arg(v)},
    "upgrade-component": {"component": lambda v: v in COMPONENTS,
                          "version": lambda v: bool(VERSION_RE.match(v))},
}
UPGRADE_TIMEOUT_S = 600
#: Seconds between the upgrade result and the restart, so the result is reported first.
RESTART_DELAY_S = 30
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
        expires = float(cmd.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0.0  # unreadable expiry = already expired
    if expires <= t:
        return "expired"
    if str(cmd.get("id") or "") in (seen if seen is not None else _load_seen()):
        return "already run"
    verb = cmd.get("verb")
    if verb not in VERBS:
        return f"verb {str(verb)[:40]!r} is not allowed on this device"
    args = cmd.get("args") or {}
    if not isinstance(args, dict):
        return "bad args"
    host_args = _HOST_ARGS.get(verb)
    if host_args is not None:
        if set(args) != set(host_args):
            return f"{verb} takes exactly {', '.join(sorted(host_args))}"
        for k, check in host_args.items():
            if not check(str(args[k])):
                return f"{verb} {k} {str(args[k])[:60]!r} is not on this host's list"
        return ""
    form_args = _FORM_ARGS.get(verb)
    if form_args is not None:
        if set(args) != set(form_args):
            return f"{verb} takes exactly {', '.join(sorted(form_args))}"
        for k, check in form_args.items():
            if not isinstance(args[k], str) or not check(args[k]):
                return f"{verb} {k} {str(args[k])[:60]!r} is not a valid {k}"
        return ""
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


def _restartable() -> List[str]:
    from adk.restartable_units import restartable_units
    return restartable_units()


def _restart_lane(args: Dict[str, str]) -> Dict[str, Any]:
    """``systemctl restart <unit>`` for a unit on this host's own list -- no shell, no
    other command, checked again here in case the list changed since verify()."""
    import subprocess

    unit = str(args.get("unit") or "")
    if unit not in _restartable():
        return {"ok": False, "error": "unit is not on this host's restartable list"}
    try:
        proc = subprocess.run(["systemctl", "restart", unit], capture_output=True, text=True,
                              timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "unit": unit, "error": type(exc).__name__}
    return {"ok": proc.returncode == 0, "unit": unit, "rc": proc.returncode,
            "stderr": (proc.stderr or "")[-400:]}


def _valid_inference_arg(value: str) -> bool:
    from adk.enrollment import validate_advertised_inference_url
    try:
        validate_advertised_inference_url(value)
    except ValueError:
        return False
    return True


def _own_user_unit() -> str:
    """The systemd USER unit this process runs in (``aither-node-beat.service`` when
    ``adk pair`` installed it), read from /proc/self/cgroup; '' when not in one."""
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        path = line.rsplit(":", 1)[-1]
        if "/user@" not in path:
            continue
        for part in reversed(path.split("/")):
            if part.endswith(".service") and not part.startswith("user@"):
                return part if re.match(r"^[A-Za-z0-9@._-]{1,120}$", part) else ""
    return ""


def _schedule_restart(unit: str) -> Dict[str, Any]:
    """Restart ``unit`` RESTART_DELAY_S from now through a transient systemd user
    timer: the timer outlives this process, and the result is reported before it dies."""
    import shutil
    import subprocess

    systemd_run, systemctl = shutil.which("systemd-run"), shutil.which("systemctl")
    if not systemd_run or not systemctl:
        return {"mechanism": "none", "detail": "systemd-run/systemctl not on PATH"}
    argv = [systemd_run, "--user", f"--on-active={RESTART_DELAY_S}",
            "--timer-property=AccuracySec=1s", systemctl, "--user", "restart", unit]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"mechanism": "systemd-run", "unit": unit, "scheduled": False,
                "detail": type(exc).__name__}
    return {"mechanism": "systemd-run", "unit": unit, "scheduled": proc.returncode == 0,
            "in_s": RESTART_DELAY_S, "detail": (proc.stderr or "").strip()[-200:]}


def _installed_awdk_version(package: str = "awdk") -> str:
    """The version of ``package`` a FRESH interpreter sees (this one has the old code imported)."""
    import subprocess

    code = f"import importlib.metadata as m; print(m.version({package!r}))"
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _uv_binary() -> Optional[str]:
    """uv on PATH, else where its installer puts it (a systemd user unit's PATH is short)."""
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    for cand in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _installer_argv(version: str, package: str = "awdk") -> Optional[List[str]]:
    """The fixed argv that installs ``awdk==<version>`` into THIS interpreter.

    pip when this interpreter has it; otherwise ``uv pip install --python <this>``.
    A venv made by uv has no pip (measured 2026-10-04 on the DGX Spark's heartbeat venv:
    ``No module named pip``), so a pip-only upgrade could never reach it.
    """
    import importlib.util

    spec = f"{package}=={version}"
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                spec]
    uv = _uv_binary()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, "-q", spec]
    return None


def _upgrade(args: Dict[str, str]) -> Dict[str, Any]:
    """Install ``awdk==<version>`` into THIS interpreter (pip, or uv), then restart onto it.

    The argv is fixed; only the version is filled in, and it is checked again here.
    When this process runs in a systemd user unit (``adk pair`` installs
    ``aither-node-beat.service``), a transient ``systemd-run --user`` timer restarts
    that unit RESTART_DELAY_S later -- after this result has been reported. Anywhere
    else (the Windows Run key, a hand-started process) nothing restarts it: the new
    code is installed and runs from the next start, and the result says so.
    """
    import subprocess

    version = str(args.get("version") or "")
    if not VERSION_RE.match(version):
        return {"ok": False, "error": "version must be an exact release like 1.2.3"}
    try:
        from adk import __version__ as before
    except Exception:  # noqa: BLE001
        before = "unknown"
    argv = _installer_argv(version)
    if argv is None:
        return {"ok": False, "from": before, "to": version, "rc": None,
                "error": "no installer: this interpreter has no pip and no uv was found"}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=UPGRADE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "from": before, "to": version, "rc": None,
                "error": type(exc).__name__}
    out: Dict[str, Any] = {"from": before, "to": version, "rc": proc.returncode,
                           "stderr": (proc.stderr or "")[-400:]}
    installed = _installed_awdk_version() if proc.returncode == 0 else ""
    out["installed"] = installed
    if proc.returncode != 0 or installed != version:
        out["ok"] = False
        return out
    unit = _own_user_unit()
    out["restart"] = (_schedule_restart(unit) if unit else
                      {"mechanism": "none",
                       "detail": "not in a systemd user unit; new code runs from the next start"})
    out["ok"] = True
    return out


def _advertise_inference(args: Dict[str, str]) -> Dict[str, Any]:
    """Persist the inference URL the heartbeat advertises ('' / 'auto' clears it)."""
    from adk import enrollment

    url = str(args.get("url") or "")
    try:
        stored = enrollment.save_advertised_inference_url(url)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:200]}
    env = (os.environ.get("AITHER_NODE_INFERENCE_URL") or "").strip()
    return {"ok": True, "url": stored, "cleared": not stored,
            "note": ("AITHER_NODE_INFERENCE_URL is set on this host and still wins"
                     if env else "advertised from the next heartbeat")}


def _upgrade_component(args: Dict[str, str]) -> Dict[str, Any]:
    """Install ``<component>==<version>`` (component from COMPONENTS) into THIS interpreter.

    Same fixed argv as :func:`_upgrade`; both arguments are checked again here. It
    restarts nothing: the component's own unit is restarted by a separate
    ``restart-lane`` the owner sends after reading this result.
    """
    import subprocess

    component = str(args.get("component") or "")
    version = str(args.get("version") or "")
    if component not in COMPONENTS:
        return {"ok": False, "error": f"component must be one of {', '.join(COMPONENTS)}"}
    if not VERSION_RE.match(version):
        return {"ok": False, "error": "version must be an exact release like 1.2.3"}
    before = _installed_awdk_version(component) or "absent"
    argv = _installer_argv(version, component)
    if argv is None:
        return {"ok": False, "component": component, "from": before, "to": version,
                "error": "no installer: this interpreter has no pip and no uv was found"}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=UPGRADE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "component": component, "from": before, "to": version,
                "error": type(exc).__name__}
    installed = _installed_awdk_version(component) if proc.returncode == 0 else ""
    return {"ok": proc.returncode == 0 and installed == version, "component": component,
            "from": before, "to": version, "installed": installed, "rc": proc.returncode,
            "stderr": (proc.stderr or "")[-400:],
            "restart": "send restart-lane for the component's unit to run the new code"}


_HANDLERS: Dict[str, Callable[[Dict[str, str]], Any]] = {
    "collect-diagnostics": _diagnostics,
    "update": _update,
    "lend-on": _lend_on,
    "lend-off": _lend_off,
    "restart-lane": _restart_lane,
    "upgrade": _upgrade,
    "advertise-inference": _advertise_inference,
    "upgrade-component": _upgrade_component,
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
