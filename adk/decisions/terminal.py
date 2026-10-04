"""Reach the terminal a card came from — focus it, open one, or type into it.

The card window used to be a dead end in one specific way: it named a session
("claude-code · fix/verify-products") and a working directory, and then left the
owner to go FIND that tab among a dozen identical ones. The ask it exists to
carry is "do this next", and the last mile of that ask was manual.

Three capabilities, in descending order of how reliably they work. Each reports
what it actually did, because a control that silently does nothing is worse than
no control — that is `.claude/rules/security-review-patterns.md` §5, and this
module is where it would hide.

1. **focus** — switch to the session's TAB and bring its window forward.
   Windows Terminal has no CLI to activate a tab of an existing window from
   outside (``wt focus-tab`` needs a window id we never learn), so the tab is
   found through UI Automation (:func:`winproc.list_tabs`): the session
   console's live title is read by attaching to it, matched against every tab
   of every WT window (:func:`winproc.match_tab`), and that tab is selected.
   When two tabs share the title, the console title is briefly set to a unique
   marker, the marker tab is selected, and the title is put back. When no tab
   can be pinned down (a conhost window, UIA unavailable, a renamed tab) it
   falls back to focusing the WINDOW and says so, naming the tab to look for.
2. **open** — start a new terminal already `cd`-ed to the card's directory.
   Always available; it just is not the same tab.
3. **type** — write text into the session's console input buffer, so an
   interactive TUI that has no IPC receives it as if typed. **Measured working
   2026-08-10 on BOTH a classic conhost console and a ConPTY (Windows Terminal
   tab)** — the second is the one that matters, because that is the shape a
   Claude Code session runs in, and a pass on conhost says nothing about it.
   Re-measure with ``python -m adk.decisions.terminal --live-console --conpty``.
   It stays OFF by default behind ``AITHER_DECISIONS_CONSOLE_INPUT=1`` anyway:
   proven-to-work is not the same as safe-to-fire, and it is the only capability
   here that can land characters in a prompt the owner is mid-way through
   typing, corrupting a command rather than failing cleanly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # normal package import
    from adk.decisions import winproc
except ImportError:  # pragma: no cover - loaded by path from a `python -S` hook
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _spec = _ilu.spec_from_file_location(
        "_aither_winproc", str(_Path(__file__).with_name("winproc.py"))
    )
    winproc = _ilu.module_from_spec(_spec)  # type: ignore[assignment]
    _spec.loader.exec_module(winproc)  # type: ignore[union-attr]

_CREATE_NO_WINDOW = 0x08000000


def ascii_safe(text: str) -> str:
    """Text that survives a cp1252 console.

    Terminal tab titles routinely carry a spinner glyph, and every Windows
    console here is cp1252 — printing one raw raises ``UnicodeEncodeError`` and
    turns a passing check into a traceback. Anything printed to a TTY from this
    package goes through here; the Tk window renders the real string.
    """
    return (text or "").encode("ascii", "replace").decode("ascii")


@dataclass
class TerminalTarget:
    """What we could find of the session's terminal. Every field may be empty."""

    pid: int = 0
    alive: bool = False
    hwnd: int = 0
    #: The process that owns ``hwnd`` (``WindowsTerminal.exe`` for a WT tab).
    owner: int = 0
    title: str = ""
    chain: str = ""

    @property
    def focusable(self) -> bool:
        return bool(self.hwnd)

    def describe(self) -> str:
        if not self.pid:
            return "no session process recorded on this card"
        if not self.alive:
            return f"session process {self.pid} has exited"
        if not self.hwnd:
            return f"session {self.pid} is alive but owns no visible window"
        return self.title.strip() or f"window {self.hwnd}"


def locate(pid: int) -> TerminalTarget:
    """Everything we know about the terminal hosting ``pid``. Never raises."""
    target = TerminalTarget(pid=int(pid or 0))
    if not target.pid:
        return target
    try:
        target.alive = winproc.pid_alive(target.pid)
        chain = winproc.ancestry(target.pid)
        target.chain = " <- ".join(name for _p, name in chain[:6])
        if target.alive:
            found = winproc.find_terminal_window(target.pid)
            if found:
                target.hwnd, target.owner, target.title = found
    except OSError as exc:
        # A process table we cannot read is a real answer ("could not look"),
        # so it is surfaced in the field the UI shows rather than swallowed.
        target.chain = f"could not inspect: {exc}"
    return target


def _tab_listing(frames: list[int]) -> list[tuple[int, int, str]]:
    """``[(frame hwnd, tab index, tab title), …]`` across every frame, in order."""
    listing: list[tuple[int, int, str]] = []
    for frame in frames:
        for index, (name, _selected) in enumerate(winproc.list_tabs(frame)):
            listing.append((frame, index, name))
    return listing


def _marker_select(pid: int, frames: list[int], original: str) -> tuple[int, str]:
    """Tie-break: give the session's tab a unique title, select it, restore it.

    Used only when the live title matches several tabs. The title is put back
    ONLY if it still reads the marker — if the session retitled itself in the
    meantime, the newer title wins and ours must not overwrite it.
    """
    marker = f"aither-card {pid} {time.time_ns() % 1_000_000_000}"
    if not winproc.set_console_title(pid, marker):
        return 0, ""
    chosen, name = 0, ""
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not chosen:
            for frame, index, title in _tab_listing(frames):
                if title == marker and winproc.select_tab(frame, index, title):
                    chosen, name = frame, original
                    break
            else:
                time.sleep(0.1)
    finally:
        if winproc.console_title(pid) == marker:
            winproc.set_console_title(pid, original)
    return chosen, name


def select_session_tab(target: TerminalTarget,
                       hints: tuple[str, ...] = ()) -> tuple[int, str, str]:
    """Switch the session's terminal to the session's own tab.

    Returns ``(frame hwnd, tab title, live title)`` on success, or
    ``(0, why not, live title)``. The live title is the session console's own
    title as read here (``""`` when unreadable) — returned so a caller that
    falls back can name the session's tab without reading the console twice.
    Does not raise the window — :func:`focus` does that, after, so the tab is
    already showing when the window comes forward.
    """
    if os.name != "nt" or not target.hwnd:
        return 0, "tab switching is implemented for Windows Terminal only", ""
    live = winproc.console_title(target.pid)
    frames = winproc.terminal_frames(target.owner) if target.owner else []
    if target.hwnd not in frames:
        frames.insert(0, target.hwnd)
    listing = _tab_listing(frames)
    if not listing:
        return 0, "that window has no tabs UI Automation can read", live
    index, how = winproc.match_tab([t for _f, _i, t in listing], live, hints)
    if index >= 0:
        frame, tab_index, title = listing[index]
        if winproc.select_tab(frame, tab_index, title):
            return frame, title, live
        how = "the tab moved before it could be selected"
    if live and "share" in how:
        frame, title = _marker_select(target.pid, frames, live)
        if frame:
            return frame, title, live
    return 0, how, live


def focus(pid: int, hints: tuple[str, ...] = (),
          raise_window: Callable[[int], bool] | None = None) -> tuple[bool, str]:
    """Bring the session's TAB to the front. ``(ok, what happened)``.

    ``hints`` (the card's directory name, branch) are used only when the live
    console title cannot be read. A tab that cannot be pinned down degrades to
    focusing the window, and the message says which one happened — naming the
    SESSION's tab title, not the window title (that is whichever tab is active).

    ``raise_window`` replaces :func:`winproc.focus_window` for the last step, so
    a GUI caller can do the slow tab search on a worker thread and still bring
    the window forward from its own UI thread (the one Windows lets take focus).
    """
    target = locate(pid)
    if not target.focusable:
        return False, target.describe()
    frame, tab_note, live = select_session_tab(target, hints)
    ok = (raise_window or winproc.focus_window)(frame or target.hwnd)
    if not ok:
        if frame:
            return False, ("switched to the tab, but Windows refused to bring "
                           "the window forward")
        return False, "Windows refused to change the foreground window"
    if frame:
        return True, f"focused the tab: {tab_note.strip()}"
    # NOT target.title: the window title is the ACTIVE tab, which is exactly
    # the tab this fallback failed to move away from.
    tab = live.strip()
    where = f" — look for the tab: {tab}" if tab else ""
    return True, f"focused the window, not the tab ({tab_note}){where}"


def open_terminal(cwd: str, app: str = "") -> tuple[bool, str]:
    """Open a NEW terminal at ``cwd``. Not the same tab, and it says so.

    ``app`` selects WHAT runs in it. The default is the platform's own shell —
    the awsh/AitherShell daily driver (``aither``, npm @aitherium/awsh) when
    installed, so "Open a terminal here" starts a live fleet session, not a
    bare prompt (owner 2026-08-29: cards must manage the real shell, not just
    Claude tabs). Pass ``app="shell"`` for a plain terminal.
    """
    directory = cwd or os.getcwd()
    if not os.path.isdir(directory):
        return False, f"no such directory: {directory}"
    kwargs: dict = {"close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    candidates: list[list[str]] = []
    if os.name == "nt":
        if shutil.which("wt.exe"):
            if app != "shell" and shutil.which("aither"):
                # The awsh shell, live in a new tab at the card's directory —
                # the "Open a terminal here" that starts a FLEET session.
                candidates.append(["wt.exe", "-d", directory, "aither"])
            candidates.append(["wt.exe", "-d", directory])
        candidates.append(["cmd.exe", "/c", "start", "", "powershell.exe", "-NoExit",
                           "-Command", f"Set-Location -LiteralPath '{directory}'"])
    elif sys.platform == "darwin":
        candidates.append(["open", "-a", "Terminal", directory])
    else:
        for term in ("wezterm", "alacritty", "gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                candidates.append([term, "--working-directory", directory]
                                  if term == "gnome-terminal" else [term])
                break
    for argv in candidates:
        try:
            subprocess.Popen(argv, cwd=directory, **kwargs)  # noqa: S603 - fixed argv
            return True, f"opened a new terminal in {directory}"
        except (OSError, ValueError):
            continue
    return False, "no terminal emulator could be launched"


# ── typing into a live console (opt-in) ─────────────────────────────────────────


def console_input_enabled() -> bool:
    """OFF unless explicitly enabled, and that is the considered default.

    Every other capability here either succeeds visibly or fails visibly. This
    one can half-succeed: characters land in a prompt the owner is mid-way
    through typing, and the result is a corrupted command rather than an error.
    An opt-in makes that a decision somebody made once, on purpose.
    """
    raw = os.getenv("AITHER_DECISIONS_CONSOLE_INPUT", "").strip().lower()
    if raw:
        # An explicit env value ALWAYS wins, including an explicit "0" — turning
        # this off for one session must not require editing a file.
        return raw in ("1", "true", "yes", "on")
    return _persisted_console_input()


def _persisted_console_input() -> bool:
    """The setting as stored on disk, for processes that never inherited it.

    Env-only was a hole, and it hid the feature's headline failure. A session
    snapshots its environment at start, so arming this reaches NEW processes
    only — every Claude Code tab already open (and every popup it spawns) keeps
    reading "off". Measured 2026-08-11: three answered cards sat undelivered in
    their mailboxes, the oldest for SEVENTEEN HOURS, while all three sessions
    were still alive and reachable.

    That combination is what "I answer the card and nothing happens" actually
    is. The Stop hook holds a turn open for ~50s; measured answer lags that day
    were 9s, 52s, 72s, 194s and 2097s, so four of five missed it. After the miss
    the answer only reaches the agent through the mailbox, which is drained by
    `UserPromptSubmit` — i.e. when the owner types in that terminal. The console
    tier is the one path that reaches an IDLE session, and it was off.

    Same shape as awgit's `enforcement_on()`, which falls back to the persisted
    User-scope value for exactly this reason: a setting configured once must not
    read as "off" to everything already running.
    """
    try:
        raw = (Path.home() / ".aither" / "decisions.json").read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return str(data.get("console_input", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def key_payload(text: str, *, submit: bool = True) -> str:
    """The characters :func:`type_into_console` turns into key events.

    ``submit=False`` carries NO carriage return: the text lands as a draft, and whatever
    the input box already held is not submitted with it (the owner-steer path relies on
    this, ``adk/harnesses/owner_steer.py``).
    """
    return text + "\r" if submit else text


def type_into_console(pid: int, text: str, *, submit: bool = True) -> tuple[bool, str]:
    """Write ``text`` into ``pid``'s console input buffer, as if typed.

    This is the only path INTO an interactive TUI that has no IPC. It works by
    detaching from our own console, attaching to the target's, and pushing key
    events, so it is Windows-only. ConPTY was the open question — a process
    whose console is a pseudo-console owned by Windows Terminal was assumed
    possibly unattachable — and :func:`live_console_probe` settled it on
    2026-08-10: a WT tab accepted the keystrokes. Every failure path still
    reports rather than guesses, because "attached and typed" and "attached and
    nothing arrived" are different outcomes and only the probe can tell them
    apart on a machine we have not measured.
    """
    if not text.strip():
        return False, "nothing to send"
    if not console_input_enabled():
        return False, "console typing is off (set AITHER_DECISIONS_CONSOLE_INPUT=1)"
    if os.name != "nt":
        return False, "console typing is implemented for Windows only"
    if not winproc.pid_alive(pid):
        return False, f"session process {pid} has exited"

    import ctypes
    from ctypes import wintypes

    class _CHAR(ctypes.Union):
        _fields_ = [("UnicodeChar", ctypes.c_wchar), ("AsciiChar", ctypes.c_char)]

    class _KeyEvent(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("uChar", _CHAR),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", _KeyEvent)]

    class _InputRecord(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]

    kernel32 = ctypes.windll.kernel32
    payload = key_payload(text, submit=submit)
    records = (_InputRecord * (len(payload) * 2))()
    for index, char in enumerate(payload):
        for offset, down in ((0, True), (1, False)):
            record = records[index * 2 + offset]
            record.EventType = 1  # KEY_EVENT
            record.Event.KeyEvent.bKeyDown = down
            record.Event.KeyEvent.wRepeatCount = 1
            record.Event.KeyEvent.wVirtualKeyCode = 0x0D if char == "\r" else 0
            record.Event.KeyEvent.uChar.UnicodeChar = char

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(int(pid)):
        error = ctypes.get_last_error() or kernel32.GetLastError()
        return False, f"could not attach to that session's console (win32 error {error})"
    try:
        handle = kernel32.CreateFileW(
            "CONIN$", 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None,
        )
        if handle == -1 or handle == 0:
            return False, "the session's console refused a handle"
        written = wintypes.DWORD(0)
        ok = kernel32.WriteConsoleInputW(
            handle, records, len(records), ctypes.byref(written)
        )
        kernel32.CloseHandle(handle)
        if not ok:
            return False, "WriteConsoleInput was rejected"
        return True, f"typed {len(text)} chars into the session"
    finally:
        kernel32.FreeConsole()


def capabilities(pid: int, cwd: str) -> dict[str, str]:
    """What this card can actually do to its terminal, in words the UI shows.

    Reported rather than assumed: the popup renders exactly this, so a control
    that will not work is labelled before it is clicked instead of after.
    """
    target = locate(pid)
    # The SESSION's tab title, read from its console. The window title is the
    # title of whichever tab is active, which is the wrong tab exactly when the
    # card is needed (the owner is somewhere else).
    tab = winproc.console_title(target.pid) if target.alive else ""
    return {
        "focus": "ready" if target.focusable else f"unavailable — {target.describe()}",
        "open": "ready" if (cwd and os.path.isdir(cwd)) else "unavailable — no directory",
        "type": (
            "ready" if (console_input_enabled() and target.alive and os.name == "nt")
            else ("off — set AITHER_DECISIONS_CONSOLE_INPUT=1"
                  if not console_input_enabled() else "unavailable — session gone")
        ),
        "tab": (tab or target.title).strip(),
        "chain": target.chain,
    }


def _spawn_reader(conpty: bool) -> tuple[int, "Path", str]:
    """Start a process that reads ONE line and records it. ``(pid, marker, note)``.

    The reader publishes its own pid to a file rather than being identified from
    the launcher's return value: under Windows Terminal the process we start is
    ``wt.exe``, which hands off and exits, so its pid is not the reader's and
    typing at it would be measuring nothing.
    """
    import tempfile

    workspace = Path(tempfile.mkdtemp())
    marker = workspace / "typed.txt"
    pidfile = workspace / "pid.txt"
    # A FILE, not `python -c`. wt.exe treats `;` as its own command separator, so
    # a one-liner reader is torn in half and the tab runs a fragment — which the
    # probe then reports as "the reader never started", blaming the feature for a
    # defect in the probe. Measured: that is exactly what happened first.
    script = workspace / "reader.py"
    script.write_text(
        "import os, sys\n"
        f"open(r'{pidfile}', 'w').write(str(os.getpid()))\n"
        f"open(r'{marker}', 'w', encoding='utf-8').write(sys.stdin.readline())\n",
        encoding="utf-8",
    )
    if conpty:
        # A Windows Terminal tab, i.e. a ConPTY — the shape a Claude Code
        # session actually runs in, and the only shape whose verdict matters
        # for the card's "type into that terminal" control.
        #
        # THIS OPENS A REAL TAB AND TAKES FOCUS. There is no hidden ConPTY: a
        # pseudo-console needs a terminal attached to it, and that terminal is a
        # window. Running it a few times in a row is indistinguishable from the
        # focus-stealing spam quality gate 1t exists to stop — which is exactly
        # what it looked like to the owner on 2026-08-10. Hence the extra
        # acknowledgement flag on the CLI, and hence it is in no sweep, no
        # self-test and no routine.
        argv = ["wt.exe", "-w", "-1", "nt", sys.executable, str(script)]
        note = "ConPTY (Windows Terminal tab)"
        flags = 0
    else:
        argv = [sys.executable, str(script)]
        note = "classic console (conhost, no window)"
        # CREATE_NO_WINDOW, not CREATE_NEW_CONSOLE: the child still gets a real
        # console we can attach to and type into, but NO window is allocated, so
        # nothing appears on screen and nothing takes focus. CREATE_NEW_CONSOLE
        # flashed a console every run.
        flags = _CREATE_NO_WINDOW
    subprocess.Popen(argv, creationflags=flags)  # noqa: S603 - fixed argv, no shell

    import time as _time

    deadline = _time.time() + 15
    while _time.time() < deadline:
        if pidfile.exists():
            raw = pidfile.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                return int(raw), marker, note
        _time.sleep(0.2)
    return 0, marker, note


def live_console_probe(conpty: bool = False) -> int:
    """Does :func:`type_into_console` actually type? Spawn a reader and find out.

    This exists because the honest answer to "does it work" was, for one turn,
    "it is implemented" — and an implemented-but-unproven control is the same
    thing as a control that silently does nothing, which is precisely what the
    rest of this module refuses to ship.

    It is a LIVE probe, not part of ``--self-test``: it spawns a real child with
    its own console, so it needs a desktop and it flashes a window. Run it after
    touching the typing path. Exit 0 typing works here, 1 it does not, 2 the
    probe could not reach a verdict — which is never reported as success.

    ``--conpty`` runs it against a Windows Terminal tab instead of a classic
    console. That distinction is the whole point: a pass on conhost says
    nothing about the case the feature is FOR.
    """
    if os.name != "nt":
        print("NOT VERIFIED - console typing is Windows-only")
        return 2

    import time

    if conpty and "--yes-open-a-tab" not in sys.argv:
        # Refuse rather than surprise. The ConPTY variant cannot be made
        # invisible, so firing it casually — or in a loop — is focus-stealing
        # window spam, and it has already been experienced as exactly that.
        print("REFUSED - the ConPTY probe OPENS A WINDOWS TERMINAL TAB and takes "
              "focus.\n          Re-run with --yes-open-a-tab if you mean it. "
              "Run it ONCE, never in a loop or a sweep.")
        return 2

    try:
        pid, marker, note = _spawn_reader(conpty)
    except OSError as exc:
        print(f"NOT VERIFIED - could not spawn a console reader: {exc}")
        return 2
    if not pid:
        print(f"NOT VERIFIED - the {note} reader never reported its pid")
        return 2
    print(f"  reader pid {pid} in a {note}")

    previous = os.environ.get("AITHER_DECISIONS_CONSOLE_INPUT")
    os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = "1"
    ok, why, typed = False, "", ""
    try:
        time.sleep(1.0)  # let the child reach its readline
        ok, why = type_into_console(pid, "PROBE-OK")
        print(f"  type_into_console -> {ok}: {ascii_safe(why)}")
        # A generous window, and ONE resend halfway. Measured: this probe passed
        # three times standing alone and failed once inside a full verification
        # sweep, purely on scheduling — and a flaky live probe is worse than no
        # probe, because it teaches you to re-run until green, which is how a
        # real regression gets waved through. Resending is safe: the child reads
        # exactly one line, so a duplicate is discarded by the OS buffer.
        deadline = time.time() + 25
        resent = False
        while time.time() < deadline:
            if marker.exists():
                typed = marker.read_text(encoding="utf-8", errors="replace").strip()
                if typed:
                    break
            if not resent and time.time() > deadline - 15:
                resent = True
                again, _why = type_into_console(pid, "PROBE-OK")
                print(f"  nothing yet after 10s; resent -> {again}")
            time.sleep(0.25)
    finally:
        if previous is None:
            os.environ.pop("AITHER_DECISIONS_CONSOLE_INPUT", None)
        else:
            os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = previous
        if winproc.pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],  # noqa: S603
                           capture_output=True, encoding="utf-8", errors="replace",
                           creationflags=_CREATE_NO_WINDOW)

    if typed == "PROBE-OK":
        print(f"LIVE: console typing WORKS on a {note}")
        return 0
    if ok:
        # The dangerous outcome: the call reported success and nothing arrived.
        print(f"LIVE: on a {note} console typing REPORTED SUCCESS BUT NOTHING "
              f"ARRIVED (child read {typed!r}) - this is the silent-no-op case")
        return 1
    print(f"LIVE: console typing does not work on a {note} - {ascii_safe(why)}")
    return 1


def _wait_for_tab(frames: list[int], title: str, seconds: float) -> tuple[int, int]:
    """``(frame, index)`` of the tab reading exactly ``title``, or ``(0, -1)``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for frame, index, name in _tab_listing(frames):
            if name == title:
                return frame, index
        time.sleep(0.2)
    return 0, -1


def _selected_index(frame: int) -> int:
    for index, (_name, selected) in enumerate(winproc.list_tabs(frame)):
        if selected:
            return index
    return -1


def live_tab_switch_probe() -> int:
    """Does :func:`winproc.select_tab` switch to a tab that is NOT active? Find out.

    The read-only self-test proves the tabs can be READ and the right one found;
    it never proves ``SelectionItem.Select`` moves the terminal off the active
    tab, because doing so changes what the owner is looking at. This does, so it
    is opt-in (``AITHER_DECISIONS_LIVE_TAB=1`` on the self-test, or
    ``--live-tab``) and meant to be run once with the owner watching:

    1. open a scratch tab in this session's terminal window (WT makes it active;
       it closes itself after ~20 s);
    2. select the tab that was active before, through UI Automation;
    3. assert, by re-reading ``IsSelected``, that it is now the active tab.

    Exit 0 the switch worked, 1 it did not, 2 no verdict (not Windows Terminal,
    ``wt`` missing, the scratch tab never appeared) — never 0 on silence.
    """
    if os.name != "nt":
        print("live tab probe: not Windows — no verdict")
        return 2
    wt = shutil.which("wt") or shutil.which("wt.exe")
    target = locate(winproc.resolve_owner_pid(os.getpid()))
    if not wt or not target.hwnd:
        print(f"live tab probe: no verdict — wt={wt!r}, window={target.hwnd}")
        return 2
    frames = winproc.terminal_frames(target.owner) or [target.hwnd]
    before = _selected_index(target.hwnd)
    tabs = winproc.list_tabs(target.hwnd)
    if before < 0 or not tabs:
        print("live tab probe: no verdict — this window shows no selected tab")
        return 2
    original = tabs[before][0]
    scratch = f"aither-tab-probe {time.time_ns() % 1_000_000_000}"
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [wt, "-w", "0", "new-tab", "--title", scratch, "--suppressApplicationTitle",
         "cmd", "/c", "ping -n 20 127.0.0.1 >nul"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frame, index = _wait_for_tab(frames, scratch, 8.0)
    if not frame:
        print("live tab probe: no verdict — the scratch tab never appeared")
        return 2
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _selected_index(frame) != index:
        time.sleep(0.1)
    if _selected_index(frame) != index:
        print("live tab probe: no verdict — the scratch tab never became active")
        return 2
    # Go back to the tab that was active before — in the scratch tab's frame
    # when it is ours, else any other tab of the scratch frame.
    if frame == target.hwnd:
        back_index, back_title = before, original
    else:
        names = [n for n, _sel in winproc.list_tabs(frame)]
        back_index = 0 if index != 0 else 1
        if back_index >= len(names):
            print("live tab probe: no verdict — the scratch window has one tab")
            return 2
        back_title = names[back_index]
    if not winproc.select_tab(frame, back_index, back_title):
        print(f"live tab probe: FAIL — Select refused tab {back_index} "
              f"({ascii_safe(back_title)})")
        return 1
    time.sleep(0.3)
    now = _selected_index(frame)
    if now != back_index:
        print(f"live tab probe: FAIL — Select returned ok but tab {now} is active, "
              f"not {back_index}")
        return 1
    print(f"live tab probe: ok — switched off the active scratch tab to tab "
          f"{back_index} ({ascii_safe(back_title)}); the scratch tab closes itself")
    return 0


def _self_test() -> int:
    """Prove the honest-failure paths really fail, not that a terminal appeared.

    Opening a window and focusing it cannot be asserted without a human looking
    at the screen. What CAN be asserted — and is where this module would rot —
    is that every unavailable path returns ``False`` with a reason, rather than
    ``True`` with nothing happening.
    """
    problems: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {name} {detail if not condition else ''}")
        if not condition:
            problems.append(name)

    dead = locate(0)
    check("a card with no pid is not focusable", not dead.focusable)
    check("and says why", "no session process" in dead.describe(), dead.describe())

    ok, why = focus(0)
    check("focusing nothing fails", not ok and bool(why), why)

    ok, why = open_terminal("Z:/definitely/not/here")
    check("opening a missing directory fails", not ok and "no such directory" in why, why)

    previous = os.environ.get("AITHER_DECISIONS_CONSOLE_INPUT")
    # An explicit "0", not "": an empty env falls through to the persisted
    # ~/.aither/decisions.json, and on a box where the owner armed typing that
    # made this check type into its own console and fail (measured 2026-10-04).
    os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = "0"
    ok, why = type_into_console(os.getpid(), "echo hi")
    check("typing is refused while opt-out", not ok and "off" in why, why)
    os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = "1"
    ok, why = type_into_console(0, "echo hi")
    check("typing at a dead pid is refused", not ok, why)
    ok, why = type_into_console(os.getpid(), "   ")
    check("typing whitespace is refused", not ok and "nothing to send" in why, why)
    if previous is None:
        os.environ.pop("AITHER_DECISIONS_CONSOLE_INPUT", None)
    else:
        os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = previous

    # The tab matcher is what decides WHICH tab gets selected; a regression here
    # selects a wrong tab and reports success, so its rules are pinned here too.
    owner_tabs = ["◐ AitherOS-Fresh develop 16:03", "✳ AitherOS-Fresh develop 07:37",
                  "◑ AitherOS-Fresh develop 09:29"]
    index, how = winproc.match_tab(owner_tabs, "◑ AitherOS-Fresh develop 07:37")
    check("a spinner-frame change still finds the tab", index == 1, how)
    index, how = winproc.match_tab(owner_tabs + [owner_tabs[1]], owner_tabs[1])
    check("two identical tabs are never guessed between", index == -1 and "2" in how, how)
    index, how = winproc.match_tab(owner_tabs, "", ("AitherOS-Fresh", "develop"))
    check("hints that name three tabs pick none", index == -1, how)
    # The OWNER pid (claude.exe, say), not ours: a tool-spawned interpreter has
    # its own hidden console whose title is no tab's title.
    target = locate(winproc.resolve_owner_pid(os.getpid()))
    if target.hwnd:
        frames = winproc.terminal_frames(target.owner) or [target.hwnd]
        listing = _tab_listing(frames)  # read-only: nothing on screen changes
        print(f"  info {len(listing)} tab(s) readable in this terminal")
        live = winproc.console_title(target.pid)
        index, how = winproc.match_tab([t for *_x, t in listing], live)
        print(f"  info this session's tab: index {index} ({ascii_safe(how)})")

    caps = capabilities(os.getpid(), os.getcwd())
    check("capabilities names every control",
          set(caps) >= {"focus", "open", "type"}, str(sorted(caps)))
    check("capabilities can say 'unavailable'",
          all(isinstance(v, str) for v in caps.values()))
    here = locate(os.getpid())
    print(f"  info this process resolves to: {ascii_safe(here.describe())[:80]}")
    print(f"  info chain: {ascii_safe(here.chain)}")

    if os.environ.get("AITHER_DECISIONS_LIVE_TAB") == "1":
        # Opt-in: it changes the owner's active tab. No verdict (2) is reported,
        # not failed; a real failure (1) fails the self-test.
        check("Select switches to a tab that is not active",
              live_tab_switch_probe() != 1)
    else:
        print("  info live tab switch not run (AITHER_DECISIONS_LIVE_TAB=1 runs it)")

    print()
    if problems:
        print(f"terminal self-test FAILED — {', '.join(problems)}")
        return 1
    print("terminal self-test passed — every unavailable path refuses with a reason")
    return 0


if __name__ == "__main__":
    if "--live-tab" in sys.argv:
        raise SystemExit(live_tab_switch_probe())
    if "--live-console" in sys.argv:
        raise SystemExit(live_console_probe(conpty="--conpty" in sys.argv))
    raise SystemExit(_self_test())
