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
* The ``appliance-*`` verbs reach a tenant appliance this host deployed from its own
  repo, by NAME only: the engine, container, repo dir and deploy script come from the
  host's ``~/.aither/appliances.json``, which the repo's deploy script writes.

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
           "load_key", "save_key", "run_commands", "sign_result", "load_appliances",
           "appliance_names"]

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
    # A tenant appliance this host deployed with its repo's deploy/deploy.{ps1,sh} (the
    # script registers it in ~/.aither/appliances.json). The ONLY argument is the
    # appliance's name, checked against that host registry; every argv is built from
    # the registry entry (engine, container, repo dir, script), never from the server.
    "appliance-status": {},
    "appliance-logs": {},
    "appliance-redeploy": {},
}
#: Companion packages `upgrade-component` may install. Closed: a package not named
#: here is refused here and in identity_node_commands.UPGRADE_COMPONENTS.
COMPONENTS: Tuple[str, ...] = ("awnode",)
#: verb -> {argument: check}. An argument whose allowed values live on this host.
_HOST_ARGS: Dict[str, Dict[str, Callable[[str], bool]]] = {
    "restart-lane": {"unit": lambda v: v in _restartable()},
    "appliance-status": {"name": lambda v: v in appliance_names()},
    "appliance-logs": {"name": lambda v: v in appliance_names()},
    "appliance-redeploy": {"name": lambda v: v in appliance_names()},
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
#: verb -> {argument: check}. A form-checked argument the verb MAY carry. An older
#: server never sends it, so the required set in _FORM_ARGS is unchanged.
_OPTIONAL_FORM_ARGS: Dict[str, Dict[str, Callable[[str], bool]]] = {
    "advertise-inference": {"probe_url": lambda v: _valid_inference_arg(v)},
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
        optional = _OPTIONAL_FORM_ARGS.get(verb, {})
        if not set(form_args) <= set(args) <= set(form_args) | set(optional):
            takes = ", ".join(sorted(form_args))
            if optional:
                takes += f" (optionally {', '.join(sorted(optional))})"
            return f"{verb} takes exactly {takes}"
        for k, check in {**form_args, **optional}.items():
            if k not in args:
                continue
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
    """Persist the inference URL the heartbeat advertises ('' / 'auto' clears it), and
    optionally ``probe_url``: where this host itself reaches that server."""
    from adk import enrollment

    url = str(args.get("url") or "")
    probe_url = str(args.get("probe_url") or "")
    try:
        stored = enrollment.save_advertised_inference_url(url, probe_url=probe_url)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:200]}
    env = (os.environ.get("AITHER_NODE_INFERENCE_URL") or "").strip()
    out: Dict[str, Any] = {
        "ok": True, "url": stored, "cleared": not stored,
        "note": ("AITHER_NODE_INFERENCE_URL is set on this host and still wins"
                 if env else "advertised from the next heartbeat")}
    if stored:
        out["probe_url"] = enrollment.validate_advertised_inference_url(probe_url)
    return out


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


# ── appliances: tenant stacks this host deployed from their own repo ─────────
#
# A tenant repo's deploy/deploy.ps1 or deploy/deploy.sh, after a successful
# ``compose up``, upserts one entry into ``~/.aither/appliances.json``:
#
#     {"appliances": {"acmebot": {"name": "acmebot", "repo_dir": "C:/Users/j/acme",
#                                 "script": "deploy.ps1", "engine": "docker",
#                                 "container": "acmebot"}}}
#
# The owner then names the appliance and nothing else. Every value that reaches an
# argv comes from that host file and is re-validated on every read (an entry that
# fails any rule is dropped as if absent), so a command never carries a path, an
# engine, a container or a script of its own.

#: An appliance name: the product slug the tenant repo was generated for.
APPLIANCE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
#: A container name. The first character is alphanumeric, so it can never be an option.
APPLIANCE_CONTAINER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
APPLIANCE_ENGINES: Tuple[str, ...] = ("docker", "podman")
APPLIANCE_SCRIPTS: Tuple[str, ...] = ("deploy.ps1", "deploy.sh")
MAX_APPLIANCES = 16
APPLIANCE_LOG_LINES = 200
#: Raw log bytes kept before the result is fitted into the channel (MAX_OUTPUT_CHARS).
APPLIANCE_LOG_MAX_BYTES = 32 * 1024
APPLIANCE_INSPECT_TIMEOUT_S = 30
APPLIANCE_PULL_TIMEOUT_S = 180
#: The detached deploy script gets this long before it is killed.
APPLIANCE_DEPLOY_TIMEOUT_S = 3600
_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REDEPLOY_WORKER = "appliance-redeploy-worker"


def _appliances_path() -> Path:
    return _aither_dir() / "appliances.json"


def _appliance_state_dir() -> Path:
    return _aither_dir() / "appliances"


def _appliance_problem(name: Any, entry: Any) -> str:
    """'' when ``entry`` is a usable registry entry for ``name``; otherwise why not."""
    if not isinstance(name, str) or not APPLIANCE_NAME_RE.match(name):
        return "bad name"
    if not isinstance(entry, dict):
        return "entry is not an object"
    if entry.get("name", name) != name:
        return "entry names another appliance"
    repo = entry.get("repo_dir")
    if not isinstance(repo, str) or not repo or "\x00" in repo or len(repo) > 1024:
        return "repo_dir is not a path string"
    if repo.startswith(("\\\\", "//")):
        return "repo_dir is a network path"
    if not os.path.isabs(repo):
        return "repo_dir is not absolute"
    if not Path(repo).is_dir():
        return "repo_dir does not exist"
    if entry.get("engine") not in APPLIANCE_ENGINES:
        return "engine is not docker or podman"
    if entry.get("script") not in APPLIANCE_SCRIPTS:
        return "script is not deploy.ps1 or deploy.sh"
    container = entry.get("container")
    if not isinstance(container, str) or not APPLIANCE_CONTAINER_RE.match(container):
        return "container is not a container name"
    return ""


def load_appliances() -> Dict[str, Dict[str, str]]:
    """name -> validated registry entry. Never raises; a bad entry is left out."""
    try:
        # utf-8-sig: a registry Windows PowerShell wrote may start with a BOM.
        data = json.loads(_appliances_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    raw = data.get("appliances") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for name, entry in raw.items():
        why = _appliance_problem(name, entry)
        if why:
            log.warning("appliance %r ignored: %s", str(name)[:64], why)
            continue
        out[name] = {"name": name, "repo_dir": str(Path(entry["repo_dir"]).resolve()),
                     "script": entry["script"], "engine": entry["engine"],
                     "container": entry["container"]}
        if len(out) >= MAX_APPLIANCES:
            break
    return out


def appliance_names() -> List[str]:
    """The appliance names this host's registry holds (what the heartbeat publishes)."""
    return sorted(load_appliances())


def _no_window() -> int:
    """No console window for a child on Windows (a console child opens a terminal tab)."""
    import subprocess

    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_fixed(argv: List[str], timeout: float, *, env: Optional[Dict[str, str]] = None,
               merge: bool = False) -> Tuple[Optional[int], str, str]:
    """(rc, stdout, stderr) of a fixed argv -- never a shell, never raises. rc is None
    (and stderr names the error) when it could not start or timed out. ``merge``
    interleaves stderr into stdout."""
    import subprocess

    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, env=env,
                              creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return None, "", type(exc).__name__
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _git_env() -> Dict[str, str]:
    """git that never waits on a person: no terminal prompt, no credential window."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}


def _head_sha(git: str, repo: str) -> str:
    rc, out, _err = _run_fixed([git, "-C", repo, "rev-parse", "HEAD"], 30, env=_git_env())
    sha = out.strip()
    return sha if rc == 0 and _SHA_RE.match(sha) else ""


def _is_git_repo(repo: str) -> bool:
    return (Path(repo) / ".git").exists()  # a dir, or a file in a linked worktree


def _script_path(entry: Dict[str, str]) -> Optional[Path]:
    """The registered deploy script inside ``repo_dir/deploy``, or None."""
    repo = Path(entry["repo_dir"]).resolve()
    script = (repo / "deploy" / entry["script"]).resolve()
    try:
        script.relative_to(repo)
    except ValueError:
        return None  # a symlink out of the repo is not this repo's script
    return script if script.is_file() else None


def _state_path(name: str) -> Path:
    return _appliance_state_dir() / f"{name}.redeploy.json"


def _log_path(name: str) -> Path:
    return _appliance_state_dir() / f"{name}.redeploy.log"


def _read_state(name: str) -> Dict[str, Any]:
    try:
        data = json.loads(_state_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _private_state_dir() -> Path:
    """``~/.aither/appliances``, created owner-only (0o700)."""
    d = _appliance_state_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(d, 0o700)
    return d


def _open_private(path: Path) -> int:
    """An fd for writing ``path`` (truncated), created 0o600 -- never wider, even
    for a moment, and tightened if an older copy was wider."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_BINARY", 0), 0o600)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    return fd


def _write_state(name: str, state: Dict[str, Any]) -> bool:
    """Write the redeploy state owner-only (tmp + replace). False when it failed."""
    try:
        d = _private_state_dir()
        tmp = d / f".{name}.redeploy.json.tmp"
        with os.fdopen(_open_private(tmp), "w", encoding="utf-8") as f:
            f.write(json.dumps(state))
        os.replace(tmp, _state_path(name))
        return True
    except OSError as exc:
        log.warning("appliance %s redeploy state not written: %s", name, exc)
        return False


def _pid_alive(pid: Any) -> bool:
    """Is process ``pid`` running? Never signals it (on Windows ``os.kill`` would
    TERMINATE it), so Windows asks the kernel for its exit code instead."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _kill_tree(proc: Any) -> str:
    """Kill ``proc`` AND everything it started: the deploy script's own children
    (compose, a build) would otherwise outlive the timeout. Returns how."""
    import subprocess

    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(proc.pid))],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60, check=False,
                           creationflags=_no_window())
            how = "taskkill /T"
        except (OSError, subprocess.TimeoutExpired) as exc:
            how = f"taskkill failed ({type(exc).__name__})"
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session
            how = "killpg"
        except OSError as exc:
            how = f"killpg failed ({type(exc).__name__})"
    try:
        proc.kill()
        proc.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001 - already dead is the expected case
        log.debug("deploy process reap after kill: %s", exc)
    return how


def _tail_text(text: str, max_bytes: int) -> str:
    """The last ``max_bytes`` (UTF-8) of ``text``."""
    raw = text.encode("utf-8", errors="replace")
    return raw[-max_bytes:].decode("utf-8", errors="replace") if len(raw) > max_bytes else text


def _fit(out: Dict[str, Any], key: str, budget: int = MAX_OUTPUT_CHARS - 200) -> Dict[str, Any]:
    """Trim ``out[key]`` from the FRONT until ``out`` serialises within the channel's
    budget, so the newest lines survive and the reported JSON stays whole."""
    text = str(out.get(key) or "")
    size = len(json.dumps(out, default=str))
    while text and size > budget:
        # Escaping makes a JSON char worth less than a text char; keep the same SHARE.
        keep = int(len(text) * budget / size * 0.95)
        text = text[-keep:] if keep > 0 else ""
        out[key] = "...[truncated]\n" + text
        size = len(json.dumps(out, default=str))
    return out


def _container_state(engine_exe: str, container: str) -> Dict[str, Any]:
    rc, out, err = _run_fixed([engine_exe, "inspect", "--type", "container", container],
                              APPLIANCE_INSPECT_TIMEOUT_S)
    if rc != 0:
        return {"found": False, "rc": rc, "error": (err or out).strip()[-300:]}
    try:
        info = json.loads(out)
        info = info[0] if isinstance(info, list) and info else info
        state = info.get("State") or {}
        health = state.get("Health") or state.get("Healthcheck") or {}
        return {"found": True, "status": state.get("Status"), "running": state.get("Running"),
                "health": health.get("Status") if isinstance(health, dict) else None,
                "exit_code": state.get("ExitCode"), "started_at": state.get("StartedAt"),
                "restart_count": info.get("RestartCount"),
                "image": (info.get("Config") or {}).get("Image")}
    except (ValueError, AttributeError, TypeError) as exc:
        return {"found": True, "error": f"unreadable inspect output ({type(exc).__name__})"}


def _appliance(args: Dict[str, str]) -> Tuple[Optional[Dict[str, str]], str]:
    """The registry entry the command names (re-read: it may have changed since verify)."""
    entry = load_appliances().get(str(args.get("name") or ""))
    return (entry, "") if entry else (None, "not an appliance in this host's registry")


def _appliance_status(args: Dict[str, str]) -> Dict[str, Any]:
    """Container state/health (``<engine> inspect``), the repo's HEAD commit and the
    last redeploy's outcome. Never raises."""
    import shutil

    try:
        entry, why = _appliance(args)
        if entry is None:
            return {"ok": False, "error": why}
        out: Dict[str, Any] = {"name": entry["name"], "engine": entry["engine"],
                               "container": entry["container"], "script": entry["script"]}
        engine = shutil.which(entry["engine"])
        out["container_state"] = (_container_state(engine, entry["container"]) if engine else
                                  {"found": False, "error": f"{entry['engine']} not on PATH"})
        git = shutil.which("git")
        out["commit"] = _head_sha(git, entry["repo_dir"]) if git else ""
        last = _read_state(entry["name"])
        if last:
            out["last_redeploy"] = last
        out["ok"] = bool(out["container_state"].get("found"))
        return out
    except Exception as exc:  # noqa: BLE001 - a status call never raises
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _appliance_logs(args: Dict[str, str]) -> Dict[str, Any]:
    """The last APPLIANCE_LOG_LINES lines of the appliance container's log. Never raises."""
    import shutil

    try:
        entry, why = _appliance(args)
        if entry is None:
            return {"ok": False, "error": why}
        engine = shutil.which(entry["engine"])
        if not engine:
            return {"ok": False, "error": f"{entry['engine']} not on PATH"}
        rc, text, err = _run_fixed([engine, "logs", "--tail", str(APPLIANCE_LOG_LINES),
                                    entry["container"]], APPLIANCE_INSPECT_TIMEOUT_S,
                                   merge=True)
        out: Dict[str, Any] = {"ok": rc == 0, "name": entry["name"],
                               "container": entry["container"], "rc": rc,
                               "logs": _redact(_tail_text(text or err,
                                                          APPLIANCE_LOG_MAX_BYTES))}
        return _fit(out, "logs")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _deploy_argv(entry: Dict[str, str], script: Path) -> Optional[List[str]]:
    """The fixed argv that runs the registered deploy script, or None (no interpreter)."""
    import shutil

    if entry["script"] == "deploy.ps1":
        shell = shutil.which("pwsh") or (shutil.which("powershell") if os.name == "nt" else None)
        if not shell:
            return None
        argv = [shell, "-NoProfile", "-NonInteractive"]
        if os.name == "nt":
            argv += ["-ExecutionPolicy", "Bypass"]
        return argv + ["-File", str(script)]
    bash = _bash()
    return [bash, str(script)] if bash else None


def _bash() -> Optional[str]:
    """bash for deploy.sh. On Windows, never System32's bash.exe: that is the WSL
    launcher, which cannot see a Windows path (measured: "No such file or directory")
    -- Git for Windows' bash, found next to git, is the one deploy.ps1 itself uses."""
    import shutil

    found = shutil.which("bash")
    if os.name != "nt":
        return found
    if found and "\\windows\\" not in found.lower().replace("/", "\\"):
        return found
    git = shutil.which("git")
    if git:
        for cand in (Path(git).parent.parent / "bin" / "bash.exe",
                     Path(git).parent.parent / "usr" / "bin" / "bash.exe"):
            if cand.is_file():
                return str(cand)
    return None


def _redact(text: str) -> str:
    """Log text with credentials scrubbed (adk.log_redact, the admin API's patterns)."""
    from adk.log_redact import redact_text

    return redact_text(text)


def _redeploy_running(name: str, now: float) -> bool:
    """Is a redeploy of ``name`` still in flight? Its worker writes its pid into the
    state file; a live pid inside the timeout window is running, a dead one is not
    (the worker crashed). Before the worker has written its pid, the window alone."""
    st = _read_state(name)
    if st.get("state") != "running":
        return False
    try:
        started = float(st.get("started_at") or 0)
    except (TypeError, ValueError):
        started = 0.0
    if now - started >= APPLIANCE_DEPLOY_TIMEOUT_S + 300:
        return False  # a pid this old may already belong to something else
    pid = st.get("pid")
    return _pid_alive(pid) if pid else True


def _appliance_redeploy(args: Dict[str, str]) -> Dict[str, Any]:
    """``git -C <repo> pull --ff-only``, then the registered deploy script, DETACHED.

    The pull runs here and its exit code is in this result. The deploy (an image build
    and a health wait: many minutes) must not hold the heartbeat that runs this, so it
    starts in its own process, which records the script's exit code and log tail;
    ``appliance-status`` reports them as ``last_redeploy``. Never raises.
    """
    import shutil
    import subprocess

    try:
        entry, why = _appliance(args)
        if entry is None:
            return {"ok": False, "error": why}
        name, repo = entry["name"], entry["repo_dir"]
        if not _is_git_repo(repo):
            return {"ok": False, "name": name, "error": "repo_dir is not a git repository"}
        script = _script_path(entry)
        if script is None:
            return {"ok": False, "name": name,
                    "error": f"deploy/{entry['script']} is missing from repo_dir"}
        git = shutil.which("git")
        if not git:
            return {"ok": False, "name": name, "error": "git not on PATH"}
        if _deploy_argv(entry, script) is None:
            return {"ok": False, "name": name,
                    "error": f"no interpreter for {entry['script']} on PATH"}
        now = time.time()
        if _redeploy_running(name, now):
            return {"ok": False, "name": name, "error": "a redeploy is already running",
                    "last_redeploy": _read_state(name)}
        before = _head_sha(git, repo)
        rc, out, err = _run_fixed([git, "-C", repo, "pull", "--ff-only"],
                                  APPLIANCE_PULL_TIMEOUT_S, env=_git_env())
        pull = {"rc": rc, "before": before, "after": _head_sha(git, repo),
                "tail": _redact((out + err).strip()[-600:])}
        if rc != 0:
            return {"ok": False, "name": name, "pull": pull,
                    "error": "git pull --ff-only failed; the deploy did not run"}
        _write_state(name, {"state": "running", "started_at": int(now),
                            "commit": pull["after"]})
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | _no_window())
        else:
            kw["start_new_session"] = True
        try:
            # cwd is the aither dir, never the repo: `-m` puts the cwd on sys.path, and a
            # repo carrying its own `adk/` would otherwise be imported in its place.
            proc = subprocess.Popen(
                [sys.executable, "-m", "adk.node_commands", _REDEPLOY_WORKER, name],
                cwd=str(_aither_dir()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, **kw)
        except OSError as exc:
            _write_state(name, {"state": "failed", "started_at": int(now), "rc": None,
                                "error": type(exc).__name__})
            return {"ok": False, "name": name, "pull": pull,
                    "error": f"deploy could not start ({type(exc).__name__})"}
        return {"ok": True, "name": name, "pull": pull,
                "deploy": {"started": True, "pid": proc.pid, "script": entry["script"],
                           "timeout_s": APPLIANCE_DEPLOY_TIMEOUT_S},
                "note": "the deploy runs detached; appliance-status reports its exit code"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _redeploy_worker(name: str) -> int:
    """The detached half of appliance-redeploy: run the registered script, record rc.

    Started only by :func:`_appliance_redeploy` with a name that already passed the
    registry; everything is re-validated here because the registry may have changed.
    """
    import subprocess

    if not APPLIANCE_NAME_RE.match(name or ""):
        return 2
    started = int(time.time())
    entry = load_appliances().get(name)
    script = _script_path(entry) if entry else None
    argv = _deploy_argv(entry, script) if entry and script else None
    if entry is None or argv is None:
        _write_state(name, {"state": "failed", "started_at": started, "rc": None,
                            "error": "the appliance, its script or an interpreter is gone"})
        return 2
    env = {**os.environ, "CONTAINER_ENGINE": entry["engine"]}
    rc: Optional[int] = None
    error = ""
    # The script leads its own process group / session, so a timeout kills IT and
    # everything it started, never this worker.
    kw: Dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _no_window()}
        if os.name == "nt" else {"start_new_session": True})
    try:
        _private_state_dir()
        with os.fdopen(_open_private(_log_path(name)), "w", encoding="utf-8",
                       errors="replace") as logf:
            try:
                proc = subprocess.Popen(argv, cwd=entry["repo_dir"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=logf,
                                        stderr=subprocess.STDOUT, **kw)
            except OSError as exc:
                error = type(exc).__name__
            else:
                _write_state(name, {"state": "running", "started_at": started,
                                    "pid": os.getpid(), "script_pid": proc.pid})
                try:
                    rc = proc.wait(timeout=APPLIANCE_DEPLOY_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    error = f"timed out after {APPLIANCE_DEPLOY_TIMEOUT_S}s; {_kill_tree(proc)}"
    except OSError as exc:
        error = f"log not writable ({type(exc).__name__})"
    try:
        tail = _redact(_tail_text(
            _log_path(name).read_text(encoding="utf-8", errors="replace"), 1500))
    except OSError:
        tail = ""
    state: Dict[str, Any] = {"state": "done" if rc == 0 else "failed", "rc": rc,
                             "started_at": started, "finished_at": int(time.time()),
                             "tail": tail}
    if error:
        state["error"] = error
    _write_state(name, state)
    return 0 if rc == 0 else 1


_HANDLERS: Dict[str, Callable[[Dict[str, str]], Any]] = {
    "collect-diagnostics": _diagnostics,
    "update": _update,
    "lend-on": _lend_on,
    "lend-off": _lend_off,
    "restart-lane": _restart_lane,
    "upgrade": _upgrade,
    "advertise-inference": _advertise_inference,
    "upgrade-component": _upgrade_component,
    "appliance-status": _appliance_status,
    "appliance-logs": _appliance_logs,
    "appliance-redeploy": _appliance_redeploy,
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


if __name__ == "__main__":  # the detached appliance-redeploy worker, and nothing else
    if len(sys.argv) == 3 and sys.argv[1] == _REDEPLOY_WORKER:
        sys.exit(_redeploy_worker(sys.argv[2]))
    sys.exit(2)
