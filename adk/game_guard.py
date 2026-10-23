"""adk.game_guard -- games come first on a device that also works for the fleet.

A Steam Deck or a gaming laptop lends RAM to the V4.1 pool (``adk mesh lend``, a
ggml-rpc-server) and may serve small jobs (``adk mesh worker``, a llama-server). While
its owner plays, the guard:

* STOPS the worker. The fleet's MicroScheduler already falls back when a worker stops
  answering, and the heartbeat stops advertising it (its pid is gone).
* KEEPS the lend server running, at idle priority. A lend tier that vanishes mid-request
  hangs the V4.1 coordinator, so it is never stopped -- it is starved instead:
  Linux: cgroup ``CPUWeight=1`` / ``IOWeight=1`` (its own unit, or a transient scope the
  guard moves it into) plus ``nice 19`` / ``SCHED_IDLE`` on every thread when this user
  may undo that; Windows: ``IDLE_PRIORITY_CLASS``.
* Restores both once no game has been seen for :data:`GRACE_S` seconds.

What counts as gaming:

* SteamOS / Linux: a Steam game (Steam starts every game under ``reaper SteamLaunch
  AppId=<n>``), a gamescope session (Game Mode), Steam in Big Picture (``-gamepadui`` /
  ``-bigpicture`` / ``-tenfoot``, or the focused window is "Steam Big Picture Mode"),
  or the GPU busy at least :data:`GPU_BUSY_PCT` percent on two ticks in a row.
* Windows: the foreground window covers its whole monitor (fullscreen or borderless) and
  either lives under a game library (``steamapps\\common``, Epic, GOG, Xbox, Riot,
  Battle.net, EA, Ubisoft) or the 3D engine is busy at least :data:`GPU_BUSY_PCT` percent.

The device heartbeat (``adk.node_beat``) runs :class:`GameGuard` every :data:`TICK_S`
seconds; its state is ``~/.aither/game_guard.json`` so a restarted beat resumes a yield
(and restores it) instead of forgetting it. ``AITHER_GAME_GUARD=off`` disables it.

    python -m adk.game_guard            # one detection, printed as JSON (changes nothing)
    python -m adk.game_guard --status   # the saved guard state
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("adk.game_guard")

#: Seconds without a game before the worker and the lend's priority come back.
GRACE_S = 60.0
#: Seconds between two detections in the beat.
TICK_S = 10.0
#: GPU busy (percent) that counts as a game when nothing else names one.
GPU_BUSY_PCT = 60
#: Big Picture / Game Mode markers on a Steam client's command line.
_STEAM_UI_FLAGS = ("-gamepadui", "-bigpicture", "-tenfoot", "steam://open/bigpicture")
_BIG_PICTURE_TITLE = re.compile(r"big picture", re.I)
#: Windows: a fullscreen process under one of these is a game.
_WIN_GAME_DIRS = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\",
                  "\\gog games\\", "\\xboxgames\\", "\\riot games\\",
                  "\\battle.net\\", "\\ea games\\", "\\ubisoft game launcher\\games\\")
#: Windows: never a game, fullscreen or not.
_WIN_NOT_GAMES = frozenset({"explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                            "shellexperiencehost.exe", "lockapp.exe", "textinputhost.exe",
                            "applicationframehost.exe"})
_WIN_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd"})
_NO_WINDOW = 0x08000000
IDLE_PRIORITY_CLASS = 0x40
NORMAL_PRIORITY_CLASS = 0x20
LEND_UNIT = "awdk-mesh-lend.service"
WORKER_UNIT = "awdk-mesh-worker.service"


def enabled() -> bool:
    return os.environ.get("AITHER_GAME_GUARD", "").strip().lower() not in (
        "0", "off", "false", "no")


def state_path() -> Path:
    return Path.home() / ".aither" / "game_guard.json"


def _host() -> str:
    if os.name == "nt":
        return "windows"
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _run(argv: List[str], timeout: float = 10.0, **kw: Any) -> subprocess.CompletedProcess:
    """A short child: never a console window, never raises on a missing binary."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    kw.setdefault("stdin", subprocess.DEVNULL)
    if os.name == "nt":
        kw.setdefault("creationflags", _NO_WINDOW)
    try:
        return subprocess.run(argv, check=False, timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


# ── detection: Linux / SteamOS ──────────────────────────────────────────────


def _cmdlines(proc: str = "/proc") -> List[Tuple[int, List[str]]]:
    out: List[Tuple[int, List[str]]] = []
    root = Path(proc)
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a for a in raw.decode("utf-8", "replace").split("\x00") if a]
        if argv:
            out.append((int(d.name), argv))
    return out


def linux_game_process(cmdlines: List[Tuple[int, List[str]]]) -> str:
    """The reason a process list says "gaming", or ''."""
    for _pid, argv in cmdlines:
        line = " ".join(argv)
        m = re.search(r"SteamLaunch\s+AppId=(\d+)", line)
        if m and m.group(1) != "0":
            return f"steam game AppId={m.group(1)}"
    for _pid, argv in cmdlines:
        name = os.path.basename(argv[0])
        if name in ("gamescope", "gamescope-session", "gamescope-session-plus") \
                or name.startswith("gamescope-wl"):
            return "gamescope session (Game Mode)"
    for _pid, argv in cmdlines:
        name = os.path.basename(argv[0]).lower()
        if name in ("steam", "steam.sh") or name.endswith("/steam"):
            low = [a.lower() for a in argv[1:]]
            if any(f in low for f in _STEAM_UI_FLAGS):
                return "steam big picture"
    return ""


def linux_gpu_busy(sys_root: str = "/sys") -> int:
    """The busiest DRM GPU's ``gpu_busy_percent`` (AMD: the Deck's APU), else -1."""
    best = -1
    for f in Path(sys_root).glob("class/drm/card*/device/gpu_busy_percent"):
        try:
            best = max(best, int(f.read_text().strip()))
        except (OSError, ValueError):
            continue
    return best


def _x_env() -> Dict[str, str]:
    env = dict(os.environ)
    if not env.get("DISPLAY") and Path("/tmp/.X11-unix/X0").exists():
        env["DISPLAY"] = ":0"  # a beat started by systemd/cron has no DISPLAY
    return env


def linux_big_picture_window(run: Callable[..., Any] = _run) -> bool:
    """The focused X window is Steam's Big Picture (desktop mode). False without xprop."""
    env = _x_env()
    if not env.get("DISPLAY"):
        return False
    r = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], env=env, timeout=3)
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", getattr(r, "stdout", "") or "")
    if getattr(r, "returncode", 1) != 0 or not m or int(m.group(1), 16) == 0:
        return False
    r = run(["xprop", "-id", m.group(1), "_NET_WM_NAME", "WM_NAME"], env=env, timeout=3)
    return bool(_BIG_PICTURE_TITLE.search(getattr(r, "stdout", "") or ""))


# ── detection: Windows ──────────────────────────────────────────────────────


def windows_foreground() -> Dict[str, Any]:
    """{fullscreen, exe, class} of the foreground window (ctypes; {} when unknown)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {}
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        m = mi.rcMonitor
        full = (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right
                and rect.bottom >= m.bottom)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                exe = buf.value
            kernel32.CloseHandle(h)
        return {"fullscreen": bool(full), "exe": exe, "class": cls.value, "pid": pid.value}
    except Exception as exc:  # noqa: BLE001 - detection never raises
        log.debug("foreground window unreadable: %s", exc)
        return {}


def windows_gpu_busy(run: Callable[..., Any] = _run) -> int:
    """3D GPU busy percent: nvidia-smi when present, else the PDH 3D engine counters."""
    r = run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            timeout=5)
    if getattr(r, "returncode", 1) == 0:
        vals = [int(v) for v in re.findall(r"\d+", r.stdout or "")]
        if vals:
            return max(vals)
    r = run(["typeperf", r"\GPU Engine(*engtype_3D)\Utilization Percentage", "-sc", "1"],
            timeout=8)
    if getattr(r, "returncode", 1) != 0:
        return -1
    rows = [ln for ln in (r.stdout or "").splitlines() if ln.startswith('"') and "/" in ln[:12]]
    if not rows:
        return -1
    vals = [float(v) for v in re.findall(r'"([0-9.]+)"', rows[-1])]
    return int(min(100.0, sum(vals))) if vals else -1


def windows_game_reason(fg: Dict[str, Any], gpu_busy: Callable[[], int]) -> str:
    if not fg or not fg.get("fullscreen") or fg.get("class") in _WIN_SHELL_CLASSES:
        return ""
    exe = str(fg.get("exe") or "")
    name = os.path.basename(exe).lower()
    if not exe or name in _WIN_NOT_GAMES:
        return ""
    low = exe.lower()
    if any(d in low for d in _WIN_GAME_DIRS):
        return f"fullscreen game {os.path.basename(exe)}"
    busy = gpu_busy()
    if busy >= GPU_BUSY_PCT:
        return f"fullscreen {os.path.basename(exe)}, 3D GPU {busy}%"
    return ""


# ── one detection ───────────────────────────────────────────────────────────


def detect(host: Optional[str] = None, *, proc: str = "/proc", sys_root: str = "/sys",
           run: Callable[..., Any] = _run, prev_gpu_hot: bool = False,
           foreground: Optional[Callable[[], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{"gaming": bool, "reason": str, "gpu_busy": int, "gpu_hot": bool}. Never raises.

    ``gpu_hot`` is this tick's GPU verdict; a busy GPU alone counts only when the
    previous tick was hot too (``prev_gpu_hot``), so one busy second is not a game."""
    host = host or _host()
    out: Dict[str, Any] = {"gaming": False, "reason": "", "gpu_busy": -1, "gpu_hot": False}
    try:
        if host == "linux":
            reason = linux_game_process(_cmdlines(proc))
            if not reason and linux_big_picture_window(run):
                reason = "steam big picture (window)"
            busy = linux_gpu_busy(sys_root)
            out["gpu_busy"] = busy
            out["gpu_hot"] = busy >= GPU_BUSY_PCT
            if not reason and out["gpu_hot"] and prev_gpu_hot:
                reason = f"GPU busy {busy}%"
        elif host == "windows":
            fg = (foreground or windows_foreground)()
            seen: Dict[str, int] = {}

            def busy() -> int:
                if "v" not in seen:
                    seen["v"] = windows_gpu_busy(run)
                return seen["v"]

            reason = windows_game_reason(fg, busy)
            out["gpu_busy"] = seen.get("v", -1)
        else:
            reason = ""
        out.update(gaming=bool(reason), reason=reason)
    except Exception as exc:  # noqa: BLE001 - a detector failure is "not gaming", logged
        log.warning("game detection failed: %s", exc)
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


# ── actions ─────────────────────────────────────────────────────────────────


def _threads(pid: int, proc: str = "/proc") -> List[int]:
    try:
        return sorted(int(t.name) for t in (Path(proc) / str(pid) / "task").iterdir()
                      if t.name.isdigit())
    except OSError:
        return [pid]


def _own_unit(pid: int, proc: str = "/proc") -> str:
    """The systemd unit whose cgroup holds ``pid`` (the last .service/.scope), or ''."""
    try:
        text = (Path(proc) / str(pid) / "cgroup").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        path = line.split(":", 2)[-1]
        for part in reversed(path.split("/")):
            if part.endswith((".service", ".scope")):
                return part
    return ""


def _may_renice_back(prev_nice: int) -> bool:
    """Can this user undo ``nice 19`` / SCHED_IDLE later? Root, or RLIMIT_NICE allows
    ``prev_nice`` again (unprivileged, Linux refuses raising priority back)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    try:
        import resource

        soft = resource.getrlimit(resource.RLIMIT_NICE)[0]  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError, ValueError):
        return False
    return soft == resource.RLIM_INFINITY or 20 - prev_nice <= soft


def _image_of(pid: int, proc: str = "/proc") -> str:
    """The executable path of ``pid`` ('' when unknown)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k.OpenProcess(0x1000, False, pid)
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                return buf.value if k.QueryFullProcessImageNameW(h, 0, buf,
                                                                 ctypes.byref(size)) else ""
            finally:
                k.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return ""
    try:
        return os.readlink(Path(proc) / str(pid) / "exe")
    except OSError:
        try:
            raw = (Path(proc) / str(pid) / "cmdline").read_bytes()
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        except OSError:
            return ""


def pid_is(pid: int, expect: str, proc: str = "/proc",
           image: Optional[Callable[[int], str]] = None) -> bool:
    """Is ``pid`` still the program a state file recorded (same executable name)?

    A state file's pid can outlive its process and be reused by another program; the
    guard must never stop or starve THAT one. No expected path = no proof = False."""
    if pid <= 0 or not expect:
        return False
    got = (image or (lambda p: _image_of(p, proc)))(pid)
    return bool(got) and _exe_name(got) == _exe_name(expect)


def _exe_name(path: str) -> str:
    name = os.path.basename(path.replace("\\", "/")).lower()
    return name.removesuffix(" (deleted)").removesuffix(".exe")


class Actions:
    """The side effects of a yield, one seam for tests. Each returns what it did.

    Every pid read from a state file is checked with :func:`pid_is` first."""

    def __init__(self, run: Callable[..., Any] = _run, proc: str = "/proc",
                 host: Optional[str] = None,
                 image: Optional[Callable[[int], str]] = None) -> None:
        self.run = run
        self.proc = proc
        self.host = host or _host()
        self.image = image or (lambda pid: _image_of(pid, proc))

    def _is(self, pid: int, expect: str) -> bool:
        return pid_is(pid, expect, image=self.image)

    # -- the worker: stopped, then started again with the same arguments ----------
    def pause_worker(self) -> Optional[Dict[str, Any]]:
        from adk import mesh_worker

        st = mesh_worker.running_state()
        if not st:
            return None
        saved = {"model": st.get("model"), "parallel": int(st.get("parallel") or 1),
                 "port": int(st.get("port") or mesh_worker.DEFAULT_PORT),
                 "persist": st.get("persist") or "", "pid": int(st.get("pid") or 0)}
        argv0 = str((st.get("argv") or [""])[0])
        if saved["persist"] == "systemd":
            # a unit with Restart=on-failure: stop it through systemd, never under it
            self.run(["systemctl", "--user", "stop", mesh_worker.UNIT_NAME])
        elif not self._is(saved["pid"], argv0):
            saved["error"] = "pid is no longer the worker; nothing stopped"
        elif self.host == "windows":
            self.run(["taskkill", "/PID", str(saved["pid"]), "/T", "/F"])
        else:
            import signal

            try:
                os.kill(saved["pid"], signal.SIGTERM)
            except OSError as exc:
                saved["error"] = str(exc)[:120]
        return saved

    def resume_worker(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        from adk import mesh_worker

        if mesh_worker.running_state():
            return {"resumed": "already running"}
        if saved.get("persist") == "systemd":
            r = self.run(["systemctl", "--user", "start", mesh_worker.UNIT_NAME])
            if getattr(r, "returncode", 1) == 0:
                r2 = self.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                               mesh_worker.UNIT_NAME])
                pid = int((getattr(r2, "stdout", "") or "0").strip() or 0)
                st = mesh_worker.read_state()
                if pid > 0 and st:
                    mesh_worker.write_state(dict(st, pid=pid))
                    return {"resumed": "systemd", "pid": pid}
        lines: List[str] = []
        ns = argparse.Namespace(worker_action="on", model=saved.get("model") or "",
                                parallel=int(saved.get("parallel") or 1),
                                port=int(saved.get("port") or mesh_worker.DEFAULT_PORT),
                                json=False)
        rc = mesh_worker.cmd_on(ns, say=lambda t, e=False: lines.append(str(t)))
        return {"resumed": "cmd_on", "rc": rc, "output": " | ".join(lines)[-300:]}

    # -- the lend: never stopped, only starved -----------------------------------
    def lower_lend(self) -> Optional[Dict[str, Any]]:
        from adk import mesh_lend

        st = mesh_lend.read_state() or {}
        pid = int(st.get("pid") or 0)
        if not pid or not mesh_lend._pid_alive(pid):
            return None
        exe = str(st.get("exe") or "")
        if not self._is(pid, exe):
            log.warning("game guard: lend pid %s is not %s; left alone", pid, exe)
            return None
        if self.host == "windows":
            prev = _win_priority(pid)
            ok = _win_set_priority(pid, IDLE_PRIORITY_CLASS)
            return {"pid": pid, "exe": exe, "how": "windows-idle-class" if ok else "failed",
                    "prev_class": prev or NORMAL_PRIORITY_CLASS}
        done: Dict[str, Any] = {"pid": pid, "exe": exe, "how": []}
        unit = _own_unit(pid, self.proc)
        if unit != LEND_UNIT:
            # not in a unit of its own (started from a terminal): move it into one, so
            # the weights below starve the lend and nothing else in that terminal
            scope = f"awdk-mesh-lend-{pid}.scope"
            r = self.run(["busctl", "--user", "call", "org.freedesktop.systemd1",
                          "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                          "StartTransientUnit", "ssa(sv)a(sa(sv))", scope, "fail", "1",
                          "PIDs", "au", "1", str(pid), "0"])
            unit = scope if getattr(r, "returncode", 1) == 0 else ""
        if unit:
            r = self.run(["systemctl", "--user", "set-property", "--runtime", unit,
                          "CPUWeight=1", "IOWeight=1"])
            if getattr(r, "returncode", 1) == 0:
                done["how"].append("cgroup")
                done["unit"] = unit
        try:
            prev = os.getpriority(os.PRIO_PROCESS, pid)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            prev = 0
        done["prev_nice"] = prev
        if _may_renice_back(prev):
            for tid in _threads(pid, self.proc):
                try:
                    os.setpriority(os.PRIO_PROCESS, tid, 19)  # type: ignore[attr-defined]
                    os.sched_setscheduler(tid, os.SCHED_IDLE,  # type: ignore[attr-defined]
                                          os.sched_param(0))  # type: ignore[attr-defined]
                except (AttributeError, OSError):
                    continue
            done["how"].append("nice19+sched_idle")
        else:
            # Unprivileged Linux cannot raise a thread's priority back, and a lend left
            # at nice 19 after the game would slow the pool for good: weights only.
            done["nice"] = "skipped: this user could not undo it (RLIMIT_NICE)"
        if not done["how"]:
            done["how"] = ["failed"]
        return done

    def restore_lend(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        pid = int(saved.get("pid") or 0)
        from adk import mesh_lend

        if not pid or not mesh_lend._pid_alive(pid) or not self._is(pid, saved.get("exe") or ""):
            return {"restored": "lend gone"}
        if self.host == "windows":
            ok = _win_set_priority(pid, int(saved.get("prev_class") or NORMAL_PRIORITY_CLASS))
            return {"restored": "windows-class" if ok else "failed"}
        out: List[str] = []
        unit = saved.get("unit") or ""
        if unit:
            r = self.run(["systemctl", "--user", "set-property", "--runtime", unit,
                          "CPUWeight=100", "IOWeight=100"])
            out.append("cgroup" if getattr(r, "returncode", 1) == 0 else "cgroup-failed")
        if "nice19+sched_idle" in (saved.get("how") or []):
            prev = int(saved.get("prev_nice") or 0)
            for tid in _threads(pid, self.proc):
                try:
                    os.sched_setscheduler(tid, os.SCHED_OTHER,  # type: ignore[attr-defined]
                                          os.sched_param(0))  # type: ignore[attr-defined]
                    os.setpriority(os.PRIO_PROCESS, tid, prev)  # type: ignore[attr-defined]
                except (AttributeError, OSError):
                    continue
            out.append("nice")
        return {"restored": "+".join(out) or "nothing to restore"}


def _win_priority(pid: int) -> int:
    try:
        import ctypes

        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return 0
        try:
            return int(k.GetPriorityClass(h))
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return 0


def _win_set_priority(pid: int, cls: int) -> bool:
    try:
        import ctypes

        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x0200 | 0x1000, False, pid)  # SET_INFORMATION | QUERY_LIMITED
        if not h:
            return False
        try:
            return bool(k.SetPriorityClass(h, cls))
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


# ── the guard ───────────────────────────────────────────────────────────────


def load_state() -> Dict[str, Any]:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st: Dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("game guard state not saved: %s", exc)


class GameGuard:
    """Yield to a game, restore GRACE_S seconds after it ends. :meth:`tick` is one step."""

    def __init__(self, *, detector: Optional[Callable[..., Dict[str, Any]]] = None,
                 actions: Optional[Actions] = None, grace_s: float = GRACE_S,
                 clock: Callable[[], float] = time.time,
                 save: Callable[[Dict[str, Any]], None] = _save_state,
                 load: Callable[[], Dict[str, Any]] = load_state) -> None:
        self.detector = detector or detect
        self.actions = actions or Actions()
        self.grace_s = grace_s
        self.clock = clock
        self.save = save
        # a restarted beat picks up a yield in progress (and restores it later)
        self.state: Dict[str, Any] = {"gaming": False, "reason": "", "yielding": False,
                                      **load()}

    def tick(self) -> Dict[str, Any]:
        now = self.clock()
        v = self.detector(prev_gpu_hot=bool(self.state.get("gpu_hot")))
        st = self.state
        st.update(gaming=bool(v.get("gaming")), reason=str(v.get("reason") or ""),
                  gpu_busy=v.get("gpu_busy", -1), gpu_hot=bool(v.get("gpu_hot")),
                  checked_at=int(now))
        if st["gaming"]:
            st["last_game_at"] = now
            if not st.get("yielding"):
                st["yielding"] = True
                st["since"] = int(now)
                try:
                    st["worker_paused"] = self.actions.pause_worker()
                except Exception as exc:  # noqa: BLE001 - one failed action never stops the guard
                    st["worker_paused"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                try:
                    st["lend_lowered"] = self.actions.lower_lend()
                except Exception as exc:  # noqa: BLE001
                    st["lend_lowered"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
                log.warning("game guard: %s -- worker paused %s, lend %s", st["reason"],
                            bool(st["worker_paused"]), (st["lend_lowered"] or {}).get("how"))
        elif st.get("yielding") and now - float(st.get("last_game_at") or 0) >= self.grace_s:
            restored: Dict[str, Any] = {}
            try:
                if st.get("lend_lowered") and "pid" in st["lend_lowered"]:
                    restored["lend"] = self.actions.restore_lend(st["lend_lowered"])
            except Exception as exc:  # noqa: BLE001
                restored["lend"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            try:
                if st.get("worker_paused") and st["worker_paused"].get("model"):
                    restored["worker"] = self.actions.resume_worker(st["worker_paused"])
            except Exception as exc:  # noqa: BLE001
                restored["worker"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            log.warning("game guard: no game for %ds -- restored %s", int(self.grace_s),
                        restored)
            st.update(yielding=False, worker_paused=None, lend_lowered=None,
                      restored=restored, restored_at=int(now))
        self.save(dict(st))
        return status(st)


def status(st: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What diagnostics and the heartbeat report: gaming, yielding and what was done."""
    st = load_state() if st is None else st
    lend = st.get("lend_lowered") or {}
    return {
        "enabled": enabled(),
        "gaming": bool(st.get("gaming")),
        "reason": str(st.get("reason") or ""),
        "yielding": bool(st.get("yielding")),
        "since": st.get("since") if st.get("yielding") else None,
        "worker_paused": bool(st.get("worker_paused")),
        "lend_priority": ("idle (" + "+".join(lend.get("how") if isinstance(lend.get("how"), list)
                                              else [str(lend.get("how") or "")]) + ")")
        if st.get("yielding") and lend else "normal",
        "checked_at": st.get("checked_at"),
    }


async def run_forever(guard: Optional[GameGuard] = None, tick_s: float = TICK_S) -> None:
    """The beat's loop: one tick every ``tick_s`` seconds, off the event loop."""
    import asyncio

    if not enabled():
        log.info("game guard off (AITHER_GAME_GUARD)")
        return
    guard = guard or GameGuard()
    while True:
        try:
            await asyncio.to_thread(guard.tick)
        except Exception as exc:  # noqa: BLE001 - the guard never takes the beat down
            log.warning("game guard tick failed: %s", exc)
        await asyncio.sleep(tick_s)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m adk.game_guard",
                                description="Is a game running? (changes nothing)")
    p.add_argument("--status", action="store_true", help="print the saved guard state")
    args = p.parse_args(argv)
    print(json.dumps(status() if args.status else detect(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
