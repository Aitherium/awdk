"""Code-update detection for long-running awdk daemons, adopting VALIDATED code only.

WHY (measured 2026-10-02): the awsh harness daemon (:8362) ran for a day from a
snapshot directory that ``refresh_agent_tools.py`` had since deleted. Its handlers
import lazily, so ``/health`` stayed 200 while ``/agents`` and ``/workforce``
raised ``ModuleNotFoundError`` and the OS showed a 500. Nothing noticed: a daemon
had no idea which code it was running, or that newer code was installed.

This module gives every daemon:

* ``register(name)`` writes ``~/.aither/run/<name>.json`` (pid, the code root it
  imported, its argv) so an updater can see which snapshots are LIVE and must not
  be deleted, and removes it at exit.
* ``status()`` answers, for ``/health``: where the running code came from, where
  ``import adk`` resolves NOW in a fresh interpreter, whether those differ, whether
  the running code's directory still exists, and whether the new code may be adopted.
* ``UpdateWatcher`` checks that every ``interval`` seconds and restarts the daemon
  onto newer code when the daemon is idle AND the code passed every gate below.

Code is only ADOPTED when it is validated (2026-10-03, owner: "how do we make sure
it's safe validated code"). Three gates, each of which can only refuse:

1. **Install** (``refresh_agent_tools.py``) runs the daemon test suites against the
   snapshot before installing it and stamps the verdict into its
   ``.aither-snapshot.json`` marker (``validated.ok``, the commit, the suites).
2. **Adopt** (this module): the installed code's marker must say ``validated.ok``,
   and a PREFLIGHT in a fresh interpreter must build the daemon's app from the NEW
   code. Unvalidated code (an editable dev checkout, a snapshot whose tests failed)
   is reported, never adopted, unless ``AITHER_DAEMON_ADOPT_UNVALIDATED=1``.
3. **Health + rollback** (the relaunch helper): the relaunched daemon must answer
   ``/health`` 200 within 90 s. Otherwise the helper stops it, reinstalls the code
   that was running, starts that again and records the bad root, which this module
   then refuses to adopt.

A busy daemon is never restarted under a live session; it reports
``restart_pending``.

**Automatic restart is OPT-IN** (2026-10-03, owner: "users should be able to or have
to opt in to automatic updates"). Detection and the ``/health`` report are always
on; restarting onto new code happens only after the person running the daemon says
so, with ``adk autoupdate on`` (stored in ``~/.aither/updates.json``) or
``AITHER_DAEMON_AUTO_UPDATE=1`` (the environment wins either way, so a supervisor
can pin it). Until then ``/health`` says an update is ready and how to opt in, and
``adk autoupdate apply`` runs the same validated, health-gated restart once. The host supervisors (the AitherOS-HarnessDaemon /
AdkDaemonWatchdog tasks) remain the fallback if a relaunch fails outright.
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

#: The snapshot marker refresh_agent_tools.py writes (and stamps with the validation verdict).
MARKER = ".aither-snapshot.json"

#: Per daemon: proof that the NEW code can build the daemon, run in a fresh interpreter.
PREFLIGHTS: dict[str, str] = {
    "harness-daemon": (
        "from fastapi.testclient import TestClient\n"
        "from adk.harnesses.daemon import create_app\n"
        "r = TestClient(create_app(token='preflight')).get('/health')\n"
        "assert r.status_code == 200, r.status_code\n"
    ),
    "adk-daemon": (
        "import adk.server as s\n"
        "app = s.create_app()\n"
        "assert any(getattr(r, 'path', '') == '/health' for r in app.routes), 'no /health'\n"
    ),
}

_STATE: dict[str, Any] = {
    "checked_at": None,
    "installed_root": None,
    "error": "",
    "pending": False,
    "relaunch": None,
    "adoptable": None,
    "refused_because": "",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = (_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008) if os.name == "nt" else 0


def run_dir() -> Path:
    base = Path(os.environ.get("AITHER_HOME") or (Path.home() / ".aither"))
    return base / "run"


def _norm(p: Any) -> str:
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def _same(a: Optional[Path], b: Optional[Path]) -> bool:
    return a is not None and b is not None and _norm(a) == _norm(b)


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
            timeout=timeout, cwd=str(Path.home()), creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is a reason, never a crash
        return None, f"probe failed: {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return None, f"probe failed: {tail[0][:200]}"
    line = (out.stdout or "").strip().splitlines()
    return (Path(line[-1]) if line else None), ""


# ── the adopt gate ──────────────────────────────────────────────────────────

def marker_of(root: Optional[Path]) -> dict[str, Any]:
    if root is None:
        return {}
    try:
        return json.loads((Path(root) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def adopt_unvalidated() -> bool:
    return os.environ.get("AITHER_DAEMON_ADOPT_UNVALIDATED", "").strip().lower() in ("1", "true", "yes", "on")


def refused_roots(name: str) -> set[str]:
    """Roots a relaunch already rolled back from (they failed /health)."""
    try:
        rec = json.loads((run_dir() / f"{name}.rollback.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {_norm(r) for r in rec.get("refused", [])}


def validation_of(root: Optional[Path], name: str = "") -> tuple[bool, str]:
    """May this daemon adopt ``root``? ``(ok, why)``. Reads only; the preflight is separate."""
    if root is None:
        return False, "no installed code found"
    if name and _norm(root) in refused_roots(name):
        return False, "rolled back once already: it failed /health after a relaunch"
    v = marker_of(root).get("validated") or {}
    if v.get("ok") is True:
        return True, f"validated {str(v.get('commit', ''))[:10]} {v.get('summary', '')}".strip()
    if adopt_unvalidated():
        return True, "unvalidated, adopted because AITHER_DAEMON_ADOPT_UNVALIDATED=1"
    if v:
        return False, f"its validation failed: {v.get('summary') or v.get('detail', '')}"[:300]
    return False, "not validated: no passing verdict in its .aither-snapshot.json"


def preflight(name: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Build the daemon from the INSTALLED code in a fresh interpreter: ``(ok, detail)``."""
    code = PREFLIGHTS.get(name)
    if not code:
        return True, "no preflight defined"
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(Path.home()),
            env={**os.environ, "AITHER_TESTING": "1"}, creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"preflight could not run: {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return False, f"preflight failed: {tail[0][:240]}"
    return True, "preflight ok"


# ── detection ───────────────────────────────────────────────────────────────

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
        "running_validated": (marker_of(RUNNING_ROOT).get("validated") or {}).get("ok", None),
        "installed_at": _STATE["installed_root"],
        "update_available": bool(installed) and not _same(installed, RUNNING_ROOT),
        "checked_at": _STATE["checked_at"],
        "check_error": _STATE["error"],
        "restart_pending": _STATE["pending"],
        "auto_restart": auto_enabled(),
        "auto_restart_source": auto_source()[1],
        "opt_in": "adk autoupdate on  (or once: adk autoupdate apply)",
        "adoptable": _STATE["adoptable"],
        "refused_because": _STATE["refused_because"],
        "last_relaunch": _STATE["relaunch"],
    }


def needs_restart(st: Optional[dict[str, Any]] = None) -> bool:
    st = st or status()
    return bool(st["update_available"] or st["running_code_missing"])


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def settings_path() -> Path:
    return run_dir().parent / "updates.json"


def read_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(**changes: Any) -> dict[str, Any]:
    data = {**read_settings(), **changes, "updated_at": time.time()}
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def auto_source() -> tuple[bool, str]:
    """``(enabled, where the answer came from)``. OFF unless someone opted in."""
    env = os.environ.get("AITHER_DAEMON_AUTO_UPDATE", "").strip().lower()
    if env in _TRUE:
        return True, "env AITHER_DAEMON_AUTO_UPDATE"
    if env in _FALSE:
        return False, "env AITHER_DAEMON_AUTO_UPDATE"
    value = read_settings().get("daemon_auto_restart")
    if value is True:
        return True, str(settings_path())
    if value is False:
        return False, str(settings_path())
    return False, "default (not opted in)"


def auto_enabled() -> bool:
    return auto_source()[0]


def _apply_requested(name: str) -> bool:
    """A one-time ``adk autoupdate apply`` for this daemon, consumed when read."""
    flag = run_dir() / f"{name}.apply"
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass
        return True
    return False


def request_apply() -> list[str]:
    """Ask every running daemon for a one-time validated update (``adk autoupdate
    apply``; also the control plane's ``update`` command). Returns the names asked."""
    run_dir().mkdir(parents=True, exist_ok=True)
    names = [f.stem for f in run_dir().glob("*.json") if not f.name.endswith(".rollback.json")]
    for n in names:
        (run_dir() / f"{n}.apply").write_text(str(time.time()), encoding="utf-8")
    return names


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
        if f.name.endswith(".rollback.json"):
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if pid_alive(int(rec.get("pid") or 0)) and rec.get("code_root"):
            out.append(Path(rec["code_root"]))
    return out


# ── relaunch, with a health gate and rollback ───────────────────────────────

def relaunch_argv() -> list[str]:
    """This process's command line, with the CURRENT interpreter path."""
    orig = list(getattr(sys, "orig_argv", []) or [])
    if len(orig) > 1:
        return [sys.executable, *orig[1:]]
    return [sys.executable, *sys.argv]


#: Runs DETACHED, after the daemon asked itself to stop. Waits for the old pid to
#: exit, starts the new code, and rolls back if it is not healthy in time.
_HELPER = r"""
import json, os, subprocess, sys, time, urllib.request
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["root"])
from adk.self_update import pid_alive
flags = spec.get("flags", 0)

def wait_gone(pid, secs):
    end = time.time() + secs
    while pid_alive(pid) and time.time() < end:
        time.sleep(0.5)

def start(argv):
    log = open(spec["log"], "ab")
    return subprocess.Popen(argv, cwd=spec["cwd"], stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)

def healthy(url, secs):
    if not url:
        return True
    end = time.time() + secs
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False

wait_gone(spec["pid"], 120)
time.sleep(1.0)
proc = start(spec["argv"])
if healthy(spec.get("health_url"), float(spec.get("health_timeout", 90))):
    sys.exit(0)
# ROLLBACK: the new code did not come up healthy. Stop it, reinstall the code that was
# running, start that again, and record the bad root so it is never re-adopted.
try:
    proc.kill()
except Exception:
    pass
wait_gone(proc.pid, 30)
prev, bad = spec.get("previous_root"), spec.get("installed_root")
if prev and spec.get("reinstall", True):
    subprocess.run([spec["python"], "-m", "pip", "install", "-q", "--no-deps",
                    "--no-build-isolation", "-e", prev], capture_output=True, creationflags=flags)
os.makedirs(spec["run_dir"], exist_ok=True)
path = os.path.join(spec["run_dir"], spec["name"] + ".rollback.json")
try:
    with open(path, encoding="utf-8") as fh:
        rec = json.load(fh)
except Exception:
    rec = {"refused": []}
if bad and bad not in rec["refused"]:
    rec["refused"].append(bad)
rec.update(at=time.time(), restored=prev, failed=bad)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(rec, fh, indent=1)
start(spec.get("rollback_argv") or spec["argv"])
"""


def relaunch_spec(name: str, argv: Optional[list[str]] = None, health_url: str = "") -> dict[str, Any]:
    logs = run_dir().parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {
        "pid": os.getpid(), "argv": argv or relaunch_argv(), "cwd": os.getcwd(),
        "log": str(logs / f"{name}.log"), "root": str(RUNNING_ROOT), "name": name,
        "run_dir": str(run_dir()), "health_url": health_url, "python": sys.executable,
        "health_timeout": float(os.environ.get("AITHER_DAEMON_HEALTH_TIMEOUT_S", "90") or 90),
        "previous_root": str(RUNNING_ROOT), "installed_root": _STATE.get("installed_root") or "",
        "flags": _DETACHED,
    }


def spawn_relaunch(name: str, argv: Optional[list[str]] = None, health_url: str = "") -> dict[str, Any]:
    """Start the detached helper (see ``_HELPER``) for this daemon."""
    spec = relaunch_spec(name, argv, health_url)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER, json.dumps(spec)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_DETACHED, close_fds=True, start_new_session=(os.name != "nt"),
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
    """Background check: restart onto VALIDATED new code when idle; else report why not."""

    def __init__(
        self, name: str, is_idle: Callable[[], bool], interval: float = 120.0,
        probe: Callable[[], tuple[Optional[Path], str]] = installed_root,
        relaunch: Optional[Callable[[str], dict[str, Any]]] = None,
        stop: Callable[[], None] = request_exit,
        health_url: str = "",
        validate: Callable[[Optional[Path], str], tuple[bool, str]] = validation_of,
        preflight_fn: Callable[[str], tuple[bool, str]] = preflight,
    ) -> None:
        self.name, self.is_idle, self.interval = name, is_idle, interval
        self._probe, self._stop = probe, stop
        self._relaunch = relaunch or (lambda n: spawn_relaunch(n, health_url=health_url))
        self._validate, self._preflight = validate, preflight_fn
        self._thread: Optional[threading.Thread] = None
        self._halt = threading.Event()

    def tick(self) -> str:
        """One check. Returns: current | report-only | refused | busy | restarting."""
        st = check(self._probe)
        if not needs_restart(st):
            _STATE.update(pending=False, adoptable=None, refused_because="")
            return "current"
        _STATE["pending"] = True
        if not (auto_enabled() or _apply_requested(self.name)):
            return "report-only"
        installed = Path(st["installed_at"]) if st["installed_at"] else None
        ok, why = self._validate(installed, self.name)
        if not ok:
            _STATE.update(adoptable=False, refused_because=why)
            return "refused"
        try:
            idle = bool(self.is_idle())
        except Exception:  # noqa: BLE001 - unknown is not idle
            idle = False
        if not idle:
            _STATE.update(adoptable=True, refused_because="")
            return "busy"
        ok, why = self._preflight(self.name)
        if not ok:
            _STATE.update(adoptable=False, refused_because=why)
            return "refused"
        _STATE.update(adoptable=True, refused_because="")
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


def start(name: str, is_idle: Callable[[], bool], interval: Optional[float] = None,
          health_url: str = "") -> UpdateWatcher:
    """Register this daemon and start its watcher. Never raises."""
    register(name)
    every = interval or float(os.environ.get("AITHER_DAEMON_UPDATE_CHECK_S", "120") or 120)
    return UpdateWatcher(name, is_idle, interval=every, health_url=health_url).start()


# ── `adk autoupdate` ────────────────────────────────────────────────────────

def _health(name: str) -> Optional[dict[str, Any]]:
    """The daemon's own /health code_update block, by its registered port."""
    import urllib.request

    ports = {"harness-daemon": 8362, "adk-daemon": 9001}
    if name not in ports:
        return None
    headers = {}
    if name == "harness-daemon":
        tok = Path.home() / ".aither" / "harness_token"
        try:
            headers["Authorization"] = "Bearer " + tok.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    req = urllib.request.Request(f"http://127.0.0.1:{ports[name]}/health", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return (json.loads(r.read()) or {}).get("code_update")
    except Exception:  # noqa: BLE001 - a daemon that is not running has no status
        return None


def main(argv: Optional[list[str]] = None) -> int:
    """``adk autoupdate [status|on|off|apply]`` -- consent for daemon self-restarts."""
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args[0] if args else "status"
    if verb in ("-h", "--help", "help"):
        print("adk autoupdate status   what each daemon runs, whether an update is ready\n"
              "adk autoupdate on       let the daemons restart onto VALIDATED new code when idle\n"
              "adk autoupdate off      detection only (the default)\n"
              "adk autoupdate apply    restart onto validated new code once, now-ish (next check)")
        return 0
    if verb == "on":
        write_settings(daemon_auto_restart=True)
        print(f"automatic daemon updates: ON ({settings_path()}). Only validated code is adopted,"
              " only when a daemon is idle, and a failed restart rolls back.")
        return 0
    if verb == "off":
        write_settings(daemon_auto_restart=False)
        print(f"automatic daemon updates: OFF ({settings_path()}). Updates are still detected"
              " and shown in /health.")
        return 0
    if verb == "apply":
        names = request_apply()
        print("requested a one-time update for: " + (", ".join(names) or "no running daemon")
              + " (each acts at its next check, if the new code is validated and it is idle)")
        return 0
    if verb != "status":
        print(f"unknown: {verb} (status | on | off | apply)", file=sys.stderr)
        return 2
    on, src = auto_source()
    print(f"automatic daemon updates: {'ON' if on else 'OFF'}  ({src})")
    for f in sorted(run_dir().glob("*.json")) if run_dir().is_dir() else []:
        if f.name.endswith(".rollback.json"):
            continue
        name = f.stem
        h = _health(name)
        if not h:
            print(f"  {name:15} not answering")
            continue
        state = "update ready" if h.get("update_available") else "current"
        why = h.get("refused_because") or ""
        print(f"  {name:15} {state:12} runs {h.get('running_from')}"
              f" (validated: {h.get('running_validated')})" + (f"  refused: {why}" if why else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
