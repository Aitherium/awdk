"""thread_subs — which sessions own which external thread (subscription pass-through).

WHY THIS EXISTS
---------------
A follow-up event on a thread that already has a worker (the next comment on PR #128,
the next reply in a Discord thread, the next mail in a chain) should reach THAT worker
without a router decision. Before this file nothing keyed a session by an external thread
id (measured 2026-11-01: zero hits for any thread->session binding), so every
follow-up was either dropped (unaddressed room events are not the
steer dispatcher's business) or had to be re-addressed by hand.

The design follows the "subscription and pass-through" fast path from DeepSeek Harness's
super-agent write-up (2026-11-01): the slow path (a router/LLM) decides who handles the
FIRST event and subscribes that worker; code passes every later event in the thread
straight through. Subscriptions are many-to-many, so two workers on the same PR both see
each new event and can notice each other.

A thread key is ``(surface, thread_id)``: ``("github", "Aitherium/AitherOS#128")``,
``("discord", "<thread snowflake>")``, ``("mail", "<root Message-ID>")``. Producers carry it
on a room event as ``payload.thread = {"surface": ..., "id": ...}`` (``payload`` is the one
free-form field ``Room._normalise`` keeps).

A SUBSCRIPTION IS A ROUTING HINT, NEVER AUTHORIZATION — exactly like ``to``. The steer
dispatcher therefore delivers every pass-through event on the PEER mailbox tier, framed;
a stranger's comment on a subscribed PR can never reach a keyboard through this table.

Rows expire after ``ttl_s`` of no activity, so a session that died without unsubscribing
stops collecting mail instead of accruing it forever. SQLite, one file, WAL; stdlib only
(this module must import on Python 3.10 with no extras).
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Default idle expiry: a week covers a PR that sits in review over a weekend.
DEFAULT_TTL_S = 7 * 24 * 3600

#: Mirrors the room actor-id rule; a subscriber is a room actor id.
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SURFACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_THREAD_ID_LEN = 512
#: A thread with more subscribers than this is a misuse (a broadcast), not a worker team.
MAX_SUBSCRIBERS_PER_THREAD = 16


class ThreadSubError(ValueError):
    """A malformed surface, thread id or subscriber."""


def default_db_path() -> Path:
    env = os.environ.get("AITHER_THREAD_SUBS_DB", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".aither" / "thread-subscriptions.db"


def thread_key_from_event(event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``(surface, thread_id)`` from ``payload.thread``, or None when absent/malformed."""
    payload = event.get("payload") if isinstance(event, dict) else None
    thread = payload.get("thread") if isinstance(payload, dict) else None
    if not isinstance(thread, dict):
        return None
    try:
        return _check_key(thread.get("surface"), thread.get("id"))
    except ThreadSubError:
        return None


def _check_key(surface: Any, thread_id: Any) -> Tuple[str, str]:
    s = str(surface or "").strip().lower()
    t = str(thread_id or "").strip()
    if not _SURFACE_RE.match(s):
        raise ThreadSubError(f"bad surface {surface!r}: lowercase letters/digits/_- only")
    if not t or len(t) > MAX_THREAD_ID_LEN:
        raise ThreadSubError(f"thread id must be 1-{MAX_THREAD_ID_LEN} chars")
    return s, t


def _check_subscriber(subscriber: Any) -> str:
    sub = str(subscriber or "").strip()
    if not _ACTOR_ID_RE.match(sub):
        raise ThreadSubError(f"bad subscriber id {subscriber!r}")
    return sub


class ThreadSubscriptions:
    """Many-to-many ``(surface, thread_id) <-> session`` table with idle expiry."""

    def __init__(self, path: Optional[Path] = None, *, ttl_s: float = DEFAULT_TTL_S,
                 now: Any = time.time) -> None:
        self._path = Path(path) if path is not None else default_db_path()
        self._ttl = float(ttl_s)
        self._now = now
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as exc:
                # some filesystems (9p/drvfs) refuse WAL; the rollback journal is fine
                sys.stderr.write(f"[thread_subs] WAL unavailable, using rollback: {exc}\n")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS subs ("
                " surface TEXT NOT NULL, thread_id TEXT NOT NULL, subscriber TEXT NOT NULL,"
                " title TEXT NOT NULL DEFAULT '', created REAL NOT NULL,"
                " last_seen REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (surface, thread_id, subscriber))")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS subs_by_sub ON subs(subscriber)")

    @property
    def path(self) -> Path:
        return self._path

    def subscribe(self, surface: Any, thread_id: Any, subscriber: Any, *,
                  title: str = "") -> bool:
        """Subscribe; True when newly added, False when it already existed (refreshed)."""
        s, t = _check_key(surface, thread_id)
        sub = _check_subscriber(subscriber)
        now = float(self._now())
        with self._lock, self._conn:
            self._purge_locked(now)
            exists = self._conn.execute(
                "SELECT 1 FROM subs WHERE surface=? AND thread_id=? AND subscriber=?",
                (s, t, sub)).fetchone()
            if exists:
                self._conn.execute(
                    "UPDATE subs SET last_seen=? WHERE surface=? AND thread_id=? "
                    "AND subscriber=?", (now, s, t, sub))
                return False
            count = self._conn.execute(
                "SELECT COUNT(*) FROM subs WHERE surface=? AND thread_id=?",
                (s, t)).fetchone()[0]
            if count >= MAX_SUBSCRIBERS_PER_THREAD:
                raise ThreadSubError(
                    f"{s}:{t} already has {count} subscribers "
                    f"(max {MAX_SUBSCRIBERS_PER_THREAD})")
            self._conn.execute(
                "INSERT INTO subs VALUES (?,?,?,?,?,?,0)",
                (s, t, sub, (title or "")[:200], now, now))
            return True

    def unsubscribe(self, surface: Any, thread_id: Any, subscriber: Any) -> bool:
        s, t = _check_key(surface, thread_id)
        sub = _check_subscriber(subscriber)
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM subs WHERE surface=? AND thread_id=? AND subscriber=?",
                (s, t, sub))
            return cur.rowcount > 0

    def unsubscribe_all(self, subscriber: Any) -> int:
        """Drop every subscription a session holds (its task finished or it died)."""
        sub = _check_subscriber(subscriber)
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM subs WHERE subscriber=?", (sub,)).rowcount

    def subscribers(self, surface: Any, thread_id: Any) -> List[str]:
        """Live subscribers of a thread, oldest first. Never raises on a bad key."""
        try:
            s, t = _check_key(surface, thread_id)
        except ThreadSubError:
            return []
        now = float(self._now())
        with self._lock, self._conn:
            self._purge_locked(now)
            rows = self._conn.execute(
                "SELECT subscriber FROM subs WHERE surface=? AND thread_id=? "
                "ORDER BY created", (s, t)).fetchall()
        return [str(r["subscriber"]) for r in rows]

    def touch(self, surface: str, thread_id: str, subscriber: str) -> None:
        """Record a delivery: refreshes the idle clock and bumps the counter."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subs SET last_seen=?, delivered=delivered+1 WHERE surface=? "
                "AND thread_id=? AND subscriber=?",
                (float(self._now()), surface, thread_id, subscriber))

    def threads_of(self, subscriber: Any) -> List[Dict[str, Any]]:
        """Every thread a session holds -- the Chain-of-Recall hop from a session."""
        sub = _check_subscriber(subscriber)
        with self._lock:
            rows = self._conn.execute(
                "SELECT surface, thread_id, title, created, last_seen, delivered FROM subs "
                "WHERE subscriber=? ORDER BY last_seen DESC", (sub,)).fetchall()
        return [dict(r) for r in rows]

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subs ORDER BY surface, thread_id, created").fetchall()
        return [dict(r) for r in rows]

    def _purge_locked(self, now: float) -> None:
        if self._ttl > 0:
            self._conn.execute("DELETE FROM subs WHERE last_seen < ?", (now - self._ttl,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_DEFAULT: Optional[ThreadSubscriptions] = None
_DEFAULT_LOCK = threading.Lock()


def default_thread_subscriptions() -> ThreadSubscriptions:
    """The process-wide table at :func:`default_db_path` (one per daemon process)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = ThreadSubscriptions()
        return _DEFAULT
