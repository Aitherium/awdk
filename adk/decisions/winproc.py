"""Process ancestry and window focus — stdlib only, importable BY PATH.

Two callers with incompatible constraints share this module, which is why it has
no ``adk`` imports at all:

* ``adk.decisions.terminal`` imports it normally, as part of the package;
* ``.claude/hooks/stop-decision-cards.py`` loads it **by file path**, because
  Claude Code hooks run under ``python -S`` (a plain interpreter start costs
  ~1.5s on this box) and ``-S`` removes site-packages — so ``import adk`` raises
  ``ModuleNotFoundError: yaml`` before it reaches anything useful. Measured, not
  assumed. One ``from adk...`` line added here silently breaks the hook.

**Why the hook needs this at all:** the terminal a card refers to can only be
found by walking UP the process tree, and the walk has to happen while the chain
is still alive. The hook's own chain is ``python ← bash ← claude(node) ←
WindowsTerminal``; the hook exits in milliseconds, and the card is written by a
DETACHED grandchild, so resolving it later — when the owner finally clicks the
card — finds a dead pid and no parent link. The pid recorded on the card is
therefore resolved at RAISE time and must be one that OUTLIVES the raise.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Iterable, Optional, Sequence

IS_WINDOWS = os.name == "nt"

#: Processes that are plumbing rather than "the session". Walking past these
#: gets from a hook's own interpreter to the agent process that owns the tab.
SHIM_NAMES = frozenset({
    "python.exe", "python3.exe", "pythonw.exe", "py.exe",
    "bash.exe", "sh.exe", "dash.exe", "zsh.exe",
    "cmd.exe", "conhost.exe", "openconsole.exe",
    "python", "python3", "bash", "sh", "zsh", "dash",
})

#: Processes that host a visible terminal window. Used only to LABEL what was
#: found — the search itself is by "has a visible top-level window", because a
#: name allowlist is exactly the false-positive machine that quality gate 1t
#: had to rip out of check_scheduled_task_windows.
TERMINAL_NAMES = frozenset({
    "windowsterminal.exe", "wt.exe", "conhost.exe", "openconsole.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "alacritty.exe", "wezterm-gui.exe",
})


# ── Windows process table ───────────────────────────────────────────────────────


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * 260),
    ]


def process_table() -> dict[int, tuple[int, str]]:
    """``{pid: (parent_pid, exe_name)}`` for every process we may inspect.

    One snapshot, not one query per pid: the caller walks a chain, and taking a
    fresh snapshot per hop can observe the tree mid-teardown and produce a chain
    that never existed.
    """
    if not IS_WINDOWS:
        return _posix_process_table()
    table: dict[int, tuple[int, str]] = {}
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == -1 or snapshot == 0:
        return table
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return table
        while True:
            name = entry.szExeFile.decode("utf-8", "replace")
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), name)
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _posix_process_table() -> dict[int, tuple[int, str]]:
    """The same shape from ``/proc``. Empty where there is no procfs (macOS)."""
    table: dict[int, tuple[int, str]] = {}
    proc = "/proc"
    if not os.path.isdir(proc):
        return table
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/stat", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        # comm can contain spaces and parens, so parse from the LAST ')'.
        close = raw.rfind(")")
        if close < 0:
            continue
        comm = raw[raw.find("(") + 1: close]
        rest = raw[close + 2:].split()
        if len(rest) < 2:
            continue
        try:
            table[int(name)] = (int(rest[1]), comm)
        except ValueError:
            continue
    return table


def ancestry(pid: int, *, limit: int = 16) -> list[tuple[int, str]]:
    """``[(pid, name), …]`` from ``pid`` upward. Stops at the root, a cycle, or
    ``limit`` hops — a corrupt table must not spin forever."""
    table = process_table()
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    current = int(pid or 0)
    while current > 0 and current not in seen and len(out) < limit:
        seen.add(current)
        entry = table.get(current)
        if entry is None:
            break
        out.append((current, entry[1]))
        current = entry[0]
    return out


def resolve_owner_pid(pid: Optional[int] = None) -> int:
    """The first ancestor that is not plumbing — the process that owns the tab.

    From a hook this walks ``python → bash → claude``. Returns ``pid`` itself
    when nothing better is found, which is honest: a wrong-but-live pid at least
    focuses *a* window, and the caller can see the name it settled on via
    :func:`ancestry`.
    """
    start = int(pid or os.getpid())
    chain = ancestry(start)
    for candidate, name in chain:
        if candidate == start:
            continue
        if name.lower() in SHIM_NAMES:
            continue
        return candidate
    return start


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


# ── Windows top-level windows ───────────────────────────────────────────────────


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _windows_for_pids(pids: Iterable[int]) -> list[tuple[int, int, str, tuple[int, int]]]:
    """``[(hwnd, pid, title, (w, h)), …]`` — visible top-level windows of ``pids``."""
    if not IS_WINDOWS:
        return []
    wanted = {int(p) for p in pids}
    found: list[tuple[int, int, str, tuple[int, int]]] = []
    user32 = ctypes.windll.user32

    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) not in wanted:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        size = (rect.right - rect.left, rect.bottom - rect.top)
        found.append((int(hwnd), int(owner.value), buffer.value, size))
        return True

    user32.EnumWindows(enum_proc(callback), None)
    return found


def _is_real_frame(match: tuple[int, int, str, tuple[int, int]]) -> bool:
    """Does this window look like something a human can see and click?

    This filter is load-bearing and was added after measurement, not theory. A
    ConPTY session's host (``pwsh.exe`` under Windows Terminal) owns a window
    that reports ``IsWindowVisible`` TRUE with an EMPTY title — the pseudo-
    console. Walking up the chain therefore stopped one hop short of the real
    ``WindowsTerminal.exe`` frame every single time, and focusing it did
    nothing at all: a successful call, a returned hwnd, and no visible effect.
    """
    _hwnd, _pid, title, (width, height) = match
    return bool(title.strip()) and width > 200 and height > 100


def find_terminal_window(pid: int) -> Optional[tuple[int, int, str]]:
    """The visible window hosting ``pid``, found by walking UP its ancestry.

    Returns ``(hwnd, owner_pid, title)`` or None. The search is by *has a real
    visible frame*, not by executable name: Windows Terminal, conhost, VS
    Code's integrated terminal and a bare pwsh window are different processes,
    and a name list would miss whichever one the owner actually uses.
    """
    if not IS_WINDOWS:
        return None
    chain = ancestry(pid)
    if not chain:
        return None
    fallback: Optional[tuple[int, int, str]] = None
    for candidate, _name in chain:
        matches = _windows_for_pids([candidate])
        real = [m for m in matches if _is_real_frame(m)]
        if real:
            real.sort(key=lambda m: m[3][0] * m[3][1], reverse=True)
            hwnd, owner, title, _size = real[0]
            return (hwnd, owner, title)
        if matches and fallback is None:
            hwnd, owner, title, _size = matches[0]
            fallback = (hwnd, owner, title)
    return fallback


def _quiet_now() -> bool:
    """``quiet.is_quiet()`` loaded BY PATH from the sibling file.

    This module must import nothing from the package (it runs under
    ``python -S`` from a hook), so the sibling ``quiet.py`` -- itself stdlib
    only -- is loaded by file path. Any failure is "not quiet": the gate never
    breaks the focus path it guards.
    """
    try:
        import importlib.util

        mod = sys.modules.get("_aither_quiet")
        if mod is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quiet.py")
            spec = importlib.util.spec_from_file_location("_aither_quiet", path)
            if spec is None or spec.loader is None:
                return False
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sys.modules["_aither_quiet"] = mod
        return bool(mod.is_quiet()[0])
    except Exception:  # noqa: BLE001 - the gate must never break focus
        return False


def focus_window(hwnd: int) -> bool:
    """Bring ``hwnd`` to the front. False when Windows refused.

    ``SetForegroundWindow`` fails silently for a process that does not own the
    foreground, which is the normal case here (the popup is a different process
    from the terminal). The documented workaround is to attach this thread's
    input queue to the foreground thread's for the duration of the call.
    """
    if not IS_WINDOWS or not hwnd:
        return False
    if _quiet_now():
        # Never pull a window over a full-screen game or through Do not disturb
        # (owner, 2026-09-23). False reads as "Windows refused", which every
        # caller already handles by leaving the owner where they are.
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    foreground = user32.GetForegroundWindow()
    this_thread = kernel32.GetCurrentThreadId()
    other_thread = user32.GetWindowThreadProcessId(foreground, None)
    attached = False
    if other_thread and other_thread != this_thread:
        attached = bool(user32.AttachThreadInput(other_thread, this_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(other_thread, this_thread, False)
    return ok


# ── Windows Terminal tabs (UI Automation, ctypes only) ─────────────────────────
#
# Raising the WINDOW was the old ceiling: Windows Terminal has no supported
# command to activate a tab of an EXISTING window from another process
# (``wt -w <id> focus-tab`` needs a window id the session never learns, and
# ``-w 0`` means "the most recent window", which is the wrong one exactly when it
# matters). What DOES work is the accessibility tree: every WT tab is a UIA
# ``TabItem`` whose Name is the tab title, and ``SelectionItemPattern.Select``
# switches to it. The tab title is the session console's title, which we can
# read -- and, when two tabs share it, briefly make unique -- by attaching to
# that console. All of this is plain COM vtable calls through ctypes: no
# comtypes, no pywinauto, because this module is loaded under ``python -S``.

_UIA_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"  # CUIAutomation
_UIA_IID = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"  # IUIAutomation
_SELECTION_ITEM_IID = "{a8efa66a-0fda-421a-9194-38021f3578ea}"
_UIA_CONTROL_TYPE_PROPERTY = 30003
_UIA_TAB_ITEM_CONTROL_TYPE = 50019
_UIA_SELECTION_ITEM_PATTERN = 10010
_TREE_SCOPE_DESCENDANTS = 4
_VT_I4 = 3

# IUnknown occupies slots 0-2 of every vtable; the slots below are the method
# positions in UIAutomationClient.h. A wrong slot calls a different method with
# the wrong arguments, so each one is named where it is used.
_RELEASE = 2
_UIA_ELEMENT_FROM_HANDLE = 6
_UIA_CREATE_PROPERTY_CONDITION = 23
_ELEMENT_FIND_ALL = 6
_ELEMENT_GET_CURRENT_PATTERN_AS = 14
_ELEMENT_GET_CURRENT_NAME = 23
_ARRAY_LENGTH = 3
_ARRAY_ELEMENT = 4
_SELECTION_ITEM_SELECT = 3
_SELECTION_ITEM_IS_SELECTED = 6


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _VARIANT(ctypes.Structure):
    # 16 bytes on x86, 24 on x64: an 8-byte header then a pointer-pair union.
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("lVal", ctypes.c_long),
                ("pad", ctypes.c_byte * (2 * ctypes.sizeof(ctypes.c_void_p) - 4))]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _vcall(obj: int, slot: int, argtypes: tuple, *args) -> int:
    """Call vtable ``slot`` of COM object ``obj``; returns the HRESULT."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return int(proto(vtable[slot])(obj, *args))


def _release(obj: Optional[int]) -> None:
    if obj:
        _vcall(obj, _RELEASE, ())


class _Uia:
    """One IUIAutomation instance for the duration of a ``with`` block."""

    def __init__(self) -> None:
        self.automation = 0
        self._uninit = False

    def __enter__(self) -> "_Uia":
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoCreateInstance.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        # S_OK / S_FALSE are ours to balance; RPC_E_CHANGED_MODE means the
        # thread is already initialised MTA, which UIA also accepts.
        self._uninit = hr in (0, 1)
        out = ctypes.c_void_p()
        clsid, iid = _guid(_UIA_CLSID), _guid(_UIA_IID)
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x1 | 0x4,
                                    ctypes.byref(iid), ctypes.byref(out))
        if hr < 0 or not out.value:
            self.__exit__()
            raise OSError(f"UI Automation unavailable (hr=0x{hr & 0xFFFFFFFF:08x})")
        self.automation = int(out.value)
        return self

    def __exit__(self, *_exc) -> None:
        _release(self.automation)
        self.automation = 0
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()
            self._uninit = False

    def tab_items(self, hwnd: int) -> list[int]:
        """Element pointers of every TabItem under ``hwnd`` (caller releases)."""
        element = ctypes.c_void_p()
        hr = _vcall(self.automation, _UIA_ELEMENT_FROM_HANDLE,
                    (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                    ctypes.c_void_p(hwnd), ctypes.byref(element))
        if hr < 0 or not element.value:
            return []
        condition = ctypes.c_void_p()
        array = ctypes.c_void_p()
        try:
            value = _VARIANT()
            value.vt = _VT_I4
            value.lVal = _UIA_TAB_ITEM_CONTROL_TYPE
            hr = _vcall(self.automation, _UIA_CREATE_PROPERTY_CONDITION,
                        (ctypes.c_int, _VARIANT, ctypes.POINTER(ctypes.c_void_p)),
                        _UIA_CONTROL_TYPE_PROPERTY, value, ctypes.byref(condition))
            if hr < 0 or not condition.value:
                return []
            hr = _vcall(element.value, _ELEMENT_FIND_ALL,
                        (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
                        _TREE_SCOPE_DESCENDANTS, condition, ctypes.byref(array))
            if hr < 0 or not array.value:
                return []
            length = ctypes.c_int(0)
            _vcall(array.value, _ARRAY_LENGTH, (ctypes.POINTER(ctypes.c_int),),
                   ctypes.byref(length))
            items: list[int] = []
            for index in range(max(0, length.value)):
                item = ctypes.c_void_p()
                hr = _vcall(array.value, _ARRAY_ELEMENT,
                            (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
                            index, ctypes.byref(item))
                if hr >= 0 and item.value:
                    items.append(int(item.value))
            return items
        finally:
            _release(array.value)
            _release(condition.value)
            _release(element.value)

    @staticmethod
    def name(item: int) -> str:
        bstr = ctypes.c_void_p()
        hr = _vcall(item, _ELEMENT_GET_CURRENT_NAME, (ctypes.POINTER(ctypes.c_void_p),),
                    ctypes.byref(bstr))
        if hr < 0 or not bstr.value:
            return ""
        try:
            return ctypes.wstring_at(bstr.value)
        finally:
            ctypes.windll.oleaut32.SysFreeString(ctypes.c_void_p(bstr.value))

    @staticmethod
    def _selection(item: int) -> int:
        pattern = ctypes.c_void_p()
        iid = _guid(_SELECTION_ITEM_IID)
        hr = _vcall(item, _ELEMENT_GET_CURRENT_PATTERN_AS,
                    (ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
                    _UIA_SELECTION_ITEM_PATTERN, ctypes.byref(iid), ctypes.byref(pattern))
        return int(pattern.value) if hr >= 0 and pattern.value else 0

    @classmethod
    def is_selected(cls, item: int) -> bool:
        pattern = cls._selection(item)
        if not pattern:
            return False
        try:
            flag = ctypes.c_int(0)
            hr = _vcall(pattern, _SELECTION_ITEM_IS_SELECTED,
                        (ctypes.POINTER(ctypes.c_int),), ctypes.byref(flag))
            return hr >= 0 and bool(flag.value)
        finally:
            _release(pattern)

    @classmethod
    def select(cls, item: int) -> bool:
        pattern = cls._selection(item)
        if not pattern:
            return False
        try:
            return _vcall(pattern, _SELECTION_ITEM_SELECT, ()) >= 0
        finally:
            _release(pattern)


def list_tabs(hwnd: int) -> list[tuple[str, bool]]:
    """``[(tab title, is_selected), …]`` of the terminal window ``hwnd``.

    Read-only: nothing on screen changes. Empty when the window exposes no tabs
    (a classic conhost window) or UI Automation is unavailable.
    """
    if not IS_WINDOWS or not hwnd:
        return []
    try:
        with _Uia() as uia:
            items = uia.tab_items(hwnd)
            try:
                return [(uia.name(i), uia.is_selected(i)) for i in items]
            finally:
                for item in items:
                    _release(item)
    except OSError:
        return []


def select_tab(hwnd: int, index: int, title: str) -> bool:
    """Switch window ``hwnd`` to tab ``index`` — only if it still reads ``title``.

    Re-enumerates rather than trusting a cached element, and re-checks the
    title: a tab opened or closed between the list and the click shifts every
    index after it, and selecting by a stale index is a wrong tab that LOOKS
    like success. Does not raise the window; :func:`focus_window` does that.
    """
    if not IS_WINDOWS or not hwnd or index < 0:
        return False
    try:
        with _Uia() as uia:
            items = uia.tab_items(hwnd)
            try:
                if index >= len(items) or uia.name(items[index]) != title:
                    return False
                return uia.select(items[index])
            finally:
                for item in items:
                    _release(item)
    except OSError:
        return False


def normalize_tab_title(title: str) -> str:
    """A tab title without the part that changes while the session works.

    Claude Code prefixes its title with a status glyph — a spinner frame while
    busy, a star while idle — so the title read from the console and the one
    read from the tab strip a moment later routinely differ in that glyph alone.
    It is dropped, with case and spacing. Everything else is KEPT, the trailing
    clock above all: measured 2026-10-04, the owner's tabs read
    ``"AitherOS-Fresh develop HH:MM"`` and nine of thirteen differ ONLY in that
    clock — it is the session's start time, i.e. its identity, not noise.
    """
    text = (title or "").strip()
    start = 0
    while start < len(text) and not text[start].isalnum():
        start += 1
    return " ".join(text[start:].split()).casefold()


def match_tab(titles: Sequence[str], wanted: str,
              hints: Sequence[str] = ()) -> tuple[int, str]:
    """Which ONE of ``titles`` is the session's tab: ``(index, how)`` or ``(-1, why)``.

    Pure, so the rules are testable without a terminal. In order:

    1. ``wanted`` (the session console's live title), compared after
       :func:`normalize_tab_title` — the glyph-only difference between the
       console read and the tab-strip read a moment later is not a mismatch;
    2. only when the title could NOT BE READ (``wanted`` is empty): the one tab
       whose title contains every hint (the card's directory name, its branch).

    A title that WAS read but matches no tab is ``-1``, never a hint guess: it
    means the session's tab does not show its own title (the owner renamed it,
    say), so the one tab that does name the directory and branch is most likely
    ANOTHER session's tab, and selecting it would report a wrong tab as success.

    Never guesses between equals: two tabs that both match is ``-1`` with the
    count, because selecting the wrong one of two identical tabs is the
    silent-wrong-answer this module exists to avoid. That includes two tabs
    that differ only in their status glyph — an exact-glyph match would pick by
    whichever spinner frame happened to be showing. The caller resolves a tie
    by making the title unique, not by picking the first.
    """
    if not titles:
        return -1, "the window exposes no tabs"
    key = normalize_tab_title(wanted)
    if key:
        same = [i for i, t in enumerate(titles) if normalize_tab_title(t) == key]
        if len(same) == 1:
            how = "exact title" if titles[same[0]] == wanted else "title, ignoring status glyph"
            return same[0], how
        if len(same) > 1:
            return -1, f"{len(same)} tabs share that title"
        return -1, "no tab carries the session's title"
    needles = [normalize_tab_title(h) for h in hints if normalize_tab_title(h)]
    if needles:
        hits = [i for i, t in enumerate(titles)
                if all(n in normalize_tab_title(t) for n in needles)]
        if len(hits) == 1:
            return hits[0], "directory/branch in the title"
        if len(hits) > 1:
            return -1, f"{len(hits)} tabs name that directory/branch"
    return -1, "the session's title could not be read and no hint singles out a tab"


# ── the session's console title ─────────────────────────────────────────────────


def _on_console(pid: int, action):
    """Run ``action(kernel32)`` attached to ``pid``'s console. None if we cannot.

    Detaches from our own console for the duration and re-attaches to our
    parent's afterwards, so a CLI caller can still print. A GUI caller (the
    card window) has no console to lose.
    """
    if not IS_WINDOWS or pid <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    had_console = bool(kernel32.GetConsoleWindow())
    kernel32.FreeConsole()
    try:
        if not kernel32.AttachConsole(int(pid)):
            return None
        try:
            return action(kernel32)
        finally:
            kernel32.FreeConsole()
    finally:
        if had_console:
            kernel32.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS


def console_title(pid: int) -> str:
    """The title of ``pid``'s console — under Windows Terminal, its TAB title."""
    def read(kernel32) -> str:
        buffer = ctypes.create_unicode_buffer(1024)
        kernel32.GetConsoleTitleW(buffer, 1024)
        return buffer.value

    return _on_console(pid, read) or ""


def set_console_title(pid: int, title: str) -> bool:
    """Set ``pid``'s console title; Windows Terminal shows it as the tab title."""
    return bool(_on_console(
        pid, lambda kernel32: bool(kernel32.SetConsoleTitleW(ctypes.c_wchar_p(title)))))


def terminal_frames(owner_pid: int) -> list[int]:
    """Every real top-level frame of ``owner_pid``, largest first.

    One ``WindowsTerminal.exe`` hosts ALL of its windows, so "the" window of the
    process is ambiguous; the tab search runs over every one of them.
    """
    frames = [m for m in _windows_for_pids([owner_pid]) if _is_real_frame(m)]
    frames.sort(key=lambda m: m[3][0] * m[3][1], reverse=True)
    return [m[0] for m in frames]


# ── self-test ───────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Prove the walk really walks and can still fail.

    The assertion that matters is that ``ancestry`` reaches a DIFFERENT process
    from the one it started at: the first version of this module returned
    ``[(pid, name)]`` and nothing else on a table it failed to read, which reads
    as a successful walk that found nothing to focus.
    """
    problems: list[str] = []
    table = process_table()
    if not table:
        if IS_WINDOWS:
            problems.append("process table came back empty on Windows")
        else:
            print("  skip process table (no procfs on this platform)")
    else:
        print(f"  ok   process table: {len(table)} processes")
        if os.getpid() not in table:
            problems.append("our own pid is missing from the table")

    chain = ancestry(os.getpid())
    if table:
        if len(chain) < 2:
            problems.append(f"ancestry found no parent: {chain}")
        else:
            # ASCII on purpose: this runs under a cp1252 console, where a single
            # arrow glyph turns a passing self-test into a UnicodeEncodeError.
            print("  ok   ancestry: " + " <- ".join(n for _p, n in chain[:6]))
        owner = resolve_owner_pid(os.getpid())
        if owner <= 0:
            problems.append("resolve_owner_pid returned a non-pid")
        else:
            print(f"  ok   owner pid {owner} ({'alive' if pid_alive(owner) else 'DEAD'})")

    if not pid_alive(os.getpid()):
        problems.append("pid_alive says this process is dead")
    if pid_alive(0) or pid_alive(-1):
        problems.append("pid_alive accepted a non-pid")

    # The frame filter gets its own guard: without it the walk stops on a
    # ConPTY pseudo-console (visible, empty title) and focus silently no-ops.
    if _is_real_frame((1, 1, "", (1200, 800))):
        problems.append("_is_real_frame accepted an untitled pseudo-console")
    if _is_real_frame((1, 1, "Windows Terminal", (10, 10))):
        problems.append("_is_real_frame accepted a 10x10 window")
    if not _is_real_frame((1, 1, "Windows Terminal", (1200, 800))):
        problems.append("_is_real_frame rejected a genuine frame")

    # Tab matching decides WHICH tab a card's "Go to that terminal" selects.
    owner_tabs = ["◐ AitherOS-Fresh develop 16:03", "✳ AitherOS-Fresh develop 07:37"]
    if match_tab(owner_tabs, "◑ AitherOS-Fresh develop 07:37")[0] != 1:
        problems.append("match_tab lost a tab whose spinner glyph changed")
    if match_tab(owner_tabs, "◑ AitherOS-Fresh develop 07:38")[0] != -1:
        problems.append("match_tab matched a different session's clock")
    if match_tab(owner_tabs * 2, owner_tabs[0])[0] != -1:
        problems.append("match_tab guessed between two identical tabs")
    if match_tab(owner_tabs, "renamed by the owner", ("16:03",))[0] != -1:
        problems.append("match_tab let a hint override a title it could read")

    if IS_WINDOWS:
        window = find_terminal_window(os.getpid())
        # No window is a legitimate outcome (a detached/service context), so this
        # is reported rather than failed — but it is PRINTED, because "found
        # nothing" and "could not look" must not read the same.
        if window:
            # A terminal tab title routinely carries a spinner glyph, and this
            # console is cp1252 — printing it raw turns a PASS into a traceback.
            safe = window[2][:60].encode("ascii", "replace").decode("ascii")
            print(f"  ok   terminal window: hwnd={window[0]} pid={window[1]} "
                  f"title={safe!r}")
            owner_name = process_table().get(window[1], (0, ""))[1].lower()
            if owner_name == "windowsterminal.exe":
                # Read-only enumeration: nothing on screen changes. A WT frame
                # with no readable tabs means the UIA path is broken, and every
                # card would silently fall back to focusing the window.
                tabs = [t for f in terminal_frames(window[1]) for t in list_tabs(f)]
                if not tabs:
                    problems.append("Windows Terminal frame exposes no tabs via UIA")
                else:
                    print(f"  ok   UIA reads {len(tabs)} Windows Terminal tab(s)")
        else:
            print("  ok   terminal window: none found (headless?)")

    print()
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print("winproc self-test passed")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
