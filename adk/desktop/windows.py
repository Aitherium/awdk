"""Enumerate and arrange top-level windows -- stdlib + ctypes, imports anywhere.

The Win32 calls live in :class:`CtypesWin32Backend`; everything else talks to a
duck-typed *backend* so the logic is testable on any OS with a fake one.

Ground rules this module enforces:

* **Elevation first, all or nothing.** :func:`arrange` resolves every target and
  probes every target's token elevation BEFORE the first move. One elevated (or
  unknown) target refuses the whole arrangement with ``needs_admin_lane: true``
  -- Windows UIPI would otherwise make the move fail half-way, or worse, look
  like it worked. An elevation probe that is itself denied is ``None``
  (assumed elevated), never ``False``.
* **Dry run by default.** Nothing moves unless the caller passes
  ``dry_run=False``.
* **Real windows only.** Enumeration drops cloaked windows (other virtual
  desktops, suspended UWP) and ConPTY pseudo-console ghosts (visible, but an
  empty title and a tiny rectangle).
* **Placeable frames.** Planned rectangles are what ``SetWindowPos`` consumes;
  the DWM visible frame is reported by :func:`list_windows` only and never fed
  back into a move, so repeated arrangements do not drift.
* **DPI.** Every rect query and move runs under per-monitor-v2 thread DPI
  awareness, restoring the previous context afterwards.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Sequence

from adk.desktop.layout import MODES, Rect, split

__all__ = [
    "IS_WINDOWS", "DesktopError", "Rect", "MonitorInfo", "WindowInfo",
    "CtypesWin32Backend", "list_windows", "find_window", "arrange", "default_backend",
]

IS_WINDOWS = os.name == "nt"

# Win32 constants.
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_SHOWNOACTIVATE = 4
MONITORINFOF_PRIMARY = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_WINDOW_HANDLE = 1400

#: DesktopError codes.
ERROR_CODES = (
    "not_windows", "not_found", "ambiguous", "gone", "elevated_target",
    "access_denied", "bad_geometry", "move_failed",
)


class DesktopError(RuntimeError):
    """A desktop operation failed for a named, machine-readable reason."""

    def __init__(self, code: str, message: str, *, needs_admin_lane: bool = False,
                 hwnd: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.needs_admin_lane = bool(needs_admin_lane)
        self.hwnd = int(hwnd or 0)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"ok": False, "reason": self.code, "error": self.message,
                               "needs_admin_lane": self.needs_admin_lane}
        if self.hwnd:
            out["hwnd"] = self.hwnd
        return out


@dataclass(frozen=True)
class MonitorInfo:
    """One display: ``work`` excludes the taskbar, ``full`` is the whole screen."""

    id: int
    work: Rect
    full: Rect
    primary: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "work": self.work.to_dict(), "full": self.full.to_dict(),
                "primary": self.primary}


@dataclass
class WindowInfo:
    """A top-level window. ``rect`` is the placeable frame; ``frame`` the visible one."""

    hwnd: int
    pid: int
    process: str
    title: str
    rect: Rect
    visible: bool
    minimized: bool
    cloaked: bool
    taskbar: bool
    elevated: Optional[bool]
    frame: Optional[Rect] = None

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd, "pid": self.pid, "process": self.process,
            "title": self.title, "rect": self.rect.to_dict(),
            "frame": self.frame.to_dict() if self.frame else None,
            "visible": self.visible, "minimized": self.minimized,
            "cloaked": self.cloaked, "taskbar": self.taskbar, "elevated": self.elevated,
        }


# ── the ctypes backend ─────────────────────────────────────────────────────────


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def to_rect(self) -> Rect:
        return Rect(int(self.left), int(self.top), int(self.right - self.left),
                    int(self.bottom - self.top))


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


class CtypesWin32Backend:
    """The only Windows implementation of the backend seam.

    Uses private ``WinDLL`` handles (never the shared ``ctypes.windll``) with full
    prototypes, so 64-bit handles are not truncated and another module's
    prototypes cannot change ours.
    """

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise DesktopError("not_windows", "desktop control needs Windows")
        self._bind()

    def _bind(self) -> None:
        from ctypes import wintypes as wt

        hwnd = ctypes.c_void_p
        handle = ctypes.c_void_p
        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = d = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = a = ctypes.WinDLL("advapi32", use_last_error=True)

        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, hwnd, ctypes.c_void_p)
        self._monitor_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, handle, handle, ctypes.POINTER(_RECT), ctypes.c_void_p)

        def proto(fn: Any, restype: Any, *argtypes: Any) -> None:
            fn.restype = restype
            fn.argtypes = list(argtypes)

        proto(u.EnumWindows, ctypes.c_int, self._enum_proc, ctypes.c_void_p)
        proto(u.IsWindow, ctypes.c_int, hwnd)
        proto(u.IsWindowVisible, ctypes.c_int, hwnd)
        proto(u.IsIconic, ctypes.c_int, hwnd)
        proto(u.IsZoomed, ctypes.c_int, hwnd)
        proto(u.GetWindow, hwnd, hwnd, ctypes.c_uint)
        proto(u.GetWindowTextLengthW, ctypes.c_int, hwnd)
        proto(u.GetWindowTextW, ctypes.c_int, hwnd, ctypes.c_wchar_p, ctypes.c_int)
        proto(u.GetWindowThreadProcessId, wt.DWORD, hwnd, ctypes.POINTER(wt.DWORD))
        proto(u.GetWindowRect, ctypes.c_int, hwnd, ctypes.POINTER(_RECT))
        proto(u.ShowWindow, ctypes.c_int, hwnd, ctypes.c_int)
        proto(u.SetWindowPos, ctypes.c_int, hwnd, hwnd, ctypes.c_int, ctypes.c_int,
              ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        proto(u.EnumDisplayMonitors, ctypes.c_int, handle, ctypes.c_void_p,
              self._monitor_proc, ctypes.c_void_p)
        proto(u.GetMonitorInfoW, ctypes.c_int, handle, ctypes.POINTER(_MONITORINFO))
        if hasattr(u, "GetWindowLongPtrW"):
            self._get_long = u.GetWindowLongPtrW
            proto(self._get_long, ctypes.c_ssize_t, hwnd, ctypes.c_int)
        else:  # 32-bit user32 exports only the non-Ptr name
            self._get_long = u.GetWindowLongW
            proto(self._get_long, ctypes.c_long, hwnd, ctypes.c_int)
        self._set_dpi = getattr(u, "SetThreadDpiAwarenessContext", None)
        if self._set_dpi is not None:
            proto(self._set_dpi, ctypes.c_void_p, ctypes.c_void_p)

        proto(d.DwmGetWindowAttribute, ctypes.c_long, hwnd, wt.DWORD, ctypes.c_void_p,
              wt.DWORD)

        proto(k.OpenProcess, handle, wt.DWORD, ctypes.c_int, wt.DWORD)
        proto(k.CloseHandle, ctypes.c_int, handle)
        proto(k.QueryFullProcessImageNameW, ctypes.c_int, handle, wt.DWORD,
              ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD))
        proto(a.OpenProcessToken, ctypes.c_int, handle, wt.DWORD, ctypes.POINTER(handle))
        proto(a.GetTokenInformation, ctypes.c_int, handle, ctypes.c_int, ctypes.c_void_p,
              wt.DWORD, ctypes.POINTER(wt.DWORD))

    @contextlib.contextmanager
    def _dpi(self) -> Iterator[None]:
        """Per-monitor-v2 DPI awareness for this thread; previous context restored."""
        previous = None
        if self._set_dpi is not None:
            previous = self._set_dpi(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        try:
            yield
        finally:
            if self._set_dpi is not None and previous:
                self._set_dpi(ctypes.c_void_p(previous))

    # -- backend protocol --------------------------------------------------------

    def enum_top_level(self) -> list[int]:
        """Every top-level hwnd, in z-order (topmost first)."""
        found: list[int] = []

        def callback(hwnd: Any, _lparam: Any) -> int:
            if hwnd:
                found.append(int(hwnd))
            return 1

        thunk = self._enum_proc(callback)  # must outlive EnumWindows
        self.user32.EnumWindows(thunk, None)
        return found

    def _dwm_int(self, hwnd: int, attr: int) -> int:
        value = ctypes.c_int(0)
        hr = self.dwmapi.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                               ctypes.sizeof(value))
        return int(value.value) if hr == 0 else 0

    def _dwm_frame(self, hwnd: int) -> Optional[Rect]:
        rc = _RECT()
        hr = self.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                               ctypes.byref(rc), ctypes.sizeof(rc))
        return rc.to_rect() if hr == 0 else None

    def _process_name(self, pid: int) -> str:
        from ctypes import wintypes as wt

        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not h:
            return ""
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            self.kernel32.CloseHandle(h)

    def window_info(self, hwnd: int) -> Optional[WindowInfo]:
        """Describe ``hwnd``; None when it no longer exists."""
        from ctypes import wintypes as wt

        u = self.user32
        if not u.IsWindow(hwnd):
            return None
        length = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(length, 0) + 1)
        u.GetWindowTextW(hwnd, buf, len(buf))
        pid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        with self._dpi():
            rc = _RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(rc)):
                return None
            frame = self._dwm_frame(hwnd)
        exstyle = int(self._get_long(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
        owner = u.GetWindow(hwnd, GW_OWNER)
        taskbar = (not owner or bool(exstyle & WS_EX_APPWINDOW)) and not (
            exstyle & WS_EX_TOOLWINDOW)
        return WindowInfo(
            hwnd=int(hwnd), pid=int(pid.value), process=self._process_name(int(pid.value)),
            title=buf.value, rect=rc.to_rect(), visible=bool(u.IsWindowVisible(hwnd)),
            minimized=bool(u.IsIconic(hwnd)), cloaked=self._dwm_int(hwnd, DWMWA_CLOAKED) != 0,
            taskbar=bool(taskbar), elevated=None, frame=frame,
        )

    def monitors(self) -> list[MonitorInfo]:
        """Every display, in EnumDisplayMonitors order; ids are list indices."""
        handles: list[int] = []

        def callback(hmon: Any, _hdc: Any, _rc: Any, _lparam: Any) -> int:
            handles.append(int(hmon or 0))
            return 1

        thunk = self._monitor_proc(callback)
        out: list[MonitorInfo] = []
        with self._dpi():
            self.user32.EnumDisplayMonitors(None, None, thunk, None)
            for idx, hmon in enumerate(handles):
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if not self.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    continue
                out.append(MonitorInfo(idx, mi.rcWork.to_rect(), mi.rcMonitor.to_rect(),
                                       bool(mi.dwFlags & MONITORINFOF_PRIMARY)))
        return out

    def set_window_pos(self, hwnd: int, x: int, y: int, w: int, h: int,
                       activate: bool = False) -> bool:
        """Move/resize ``hwnd`` (placeable frame). False = read :meth:`last_error`."""
        u = self.user32
        flags = SWP_NOZORDER | (0 if activate else SWP_NOACTIVATE)
        with self._dpi():
            if u.IsIconic(hwnd) or u.IsZoomed(hwnd):
                # A maximised/minimised window ignores a plain SetWindowPos.
                u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            ctypes.set_last_error(0)
            return bool(u.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h), flags))

    def is_elevated(self, pid: int) -> Optional[bool]:
        """Token elevation of ``pid``; None when the probe itself is denied."""
        from ctypes import wintypes as wt

        h = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return None
        try:
            token = ctypes.c_void_p()
            if not self.advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
                return None
            try:
                elevation = wt.DWORD(0)
                ret = wt.DWORD(0)
                if not self.advapi32.GetTokenInformation(
                        token, TOKEN_ELEVATION, ctypes.byref(elevation),
                        ctypes.sizeof(elevation), ctypes.byref(ret)):
                    return None
                return bool(elevation.value)
            finally:
                self.kernel32.CloseHandle(token)
        finally:
            self.kernel32.CloseHandle(h)

    def last_error(self) -> int:
        return int(ctypes.get_last_error())


# ── backend-agnostic logic ─────────────────────────────────────────────────────


def default_backend() -> Any:
    """The real backend; raises ``not_windows`` elsewhere."""
    if not IS_WINDOWS:
        raise DesktopError("not_windows", "desktop control needs Windows")
    return CtypesWin32Backend()


def _is_real_frame(info: WindowInfo) -> bool:
    """Something a human can see: a title and (unless minimised) a real size.

    Mirrors the ConPTY filter in ``adk.decisions.winproc``: a pseudo-console is
    visible with an EMPTY title. A minimised window's rect is the parked icon
    position, so the size test does not apply to it.
    """
    if not info.title.strip():
        return False
    return info.minimized or (info.rect.w > 200 and info.rect.h > 100)


def list_windows(*, backend: Any = None, taskbar_only: bool = True,
                 query: str = "") -> list[WindowInfo]:
    """Real, visible, uncloaked top-level windows in z-order.

    Args:
        backend: Backend seam; the ctypes backend when None.
        taskbar_only: Keep only windows Explorer would show on the taskbar.
        query: Case-insensitive substring filter on title or process name.

    Returns:
        WindowInfo rows with ``elevated`` resolved.

    Raises:
        DesktopError: ``not_windows`` when no backend can run here.
    """
    be = backend if backend is not None else default_backend()
    needle = (query or "").strip().lower()
    out: list[WindowInfo] = []
    for hwnd in be.enum_top_level():
        info = be.window_info(hwnd)
        if info is None or not info.visible or info.cloaked or not _is_real_frame(info):
            continue
        if taskbar_only and not info.taskbar:
            continue
        if needle and needle not in info.title.lower() and needle not in info.process.lower():
            continue
        info.elevated = be.is_elevated(info.pid)
        out.append(info)
    return out


def _parse_hwnd(needle: str) -> Optional[int]:
    text = needle.strip().lower()
    if not text.startswith("hwnd:"):
        return None
    try:
        return int(text[5:].strip(), 0)
    except ValueError:
        return None


def find_window(needle: str, *, backend: Any = None) -> WindowInfo:
    """Resolve one window by exact title, then by title/process substring.

    ``hwnd:<n>`` (decimal or 0x hex) addresses a window directly.

    Raises:
        DesktopError: ``not_found``, ``ambiguous`` or ``gone``.
    """
    be = backend if backend is not None else default_backend()
    hwnd = _parse_hwnd(needle)
    if hwnd is not None:
        info = be.window_info(hwnd)
        if info is None:
            raise DesktopError("gone", f"window {needle!r} no longer exists", hwnd=hwnd)
        return info
    text = (needle or "").strip().lower()
    if not text:
        raise DesktopError("not_found", "empty window name")
    rows = list_windows(backend=be, taskbar_only=True)
    exact = [w for w in rows if w.title.strip().lower() == text]
    matches = exact or [w for w in rows
                        if text in w.title.lower() or text in w.process.lower()]
    if not matches:
        raise DesktopError("not_found", f"no window matches {needle!r}")
    if len(matches) > 1:
        names = ", ".join(f"{w.title!r} (hwnd:{w.hwnd})" for w in matches[:6])
        raise DesktopError("ambiguous",
                           f"{len(matches)} windows match {needle!r}: {names}; "
                           "use a longer title or hwnd:<n>")
    return matches[0]


def _monitor_of(rect: Rect, monitors: Sequence[MonitorInfo]) -> MonitorInfo:
    cx, cy = rect.x + rect.w // 2, rect.y + rect.h // 2
    for mon in monitors:
        if mon.full.contains(cx, cy):
            return mon
    return next((m for m in monitors if m.primary), monitors[0])


def _focus(hwnd: int) -> bool:
    """Raise ``hwnd`` using the one working raise in the package."""
    from adk.decisions import winproc

    return bool(winproc.focus_window(hwnd))


def arrange(windows: Sequence[str], *, mode: str = "side_by_side", monitor: int = -1,
            gap: int = 8, dry_run: bool = True, activate: bool = False,
            backend: Any = None) -> dict:
    """Tile the named windows on one monitor's work area.

    Args:
        windows: Window names (title, title/process substring, or ``hwnd:<n>``).
        mode: One of ``side_by_side``, ``stacked``, ``grid``.
        monitor: Monitor index; -1 = the monitor holding the first window.
        gap: Pixels between windows (placeable frame).
        dry_run: Plan only; no window moves. Default True.
        activate: After moving, bring the first window to the foreground.
        backend: Backend seam; the ctypes backend when None.

    Returns:
        A receipt dict. ``ok`` false with ``needs_admin_lane`` true when any
        target is elevated or of unknown elevation -- and then nothing moved.

    Raises:
        DesktopError: resolution, geometry or move failures.
    """
    be = backend if backend is not None else default_backend()
    if mode not in MODES:
        raise DesktopError("bad_geometry", f"unknown mode {mode!r}; one of {', '.join(MODES)}")
    names = [str(w) for w in (windows or []) if str(w).strip()]
    if not names:
        raise DesktopError("bad_geometry", "no windows named")

    # 1. Resolve every target.
    targets = [find_window(n, backend=be) for n in names]
    seen: set[int] = set()
    for t in targets:
        if t.hwnd in seen:
            raise DesktopError("bad_geometry", f"{t.title!r} was named twice", hwnd=t.hwnd)
        seen.add(t.hwnd)

    # 2. Probe every target's elevation before any move.
    refused = []
    for t in targets:
        t.elevated = be.is_elevated(t.pid)
        if t.elevated is not False:
            refused.append({"hwnd": t.hwnd, "title": t.title, "process": t.process,
                            "elevation": "elevated" if t.elevated else "unknown"})
    if refused:
        return {"ok": False, "reason": "elevated_target", "needs_admin_lane": True,
                "refused": refused, "moved": 0, "dry_run": dry_run,
                "error": "an elevated (or unprobeable) window cannot be moved from a "
                         "non-elevated process; nothing was moved"}

    # 3. Plan.
    mons = be.monitors()
    if not mons:
        raise DesktopError("bad_geometry", "no monitors reported")
    if monitor is None or int(monitor) < 0:
        mon = _monitor_of(targets[0].rect, mons)
    elif int(monitor) < len(mons):
        mon = mons[int(monitor)]
    else:
        raise DesktopError("bad_geometry", f"monitor {monitor} out of range 0..{len(mons) - 1}")
    try:
        cells = split(mon.work, len(targets), mode=mode, gap=int(gap))
    except ValueError as exc:
        raise DesktopError("bad_geometry", str(exc)) from exc
    plan = [{"hwnd": t.hwnd, "title": t.title, "process": t.process,
             "from": t.rect.to_dict(), "to": r.to_dict()} for t, r in zip(targets, cells)]
    receipt: dict[str, Any] = {"ok": True, "dry_run": bool(dry_run), "mode": mode,
                               "monitor": mon.id, "work_area": mon.work.to_dict(),
                               "plan": plan, "moved": 0, "needs_admin_lane": False}
    if dry_run:
        return receipt

    # 4. Move.
    for t, r in zip(targets, cells):
        if be.set_window_pos(t.hwnd, r.x, r.y, r.w, r.h, False):
            receipt["moved"] += 1
            continue
        err = be.last_error()
        done = f"({receipt['moved']} of {len(targets)} moved before this)"
        if err == ERROR_ACCESS_DENIED:
            raise DesktopError("access_denied",
                               f"Windows refused to move {t.title!r} (UIPI) {done}",
                               needs_admin_lane=True, hwnd=t.hwnd)
        if err == ERROR_INVALID_WINDOW_HANDLE:
            raise DesktopError("gone", f"{t.title!r} closed mid-arrange {done}", hwnd=t.hwnd)
        raise DesktopError("move_failed", f"SetWindowPos failed for {t.title!r} "
                           f"(Win32 error {err}) {done}", hwnd=t.hwnd)
    if activate:
        receipt["activated"] = _focus(targets[0].hwnd)
    return receipt
