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


def wait_for_health(port: int, timeout: float = 60.0) -> bool:
    """Poll ``http://127.0.0.1:<port>/health`` until OUR server answers, or timeout.

    A 200 from a different process on the same port (a llama-server, a dev web
    app) is not health — see is_adk_health.
    """
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3)
            if r.status_code == 200:
                try:
                    if is_adk_health(r.json()):
                        return True
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


def remove_autostart() -> bool:
    """Remove the platform autostart entry. Best-effort; returns success."""
    if sys.platform == "win32":
        rc = subprocess.run(
            ["schtasks", "/delete", "/tn", WINDOWS_TASK_NAME, "/f"],
            capture_output=True, text=True,
        )
        # Also clear the no-admin HKCU Run fallback (whichever was used).
        hkcu = _remove_hkcu_run()
        return rc.returncode == 0 or hkcu
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


def _remove_hkcu_run() -> bool:
    """Remove the HKCU Run autostart entry (for `adk down`). Idempotent."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as key:
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
