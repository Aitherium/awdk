"""Reach the terminal a card came from — focus it, open one, or type into it.

The card window used to be a dead end in one specific way: it named a session
("claude-code · fix/verify-products") and a working directory, and then left the
owner to go FIND that tab among a dozen identical ones. The ask it exists to
carry is "do this next", and the last mile of that ask was manual.

Three capabilities, in descending order of how reliably they work. Each reports
what it actually did, because a control that silently does nothing is worse than
no control — that is `.claude/rules/security-review-patterns.md` §5, and this
module is where it would hide.

1. **focus** — bring the hosting window forward. Works whenever the session
   process is still alive and owns (or is descended from something that owns) a
   real window. This focuses the WINDOW, not the TAB: Windows Terminal exposes
   no supported way to activate one tab of an existing window from outside, so
   the honest promise is "your terminal is now in front, on whatever tab it was
   on". The tab TITLE is reported so the owner knows which one to look for.
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
from dataclasses import dataclass
from pathlib import Path

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
                target.hwnd, _owner, target.title = found
    except OSError as exc:
        # A process table we cannot read is a real answer ("could not look"),
        # so it is surfaced in the field the UI shows rather than swallowed.
        target.chain = f"could not inspect: {exc}"
    return target


def focus(pid: int) -> tuple[bool, str]:
    """Bring the session's terminal to the front. ``(ok, what happened)``."""
    target = locate(pid)
    if not target.focusable:
        return False, target.describe()
    ok = winproc.focus_window(target.hwnd)
    if not ok:
        return False, "Windows refused to change the foreground window"
    tab = target.title.strip()
    return True, (f"focused — look for the tab: {tab}" if tab else "focused the terminal")


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
    payload = text if not submit else text + "\r"
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
    return {
        "focus": "ready" if target.focusable else f"unavailable — {target.describe()}",
        "open": "ready" if (cwd and os.path.isdir(cwd)) else "unavailable — no directory",
        "type": (
            "ready" if (console_input_enabled() and target.alive and os.name == "nt")
            else ("off — set AITHER_DECISIONS_CONSOLE_INPUT=1"
                  if not console_input_enabled() else "unavailable — session gone")
        ),
        "tab": target.title.strip(),
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
    os.environ["AITHER_DECISIONS_CONSOLE_INPUT"] = ""
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

    caps = capabilities(os.getpid(), os.getcwd())
    check("capabilities names every control",
          set(caps) >= {"focus", "open", "type"}, str(sorted(caps)))
    check("capabilities can say 'unavailable'",
          all(isinstance(v, str) for v in caps.values()))
    here = locate(os.getpid())
    print(f"  info this process resolves to: {ascii_safe(here.describe())[:80]}")
    print(f"  info chain: {ascii_safe(here.chain)}")

    print()
    if problems:
        print(f"terminal self-test FAILED — {', '.join(problems)}")
        return 1
    print("terminal self-test passed — every unavailable path refuses with a reason")
    return 0


if __name__ == "__main__":
    if "--live-console" in sys.argv:
        raise SystemExit(live_console_probe(conpty="--conpty" in sys.argv))
    raise SystemExit(_self_test())
