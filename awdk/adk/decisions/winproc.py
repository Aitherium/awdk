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
from typing import Iterable, Optional

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


def focus_window(hwnd: int) -> bool:
    """Bring ``hwnd`` to the front. False when Windows refused.

    ``SetForegroundWindow`` fails silently for a process that does not own the
    foreground, which is the normal case here (the popup is a different process
    from the terminal). The documented workaround is to attach this thread's
    input queue to the foreground thread's for the duration of the call.
    """
    if not IS_WINDOWS or not hwnd:
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
