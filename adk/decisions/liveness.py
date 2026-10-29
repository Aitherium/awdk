"""Which Claude Code sessions are alive AND waiting — the beam's liveness rule.

A ``cc-waiting:<session>:<stamp>`` card is raised by the Claude Code Notification
hook (``awask_notification_card.py``) and withdrawn by a UserPromptSubmit hook —
i.e. only when the owner next types in THAT session. A session that ended while
waiting never types again, so its card stayed open forever: measured 2026-10-28,
eight of them, up to 13 days old, pinned above every real ask on every surface.

The rule, shared so the status line, the daemon, awdesk and the reconcile sweep
cannot disagree:

* a session is LIVE when its registry file names a pid that is still running;
* it is WAITING when it is live and its status is not ``busy`` (a working session
  is not waiting on the owner);
* ``None`` means "not measured" and callers must count the card anyway — an
  unknown must never hide an ask.

Registry: ``~/.claude/sessions/<pid>.json`` (Claude Code's own; ``sessionId``,
``pid``, ``status``, ``updatedAt``). Override the directory with
``AITHER_CLAUDE_SESSIONS_DIR`` for tests.

Known limit: pid reuse can keep one stale card counting until the session file is
cleaned up; the reconcile sweep (OPS-STALE) is the backstop, not this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional


def sessions_dir() -> Path:
    env = os.environ.get("AITHER_CLAUDE_SESSIONS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "sessions"


def pid_alive(pid: int) -> bool:
    """True while the process exists. An unanswerable check counts as alive."""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        # Windows raises OSError(EINVAL/22) for a gone pid; anything else is an
        # unanswerable question, and an unknown must not hide an ask.
        return not _is_gone_on_windows(pid)


def _is_gone_on_windows(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        kernel32.CloseHandle(handle)
        return False
    except Exception:  # noqa: BLE001 - unanswerable is not dead
        return False


def live_waiting_session_ids(directory: Optional[Path] = None) -> Optional[set[str]]:
    """Session ids that are alive and not busy, or ``None`` when not measurable."""
    root = directory or sessions_dir()
    try:
        names = list(root.iterdir())
    except OSError:
        return None
    out: set[str] = set()
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            pid = int(raw.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or str(raw.get("status") or "") == "busy":
            continue
        if not pid_alive(pid):
            continue
        sid = raw.get("sessionId")
        if isinstance(sid, str) and sid:
            out.add(sid)
    return out


def keep_waiting(cards: Iterable, live: Optional[Iterable[str]] = None) -> list:
    """The cards still waiting, given ``live`` (``None`` = not measured)."""
    from adk.decisions import beam

    return [c for c in cards if beam.still_waiting(c, live)]


__all__ = ["keep_waiting", "live_waiting_session_ids", "pid_alive", "sessions_dir"]
