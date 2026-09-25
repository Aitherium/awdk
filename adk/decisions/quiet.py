"""Is the owner busy with something full-screen right now?

Owner, 2026-09-23: "annoying that i keep getting awdesk/awask/awdecision cards
popping up on my main screen while im playing games". Nothing that raised a card
asked first. This is the one question every interrupting path asks first:

    quiet = doNotDisturb  OR  (quietWhenFullscreen AND busy)

It is the same rule the desk applies to its own windows, read from the same
switches (the desk's ``cast.json``, ``"input": {"doNotDisturb", "quietWhenFullscreen"}``),
so turning Do-not-disturb on in the desk also holds the Python card window, the
masked credential prompt and the toast.

"Busy" is two measurements, because games go full-screen two ways:

* ``SHQueryUserNotificationState`` -- Windows' own answer: a full-screen app, an
  exclusive-mode (D3D) game, presentation mode, or Focus Assist.
* borderless-windowed games usually do NOT trip that, so also: the foreground
  window covers its whole monitor, and it is neither the shell/desktop nor one of
  the desk's own windows (its overlay IS full-screen, and is not a game).

HELD, NEVER DROPPED. Quiet changes whether a window is put in front of the owner
right now. It never changes the card: a held card stays open in the store, in the
badge and the inbox, and nothing is marked answered or delivered because it was
held.

Constraints, because this runs inside popup and notify paths:

* stdlib only, ``ctypes`` on Windows -- no subprocess, no PowerShell. A probe that
  spawns a process is its own frame-time spike in the game it is trying not to
  disturb.
* never raises: any error reads as "not quiet" (``(False, "")``), the same answer
  as before this module existed.
* ``AITHER_QUIET=1`` forces quiet, ``AITHER_QUIET=0`` forces not quiet -- the
  owner's escape hatch, and the seam tests use so they never depend on the real
  screen.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

#: ``1`` forces quiet, ``0`` forces not quiet. Anything else: measure.
QUIET_ENV = "AITHER_QUIET"

#: The desk's own test seam for its cast file; honoured so both read one file.
CAST_ENV = "DESK_CAST_FILE"

#: QUERY_USER_NOTIFICATION_STATE values that mean "do not interrupt".
QUNS_QUIET = {
    2: "a full-screen app",
    3: "a full-screen game",
    4: "presentation mode",
    6: "Focus Assist",
}

#: Foreground windows that are the desktop itself, never a full-screen app.
_SHELL_CLASSES = frozenset({"WorkerW", "Progman", "Shell_TrayWnd"})

#: Process images that are the desk. Its overlay covers the monitor by design.
_DESK_IMAGES = frozenset({"electron.exe", "desk.exe"})

_MONITOR_DEFAULTTONEAREST = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WIN_API: Any = None


def cast_path() -> Optional[Path]:
    """The desk's cast file: ``DESK_CAST_FILE`` or ``%APPDATA%\\Desk\\cast.json``."""
    override = os.environ.get(CAST_ENV, "").strip()
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return None
    return Path(appdata) / "Desk" / "cast.json"


def read_prefs() -> "tuple[bool, bool]":
    """``(doNotDisturb, quietWhenFullscreen)``; defaults ``(False, True)``.

    An absent, unreadable or malformed file is the defaults, and so is a key that
    is not a real boolean -- the desk's own validator drops those too.
    """
    dnd, fullscreen = False, True
    path = cast_path()
    if path is None:
        return dnd, fullscreen
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return dnd, fullscreen
    section = raw.get("input") if isinstance(raw, dict) else None
    if isinstance(section, dict):
        if isinstance(section.get("doNotDisturb"), bool):
            dnd = section["doNotDisturb"]
        if isinstance(section.get("quietWhenFullscreen"), bool):
            fullscreen = section["quietWhenFullscreen"]
    return dnd, fullscreen


def _win_api() -> Any:
    """Private DLL handles with their own prototypes.

    Private (``WinDLL``, not ``ctypes.windll``) so the 64-bit handle prototypes set
    here cannot change how any other module's calls into user32 behave.
    """
    global _WIN_API
    if _WIN_API is not None:
        return _WIN_API
    import ctypes
    from ctypes import wintypes

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32")
    shell32 = ctypes.WinDLL("shell32")
    kernel32 = ctypes.WinDLL("kernel32")

    shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
    shell32.SHQueryUserNotificationState.restype = ctypes.c_long
    for name in ("GetForegroundWindow", "GetShellWindow", "GetDesktopWindow"):
        getattr(user32, name).argtypes = []
        getattr(user32, name).restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    class _Api:
        pass

    api = _Api()
    api.ctypes = ctypes
    api.wintypes = wintypes
    api.MonitorInfo = _MonitorInfo
    api.user32 = user32
    api.shell32 = shell32
    api.kernel32 = kernel32
    _WIN_API = api
    return api


def _process_image(api: Any, hwnd: Any) -> str:
    """The foreground window's process image name (``game.exe``), or ``""``."""
    ctypes, wintypes = api.ctypes, api.wintypes
    pid = wintypes.DWORD(0)
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = api.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not api.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        api.kernel32.CloseHandle(handle)


def _win_busy() -> "tuple[bool, str]":
    """Windows: a full-screen app, game, presentation, Focus Assist -- or a
    foreground window covering its whole monitor that is not the shell or the desk."""
    api = _win_api()
    ctypes = api.ctypes
    state = ctypes.c_int(0)
    if api.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        label = QUNS_QUIET.get(state.value)
        if label:
            return True, f"Windows reports {label}"

    user32 = api.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetShellWindow(), user32.GetDesktopWindow()):
        return False, ""
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False, ""
    rect = api.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False, ""
    monitor = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    info = api.MonitorInfo()
    info.cbSize = ctypes.sizeof(api.MonitorInfo)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False, ""
    screen = info.rcMonitor
    covers = (rect.left <= screen.left and rect.top <= screen.top
              and rect.right >= screen.right and rect.bottom >= screen.bottom)
    if not covers:
        return False, ""
    image = _process_image(api, hwnd)
    if image.lower() in _DESK_IMAGES:
        return False, ""
    name = image[:-4] if image.lower().endswith(".exe") else image
    return True, f"{name or 'an app'} is full-screen"


def _busy() -> "tuple[bool, str]":
    """Is the foreground full-screen? Off Windows there is no probe: not busy."""
    if sys.platform != "win32":
        return False, ""
    return _win_busy()


def is_quiet() -> "tuple[bool, str]":
    """``(quiet, reason)``. Never raises; any error is ``(False, "")``."""
    try:
        forced = os.environ.get(QUIET_ENV, "").strip()
        if forced == "1":
            return True, f"{QUIET_ENV}=1"
        if forced == "0":
            return False, ""
        dnd, fullscreen = read_prefs()
        if dnd:
            return True, "Do not disturb is on"
        if not fullscreen:
            return False, ""
        busy, why = _busy()
        return (True, why or "a full-screen app") if busy else (False, "")
    except Exception:  # noqa: BLE001 - a probe error must read as "not quiet"
        return False, ""


def quiet_reason() -> str:
    """The reason when quiet, else ``""`` -- for a one-line "held while ..." note."""
    quiet, why = is_quiet()
    return why if quiet else ""


def _self_test() -> int:
    import tempfile

    ok = True

    def check(cond: bool, what: str) -> None:
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            ok = False

    saved = {k: os.environ.get(k) for k in (QUIET_ENV, CAST_ENV)}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cast = Path(tmp) / "cast.json"
            os.environ[CAST_ENV] = str(cast)
            os.environ[QUIET_ENV] = "1"
            check(is_quiet()[0] is True, "AITHER_QUIET=1 forces quiet")
            cast.write_text(json.dumps({"input": {"doNotDisturb": True}}), encoding="utf-8")
            os.environ[QUIET_ENV] = "0"
            check(is_quiet() == (False, ""), "AITHER_QUIET=0 beats Do not disturb")
            os.environ.pop(QUIET_ENV, None)
            check(is_quiet() == (True, "Do not disturb is on"), "doNotDisturb is read")
            cast.write_text("{not json", encoding="utf-8")
            check(read_prefs() == (False, True), "an unreadable cast file is the defaults")
        live = is_quiet()
        check(isinstance(live, tuple) and isinstance(live[0], bool),
              f"the live probe answers without raising (got {live})")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    print(is_quiet())
