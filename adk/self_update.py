"""Code-update detection for long-running awdk daemons.

WHY (measured 2026-10-02): the awsh harness daemon (:8362) ran for a day from a
snapshot directory that ``refresh_agent_tools.py`` had since deleted. Its handlers
import lazily, so ``/health`` stayed 200 while ``/agents`` and ``/workforce``
raised ``ModuleNotFoundError`` and the OS showed a 500. Nothing noticed: a daemon
had no idea which code it was running, or that newer code was installed.

This module gives every daemon three things:

* ``register(name)`` writes ``~/.aither/run/<name>.json`` (pid, the code root it
  imported, its argv) so an updater can see which snapshots are LIVE and must not
  be deleted, and removes it at exit.
* ``status()`` answers, for ``/health``: where the running code came from, where
  ``import adk`` resolves NOW in a fresh interpreter, whether those differ, and
  whether the running code's directory still exists.
* ``UpdateWatcher`` checks that every ``interval`` seconds. When newer code is
  installed (or the running code was deleted) and the daemon is idle, it starts
  the same command line again in a detached helper that waits for this process
  to exit, then asks this process to stop. A busy daemon is never restarted
  under a live session; it reports ``pending`` until it is idle.

``AITHER_DAEMON_AUTO_UPDATE=0`` turns the restart off (detection still reports).
The host supervisors (the AitherOS-HarnessDaemon / AdkDaemonWatchdog tasks) remain
the fallback: if the relaunch fails they start the daemon on their next tick.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("adk.self_update")

#: The checkout or snapshot this process imported ``adk`` from (the dir holding ``adk/``).
RUNNING_ROOT = Path(__file__).resolve().parent.parent

_STATE: dict[str, Any] = {
    "checked_at": None,
    "installed_root": None,
    "error": "",
    "pending": False,
    "relaunch": None,
}


def run_dir() -> Path:
    base = Path(os.environ.get("AITHER_HOME") or (Path.home() / ".aither"))
    return base / "run"


def _same(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def installed_root(timeout: float = 20.0) -> tuple[Optional[Path], str]:
    """Where ``import adk`` resolves for a NEW interpreter: ``(root, error)``.

    Asked of a subprocess on purpose: this process's ``sys.modules`` will always
    say the old location. Run from the home directory so a checkout in the cwd
    cannot shadow the installed package.
    """
    code = "import adk, os; print(os.path.dirname(os.path.dirname(os.path.abspath(adk.__file__))))"
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(Path.home()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is a reason, never a crash
        return None, f"probe failed: {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return None, f"probe failed: {(out.stderr or '').strip().splitlines()[-1:] or 'no output'}"
    line = (out.stdout or "").strip().splitlines()
    return (Path(line[-1]) if line else None), ""


def check(probe: Callable[[], tuple[Optional[Path], str]] = installed_root) -> dict[str, Any]:
    root, err = probe()
    _STATE.update(checked_at=time.time(), installed_root=str(root) if root else None, error=err)
    return status()


def status() -> dict[str, Any]:
    """The update picture for ``/health``. Reads only cached state."""
    installed = Path(_STATE["installed_root"]) if _STATE["installed_root"] else None
    missing = not (RUNNING_ROOT / "adk").is_dir()
    return {
        "running_from": str(RUNNING_ROOT),
        "running_code_missing": missing,
        "installed_at": _STATE["installed_root"],
        "update_available": bool(installed) and not _same(installed, RUNNING_ROOT),
        "checked_at": _STATE["checked_at"],
        "check_error": _STATE["error"],
        "restart_pending": _STATE["pending"],
        "auto_restart": auto_enabled(),
        "last_relaunch": _STATE["relaunch"],
    }


def needs_restart(st: Optional[dict[str, Any]] = None) -> bool:
    st = st or status()
    return bool(st["update_available"] or st["running_code_missing"])


def auto_enabled() -> bool:
    return os.environ.get("AITHER_DAEMON_AUTO_UPDATE", "1").strip().lower() not in ("0", "false", "no", "off")


# ── which snapshots are live ────────────────────────────────────────────────

def register(name: str) -> Path:
    """Record this daemon (pid, code root, argv). Removed at exit; never raises."""
    import atexit

    path = run_dir() / f"{name}.json"
    record = {
        "name": name, "pid": os.getpid(), "code_root": str(RUNNING_ROOT),
        "argv": relaunch_argv(), "cwd": os.getcwd(), "started_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("self_update: could not write %s: %s", path, exc)
        return path

    def _clear() -> None:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass

    atexit.register(_clear)
    return path


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_code_roots(directory: Optional[Path] = None) -> list[Path]:
    """Code roots of daemons that are still running. Stale records are ignored."""
    out: list[Path] = []
    for f in sorted((directory or run_dir()).glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if pid_alive(int(rec.get("pid") or 0)) and rec.get("code_root"):
            out.append(Path(rec["code_root"]))
    return out


# ── relaunch ────────────────────────────────────────────────────────────────

def relaunch_argv() -> list[str]:
    """This process's command line, with the CURRENT interpreter path."""
    orig = list(getattr(sys, "orig_argv", []) or [])
    if len(orig) > 1:
        return [sys.executable, *orig[1:]]
    return [sys.executable, *sys.argv]


_HELPER = r"""
import json, os, subprocess, sys, time
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
from adk.self_update import pid_alive
deadline = time.time() + 120
while pid_alive(spec["pid"]) and time.time() < deadline:
    time.sleep(0.5)
time.sleep(1.0)
log = open(spec["log"], "ab")
flags = 0
if os.name == "nt":
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                 creationflags=flags, close_fds=True)
"""


def spawn_relaunch(name: str, argv: Optional[list[str]] = None) -> dict[str, Any]:
    """Start a detached helper that relaunches ``argv`` once this pid has exited."""
    logs = run_dir().parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    spec = {
        "pid": os.getpid(), "argv": argv or relaunch_argv(), "cwd": os.getcwd(),
        "log": str(logs / f"{name}.log"), "root": str(RUNNING_ROOT),
    }
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER, json.dumps(spec)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, start_new_session=(os.name != "nt"),
    )
    return {"helper_pid": proc.pid, "argv": spec["argv"], "log": spec["log"], "at": time.time()}


def request_exit(grace_s: float = 30.0) -> None:
    """Ask the server to stop the way Ctrl-C would; force it after ``grace_s``."""
    try:
        signal.raise_signal(signal.SIGINT)
    except Exception as exc:  # noqa: BLE001
        log.warning("self_update: SIGINT failed (%s); exiting hard", exc)
        os._exit(0)
    t = threading.Timer(grace_s, lambda: os._exit(0))
    t.daemon = True
    t.start()


class UpdateWatcher:
    """Background check: relaunch on new code when idle, else report pending."""

    def __init__(
        self, name: str, is_idle: Callable[[], bool], interval: float = 120.0,
        probe: Callable[[], tuple[Optional[Path], str]] = installed_root,
        relaunch: Callable[[str], dict[str, Any]] = spawn_relaunch,
        stop: Callable[[], None] = request_exit,
    ) -> None:
        self.name, self.is_idle, self.interval = name, is_idle, interval
        self._probe, self._relaunch, self._stop = probe, relaunch, stop
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()

    def tick(self) -> str:
        """One check. Returns what it did: current | pending | busy | restarting | report-only."""
        st = check(self._probe)
        if not needs_restart(st):
            _STATE["pending"] = False
            return "current"
        _STATE["pending"] = True
        if not auto_enabled():
            return "report-only"
        try:
            idle = bool(self.is_idle())
        except Exception:  # noqa: BLE001 - unknown is not idle
            idle = False
        if not idle:
            return "busy"
        log.warning("self_update: %s restarting onto %s (was %s)",
                    self.name, st["installed_at"], st["running_from"])
        try:
            _STATE["relaunch"] = self._relaunch(self.name)
        except Exception as exc:  # noqa: BLE001 - the supervisor task is the fallback
            log.warning("self_update: relaunch helper failed (%s); supervisor will restart", exc)
            _STATE["relaunch"] = {"error": str(exc), "at": time.time()}
        self._stop()
        return "restarting"

    def _run(self) -> None:
        self._halt.wait(min(30.0, self.interval))  # let the server finish booting
        while not self._halt.is_set():
            try:
                if self.tick() == "restarting":
                    return
            except Exception as exc:  # noqa: BLE001 - a watcher never kills its daemon
                log.warning("self_update: check failed: %s", exc)
            self._halt.wait(self.interval)

    def start(self) -> "UpdateWatcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"self-update-{self.name}", daemon=True)
            self._thread.start()
        return self

    def halt(self) -> None:
        self._halt.set()


def start(name: str, is_idle: Callable[[], bool], interval: Optional[float] = None) -> UpdateWatcher:
    """Register this daemon and start its watcher. Never raises."""
    register(name)
    every = interval or float(os.environ.get("AITHER_DAEMON_UPDATE_CHECK_S", "120") or 120)
    return UpdateWatcher(name, is_idle, interval=every).start()
