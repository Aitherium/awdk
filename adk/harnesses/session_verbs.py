"""What a surface may DO to one session row, and the two verbs the daemon adds.

``GET /sessions/unified`` used to hand every client a ``steer_capability`` string
and leave each one to re-derive which buttons that implies. Three clients (the
desk, the web cockpit, the terminal UI) then disagreed about what "turn-boundary"
allows, and a disabled button carried no reason. :func:`row_actions` decides it
ONCE, next to the rows, and says WHY a verb is unavailable in words a person can
act on.

The two verbs:

* **message** -- tier 2, the steering mailbox (``adk.decisions.store.write_steer``).
  It never types into a terminal: the session's own prompt hook drains the file at
  its next prompt (or next tool call, while it works). That is why it is offered
  for a discovered tab the daemon does not own, where ``input`` is not.
* **focus** -- LOCAL only. A live session's terminal window is raised by walking
  the session's parent processes to the first real top-level window; a session
  that is not running is reopened with ``claude --resume`` in a new Windows
  Terminal tab. Windows Terminal can raise its window but cannot be told which
  TAB to select, so a raised window may show a sibling tab.

Stdlib only at import time: the window helpers are loaded lazily, so importing
this module never touches ctypes on a platform without it.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

IS_WINDOWS = os.name == "nt"

#: A session id reaches a filename (the mailbox) and a command line (resume), so
#: its SHAPE is validated, never trusted. Same expression the mailbox writer and
#: its drain hook use.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: A message is text for a prompt, not a document. The drain hook caps what it
#: injects at 8000 characters; refusing above that here says so up front instead
#: of truncating silently at delivery.
MAX_MESSAGE_CHARS = 8000

#: Harnesses whose sessions drain the steering mailbox (the prompt hook is a
#: Claude Code hook). Another harness would leave the file unread forever.
MAILBOX_HARNESSES = frozenset({"claude", "claude-tty"})

#: Where a Claude Code remote-control session is reachable from a browser.
REMOTE_URL_BASE = "https://claude.ai/code/"

VERBS = ("message", "interrupt", "focus", "input")


def mailbox_id(row: dict[str, Any]) -> str:
    """The id the session's prompt hook drains its mailbox under.

    A daemon-owned claude session carries the id the PROGRAM was started with
    (``harness_session_id``); its hooks see that id, not the daemon's own.
    """
    return str(row.get("harness_session_id") or row.get("id") or "")


def remote_url(row: dict[str, Any]) -> str:
    """The browser URL of a remote-control session, or "" when it has none."""
    extras = row.get("extras") if isinstance(row.get("extras"), dict) else {}
    bridge = str(row.get("bridge_session_id") or (extras or {}).get("bridge_session_id") or "")
    if not bridge or not SESSION_ID_RE.match(bridge):
        return ""
    return REMOTE_URL_BASE + bridge


def row_actions(row: dict[str, Any], *, local_windows: Optional[bool] = None) -> dict[str, Any]:
    """``{message, interrupt, focus, input: bool, why_not: {verb: reason}}``.

    ``why_not`` carries a reason for every verb that is False, and nothing for
    one that is True -- a client renders the reason as the disabled button's
    text, so "disabled, no idea why" cannot happen.
    """
    windows = IS_WINDOWS if local_windows is None else bool(local_windows)
    status = str(row.get("status") or "")
    origin = str(row.get("origin") or "")
    harness = str(row.get("harness") or "")
    full = str(row.get("steer_capability") or "") == "full"
    exited = status in ("exited", "failed")
    why: dict[str, str] = {}

    # input / interrupt: only a pty the daemon owns.
    if exited:
        pty_reason = "the session has exited"
    elif not full:
        pty_reason = (
            "this tab was discovered, not started by the daemon: its keyboard is "
            "not reachable -- Message it (delivered at its next prompt), or Focus "
            "it and press Esc"
        )
    else:
        pty_reason = ""
    can_pty = not pty_reason
    if not can_pty:
        why["input"] = pty_reason
        why["interrupt"] = pty_reason

    # message: the mailbox, drained by the session's own prompt hook.
    if exited:
        why["message"] = "the session has exited -- Focus reopens it, then Message it"
    elif harness not in MAILBOX_HARNESSES:
        why["message"] = (
            f"the {harness or 'unknown'} harness does not drain the steering "
            "mailbox -- use its input instead"
        )
    elif origin == "daemon" and not row.get("harness_session_id"):
        # The prompt hook drains the mailbox under the id CLAUDE uses, which a
        # stream-json session reports only with its first turn. Queued under the
        # daemon's own id it would sit unread forever.
        why["message"] = (
            "the session has not reported its Claude session id yet (it does on its "
            "first prompt) -- use Input"
        )
    elif not SESSION_ID_RE.match(mailbox_id(row)):
        why["message"] = "the session id is not a valid mailbox name"
    can_message = "message" not in why

    # focus: a window on THIS machine.
    if not windows:
        why["focus"] = "focus needs a Windows desktop on the session's machine"
    elif origin == "daemon" and not exited:
        why["focus"] = (
            "runs inside the daemon's own terminal -- there is no window to raise; "
            "watch it here"
        )
    elif harness not in MAILBOX_HARNESSES:
        why["focus"] = f"the {harness or 'unknown'} harness cannot be reopened by id"
    can_focus = "focus" not in why

    return {
        "message": can_message,
        "interrupt": can_pty,
        "focus": can_focus,
        "input": can_pty,
        "why_not": why,
    }


#: Peer messages per (sender, session) per window. A peer agent in a loop could
#: otherwise fill a session's mailbox faster than its prompt hook drains it; the
#: owner is never limited.
PEER_MESSAGE_LIMIT = 6
PEER_MESSAGE_WINDOW_S = 60.0


class MessageRateLimiter:
    """Sliding-window count of peer messages, keyed by (sender, session)."""

    def __init__(self, limit: int = PEER_MESSAGE_LIMIT,
                 window_s: float = PEER_MESSAGE_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._sent: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, sender: str, session_id: str) -> float:
        """0 and the send is recorded, or the seconds until one is allowed."""
        now = self._clock()
        key = (sender, session_id)
        with self._lock:
            recent = [t for t in self._sent.get(key, []) if now - t < self.window_s]
            if len(recent) >= self.limit:
                self._sent[key] = recent
                return max(0.0, self.window_s - (now - recent[0]))
            recent.append(now)
            self._sent[key] = recent
            return 0.0


PEER_MESSAGE_LIMITER = MessageRateLimiter()


def deliver_message(
    row: dict[str, Any],
    text: str,
    *,
    authority: str,
    sender: str,
    writer: Optional[Callable[..., Optional[Path]]] = None,
) -> Optional[Path]:
    """Queue ``text`` in the session's steering mailbox. Returns the file, or None.

    ``authority`` is decided by the CALLER from the authenticated principal, never
    from anything in the request body; the writer coerces anything but "owner" and
    "peer" down to "peer".
    """
    if writer is None:
        from adk.decisions.store import write_steer as writer  # lazy: see module doc
    lines = [text]
    if authority == "owner":
        # The drain hook's owner preamble was written for answered decision cards;
        # say where THIS one came from so the agent does not look for a card.
        lines = [f"Message from the owner (sent from the session directory):\n\n{text}"]
    return writer(
        mailbox_id(row),
        lines,
        suffix="message",
        sender=sender,
        authority=authority,
        origin_id="",
        kind="session-message",
    )


# ── focus ────────────────────────────────────────────────────────────────────

_RESUME_PAYLOAD = (
    "Remove-Item Env:NO_COLOR, Env:CLAUDECODE, Env:CLAUDE_CODE_SESSION_ID, "
    "Env:CLAUDE_CODE_CHILD_SESSION, Env:CLAUDE_CODE_ENTRYPOINT, Env:CLAUDE_PID "
    "-ErrorAction SilentlyContinue; `$PSStyle.OutputRendering='Ansi'; "
    "claude --resume {session_id}"
)


#: Anything outside this set is folded out of a tab title (see resume_command).
_TITLE_UNSAFE = re.compile(r"[^A-Za-z0-9 ._#:-]+")


def resume_command(session_id: str, cwd: str, title: str = "") -> list[str]:
    """The argv that opens ``claude --resume <id>`` in a new Windows Terminal tab.

    The payload goes through ``-EncodedCommand`` because ``wt`` splits its own
    command line on semicolons even inside quoted arguments, and the calling
    process's session variables are scrubbed so the resumed tab is its own
    session. Raises ValueError on an id that is not a plain token.
    """
    if not SESSION_ID_RE.match(session_id or "") or session_id in (".", ".."):
        raise ValueError(f"not a resumable session id: {session_id!r}")
    payload = _RESUME_PAYLOAD.format(session_id=session_id)
    encoded = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")
    # `wt` splits ITS OWN command line on ";" even inside a quoted argument, so a
    # directory or title carrying one would start a second, attacker-shaped
    # subcommand. The title comes from transcript-derived text (a branch name
    # may hold ";"), so it is rebuilt from a safe charset; a cwd with ";" falls
    # back to the home directory rather than being escaped.
    directory = cwd if cwd and ";" not in cwd and Path(cwd).is_dir() else str(Path.home())
    safe_title = _TITLE_UNSAFE.sub(" ", title or "").strip()[:60] or session_id[:8]
    return [
        shutil.which("wt") or "wt",
        "new-tab",
        "-d", directory,
        "--title", safe_title,
        shutil.which("pwsh") or "pwsh", "-NoExit", "-EncodedCommand", encoded,
    ]


def _winproc():
    from adk.decisions import winproc  # lazy: ctypes window helpers

    return winproc


def focus_session(
    row: dict[str, Any],
    *,
    winproc: Any = None,
    spawn: Optional[Callable[[list[str]], Any]] = None,
) -> dict[str, Any]:
    """Raise a live session's window, or reopen a stopped one. Never raises.

    Returns ``{ok, mode, detail, remote_url}``; ``mode`` is ``raised`` (window
    brought forward), ``resumed`` (a new tab was opened), or ``none``.
    """
    url = remote_url(row)
    base = {"remote_url": url, "session_id": str(row.get("id") or "")}
    actions = row_actions(row)
    if not actions["focus"]:
        return {**base, "ok": False, "mode": "none", "detail": actions["why_not"]["focus"]}
    wp = winproc or _winproc()
    pid = int(row.get("pid") or 0)
    if pid and wp.pid_alive(pid):
        found = wp.find_terminal_window(pid)
        if not found:
            return {**base, "ok": False, "mode": "none",
                    "detail": f"session pid {pid} is running but no window hosts it"}
        hwnd, _owner, title = found
        if wp.focus_window(hwnd):
            return {**base, "ok": True, "mode": "raised",
                    "detail": f"raised {title!r} (Windows Terminal cannot select the tab)"}
        return {**base, "ok": False, "mode": "none",
                "detail": "Windows refused to bring the window forward (do-not-disturb, "
                          "a full-screen app, or focus-steal protection)"}
    try:
        argv = resume_command(mailbox_id(row), str(row.get("cwd") or ""),
                              str(row.get("name") or row.get("title") or ""))
    except ValueError as exc:
        return {**base, "ok": False, "mode": "none", "detail": str(exc)}
    try:
        if spawn is not None:
            spawn(argv)
        else:
            subprocess.Popen(  # noqa: S603 - argv list, validated id, no shell
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        return {**base, "ok": False, "mode": "none", "detail": f"could not open a tab: {exc}"}
    return {**base, "ok": True, "mode": "resumed",
            "detail": "opened a new terminal tab with claude --resume"}
