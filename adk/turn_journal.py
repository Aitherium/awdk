"""Durable turns: a journal that lets a turn survive the process being killed.

Measured 2026-11-12 against Flue (commit deee93b): after a hard kill and a restart
the kit kept the conversation and nothing else. The interrupted turn was not
resumed, the reconnecting client read 0 bytes, and nothing recorded what happened
to the tool call that was in flight.

What this module adds, and nothing more:

* **A per-session turn journal**, in the SAME SQLite file that already holds the
  session's history (``adk.memory.Memory``: ``~/.aither/memory/<agent>.db``), as
  two more tables. One append-only row per step: ``turn_accepted`` (with the
  request), ``tool_started`` (name, arguments, idempotency key), ``tool_finished``
  (result or error), ``turn_finished`` (the terminal outcome). Every row is its own
  transaction with ``synchronous=FULL``, so a row that was written is on disk
  before the step it describes runs.
* **Resume.** A turn that was accepted and never finished, whose owning process is
  gone, is run again -- at startup (:func:`resume_open_turns`) or by the client
  that sends the same request for the same session again. The decision for each
  tool call the resumed turn makes is :func:`decide`:

  ==========================  =====================  ============================
  journal says                tool declared          the resumed turn
  ==========================  =====================  ============================
  finished (result or error)  anything               gets the recorded result;
                                                     the tool is NOT run again
  started, no finish          ``repeat_safe``        runs it again
  started, no finish          not declared (default) is told the outcome is
                                                     unknown; the tool is NOT run
                                                     (calling it again runs it)
  no record                   anything               runs it
  ==========================  =====================  ============================

* **A terminal outcome for every turn**: ``finished``, ``failed`` (with the
  reason), ``resumed`` (interrupted by a kill and completed after it) or
  ``abandoned`` (cancelled, too old, or out of resume attempts).

The loop itself is not edited. :func:`journaled` wraps ``chat`` / ``stream_react``
and the agent's tool registry is wrapped once (:func:`_hook_tools`); with no turn
active both are pass-throughs. Every journal failure is logged and the turn goes
on without it: durability must never be the reason a turn fails.

Off switch: ``AITHER_TURN_JOURNAL=0``.

Not covered (said plainly so nobody assumes it): a kill DURING model generation
loses that generation (the turn restarts from its last recorded tool result);
tokens already streamed to a client are not replayed, the reconnecting client
gets the final answer; a turn that ran under a caller's ``auth`` context is not
resumed at startup (the principal cannot be restored from disk) and waits for
that caller to send the request again.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from adk.turn_cwd import TURN_CWD

logger = logging.getLogger("adk.turn_journal")

#: One id per process. A turn whose last owner carries another boot id and a pid
#: that is no longer running is an orphan.
BOOT_ID = uuid.uuid4().hex[:12]

#: Stored tool results and answers are capped; a replayed result that was cut says so.
MAX_RESULT_CHARS = 1_000_000

# Terminal outcomes.
FINISHED = "finished"
FAILED = "failed"
RESUMED = "resumed"          # interrupted by a kill, completed after it
ABANDONED = "abandoned"
OPEN = "open"
TERMINAL = (FINISHED, FAILED, RESUMED, ABANDONED)

# Resume decisions.
RUN = "run"
REPLAY = "replay"
RERUN = "rerun"
UNKNOWN = "unknown"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_journal_turn ON turn_journal(turn_id);
CREATE INDEX IF NOT EXISTS idx_turn_journal_session ON turn_journal(session_id);
CREATE TABLE IF NOT EXISTS turn_open (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL
);
"""


def enabled() -> bool:
    return os.environ.get("AITHER_TURN_JOURNAL", "1").strip().lower() not in (
        "0", "off", "false", "no")


def max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("AITHER_TURN_RESUME_ATTEMPTS", "2")))
    except ValueError:
        return 2


def max_age_s() -> float:
    try:
        return float(os.environ.get("AITHER_TURN_RESUME_MAX_AGE_S", "86400"))
    except ValueError:
        return 86400.0


def keep_s() -> float:
    """How long a finished turn's journal rows are kept (default 14 days)."""
    try:
        return float(os.environ.get("AITHER_TURN_JOURNAL_KEEP_DAYS", "14")) * 86400.0
    except ValueError:
        return 14 * 86400.0


def redeliver_s() -> float:
    try:
        return float(os.environ.get("AITHER_TURN_REDELIVER_S", "3600"))
    except ValueError:
        return 3600.0


def message_hash(message: str) -> str:
    return hashlib.sha256((message or "").encode("utf-8", "replace")).hexdigest()[:24]


def _canonical(arguments: Any) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(arguments)


def step_key(name: str, arguments: Any, occurrence: int) -> str:
    """The idempotency key of one tool call inside a turn.

    The n-th call of the same tool with the same arguments in a turn has the same
    key in every attempt at that turn, which is what lets a resumed attempt find
    the record its predecessor left.
    """
    raw = f"{name}\x1f{_canonical(arguments)}\x1f{occurrence}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a running process? Never signals it (``os.kill(pid, 0)`` on
    Windows TERMINATES the process)."""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ── the folded view of one turn ──────────────────────────────────────────────


@dataclass
class StepRecord:
    key: str
    name: str
    arguments: Any = None
    status: str = "started"     # started | finished | error
    result: str = ""
    error: str = ""
    truncated: bool = False
    attempt: int = 0

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "tool": self.name,
                               "arguments": self.arguments, "status": self.status,
                               "attempt": self.attempt}
        if self.status == "finished":
            out["result"] = self.result[:2000]
        if self.error:
            out["error"] = self.error[:2000]
        return out


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    agent: str = ""
    mode: str = "chat"
    message: str = ""
    kwargs: dict = field(default_factory=dict)
    auth_bound: bool = False
    cwd: str = ""
    accepted_at: float = 0.0
    finished_at: float = 0.0
    owner_pid: int = 0
    owner_boot: str = ""
    attempts: int = 0             # resumes so far
    outcome: str = OPEN
    reason: str = ""
    result: str = ""
    model: str = ""
    finish_reason: str = ""
    tool_calls: list = field(default_factory=list)
    delivered: bool = False
    steps: dict[str, StepRecord] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)   # replayed / unknown reports

    @property
    def is_open(self) -> bool:
        return self.outcome == OPEN

    def orphaned(self) -> bool:
        """Open, and the process that owned it is gone."""
        if not self.is_open or self.owner_boot == BOOT_ID:
            return False
        return self.owner_pid == os.getpid() or not pid_alive(self.owner_pid)

    def public(self, *, with_result: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "mode": self.mode,
            "message": self.message[:500],
            "outcome": self.outcome,
            "reason": self.reason,
            "attempts": self.attempts,
            "interrupted": self.attempts > 0,
            "accepted_at": self.accepted_at,
            "finished_at": self.finished_at or None,
            "delivered": self.delivered,
            "steps": [s.public() for s in self.steps.values()],
            "notes": self.notes,
        }
        if with_result and self.outcome in (FINISHED, RESUMED):
            out["result"] = self.result
            out["model"] = self.model
            out["finish_reason"] = self.finish_reason
            out["tool_calls"] = self.tool_calls
        return out


def fold(rows: list[tuple[str, str, str, float, str]]) -> dict[str, TurnState]:
    """Journal rows (session_id, turn_id, kind, ts, payload), oldest first -> turns."""
    turns: dict[str, TurnState] = {}
    for session_id, turn_id, kind, ts, payload in rows:
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {}
        turn = turns.get(turn_id)
        if turn is None:
            turn = turns[turn_id] = TurnState(turn_id=turn_id, session_id=session_id)
        if kind == "turn_accepted":
            turn.agent = str(data.get("agent") or "")
            turn.mode = str(data.get("mode") or "chat")
            turn.message = str(data.get("message") or "")
            turn.kwargs = data.get("kwargs") or {}
            turn.auth_bound = bool(data.get("auth_bound"))
            turn.cwd = str(data.get("cwd") or "")
            turn.accepted_at = ts
            turn.owner_pid = int(data.get("pid") or 0)
            turn.owner_boot = str(data.get("boot") or "")
        elif kind == "turn_resumed":
            turn.attempts = int(data.get("attempt") or (turn.attempts + 1))
            turn.owner_pid = int(data.get("pid") or 0)
            turn.owner_boot = str(data.get("boot") or "")
        elif kind == "tool_started":
            key = str(data.get("key") or "")
            turn.steps[key] = StepRecord(
                key=key, name=str(data.get("name") or ""),
                arguments=data.get("arguments"), attempt=int(data.get("attempt") or 0))
        elif kind == "tool_finished":
            key = str(data.get("key") or "")
            step = turn.steps.get(key)
            if step is None:
                step = turn.steps[key] = StepRecord(key=key, name=str(data.get("name") or ""))
            if data.get("error") is not None:
                step.status, step.error = "error", str(data.get("error"))
            else:
                step.status, step.result = "finished", str(data.get("result") or "")
                step.truncated = bool(data.get("truncated"))
        elif kind in ("tool_replayed", "tool_unknown"):
            turn.notes.append({"kind": kind, "key": data.get("key"), "tool": data.get("name"),
                               "attempt": data.get("attempt")})
        elif kind == "turn_finished":
            turn.outcome = str(data.get("outcome") or FINISHED)
            turn.reason = str(data.get("reason") or "")
            turn.result = str(data.get("result") or "")
            turn.model = str(data.get("model") or "")
            turn.finish_reason = str(data.get("finish_reason") or "")
            turn.tool_calls = data.get("tool_calls") or []
            turn.delivered = bool(data.get("delivered"))
            turn.finished_at = ts
        elif kind == "turn_delivered":
            turn.delivered = True
    return turns


def decide(record: Optional[StepRecord], repeat_safe: bool) -> str:
    """What a resumed turn does with one tool call. See the module table."""
    if record is None:
        return RUN
    if record.status in ("finished", "error"):
        return REPLAY
    return RERUN if repeat_safe else UNKNOWN


def unknown_outcome_text(name: str, key: str) -> str:
    """What the model is told instead of a result. Deliberately not an ``{"error"``
    payload: the call did not fail, its outcome is not known."""
    return json.dumps({
        "status": "outcome_unknown",
        "tool": name,
        "idempotency_key": key,
        "message": (
            "This call was started before the process was killed and no result was "
            "recorded. It was NOT run again. It may or may not have taken effect. "
            "Check its effect if you can; if it is safe to do so, call it again with "
            "the same arguments to run it now."),
    })


# ── the store ────────────────────────────────────────────────────────────────


class TurnJournal:
    """The journal tables inside one agent's memory database."""

    def __init__(self, db_path: str | Path, *, readonly: bool = False):
        self.db_path = str(db_path)
        self.readonly = readonly
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            # A reader that is not the kit (awsh's MCP server) creates nothing.
            return sqlite3.connect(
                f"file:{Path(self.db_path).as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        # FULL: a committed row is fsync'd before commit returns, in WAL too.
        conn.execute("PRAGMA synchronous=FULL")
        if not self._ready:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    # writes -----------------------------------------------------------------

    def append(self, session_id: str, turn_id: str, kind: str, payload: dict) -> None:
        """One journal row, committed and flushed before this returns."""
        body = json.dumps(payload, default=str, ensure_ascii=False)
        conn = self._connect()
        try:
            with conn:
                now = time.time()
                conn.execute(
                    "INSERT INTO turn_journal (session_id, turn_id, kind, ts, payload) "
                    "VALUES (?, ?, ?, ?, ?)", (session_id, turn_id, kind, now, body))
                if kind == "turn_accepted":
                    conn.execute(
                        "INSERT OR REPLACE INTO turn_open (turn_id, session_id, ts) "
                        "VALUES (?, ?, ?)", (turn_id, session_id, now))
                elif kind == "turn_finished":
                    conn.execute("DELETE FROM turn_open WHERE turn_id = ?", (turn_id,))
        finally:
            conn.close()

    def accept(self, session_id: str, *, agent: str, mode: str, message: str,
               kwargs: dict, auth_bound: bool, cwd: str = "") -> str:
        turn_id = uuid.uuid4().hex[:16]
        self.append(session_id, turn_id, "turn_accepted", {
            "agent": agent, "mode": mode, "message": message, "kwargs": kwargs,
            "auth_bound": auth_bound, "cwd": cwd, "pid": os.getpid(), "boot": BOOT_ID,
            "message_hash": message_hash(message)})
        return turn_id

    def finish(self, turn: "TurnState | str", session_id: str, outcome: str, *,
               reason: str = "", result: str = "", model: str = "",
               finish_reason: str = "", tool_calls: Optional[list] = None,
               delivered: bool = False) -> None:
        turn_id = turn if isinstance(turn, str) else turn.turn_id
        self.append(session_id, turn_id, "turn_finished", {
            "outcome": outcome, "reason": reason[:2000],
            "result": (result or "")[:MAX_RESULT_CHARS], "model": model,
            "finish_reason": finish_reason, "tool_calls": tool_calls or [],
            "delivered": delivered})

    def claim(self, turn_id: str) -> Optional[int]:
        """Take ownership of an orphaned turn. Returns the attempt number, or None
        when another caller got there first (or the turn is no longer open)."""
        conn = self._connect()
        try:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT session_id, turn_id, kind, ts, payload FROM turn_journal "
                    "WHERE turn_id = ? ORDER BY id", (turn_id,)).fetchall()
                turn = fold(rows).get(turn_id)
                if turn is None or not turn.orphaned():
                    conn.execute("ROLLBACK")
                    return None
                attempt = turn.attempts + 1
                conn.execute(
                    "INSERT INTO turn_journal (session_id, turn_id, kind, ts, payload) "
                    "VALUES (?, ?, 'turn_resumed', ?, ?)",
                    (turn.session_id, turn_id, time.time(), json.dumps(
                        {"attempt": attempt, "pid": os.getpid(), "boot": BOOT_ID})))
                conn.execute("COMMIT")
                return attempt
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def prune(self, keep_s: float) -> int:
        """Drop the rows of turns that ended more than ``keep_s`` ago. An open
        turn is never pruned, whatever its age."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM turn_journal WHERE turn_id IN ("
                    " SELECT turn_id FROM turn_journal WHERE kind = 'turn_finished'"
                    " AND ts < ?) AND turn_id NOT IN (SELECT turn_id FROM turn_open)",
                    (time.time() - keep_s,))
                return cur.rowcount or 0
        finally:
            conn.close()

    # reads ------------------------------------------------------------------

    def _rows(self, where: str, args: tuple) -> list[tuple]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT session_id, turn_id, kind, ts, payload FROM turn_journal "
                f"WHERE {where} ORDER BY id", args).fetchall()
        finally:
            conn.close()

    def turn(self, turn_id: str) -> Optional[TurnState]:
        return fold(self._rows("turn_id = ?", (turn_id,))).get(turn_id)

    def session_turns(self, session_id: str, limit: int = 20) -> list[TurnState]:
        turns = list(fold(self._rows("session_id = ?", (session_id,))).values())
        return turns[-limit:]

    def open_turns(self) -> list[TurnState]:
        conn = self._connect()
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT turn_id FROM turn_open ORDER BY ts").fetchall()]
        finally:
            conn.close()
        out = []
        for turn_id in ids:
            turn = self.turn(turn_id)
            if turn is not None and turn.is_open:
                out.append(turn)
        return out

    def events(self, session_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, turn_id, kind, ts, payload FROM turn_journal "
                "WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, after, limit)).fetchall()
        finally:
            conn.close()
        out = []
        for rid, turn_id, kind, ts, payload in rows:
            try:
                data = json.loads(payload)
            except ValueError:
                data = {}
            for big in ("result", "message"):
                if isinstance(data.get(big), str) and len(data[big]) > 2000:
                    data[big] = data[big][:2000] + "..."
            out.append({"seq": rid, "turn_id": turn_id, "kind": kind, "ts": ts, "data": data})
        return out

    def sessions(self, limit: int = 50) -> list[dict]:
        """Sessions in this store, newest first, with the state of their last turn."""
        conn = self._connect()
        try:
            try:
                conv = conn.execute(
                    "SELECT session_id, COUNT(*), MAX(timestamp) FROM conversations "
                    "GROUP BY session_id").fetchall()
            except sqlite3.OperationalError:
                conv = []
            try:
                journ = conn.execute(
                    "SELECT session_id, MAX(ts) FROM turn_journal "
                    "GROUP BY session_id").fetchall()
                open_ids = {r[0] for r in conn.execute(
                    "SELECT session_id FROM turn_open").fetchall()}
            except sqlite3.OperationalError:   # a store no journaled turn has touched
                journ, open_ids = [], set()
        finally:
            conn.close()
        rows: dict[str, dict] = {}
        for sid, count, last in conv:
            rows[sid] = {"session_id": sid, "messages": int(count or 0),
                         "last_activity_at": float(last or 0)}
        for sid, last in journ:
            row = rows.setdefault(sid, {"session_id": sid, "messages": 0,
                                        "last_activity_at": 0.0})
            row["last_activity_at"] = max(row["last_activity_at"], float(last or 0))
        for sid, row in rows.items():
            row["open_turn"] = sid in open_ids
        ordered = sorted(rows.values(), key=lambda r: -r["last_activity_at"])
        return ordered[:limit]

    def trim_dangling_user(self, session_id: str, message: str) -> bool:
        """Drop the user row an interrupted ``chat`` left as the session's last
        message, so the resumed attempt does not store it twice."""
        conn = self._connect()
        try:
            with conn:
                try:
                    row = conn.execute(
                        "SELECT id, role, content FROM conversations WHERE session_id = ? "
                        "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
                except sqlite3.OperationalError:
                    return False
                if row and row[1] == "user" and row[2] == message:
                    conn.execute("DELETE FROM conversations WHERE id = ?", (row[0],))
                    return True
            return False
        finally:
            conn.close()


_JOURNALS: dict[str, TurnJournal] = {}


def journal_at(db_path: str | Path) -> TurnJournal:
    key = str(db_path)
    journal = _JOURNALS.get(key)
    if journal is None:
        journal = _JOURNALS[key] = TurnJournal(key)
    return journal


def journal_for(agent: Any) -> Optional[TurnJournal]:
    """The journal beside ``agent``'s history, or None (disabled, or the agent's
    memory is not the SQLite store -- a mock, a custom backend)."""
    if not enabled():
        return None
    path = getattr(getattr(agent, "memory", None), "_db_path", None)
    if not isinstance(path, str) or not path or path == ":memory:":
        return None
    return journal_at(path)


# ── the live turn ────────────────────────────────────────────────────────────


@dataclass
class _Active:
    journal: TurnJournal
    turn_id: str
    session_id: str
    attempt: int = 0
    replay: dict[str, StepRecord] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def next_key(self, name: str, arguments: Any) -> str:
        base = f"{name}\x1f{_canonical(arguments)}"
        n = self.counts.get(base, 0)
        self.counts[base] = n + 1
        return step_key(name, arguments, n)


_ACTIVE: contextvars.ContextVar[Optional[_Active]] = contextvars.ContextVar(
    "adk_turn_journal_active", default=None)
_IN_TOOL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "adk_turn_journal_in_tool", default=False)
#: ``<turn_id>:<step key>`` while a journaled tool call runs: a tool that talks to
#: a system with its own idempotency keys can pass this through.
IDEMPOTENCY_KEY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "adk_tool_idempotency_key", default="")

#: Counters for /health. Process-local.
STATS: dict[str, Any] = {"accepted": 0, FINISHED: 0, FAILED: 0, RESUMED: 0,
                         ABANDONED: 0, "replayed_steps": 0, "unknown_steps": 0,
                         "rerun_steps": 0, "journal_errors": 0, "last": None}

#: Resumes running in this process, so the client that reconnects with the same
#: request joins the one already running instead of starting a second turn.
_RESUMING: dict[tuple[str, str, str], tuple["asyncio.Future", str]] = {}


async def _write(journal: TurnJournal, session_id: str, turn_id: str, kind: str,
                 payload: dict) -> bool:
    """One row, in a worker thread. A cancellation that arrives while the row is
    being written waits for the row and is then given back: rows land in the
    order the turn wrote them, cancelled or not."""
    writing = asyncio.ensure_future(
        asyncio.to_thread(journal.append, session_id, turn_id, kind, payload))
    try:
        await asyncio.shield(writing)
        return True
    except asyncio.CancelledError:
        try:
            await writing
        except Exception as exc:  # noqa: BLE001
            STATS["journal_errors"] += 1
            logger.warning("turn journal write failed (%s %s): %s", kind, turn_id, exc)
        raise
    except Exception as exc:  # noqa: BLE001 -- the turn goes on without the journal
        STATS["journal_errors"] += 1
        logger.warning("turn journal write failed (%s %s): %s", kind, turn_id, exc)
        return False


def _repeat_safe(registry: Any, name: str) -> bool:
    fn = getattr(registry, "repeat_safe", None)
    if not callable(fn):
        return False
    try:
        return fn(name) is True
    except Exception:  # noqa: BLE001 -- unknown means not safe
        return False


def _hook_tools(agent: Any) -> None:
    """Wrap ``agent._tools.execute`` once. With no turn active it is a pass-through."""
    registry = getattr(agent, "_tools", None)
    if registry is None or getattr(registry, "_turn_journal_hooked", False) is True:
        return
    inner = getattr(registry, "execute", None)
    if not callable(inner):
        return

    async def execute(name: str, arguments: Any, *args: Any, **kwargs: Any):
        act = _ACTIVE.get()
        if act is None or _IN_TOOL.get():
            return await inner(name, arguments, *args, **kwargs)
        return await _run_step(act, registry, inner, name, arguments, args, kwargs)

    try:
        registry.execute = execute
        registry._turn_journal_hooked = True
    except Exception as exc:  # noqa: BLE001 -- a registry that refuses attributes
        logger.debug("turn journal: tool hook not installed: %s", exc)


async def _run_step(act: _Active, registry: Any, inner: Callable[..., Awaitable[Any]],
                    name: str, arguments: Any, args: tuple, kwargs: dict):
    key = act.next_key(name, arguments)
    verdict = decide(act.replay.get(key), _repeat_safe(registry, name))
    base = {"key": key, "name": name, "attempt": act.attempt}

    if verdict == REPLAY:
        record = act.replay[key]
        STATS["replayed_steps"] += 1
        await _write(act.journal, act.session_id, act.turn_id, "tool_replayed", base)
        if record.status == "error":
            return json.dumps({"error": record.error, "replayed_from_journal": True})
        if record.truncated:
            return record.result + "\n[result cut at the journal's size cap]"
        return record.result

    if verdict == UNKNOWN:
        STATS["unknown_steps"] += 1
        await _write(act.journal, act.session_id, act.turn_id, "tool_unknown", base)
        return unknown_outcome_text(name, f"{act.turn_id}:{key}")

    if verdict == RERUN:
        STATS["rerun_steps"] += 1
    in_tool = _IN_TOOL.set(True)
    idem = IDEMPOTENCY_KEY.set(f"{act.turn_id}:{key}")
    try:
        await _write(act.journal, act.session_id, act.turn_id, "tool_started", {
            **base, "arguments": arguments, "idempotency_key": f"{act.turn_id}:{key}",
            "rerun": verdict == RERUN})
        result = await inner(name, arguments, *args, **kwargs)
    except asyncio.CancelledError:
        # Cancelled by this process (an interrupt, a wait_for timeout): that is a
        # recorded end, unlike a kill, which leaves the step with no finish row.
        try:
            await asyncio.shield(_write(
                act.journal, act.session_id, act.turn_id, "tool_finished",
                {**base, "error": "cancelled before it returned"}))
        except asyncio.CancelledError:
            pass      # cancelled again while recording: the first one is re-raised
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded, then the loop sees it as before
        await _write(act.journal, act.session_id, act.turn_id, "tool_finished", {
            **base, "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        _IN_TOOL.reset(in_tool)
        IDEMPOTENCY_KEY.reset(idem)
    text = result if isinstance(result, str) else str(result)
    await _write(act.journal, act.session_id, act.turn_id, "tool_finished", {
        **base, "result": text[:MAX_RESULT_CHARS],
        "truncated": len(text) > MAX_RESULT_CHARS})
    return result


# ── the entry-point wrapper ──────────────────────────────────────────────────

_CHAT_KWARGS = ("effort", "temperature", "top_p", "repetition_penalty", "max_tokens",
                "model", "tool_choice", "system_additions", "history")
_STREAM_KWARGS = ("max_steps", "system_additions", "effort", "history")


def _replayable_kwargs(mode: str, arguments: dict) -> dict:
    """The part of a call that can be stored and passed again after a restart."""
    out = {}
    for name in (_CHAT_KWARGS if mode == "chat" else _STREAM_KWARGS):
        value = arguments.get(name)
        if value is None:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[name] = value
    return out


def resume_note(turn: TurnState) -> str:
    """Tells the model what the journal knows, so it does not redo finished work
    even when it words a call differently this time."""
    lines = [
        "[TURN RESUMED AFTER A RESTART] The process was killed while it was working "
        "on the user's last message. This is the same turn, resumed. The journal of "
        "that attempt:"]
    for step in turn.steps.values():
        args = _canonical(step.arguments)[:400]
        if step.status == "finished":
            lines.append(f"- DONE {step.name}({args}) -> {step.result[:600]}")
        elif step.status == "error":
            lines.append(f"- FAILED {step.name}({args}) -> {step.error[:300]}")
        else:
            lines.append(
                f"- IN FLIGHT WHEN KILLED {step.name}({args}) -> outcome unknown; it "
                "was not run again")
    if len(lines) == 1:
        lines.append("- no tool call had been recorded")
    lines.append(
        "Do not repeat a DONE step: calling it again with the same arguments returns "
        "the recorded result without running it. For an in-flight step, check its "
        "effect before deciding whether to run it again. Then finish the turn.")
    return "\n".join(lines)


def _response_from(turn: TurnState):
    from adk.agent import AgentResponse

    return AgentResponse(
        content=turn.result, model=turn.model, session_id=turn.session_id,
        tool_calls_made=list(turn.tool_calls or []),
        finish_reason=turn.finish_reason or "stop")


async def _emit_outcome(agent: Any, turn_id: str, session_id: str, outcome: str,
                        reason: str, attempts: int) -> None:
    STATS[outcome] = STATS.get(outcome, 0) + 1
    STATS["last"] = {"turn_id": turn_id, "session_id": session_id, "outcome": outcome,
                     "reason": reason[:300], "attempts": attempts, "ts": time.time()}
    events = getattr(agent, "_events", None)
    if events is None:
        return
    try:
        await events.emit("turn_outcome", agent=getattr(agent, "name", ""),
                          session_id=session_id, turn_id=turn_id, outcome=outcome,
                          reason=reason[:300], attempts=attempts)
    except Exception as exc:  # noqa: BLE001 -- an event, never the turn
        logger.debug("turn_outcome event failed: %s", exc)


def journaled(mode: str):
    """Decorator for ``AitherAgent.chat`` / ``stream_react``: journal the turn."""

    def deco(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            journal = None
            if _ACTIVE.get() is None:      # a nested turn belongs to the outer one
                try:
                    journal = journal_for(self)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("turn journal unavailable: %s", exc)
            if journal is None:
                return await fn(self, *args, **kwargs)
            try:
                bound = signature.bind(self, *args, **kwargs)
            except TypeError:
                return await fn(self, *args, **kwargs)   # let the real call raise
            arguments = dict(bound.arguments)
            arguments.update(arguments.pop("kwargs", None) or {})
            message = arguments.get("message")
            if not isinstance(message, str):
                return await fn(self, *args, **kwargs)
            session_id = str(arguments.get("session_id")
                             or getattr(self, "_session_id", "") or "")
            def call(extra_system: Optional[str] = None):
                if extra_system:
                    # chat() takes system_additions through **kwargs, stream_react
                    # as a named parameter; either way the bound call is rebuilt.
                    target = bound.arguments
                    if "system_additions" not in signature.parameters:
                        target = bound.arguments.setdefault("kwargs", {})
                    target["system_additions"] = [
                        *(target.get("system_additions") or []), extra_system]
                return fn(*bound.args, **bound.kwargs)

            return await _run_turn(self, call, mode, journal, session_id, message,
                                   arguments)

        wrapper.__wrapped_turn__ = fn
        return wrapper

    return deco


async def _run_turn(agent: Any, call, mode: str, journal: TurnJournal, session_id: str,
                    message: str, arguments: dict):
    key = (journal.db_path, session_id, message_hash(message))

    # 1. The same request, while its resume is already running here: join it.
    running = _RESUMING.get(key)
    if running is not None and not running[0].done():
        return await _join(journal, session_id, running)

    # 2. An earlier attempt at exactly this request died with its process: this
    #    call carries the resume.  3. It already completed after a restart and
    #    nobody collected the answer: hand it over once.
    turn: Optional[TurnState] = None
    attempt = 0
    future: Optional[asyncio.Future] = None
    try:
        recent = await asyncio.to_thread(journal.session_turns, session_id, 20)
    except Exception as exc:  # noqa: BLE001
        STATS["journal_errors"] += 1
        logger.warning("turn journal read failed: %s", exc)
        return await call()
    only = _RESUME_ONLY.get()
    for candidate in recent:
        if candidate.message != message or not candidate.orphaned():
            continue
        if only and candidate.turn_id != only:
            continue
        if candidate.attempts >= max_attempts():
            await asyncio.to_thread(
                journal.finish, candidate, session_id, ABANDONED,
                reason=f"resume attempts exhausted ({candidate.attempts})")
            await _emit_outcome(agent, candidate.turn_id, session_id, ABANDONED,
                                "resume attempts exhausted", candidate.attempts)
            continue
        claimed = await asyncio.to_thread(journal.claim, candidate.turn_id)
        if not claimed:
            # Lost the claim to a resume that started at the same moment in this
            # process: it is registered by now, and this request joins it.
            running = _RESUMING.get(key)
            if running is not None and not running[0].done():
                return await _join(journal, session_id, running)
        if claimed:
            turn, attempt = candidate, claimed
            # Registered in the same step as the claim, with no await between: a
            # reconnect landing a moment later finds no orphan any more, and must
            # find the resume to join instead of starting the request again.
            future = asyncio.get_running_loop().create_future()
            _RESUMING[key] = (future, candidate.turn_id)
            break
    if turn is None and not only and any(
            c.is_open and c.message == message and c.attempts > 0
            and c.owner_boot == BOOT_ID for c in recent):
        # The journal says a resume of exactly this request was claimed by THIS
        # process a moment ago; its registration is at most a loop tick behind
        # the claim. Join it rather than run the request a second time.
        for _ in range(200):
            running = _RESUMING.get(key)
            if running is not None and not running[0].done():
                return await _join(journal, session_id, running)
            await asyncio.sleep(0.01)
            fresh = await asyncio.to_thread(journal.session_turns, session_id, 20)
            if not any(c.is_open and c.message == message and c.attempts > 0
                       for c in fresh):
                recent = fresh       # it ended meanwhile: the hand-over below applies
                break
    if only and turn is None:
        # The startup resume came for one specific turn and somebody else (the
        # reconnecting client) already carries it. Starting a NEW turn here would
        # run the request twice.
        raise _NotResumed(only)
    if turn is None and recent:
        last = recent[-1]
        if (last.outcome == RESUMED and not last.delivered and last.message == message
                and time.time() - last.finished_at <= redeliver_s()):
            await _write(journal, session_id, last.turn_id, "turn_delivered", {})
            return _response_from(last)

    try:
        return await _execute_turn(agent, call, mode, journal, session_id, message,
                                   arguments, turn, attempt, future, bool(only))
    finally:
        # Whatever happened, nobody may be left waiting on a resume that is over.
        if future is not None:
            if not future.done():
                future.cancel()
            if (_RESUMING.get(key) or (None,))[0] is future:
                _RESUMING.pop(key, None)


async def _execute_turn(agent: Any, call, mode: str, journal: TurnJournal,
                        session_id: str, message: str, arguments: dict,
                        turn: Optional[TurnState], attempt: int,
                        future: Optional[asyncio.Future], startup: bool):
    """Run one attempt at a turn (a new one, or the resume of ``turn``) and write
    its terminal row."""
    note = None
    if turn is None:
        try:
            turn_id = await asyncio.to_thread(
                functools.partial(
                    journal.accept, session_id, agent=str(getattr(agent, "name", "")),
                    mode=mode, message=message,
                    kwargs=_replayable_kwargs(mode, arguments),
                    auth_bound=arguments.get("auth") is not None,
                    cwd=TURN_CWD.get()))
        except Exception as exc:  # noqa: BLE001 -- run the turn undurably, loudly
            STATS["journal_errors"] += 1
            logger.warning("turn journal accept failed, turn runs without it: %s", exc)
            return await call()
        STATS["accepted"] += 1
        act = _Active(journal, turn_id, session_id)
    else:
        turn_id = turn.turn_id
        act = _Active(journal, turn_id, session_id, attempt=attempt,
                      replay=dict(turn.steps))
        logger.info("turn %s (session %s) resumes, attempt %d, %d recorded step(s)",
                    turn_id, session_id, attempt, len(turn.steps))
        try:
            await asyncio.to_thread(journal.trim_dangling_user, session_id, message)
            await _trim_conversation_store(session_id, message)
        except Exception as exc:  # noqa: BLE001 -- a duplicate row, not a failed turn
            logger.debug("dangling user row not trimmed: %s", exc)
        note = resume_note(turn)

    _hook_tools(agent)
    token = _ACTIVE.set(act)
    try:
        response = await call(note)
    except asyncio.CancelledError:
        await _close(agent, journal, act, ABANDONED, "cancelled before it finished")
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded, then raised as before
        await _close(agent, journal, act, FAILED, f"{type(exc).__name__}: {exc}")
        if future is not None and not future.done():
            future.set_exception(exc)
            future.exception()   # mark retrieved: a resume nobody joined is not a leak
        raise
    finally:
        _ACTIVE.reset(token)
    outcome = RESUMED if attempt else FINISHED
    reason = ""
    content = str(getattr(response, "content", "") or "")
    if not content.strip() and not getattr(response, "requires_action", False):
        # The loop returned, with nothing to show for it. Calling that finished
        # (or resumed) would hand a reconnecting client an empty answer as success.
        outcome, reason = FAILED, "the model returned an empty answer"
    await _close(
        agent, journal, act, outcome, reason,
        result=content,
        model=str(getattr(response, "model", "") or ""),
        finish_reason=str(getattr(response, "finish_reason", "") or ""),
        tool_calls=_plain_list(getattr(response, "tool_calls_made", None)),
        # a startup resume has no client on the line; anything else was answered
        delivered=(not attempt) or not startup)
    if future is not None and not future.done():
        future.set_result(response)
    return response


async def _join(journal: TurnJournal, session_id: str, running: tuple):
    response = await asyncio.shield(running[0])
    await _write(journal, session_id, running[1], "turn_delivered", {})
    return response


def _plain_list(value: Any) -> list:
    try:
        return [str(v) for v in (value or [])]
    except TypeError:
        return []


async def _close(agent: Any, journal: TurnJournal, act: _Active, outcome: str,
                 reason: str, **extra: Any) -> None:
    try:
        await asyncio.shield(asyncio.to_thread(functools.partial(
            journal.finish, act.turn_id, act.session_id, outcome, reason=reason, **extra)))
    except Exception as exc:  # noqa: BLE001
        STATS["journal_errors"] += 1
        logger.warning("turn journal finish failed (%s): %s", act.turn_id, exc)
    await _emit_outcome(agent, act.turn_id, act.session_id, outcome, reason, act.attempt)


async def _trim_conversation_store(session_id: str, message: str) -> None:
    """The JSON conversation mirror: drop the same dangling user message."""
    from adk.conversations import get_conversation_store

    store = get_conversation_store()
    conv = await store.get_or_create(session_id)
    if conv.messages and conv.messages[-1].get("role") == "user" \
            and conv.messages[-1].get("content") == message:
        conv.messages.pop()
        store._save(conv)


# ── resume at startup ────────────────────────────────────────────────────────

#: Set by the startup resume to the ONE turn it came for.
_RESUME_ONLY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "adk_turn_journal_resume_only", default="")


class _NotResumed(Exception):
    """The startup resume found its turn already carried by someone else."""


async def _noop_event(_event: dict) -> None:
    return None


async def _resume_one(agent: Any, turn: TurnState) -> None:
    _RESUME_ONLY.set(turn.turn_id)
    if turn.cwd and os.path.isdir(turn.cwd):
        TURN_CWD.set(turn.cwd)      # this task's own context; the tools read it
    kwargs = dict(turn.kwargs or {})
    try:
        if turn.mode == "stream_react":
            await agent.stream_react(turn.message, _noop_event,
                                     session_id=turn.session_id, **kwargs)
        else:
            await agent.chat(turn.message, session_id=turn.session_id, **kwargs)
    except _NotResumed:
        logger.info("turn %s is already being resumed by its client", turn.turn_id)
    except Exception as exc:  # noqa: BLE001 -- recorded as the turn's outcome already
        logger.warning("resumed turn %s failed: %s: %s", turn.turn_id,
                       type(exc).__name__, exc)


async def resume_open_turns(get_agent: Callable[..., Awaitable[Any]],
                            *, wait: bool = False) -> dict[str, Any]:
    """Find turns a dead process left open and run them to a terminal outcome.

    ``get_agent(name=None)`` is the server's agent lookup. Every ``*.db`` beside
    the default agent's memory is scanned, so a fleet's agents are covered.
    Returns what it did; with ``wait`` it returns after the resumed turns end.
    """
    report: dict[str, Any] = {"resumed": [], "abandoned": [], "waiting": [], "skipped": []}
    if not enabled():
        return report
    try:
        default = await get_agent()
    except Exception as exc:  # noqa: BLE001 -- no model yet: the turns stay open
        logger.warning("turn resume: no agent available (%s); open turns stay open", exc)
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    home = journal_for(default)
    if home is None:
        return report
    paths = {home.db_path}
    try:
        paths.update(str(p) for p in Path(home.db_path).parent.glob("*.db"))
    except OSError:
        pass
    tasks = []
    for path in sorted(paths):
        try:
            journal = journal_at(path)
            if path != home.db_path and not _has_journal(path):
                continue
            open_turns = await asyncio.to_thread(journal.open_turns)
            report["pruned"] = report.get("pruned", 0) + await asyncio.to_thread(
                journal.prune, keep_s())
        except Exception as exc:  # noqa: BLE001
            logger.warning("turn resume: cannot read %s: %s", path, exc)
            continue
        for turn in open_turns:
            if not turn.orphaned():
                continue
            brief = {"turn_id": turn.turn_id, "session_id": turn.session_id,
                     "agent": turn.agent}
            if time.time() - turn.accepted_at > max_age_s():
                await asyncio.to_thread(journal.finish, turn, turn.session_id, ABANDONED,
                                        reason="older than the resume window")
                await _emit_outcome(default, turn.turn_id, turn.session_id, ABANDONED,
                                    "older than the resume window", turn.attempts)
                report["abandoned"].append(brief)
                continue
            if turn.attempts >= max_attempts():
                await asyncio.to_thread(
                    journal.finish, turn, turn.session_id, ABANDONED,
                    reason=f"resume attempts exhausted ({turn.attempts})")
                await _emit_outcome(default, turn.turn_id, turn.session_id, ABANDONED,
                                    "resume attempts exhausted", turn.attempts)
                report["abandoned"].append(brief)
                continue
            if turn.auth_bound:
                # It ran under a caller's authority; without that caller it would
                # run with none, which is MORE than it had. It waits for them.
                report["waiting"].append(brief)
                continue
            try:
                agent = default if turn.agent in ("", getattr(default, "name", "")) \
                    else await get_agent(turn.agent)
            except Exception as exc:  # noqa: BLE001
                report["skipped"].append({**brief, "why": f"{type(exc).__name__}: {exc}"})
                continue
            own = journal_for(agent)
            if own is None or own.db_path != path:
                report["skipped"].append({**brief, "why": "agent does not own this store"})
                continue
            report["resumed"].append(brief)
            tasks.append(asyncio.create_task(_resume_one(agent, turn)))
    report["tasks"] = len(tasks)
    if tasks:
        logger.info("turn resume: %d interrupted turn(s) resuming", len(tasks))
        _BACKGROUND.update(tasks)
        for task in tasks:
            task.add_done_callback(_BACKGROUND.discard)
        if wait:
            await asyncio.gather(*tasks, return_exceptions=True)
    return report


_BACKGROUND: set = set()


def _has_journal(path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_open'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# ── read side: status route, /health, awsh ───────────────────────────────────


def session_status(agent: Any, session_id: str, *, after: int = 0) -> dict[str, Any]:
    journal = journal_for(agent)
    if journal is None:
        return {"session_id": session_id, "enabled": False, "turns": [], "events": []}
    turns = journal.session_turns(session_id)
    return {
        "session_id": session_id,
        "enabled": True,
        "open": [t.turn_id for t in turns if t.is_open],
        "turns": [t.public() for t in turns],
        "events": journal.events(session_id, after=after),
    }


def acknowledge(agent: Any, session_id: str, turn_id: str) -> bool:
    """A client that read a resumed turn's answer off the status route says so."""
    journal = journal_for(agent)
    if journal is None:
        return False
    turn = journal.turn(turn_id)
    if turn is None or turn.session_id != session_id or turn.is_open or turn.delivered:
        return False
    journal.append(session_id, turn_id, "turn_delivered", {"via": "status"})
    return True


def health(agent: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"enabled": enabled(), "boot": BOOT_ID,
                           **{k: v for k, v in STATS.items()}}
    out["resuming"] = sum(1 for f, _ in _RESUMING.values() if not f.done())
    journal = None
    try:
        journal = journal_for(agent) if agent is not None else None
        if journal is not None:
            open_turns = journal.open_turns()
            out["open"] = len(open_turns)
            out["open_orphaned"] = sum(1 for t in open_turns if t.orphaned())
            out["store"] = journal.db_path
    except Exception as exc:  # noqa: BLE001 -- /health always answers
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def memory_dir() -> Path:
    return Path.home() / ".aither" / "memory"


def list_resumable(limit: int = 20, root: Optional[Path] = None) -> list[dict]:
    """The kit's sessions, read straight from its store (the daemon may be down).

    One row per session across every agent database, newest first, with whether
    its last turn is still open -- what ``awsh_resumable(harness="awdk")`` lists.
    """
    base = root or memory_dir()
    rows: list[dict] = []
    if not base.is_dir():
        return rows
    for path in base.glob("*.db"):
        try:
            if not _has_table(str(path), "conversations"):
                continue
            journal = TurnJournal(str(path), readonly=True)
            for row in journal.sessions(limit=limit):
                row["agent"] = path.stem
                row["store"] = str(path)
                if row.get("open_turn"):
                    last = journal.session_turns(row["session_id"], 1)
                    if last:
                        row["last_turn"] = last[-1].public(with_result=False)
                rows.append(row)
        except Exception as exc:  # noqa: BLE001 -- one bad file does not hide the rest
            logger.debug("list_resumable: %s skipped: %s", path, exc)
    rows.sort(key=lambda r: -r["last_activity_at"])
    return rows[:limit]


def _has_table(path: str, table: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()
