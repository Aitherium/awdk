"""Background-agent lifecycle for `adk up` / `adk down` / `adk status`.

Small, self-contained helpers to run a self-hosted agent (aither-serve) and its
Cloudflare tunnel as **detached** background processes that outlive the launching
terminal, plus a status file that is the single source of truth for the companion
commands, plus cross-platform autostart (survives reboot).

Design notes:
- Detach: children are spawned in their own process group / session with stdio
  redirected to log files, so the parent can capture what it needs (health, the
  tunnel URL) and then exit while the children keep running.
- Status: ``~/.aither/adk-up.json`` records pids + wiring. ``adk status`` reads it,
  probes liveness + ``/health``; ``adk down`` reads it to tear everything down.
- Autostart: reuses the same ``schtasks /sc onlogon`` pattern as
  ``llamacpp_setup._install_windows_task`` (Windows), ``systemd --user`` (Linux),
  ``launchd`` (macOS). The task/unit re-runs ``adk up`` at logon; the supervisor
  in the running process (or the OS restart policy) covers crashes.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

AITHER_HOME = Path.home() / ".aither"
LOG_DIR = AITHER_HOME / "logs"
STATUS_PATH = AITHER_HOME / "adk-up.json"

WINDOWS_TASK_NAME = "AitherAgent"
SYSTEMD_UNIT = "aither-agent"
LAUNCHD_LABEL = "com.aitherium.agent"

# Windows process-creation flags (avoid importing subprocess constants that are
# absent on non-Windows): DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
_WIN_DETACHED = 0x00000008 | 0x00000200


# ---------------------------------------------------------------------------
# Paths / status file
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def read_status() -> Optional[dict]:
    """Return the persisted status dict, or None if no agent has been brought up."""
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_status(status: dict) -> None:
    _ensure_dirs()
    try:
        STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear_status() -> None:
    try:
        STATUS_PATH.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def spawn_detached(argv: list[str], log_path: Path, env: Optional[dict] = None) -> int:
    """Spawn a fully detached background process; return its pid.

    stdout+stderr are redirected to ``log_path`` (append). On Windows the process
    is created detached + in a new process group; on POSIX it starts a new session
    so it is not killed when the launching shell exits.
    """
    _ensure_dirs()
    log_f = open(log_path, "ab")  # noqa: SIM115 — handle is owned by the child
    kwargs: dict[str, Any] = {
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env or dict(os.environ),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _WIN_DETACHED
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


def pid_alive(pid: Optional[int]) -> bool:
    """True if a process with ``pid`` is currently running."""
    if not pid:
        return False
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, PermissionError):
        return isinstance(sys.exc_info()[1], PermissionError)
    except OSError:
        return False
    return True


def kill_pid(pid: Optional[int]) -> bool:
    """Terminate a process (and its tree on Windows). Best-effort; returns success."""
    if not pid:
        return False
    if sys.platform == "win32":
        rc = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True, text=True,
        )
        return rc.returncode == 0
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def tail_for_pattern(log_path: Path, pattern: str, timeout: float = 45.0) -> Optional[str]:
    """Poll ``log_path`` until ``pattern`` matches, returning the first match.

    Used to capture the ``trycloudflare.com`` URL a detached tunnel writes to its
    log. Returns None on timeout.
    """
    rx = re.compile(pattern)
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if len(text) > seen:
            m = rx.search(text)
            if m:
                return m.group(0)
            seen = len(text)
        time.sleep(1.0)
    return None


def is_adk_health(body) -> bool:
    """True only for OUR server's /health body.

    llama-server (install-bonsai.sh, :8080) answers ``{"status":"ok"}`` on
    /health too. Until 2026-09-12 `adk up --port 8080` on such a box polled
    that, got 200, printed "Agent healthy on :8080" — and the uvicorn child
    had already died on EADDRINUSE. Our body carries ``"agent"`` and
    ``"version"`` (adk/server.py health()); require them.
    """
    return isinstance(body, dict) and "agent" in body and "version" in body


def port_owner(port: int) -> Optional[str]:
    """Who answers ``/health`` on this loopback port: 'adk', 'other', or None (free)."""
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
    except (httpx.HTTPError, OSError):
        return None
    try:
        body = r.json()
    except ValueError:
        body = None
    return "adk" if is_adk_health(body) else "other"


def is_our_health(body, not_before: Optional[float]) -> bool:
    """True only for an adk /health body from a server started at/after ``not_before``.

    is_adk_health() cannot tell OUR daemon from ANOTHER adk daemon on the same
    port (measured 2026-08-26: a WSL-side adk behind wslrelay held :8080, the freshly spawned
    aither-serve lost the bind, and `adk up`/`adk status` verified the foreign
    one). adk/server.py reports ``started_at`` (its import-time wall clock), so a
    body whose server started BEFORE we spawned ours is not ours. A body with no
    usable ``started_at`` cannot prove it is ours and is refused when a floor is
    given. 2 s of slack absorbs clock granularity between parent and child.
    """
    if not is_adk_health(body):
        return False
    if not_before is None:
        return True
    started = body.get("started_at")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return False
    return float(started) >= float(not_before) - 2.0


def wait_for_health(port: int, timeout: float = 60.0, *,
                    pid: Optional[int] = None,
                    not_before: Optional[float] = None) -> bool:
    """Poll ``http://127.0.0.1:<port>/health`` until OUR server answers, or timeout.

    A 200 from a different process on the same port (a llama-server, a dev web
    app) is not health — see is_adk_health. With ``not_before`` a 200 from a
    DIFFERENT adk daemon (one started before ours) is not health either — see
    is_our_health. With ``pid``, a spawned child that has exited (e.g. it lost
    the bind to a foreign port owner) fails fast instead of being "healthy"
    through whoever else answers the port.
    """
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid is not None and not pid_alive(pid):
            return False
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3)
            if r.status_code == 200:
                try:
                    if is_our_health(r.json(), not_before):
                        return pid is None or pid_alive(pid)
                except ValueError:
                    pass
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(2.0)
    return False


# ---------------------------------------------------------------------------
# Autostart (survives reboot)
# ---------------------------------------------------------------------------

def install_autostart(up_argv: list[str], dry_run: bool = False) -> Optional[str]:
    """Install a platform autostart entry that runs ``up_argv`` at logon.

    Returns a short identifier (e.g. ``windows-task:AitherAgent``) on success, or
    None if autostart could not be installed. Idempotent.
    """
    _ensure_dirs()
    if sys.platform == "win32":
        return _install_windows_task(up_argv, dry_run)
    if sys.platform == "darwin":
        return _install_launchd(up_argv, dry_run)
    return _install_systemd_user(up_argv, dry_run)


_WRAPPER_RE = re.compile(r'([A-Za-z]:[\\/][^"<>|*?\r\n]*?aither-agent\.cmd)', re.I)


def _hkcu_run_value() -> str:
    """The HKCU Run value named :data:`WINDOWS_TASK_NAME` ('' when absent)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_QUERY_VALUE,
        ) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_TASK_NAME)
        return str(value or "")
    except (OSError, ImportError):
        return ""


def _foreign_owner(command: str, wrapper: str) -> str:
    """The OTHER agent home's wrapper an entry launches, when that wrapper still
    exists on disk ('' otherwise: an entry whose wrapper is gone is stale, not owned)."""
    if not command or _names_wrapper(command, wrapper):
        return ""
    m = _WRAPPER_RE.search(command)
    return m.group(1) if m and Path(m.group(1)).exists() else ""


def autostart_state() -> dict:
    """The logon entry as the OS has it NOW -- never what adk-up.json remembers.

    ``{"state": "present" | "missing" | "other-home", "entry": <id or None>,
    "owner": <the other home's wrapper, for other-home>}``. ``present`` means an
    entry exists AND launches THIS home's wrapper; the task name and the Run value
    are per Windows user, so an entry that launches another home's wrapper is that
    home's (``other-home``) and is neither ours to count nor ours to replace.
    """
    if sys.platform == "win32":
        wrapper = str(AITHER_HOME / "aither-agent.cmd")
        task = subprocess.run(
            ["schtasks", "/query", "/tn", WINDOWS_TASK_NAME, "/xml"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        task_xml = task.stdout if task.returncode == 0 else ""
        run_value = _hkcu_run_value()
        if task_xml and _names_wrapper(task_xml, wrapper):
            return {"state": "present", "entry": f"windows-task:{WINDOWS_TASK_NAME}"}
        if run_value and _names_wrapper(run_value, wrapper):
            return {"state": "present", "entry": f"hkcu-run:{WINDOWS_TASK_NAME}"}
        owner = _foreign_owner(task_xml, wrapper) or _foreign_owner(run_value, wrapper)
        if owner:
            return {"state": "other-home", "entry": None, "owner": owner}
        return {"state": "missing", "entry": None}
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return ({"state": "present", "entry": f"launchd:{LAUNCHD_LABEL}"} if plist.exists()
                else {"state": "missing", "entry": None})
    unit = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"
    return ({"state": "present", "entry": f"systemd-user:{SYSTEMD_UNIT}"} if unit.exists()
            else {"state": "missing", "entry": None})


def ensure_autostart(up_argv: list[str], refresh: bool = False) -> dict:
    """Make the logon entry exist, then report what the OS actually has.

    ``refresh`` (a fresh ``adk up``) rewrites the entry even when it is there, so a
    changed port or identity takes effect. Without it (``adk up`` on an agent that
    is already running) an entry that is present is left exactly as it is, and one
    that is MISSING is put back -- from the wrapper already on disk on Windows, so
    the command the running agent was started with is the one that comes back.
    An entry that belongs to another agent home is never replaced.

    Returns :func:`autostart_state` after the fact, plus ``"reinstalled": True``
    when this call had to put a missing entry back.
    """
    before = autostart_state()
    if before["state"] == "other-home" or (before["state"] == "present" and not refresh):
        return before
    wrapper = AITHER_HOME / "aither-agent.cmd"
    if sys.platform == "win32" and not refresh and wrapper.exists():
        _ensure_dirs()
        _register_windows_entry(wrapper, dry_run=False)
    else:
        install_autostart(up_argv)
    after = autostart_state()
    if before["state"] == "missing" and after["state"] == "present" and not refresh:
        after["reinstalled"] = True
    return after


def remove_autostart() -> bool:
    """Remove the platform autostart entry. Best-effort; returns success."""
    if sys.platform == "win32":
        # The task name and the Run value are per Windows USER, not per agent home:
        # `adk down` under another AITHER_HOME (a test with a fake HOME, a second
        # install) used to delete the real agent's logon entry (2026-10-01). Only an
        # entry whose command names THIS home's wrapper is ours to remove.
        wrapper = str(AITHER_HOME / "aither-agent.cmd")
        removed = False
        task = subprocess.run(
            ["schtasks", "/query", "/tn", WINDOWS_TASK_NAME, "/xml"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if task.returncode == 0:
            if _names_wrapper(task.stdout, wrapper):
                rc = subprocess.run(
                    ["schtasks", "/delete", "/tn", WINDOWS_TASK_NAME, "/f"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                removed = rc.returncode == 0
            else:
                print(f"  [i] Scheduled task {WINDOWS_TASK_NAME} belongs to another agent "
                      f"home; left in place.", file=sys.stderr)
        # Also clear the no-admin HKCU Run fallback (whichever was used).
        hkcu = _remove_hkcu_run(wrapper)
        return removed or hkcu
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        try:
            plist.unlink()
            return True
        except OSError:
            return False
    unit = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.service"],
        capture_output=True,
    )
    try:
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return True
    except OSError:
        return False


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def _install_windows_task(up_argv: list[str], dry_run: bool) -> Optional[str]:
    """Windows Task Scheduler entry (runs at user logon, restarts on failure).

    Mirrors ``llamacpp_setup._install_windows_task``: a wrapper ``.cmd`` captures
    logs, then ``schtasks`` registers it. Falls back to a detached launch (no
    reboot persistence) if ``schtasks`` is unavailable.
    """
    wrapper = AITHER_HOME / "aither-agent.cmd"
    log_out = LOG_DIR / "agent.log"
    cmd_line = " ".join(_quote(c) for c in up_argv)
    wrapper.write_text(
        f'@echo off\r\ncd /d "%~dp0"\r\n{cmd_line} 1>>"{log_out}" 2>&1\r\n',
        encoding="utf-8",
    )
    # Route through the GUI-subsystem shim. An interactive-logon task pointed at a
    # console payload makes Task Scheduler open a console ON THE DESKTOP that TAKES
    # FOCUS, and the agent loop runs indefinitely — so this is a window that sits
    # there eating keystrokes, not a flash. Imported from llamacpp_setup rather than
    # re-implemented: this function used to say it "mirrors" that one, and mirroring
    # is exactly how the defect got here.
    return _register_windows_entry(wrapper, dry_run)


def _register_windows_entry(wrapper: Path, dry_run: bool) -> Optional[str]:
    """Register ``wrapper`` to run at logon: the scheduled task, else the HKCU Run
    value. The wrapper is not rewritten here."""
    from adk.llamacpp_setup import hidden_task_run, write_hidden_launch_shim

    shim = write_hidden_launch_shim(AITHER_HOME)
    task_run = hidden_task_run(shim, wrapper)
    if dry_run:
        print(f"  [DRY] would register scheduled task {WINDOWS_TASK_NAME} -> {wrapper}")
        print(f"  [DRY]   /tr {task_run}")
        return f"windows-task:{WINDOWS_TASK_NAME}"
    rc = subprocess.run(
        ["schtasks", "/create", "/f", "/tn", WINDOWS_TASK_NAME,
         "/tr", task_run, "/sc", "onlogon", "/rl", "limited"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if rc.returncode != 0:
        # schtasks can require elevation on some machines ("Access is denied").
        # Fall back to the per-user Run key (HKCU) — logon autostart with NO admin
        # needed — and tell the user how to get the richer system task if they want it.
        if _install_hkcu_run(wrapper):
            print("  [+] Autostart set via HKCU Run key (per-user logon, no admin needed).")
            print("      For a system task with restart-on-failure, re-run `adk up` from an")
            print("      elevated terminal (right-click -> Run as administrator).")
            return f"hkcu-run:{WINDOWS_TASK_NAME}"
        print(f"  WARN: schtasks failed ({rc.stderr.strip()}) and the HKCU fallback failed; "
              "agent will not auto-start on reboot. Re-run `adk up` as administrator to enable it.",
              file=sys.stderr)
        return None
    return f"windows-task:{WINDOWS_TASK_NAME}"


def _install_hkcu_run(wrapper: Path) -> bool:
    """Per-user logon autostart via the HKCU Run key (no admin required).

    The Run key has the SAME console-window problem as the scheduled task, and it
    is easier to miss because it is the fallback path — the one that runs on
    machines where schtasks was denied, i.e. exactly the unattended ones nobody
    is watching. It gets the same shim.
    """
    from adk.llamacpp_setup import write_hidden_launch_shim

    try:
        import winreg

        shim = write_hidden_launch_shim(AITHER_HOME)
        value = f'wscript.exe //B //Nologo "{shim}" "{wrapper}"'
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, WINDOWS_TASK_NAME, 0, winreg.REG_SZ, value)
        return True
    except OSError:
        return False


def _names_wrapper(command: str, wrapper: str) -> bool:
    """True when an autostart command (task XML or Run value) launches ``wrapper``."""
    norm = lambda s: s.replace("/", "\\").lower()  # noqa: E731
    return norm(wrapper) in norm(command or "")


def _remove_hkcu_run(wrapper: Optional[str] = None) -> bool:
    """Remove the HKCU Run autostart entry (for `adk down`). Idempotent.

    With ``wrapper``, only a value that launches that wrapper is removed: the value
    name is shared by every agent home of this Windows user.
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            if wrapper is not None:
                value, _ = winreg.QueryValueEx(key, WINDOWS_TASK_NAME)
                if not _names_wrapper(str(value), wrapper):
                    return False
            winreg.DeleteValue(key, WINDOWS_TASK_NAME)
        return True
    except OSError:
        return False


def _install_systemd_user(up_argv: list[str], dry_run: bool) -> Optional[str]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / f"{SYSTEMD_UNIT}.service"
    exec_line = " ".join(_quote(c) for c in up_argv)
    unit = (
        "[Unit]\nDescription=AitherOS self-hosted agent\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={exec_line}\nRestart=on-failure\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if dry_run:
        print(f"  [DRY] would write systemd unit: {unit_path}")
        return f"systemd:{SYSTEMD_UNIT}"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    rc = subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.service"],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        print(f"  WARN: systemctl enable failed ({rc.stderr.strip()}).", file=sys.stderr)
        return None
    return f"systemd:{SYSTEMD_UNIT}"


def _install_launchd(up_argv: list[str], dry_run: bool) -> Optional[str]:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{LAUNCHD_LABEL}.plist"
    args_xml = "".join(f"    <string>{a}</string>\n" for a in up_argv)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{args_xml}  </array>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n</dict>\n</plist>\n"
    )
    if dry_run:
        print(f"  [DRY] would write launchd plist: {plist_path}")
        return f"launchd:{LAUNCHD_LABEL}"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    return f"launchd:{LAUNCHD_LABEL}"


# ---------------------------------------------------------------------------
# Named user autostart (any long-running adk command, e.g. `adk home serve`)
# ---------------------------------------------------------------------------
#
# The AitherAgent entry above is one fixed name routed through ``run-hidden.vbs``.
# Two reasons the named helper below does NOT reuse that shim:
#   1. wscript.exe + the .vbs returns 127 when Task Scheduler launches it, so the
#      task records a failure and never restarts the payload.
#   2. A console-subsystem python launched from a logon task opens a console ON
#      THE DESKTOP that takes focus (see llamacpp_setup._install_windows_task).
# So Windows runs ``pythonw.exe <launcher>.pyw``: pythonw is GUI-subsystem (no
# console is ever allocated) and the launcher starts the real command with
# CREATE_NO_WINDOW -- a pythonw child WITHOUT that flag still opens a console
# per child, which is the same defect one level down.

#: schtasks rejects the whole create when /tr exceeds this (measured, undocumented).
SCHTASKS_TR_MAX = 261

_LAUNCHER_TEMPLATE = '''\
# Generated by adk.agent_daemon.install_user_autostart -- rewritten on every
# install; do not edit. Runs the command below with no console window and
# appends its output to the log; the task's exit code is the command's.
import os
import subprocess
import sys

ARGV = {argv!r}
LOG = {log!r}
ENV = {env!r}


def main():
    env = dict(os.environ)
    env.update(ENV)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "ab") as log:
        rc = subprocess.call(ARGV, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, creationflags=flags, env=env)
    sys.exit(rc)


if __name__ == "__main__":
    main()
'''


def _safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        raise ValueError(f"autostart name {name!r}: letters, digits, '.', '_' or '-' only")
    return name


def pythonw_executable(python: Optional[str] = None) -> Optional[Path]:
    """``pythonw.exe`` beside ``python`` (default: this interpreter), or None."""
    exe = Path(python or sys.executable)
    cand = exe.with_name("pythonw.exe")
    return cand if cand.is_file() else None


def launcher_path(name: str) -> Path:
    return AITHER_HOME / f"{_safe_name(name)}-launch.pyw"


def write_pythonw_launcher(name: str, argv: list[str], log_path: Path,
                           env: Optional[dict] = None) -> Path:
    """Write the no-window launcher for ``argv`` and return its path."""
    path = launcher_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_LAUNCHER_TEMPLATE.format(argv=list(argv), log=str(log_path),
                                              env=dict(env or {})),
                    encoding="utf-8")
    return path


def systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def launchd_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def install_user_autostart(name: str, argv: list[str], *, description: str = "",
                           env: Optional[dict] = None, dry_run: bool = False,
                           platform: Optional[str] = None) -> Optional[str]:
    """Run ``argv`` at user logon, restarted on failure; returns an id or None.

    Windows: a Task Scheduler ``onlogon`` task named ``name`` running
    ``pythonw.exe <name>-launch.pyw`` (no console, no .vbs). POSIX: a
    ``systemd --user`` unit ``<name>.service``; macOS: a launchd agent
    ``com.aitherium.<name>``. ``env`` is for NON-SECRET settings only (a path, a
    port) -- it is written into the task/unit in plain text. Idempotent.
    """
    _safe_name(name)
    _ensure_dirs()
    plat = platform or sys.platform
    if plat == "win32":
        return _install_named_windows(name, argv, env, dry_run)
    if plat == "darwin":
        return _install_named_launchd(name, argv, env, dry_run)
    return _install_named_systemd(name, argv, description, env, dry_run)


def remove_user_autostart(name: str, *, platform: Optional[str] = None) -> bool:
    """Remove what :func:`install_user_autostart` installed. True if anything was
    removed or nothing was installed; False only when a removal failed."""
    _safe_name(name)
    plat = platform or sys.platform
    if plat == "win32":
        rc = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
        task_gone = rc.returncode == 0 or not _windows_task_exists(name)
        run_gone = _del_hkcu_run_value(name)
        launcher_path(name).unlink(missing_ok=True)
        return task_gone and run_gone
    if plat == "darwin":
        plist = launchd_agents_dir() / f"com.aitherium.{name}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
        return not plist.exists()
    unit = systemd_user_dir() / f"{name}.service"
    if unit.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.service"],
                       capture_output=True)
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return not unit.exists()


def _windows_task_exists(name: str) -> bool:
    rc = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    return rc.returncode == 0


def _hkcu_run_key(access: int):
    import winreg

    return winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Microsoft\Windows\CurrentVersion\Run", 0, access)


def _set_hkcu_run_value(name: str, value: str) -> bool:
    try:
        import winreg

        with _hkcu_run_key(winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        return True
    except OSError as exc:
        print(f"  WARN: HKCU Run fallback failed: {exc}", file=sys.stderr)
        return False


def _del_hkcu_run_value(name: str) -> bool:
    """True when the value is gone (deleted now, or never there)."""
    try:
        import winreg
    except ImportError:
        return True
    try:
        with _hkcu_run_key(winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"  WARN: could not remove HKCU Run value {name}: {exc}", file=sys.stderr)
        return False


def _install_named_windows(name: str, argv: list[str], env: Optional[dict],
                           dry_run: bool) -> Optional[str]:
    pyw = pythonw_executable()
    if pyw is None:
        # Falling back to python.exe would put a focus-stealing console on the
        # desktop at every logon; refuse instead.
        print(f"  ERROR: no pythonw.exe beside {sys.executable}; autostart not installed.",
              file=sys.stderr)
        return None
    launcher = write_pythonw_launcher(name, argv, LOG_DIR / f"{name}.log", env)
    task_run = f'"{pyw}" "{launcher}"'
    if len(task_run) > SCHTASKS_TR_MAX:
        print(f"  ERROR: schtasks /tr would be {len(task_run)} chars (max "
              f"{SCHTASKS_TR_MAX}); shorten the install path.", file=sys.stderr)
        return None
    if dry_run:
        print(f"  [DRY] would register scheduled task {name}: /tr {task_run}")
        return f"windows-task:{name}"
    rc = subprocess.run(
        ["schtasks", "/create", "/f", "/tn", name, "/tr", task_run,
         "/sc", "onlogon", "/rl", "limited"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if rc.returncode == 0:
        return f"windows-task:{name}"
    # schtasks can need elevation on some machines; the per-user Run key needs none.
    if _set_hkcu_run_value(name, task_run):
        print("  [+] Autostart set via the HKCU Run key (per-user logon, no admin needed).")
        return f"hkcu-run:{name}"
    print(f"  WARN: schtasks failed ({(rc.stderr or '').strip()}); not installed.",
          file=sys.stderr)
    return None


def _install_named_systemd(name: str, argv: list[str], description: str,
                           env: Optional[dict], dry_run: bool) -> Optional[str]:
    unit_path = systemd_user_dir() / f"{name}.service"
    env_lines = "".join(f'Environment="{k}={v}"\n' for k, v in (env or {}).items())
    unit = (
        f"[Unit]\nDescription={description or name}\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"{env_lines}"
        f"ExecStart={' '.join(_quote(c) for c in argv)}\n"
        "Restart=on-failure\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if dry_run:
        print(f"  [DRY] would write systemd unit: {unit_path}")
        return f"systemd:{name}"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")
    # No systemctl (a container, WSL without systemd, Alpine, a Chromebook's Linux):
    # there is nothing to register with. Report it and return None, so the caller says
    # "runs until this computer restarts" instead of dying with a traceback mid-setup.
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        rc = subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.service"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  WARN: start-at-login could not be installed: systemctl is not usable "
              f"here ({type(exc).__name__}). The unit is at {unit_path}.", file=sys.stderr)
        return None
    if rc.returncode != 0:
        print(f"  WARN: systemctl --user enable failed ({(rc.stderr or '').strip()}); "
              f"the unit is at {unit_path}.", file=sys.stderr)
        return None
    return f"systemd:{name}"


def _install_named_launchd(name: str, argv: list[str], env: Optional[dict],
                           dry_run: bool) -> Optional[str]:
    from xml.sax.saxutils import escape

    label = f"com.aitherium.{name}"
    plist_path = launchd_agents_dir() / f"{label}.plist"
    args_xml = "".join(f"    <string>{escape(a)}</string>\n" for a in argv)
    env_xml = ""
    if env:
        pairs = "".join(f"    <key>{escape(k)}</key>\n    <string>{escape(str(v))}</string>\n"
                        for k, v in env.items())
        env_xml = f"  <key>EnvironmentVariables</key>\n  <dict>\n{pairs}  </dict>\n"
    log = escape(str(LOG_DIR / f"{name}.log"))
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{label}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{args_xml}  </array>\n"
        f"{env_xml}"
        f"  <key>StandardOutPath</key>\n  <string>{log}</string>\n"
        f"  <key>StandardErrorPath</key>\n  <string>{log}</string>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n</dict>\n</plist>\n"
    )
    if dry_run:
        print(f"  [DRY] would write launchd plist: {plist_path}")
        return f"launchd:{label}"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    rc = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    if rc.returncode != 0:
        print(f"  WARN: launchctl load failed ({(rc.stderr or '').strip()}).",
              file=sys.stderr)
        return None
    return f"launchd:{label}"
