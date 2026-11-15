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


# ── the raiser question: is the session that ASKED still there? ───────────────
#
# ``live_waiting_session_ids`` answers "is this session waiting on the owner?".
# Retirement asks a narrower and more dangerous question -- "is the session that
# raised this card gone, so nobody will ever act on the answer?" -- and it may
# only return True on POSITIVE proof of death. An unmeasured session must never
# make an open ask disappear: retiring on an unknown would hide a real question,
# the exact opposite of the doctrine above.

def _field(obj, name: str, default=""):
    """Read ``name`` from a DecisionSource/dict or a card's ``source``.

    A dict carrying ``source`` is a CARD; any other dict is the source itself
    (``card.waiters`` serialize as bare ``DecisionSource`` dicts), so its own
    key is the answer -- not "not measured".
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        src = obj.get("source") if "source" in obj else obj
    else:
        src = getattr(obj, "source", obj)
    if src is None:
        return default
    if isinstance(src, dict):
        return src.get(name, default)
    return getattr(src, name, default)


def session_entry(session_id: str, directory: Optional[Path] = None) -> Optional[dict]:
    """The registry record for ``session_id``, or None when it is not listed."""
    want = str(session_id or "").strip()
    if not want:
        return None
    root = directory or sessions_dir()
    try:
        names = list(root.iterdir())
    except OSError:
        return None
    for path in names:
        if path.suffix != ".json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        if str(raw.get("sessionId") or "") == want:
            return raw
    return None


def raiser_gone(card, *, directory: Optional[Path] = None) -> bool:
    """True only when the session that raised ``card`` is PROVABLY dead.

    Registry first: a record for the session names the live process, so a dead
    recorded pid is proof and a live one is proof of the opposite. A session the
    registry no longer lists is NOT proof (the registry may have been cleaned or
    never enabled), so the pid resolved at raise time is the fallback. No usable
    pid at all means "not measured" -- never "gone".
    """
    session_id = str(_field(card, "session_id") or "")
    entry = session_entry(session_id, directory) if session_id else None
    if entry is not None:
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            return False
        return not pid_alive(pid)
    try:
        session_pid = int(_field(card, "session_pid") or 0)
    except (TypeError, ValueError):
        session_pid = 0
    if session_pid <= 0:
        return False
    return not pid_alive(session_pid)


def waiter_alive(waiter, *, directory: Optional[Path] = None) -> bool:
    """True while a card still has a session that will act on the answer.

    Deliberately conservative: anything unmeasured counts as ALIVE, because a
    waiter wrongly declared dead is a question retired out from under a session
    that was still blocked on it.
    """
    session_id = str(_field(waiter, "session_id") or "")
    entry = session_entry(session_id, directory) if session_id else None
    if entry is not None:
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            return True
        return pid <= 0 or pid_alive(pid)
    try:
        session_pid = int(_field(waiter, "session_pid") or 0)
    except (TypeError, ValueError):
        return True
    if session_pid <= 0:
        return True
    return pid_alive(session_pid)


__all__ = [
    "keep_waiting",
    "live_waiting_session_ids",
    "pid_alive",
    "raiser_gone",
    "session_entry",
    "sessions_dir",
    "waiter_alive",
]
