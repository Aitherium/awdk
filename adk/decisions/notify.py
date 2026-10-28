"""Get the owner's attention without stealing their keyboard.

A card in a store nobody looks at is the original problem with extra steps, so a
raise has to *push*. That push has three hard constraints, and each one has already
bitten this repo:

1. **It must not steal focus.** Spawning ``powershell.exe`` from Python allocates a
   console on the interactive desktop that takes focus and eats keystrokes for as
   long as it lives. This is quality-gate 1t (STW001) — and note that
   ``-WindowStyle Hidden`` does NOT fix it (STW002): the console is allocated
   before the shell can hide it, so it flashes every single time. The fix is the
   ``CREATE_NO_WINDOW`` creation flag, which prevents allocation outright.

2. **It must not block the agent.** Notification is fire-and-forget. A toast
   backend that hangs must cost the raising session nothing, so everything here is
   spawned detached with a timeout and failures are returned, never raised.

3. **It must coalesce.** Cards fire on every fork, so a naive
   one-notification-per-card turns into a storm that trains the owner to dismiss
   without reading — which is exactly the outcome this feature exists to
   prevent. Inside the quiet window a burst collapses into one "N decisions
   waiting" summary.

The Windows toast channel is REMOVED (2026-08-31, owner decision): card toasts
presented under the PowerShell app id ("{1AC14E77-...}\\WindowsPowerShell\\...")
and their clicks led nowhere — the launch URL had no registered handler
(DTOAST001, measured 2026-08-30). The card WINDOW is the channel; the tray
badge, the desk deck and the Discord fanout are the bells. DTOAST001 asserts
both twin copies stay free of Windows toast code.

And since 2026-10-05 the WINDOW itself yields to the desk. Owner, on the two
parallel dialogues: the awask notifications and decisions "need to actually
come up in awdesk ... not a separate thing". awdesk is always on screen and
watches the card store directly, so while its liveness heartbeat is fresh
(:func:`desk_alive`) a raise delivers to the DESK and does NOT spawn the
standalone Tk window beside it — one ask, one surface. When the heartbeat is
missing or stale (the desk is not running, or died without cleaning up) the
Tk window is back on the very next raise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from adk.decisions.quiet import is_quiet, quiet_reason
from adk.decisions.render import human_age, render_summary
from adk.decisions.store import STATUS_OPEN, DecisionCard, DecisionStore, decisions_dir
from adk.decisions.triage import decisions_waiting

#: Within this many seconds of the last raise, a new card is folded into a single
#: summary rather than raising its own window.
QUIET_WINDOW_SECONDS = float(os.getenv("AITHER_DECISIONS_QUIET_SECONDS", "45"))

#: The desk's liveness heartbeat, inside the card store's own directory (respecting
#: ``AITHER_DECISIONS_DIR``). awdesk rewrites it every 20 s while it is running with
#: its card watcher up.
DESK_ALIVE_FILENAME = ".desk-alive"

#: A heartbeat older than this is a desk that died (or was killed) without clearing
#: its file, NOT a desk that is about to show the card. The desk beats every 20 s, so
#: this is more than four missed beats; the file must read as dead well before a
#: raise is lost, because the cost of guessing wrong is a card nobody sees.
DESK_ALIVE_STALE_SECONDS = 90.0


@dataclass
class NotifyResult:
    """What actually happened. Reported, never raised."""

    delivered: list[str]
    skipped: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.delivered)

    def describe(self) -> str:
        parts = []
        if self.delivered:
            parts.append("notified via " + ", ".join(self.delivered))
        if self.skipped:
            parts.append("skipped " + ", ".join(self.skipped))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return " · ".join(parts) or "no notification channels available"


def _state_file() -> Path:
    return decisions_dir() / ".notify-state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    target = _state_file()
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        # Losing the coalescing state costs one extra toast. It must never cost a
        # raise, so this is deliberately swallowed rather than propagated.
        return


# ── macOS / Linux ───────────────────────────────────────────────────────────────


def _macos_toast(title: str, body: str, **_: object) -> Optional[str]:
    script = (
        f'display notification "{body[:200]}" with title "{title[:100]}" '
        'sound name "Submarine"'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"osascript unavailable: {exc}"
    return None if proc.returncode == 0 else (proc.stderr or "").strip()[:300]


def _linux_toast(title: str, body: str, *, urgency: str = "normal") -> Optional[str]:
    level = {"critical": "critical", "high": "critical"}.get(urgency, "normal")
    try:
        proc = subprocess.run(
            ["notify-send", "-u", level, "-a", "AitherShell", title, body],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"notify-send unavailable: {exc}"
    return None if proc.returncode == 0 else (proc.stderr or "").strip()[:300]


def native_toast(title: str, body: str, *, urgency: str = "normal") -> Optional[str]:
    """Platform-appropriate native notification. Returns an error string or None.

    Windows is REMOVED from this channel (2026-08-31, owner decision) — the
    card window, tray badge, deck and Discord fanout carry the push there, and
    DTOAST001 asserts the twin copies stay free of Windows toast code. The
    remaining macOS/Linux toasts are OFF by default — see ``toast_enabled``.
    """
    if sys.platform.startswith("win"):
        return "windows toast channel removed 2026-08-31 (owner decision)"
    if not toast_enabled():
        return "off by default (set AITHER_DECISIONS_TOAST=1 to enable)"
    # Quiet (Do-not-disturb, or a full-screen app): the card stays in the store
    # and the badge; only the interruption is held.
    quiet, why = is_quiet()
    if quiet:
        return f"held while quiet: {why}"
    if sys.platform == "darwin":
        return _macos_toast(title, body)
    return _linux_toast(title, body, urgency=urgency)


def toast_enabled() -> bool:
    """Native toasts are OPT-IN, and that is a considered default, not an oversight.

    Measured against the owner's actual workflow, a toast fails as the channel for
    this: it is a two-line strip that queues in the same tray as mail and chat, so
    it is triaged as noise; it carries no room for the facts, the options or their
    consequences, so the content that makes a card decidable never reaches the
    screen; it does not interrupt; and clicking it does nothing, because a toast
    cannot host controls. Every one of those is fatal for "you are the bottleneck".

    The card WINDOW is the channel. The toast stays available for the one case the
    window cannot serve — a headless or SSH-only box with no display. (Windows is
    excluded outright since 2026-08-31: see ``native_toast``.)
    """
    if sys.platform.startswith("win"):
        return False
    flag = os.getenv("AITHER_DECISIONS_TOAST", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def popup_enabled() -> bool:
    """The card window, unless explicitly disabled or there is no display.

    Two switches, OR-ed off: the ``AITHER_DECISIONS_POPUP`` env var (process
    scope — only NEW processes see it) and the kill-switch FILE
    ``~/.aither/decisions/.popup-off`` (read at call time — RUNNING processes
    honor it on their next raise, no restart). The file exists because the
    owner's background-first preference must be enforceable against sessions
    that started before the env was set — measured 2026-08-29: 18 long-lived
    sessions could not see the env var, and each card raise still popped a
    focus-stealing topmost window on the desktop.

    A third, automatic one: QUIET (``adk.decisions.quiet`` -- Do-not-disturb, or a
    full-screen game/app/presentation in front). Owner, 2026-09-23: cards kept
    popping up over a game. A held card is not dropped -- it stays open in the
    store and the badge -- and because this answers False the at-desk DM
    suppression in ``channels`` no longer applies, so the DM carries it.
    """
    flag = os.getenv("AITHER_DECISIONS_POPUP", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if is_quiet()[0]:
        return False
    try:
        if (Path.home() / ".aither" / "decisions" / ".popup-off").is_file():
            return False
    except OSError:
        # An unreadable home is not a reason to start popping windows.
        return True
    if not sys.platform.startswith("win") and sys.platform != "darwin":
        # An X-less Linux box would raise a window nobody can see; fall back to
        # the toast path rather than spawning a process that cannot draw.
        return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
    return True


def desk_alive(store_dir: Optional[Path] = None) -> bool:
    """Is awdesk running with its card watcher up, on THIS card store?

    The desk writes ``<store>/.desk-alive`` every 20 s (``startHeartbeat`` in
    awdesk's ``decision-cards.cjs``); the file's mtime is the
    liveness signal, and its CONTENT (``{"pid":…,"at":…}``) is informational —
    a killed app leaves the file behind, so existence alone proves nothing and a
    heartbeat older than :data:`DESK_ALIVE_STALE_SECONDS` reads as dead.

    Why this exists, in the owner's words (2026-10-05): the awask notifications
    and decisions "need to actually come up in awdesk … not a separate thing".
    The desk is always on screen and watches this directory, so while it is up a
    raised card is ALREADY on the owner's surface — spawning the standalone Tk
    window as well is the second, rival dialogue. This is deliberately checked
    at RAISE time and not cached: the desk can start or die between two raises,
    and every failure mode here must fall toward the window (an extra window
    beats an invisible card), which is what a missing/unreadable/stale file
    returning False gives.

    Kills the window ONLY — every explicit door stays open: ``awask window``
    (and the desk's own "Pop out") still spawns the Tk window unconditionally,
    and the kill-switches (``AITHER_DECISIONS_POPUP``, the ``.popup-off``
    kill-file, quiet) are what they were.
    """
    try:
        beat = (Path(store_dir) if store_dir is not None else decisions_dir())
        age = time.time() - (beat / DESK_ALIVE_FILENAME).stat().st_mtime
    except OSError:
        # Absent, unreadable, or an unreadable decisions dir: not alive. A
        # heartbeat the desk never wrote is exactly the pre-2026-10-05 world,
        # and the window must keep working there.
        return False
    # No lower bound on `age`: a heartbeat with an mtime slightly in the future
    # (same-machine clock jitter) is a desk that JUST wrote it, not a dead one.
    return age < DESK_ALIVE_STALE_SECONDS


# ── starting the desk: where a credential ask goes when the desk is down ───────
#
# Owner, 2026-10-28: credential intake goes through awdesk/awconnect, "not one-off
# Python scripts". The masked field lives in the desk Inbox (its CredentialRow pipes
# the value into the vault's stdin door), so a credential ask that finds the desk
# down STARTS it, the way this machine starts it at logon, and waits for its
# heartbeat. Nothing here opens any other window.

#: The command awsh already reads for "how do I start the desk" (an explicit override).
DESK_START_ENV = "AWDESK_START_CMD"

#: Seconds to wait for a desk this process started to write its first heartbeat
#: (the beat starts after the desk's card watcher, so it is not instant). Measured
#: 2026-10-28 on the owner's desktop: 15 s from the logon launcher to the first beat.
DESK_START_WAIT_SECONDS = float(os.getenv("AITHER_DESK_START_WAIT", "60"))

#: The desk's own login entries (awdesk linux-integration.cjs / macos-integration.cjs).
_LINUX_DESKTOP_ID = "aither-desk.desktop"
_MAC_AGENT_LABEL = "com.aitherium.desk"

#: CREATE_NO_WINDOW alone, never with DETACHED_PROCESS: a console launcher gets a
#: console with NO window, and the console programs IT runs inherit that hidden console
#: instead of each being handed a new, visible one (a detached parent has none to give).
_LAUNCHER_FLAGS = 0x08000000


@dataclass
class DeskLauncher:
    """How this machine starts awdesk: the argv, and one line naming it."""

    argv: list[str]
    how: str


def _split_command(cmd: str, *, windows: bool) -> list[str]:
    import shlex

    # posix=False on Windows: POSIX mode reads a backslash as an escape and mangles
    # every path. It keeps the quotes on a quoted argument instead, so strip them.
    parts = shlex.split(cmd, posix=not windows)
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p
            for p in parts]


def _desktop_entry_argv(path: Path) -> Optional[list[str]]:
    """The ``Exec=`` argv of a freedesktop entry, its ``%U``-style field codes dropped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Exec="):
            argv = [a for a in _split_command(line[len("Exec="):], windows=False)
                    if not (len(a) == 2 and a.startswith("%"))]
            return argv or None
    return None


def desk_launcher(env: Optional[dict] = None, home: Optional[Path] = None,
                  platform: Optional[str] = None) -> Optional[DeskLauncher]:
    """How THIS machine normally starts awdesk, or None when it is not installed.

    In order: the explicit ``AWDESK_START_CMD``; on Windows the logon launcher in the
    Startup folder (``*awdesk*.vbs``, the same bytes that start it at every logon, run
    by cscript in a hidden console) and then the installed ``Desk.exe``; on macOS the desk's
    LaunchAgent; on Linux its autostart entry, then its menu entry. None is "not
    installed here", never a guessed path.
    """
    env = os.environ if env is None else env
    plat = platform or sys.platform
    home = Path(home) if home is not None else Path.home()
    windows = plat.startswith("win")
    explicit = (env.get(DESK_START_ENV) or "").strip()
    if explicit:
        argv = _split_command(explicit, windows=windows)
        return DeskLauncher(argv, f"{DESK_START_ENV} command") if argv else None
    if windows:
        appdata = (env.get("APPDATA") or "").strip()
        startup = (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                   / "Startup") if appdata else None
        shims = sorted(startup.glob("*awdesk*.vbs")) if startup and startup.is_dir() else []
        if shims:
            system_root = (env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows")
            # cscript, not wscript. The shim runs a console program through
            # WScript.Shell.Exec (its netstat port check); under the windowless wscript
            # that child is handed a NEW console. Measured 2026-10-28, twice each:
            # wscript -> 1 new top-level window (OpenConsole.exe PseudoConsoleWindow);
            # cscript started with _LAUNCHER_FLAGS -> 0, the child shares its hidden
            # console. Same script, same exit codes; electron is GUI and attaches to none.
            cscript = str(Path(system_root) / "System32" / "cscript.exe")
            return DeskLauncher([cscript, "//B", "//Nologo", str(shims[0])],
                                f"logon launcher {shims[0].name}")
        try:
            from adk.desk_release import find_desk_exe, installed_desk_windows
        except ImportError:  # awask without awdk: only the launchers above exist
            return None
        exe = find_desk_exe(installed_desk_windows())
        return DeskLauncher([exe, "--background"], "installed Desk.exe") if exe else None
    if plat == "darwin":
        agent = home / "Library" / "LaunchAgents" / f"{_MAC_AGENT_LABEL}.plist"
        if agent.is_file() and hasattr(os, "getuid"):
            return DeskLauncher(["launchctl", "kickstart",
                                 f"gui/{os.getuid()}/{_MAC_AGENT_LABEL}"],
                                f"LaunchAgent {_MAC_AGENT_LABEL}")
        return None
    for entry in (home / ".config" / "autostart" / _LINUX_DESKTOP_ID,
                  home / ".local" / "share" / "applications" / _LINUX_DESKTOP_ID):
        argv = _desktop_entry_argv(entry) if entry.is_file() else None
        if argv:
            return DeskLauncher(argv, f"desktop entry {entry.name}")
    return None


def _spawn_detached(argv: list[str]) -> Optional[str]:
    """Run the launcher with no console window and no handle on it. An error, or None."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = _LAUNCHER_FLAGS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv from desk_launcher, no shell
    except (OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def start_desk(*, store_dir: Optional[Path] = None,
               launcher: Optional[DeskLauncher] = None,
               wait_s: Optional[float] = None,
               spawn=None, sleep=time.sleep, clock=time.monotonic) -> "tuple[bool, str]":
    """Start awdesk through its own launcher; ``(True, why)`` once its heartbeat is fresh.

    Never raises and never opens anything but the desk. ``(False, why)`` names the
    step that failed (not installed, the launcher would not run, no heartbeat in
    time), so the caller can move on to the remote surfaces and say why.
    """
    if desk_alive(store_dir):
        return True, "awdesk is running"
    found = launcher if launcher is not None else desk_launcher()
    if found is None:
        return False, "awdesk is not installed on this machine (no launcher found)"
    # 🚩 A TEST RUN MUST NEVER START THE OWNER'S DESK. Keyed on the marker pytest sets
    # for every test, so a new test file is safe without remembering to stub this;
    # a test that means to start one passes its own `spawn`.
    if spawn is None and (os.environ.get("PYTEST_CURRENT_TEST")
                          or os.environ.get("AITHER_NO_GUI_PROMPT")):
        return False, f"awdesk start suppressed under test (would run the {found.how})"
    error = (spawn or _spawn_detached)(list(found.argv))
    if error:
        return False, f"the {found.how} did not start ({error})"
    budget = DESK_START_WAIT_SECONDS if wait_s is None else float(wait_s)
    started = clock()
    while True:
        if desk_alive(store_dir):
            return True, (f"started awdesk with its {found.how} "
                          f"({clock() - started:.0f} s to its first heartbeat)")
        if clock() - started >= budget:
            return False, (f"ran the {found.how}, but awdesk wrote no heartbeat in "
                           f"{budget:.0f} s")
        sleep(0.5)


def raise_desk_inbox(timeout: float = 4.0) -> Optional[str]:
    """Ask the running desk to bring its Inbox forward. An error string, or None.

    ``POST /console/open {"pane": "inbox"}`` on the desk bridge -- the same door the
    escalation ladder uses. The Inbox leads with the asks only the owner can unblock,
    credential first, so the masked field is what it shows. Used only when the OWNER
    asked for the field (a remote "open it" tap); an agent's raise leaves the desk's
    own Do-not-disturb and raise policy in charge.
    """
    import urllib.error
    import urllib.request

    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("AITHER_NO_GUI_PROMPT"):
        return "the desk Inbox is not raised under test"
    base = (os.getenv("AWDESK_URL") or os.getenv("AITHERSHELL_AWDESK_URL")
            or "http://127.0.0.1:47931").rstrip("/")
    req = urllib.request.Request(base + "/console/open", method="POST",
                                 data=json.dumps({"pane": "inbox"}).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            if response.status >= 400:
                return f"desk bridge HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"desk bridge unreachable: {exc}"
    return None


def open_card_window(card_id: str) -> Optional[str]:
    """Spawn the card window DETACHED. Returns an error string, or None.

    Detached because the window lives until the owner acts on it, and the process
    that raised the card must not wait for that — an agent blocked on its own
    notification is the bug this feature exists to remove.
    """
    argv = [sys.executable, "-m", "adk.decisions.popup", card_id]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parents[2]),
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW: no console is allocated, so the
        # only thing that appears is the card itself (quality gate 1t).
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed argv, no shell
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


# ── webhook fan-out (Awconnect / Relay / portal) ────────────────────────────


def _webhook_post(card: DecisionCard, open_count: int) -> Optional[str]:
    """Best-effort POST to a local fan-out endpoint.

    Points at the harness daemon by default, which is what AitherShell, the portal
    app and Awconnect all read. A failure here is normal (the daemon is often
    down) and must be reported as skipped rather than treated as an outage.
    """
    url = os.getenv("AITHER_DECISIONS_WEBHOOK", "").strip()
    if not url:
        return "no webhook configured"
    payload = json.dumps({
        "type": "decision.raised",
        "card": card.to_dict(),
        "open_count": open_count,
    }).encode("utf-8")
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    token = os.getenv("AITHER_HARNESS_TOKEN", "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            if response.status >= 400:
                return f"webhook HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"webhook unreachable: {exc}"
    return None


# ── the entry point ─────────────────────────────────────────────────────────────


def notify(card: DecisionCard, store: Optional[DecisionStore] = None) -> NotifyResult:
    """Push a newly-raised card to every available channel.

    Never raises. A notification failure must not fail the raise — the card is
    already durable by the time this runs, so the worst case is a card the owner
    finds later rather than a card that was lost.
    """
    delivered: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    store = store or DecisionStore()
    try:
        open_cards = store.list(status=STATUS_OPEN)
    except OSError as exc:
        open_cards = [card]
        errors.append(f"could not count open cards: {exc}")

    state = _read_state()
    last = float(state.get("last_toast_at") or 0.0)
    now = time.time()
    # Only decisions count toward "N decisions waiting": an open info digest is
    # context and must not inflate the number or turn a toast into a summary.
    waiting = decisions_waiting(open_cards)
    coalesced = (now - last) < QUIET_WINDOW_SECONDS and len(waiting) > 1

    if coalesced:
        title = f"{len(waiting)} decisions waiting"
        body = render_summary(open_cards)
    else:
        prefix = {"blocked": "Blocked on you", "info": "You should know"}.get(
            card.kind, "Decision needed"
        )
        title = f"{prefix}: {card.title}"[:110]
        recommended = card.recommended_key()
        hint = f"  ·  recommended: {recommended}" if recommended else ""
        left = card.seconds_left
        when = f"  ·  default in {human_age(left)}" if left is not None else ""
        body = (card.summary or card.detail or "Open AitherShell to answer.")[:180] + hint + when

    # The card WINDOW is the primary channel. It is the only one that shows the
    # facts and the options, and the only one where clicking answers anything.
    #
    # UNLESS the desk is up. awdesk is the always-on surface and watches this
    # store directory itself, so while its heartbeat is fresh the card is
    # ALREADY in front of the owner — the desk Inbox, the tray badge and the
    # spoken line. Spawning the standalone Tk window beside it is the "separate
    # dialogue" the owner vetoed (2026-10-05): one ask, one surface. The desk
    # branch runs FIRST, before popup_enabled(): quiet holds the Tk window, but
    # it must not turn a desk delivery into a lie about what happened.
    #
    # Inside the quiet window we do NOT spawn a second one: a window already open
    # walks the queue itself when it is answered, so a burst of eight cards
    # produces one window showing eight in turn, not eight stacked windows
    # fighting for the same corner of the screen.
    if desk_alive():
        delivered.append("awdesk")
    elif not popup_enabled():
        held = quiet_reason()
        skipped.append(f"card window (held while quiet: {held})" if held
                       else "card window (disabled or no display)")
    elif not card.options:
        # A window with nothing to click is not a decision surface, it is an
        # interruption. Optionless cards (a bare "waiting for input") stay in the
        # store for the cockpit to show and never raise a window — that combination
        # was measured in real use and it is pure noise.
        skipped.append("card window (no options — nothing to click)")
    elif coalesced:
        skipped.append(f"card window (one is already open · {len(waiting)} queued)")
    else:
        window_error = open_card_window(card.id)
        if window_error is None:
            delivered.append("card window")
            state["last_toast_at"] = now
            state["last_card_id"] = card.id
            _write_state(state)
        else:
            skipped.append(f"card window ({window_error})")

    error = native_toast(title, body, urgency=card.urgency)
    if error is None:
        delivered.append("toast")
        state["last_toast_at"] = now
        state["last_card_id"] = card.id
        _write_state(state)
    else:
        skipped.append(f"toast ({error})")

    hook_error = _webhook_post(card, len(open_cards))
    if hook_error is None:
        delivered.append("webhook")
    else:
        skipped.append(f"webhook ({hook_error})")

    # The relay: ONE structured message per card in the owner-only #decisions
    # channel, rendered with its options as buttons on every surface that reads the
    # relay (desk, browser, phone, terminal). Not coalesced -- it is a message in a
    # channel, not an interruption, and each card needs its own buttons.
    relay_error = _relay_post(card)
    if relay_error is None:
        delivered.append("relay")
    else:
        skipped.append(f"relay ({relay_error})")

    return NotifyResult(delivered=delivered, skipped=skipped, errors=errors)


def _relay_post(card: DecisionCard) -> Optional[str]:
    """Post the card to the relay. An error string, or None. Never raises."""
    try:
        from adk.decisions.relay_post import post_card
    except ImportError as exc:
        return f"relay poster unavailable: {exc}"
    try:
        return post_card(card)
    except Exception as exc:  # noqa: BLE001 - a notification must never fail a raise
        return f"{exc.__class__.__name__}: {exc}"
