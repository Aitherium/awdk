"""Canonical per-conversation reasoning session — chat-as-steering (Layer 2).

ONE continuous, STEERABLE background reasoning loop per conversation. New chat
messages STEER the already-running loop (enqueued as steering input the loop
drains between ReAct steps) instead of each spawning a brand-new agentic loop.
The grounded result streams back through the session while the user keeps
chatting on the fast lane.

This is the substrate the whole platform shares — awkit, Genesis and ADK
agents register/look-up sessions here so "keep chatting while it works" behaves
identically everywhere instead of each reinventing a queue.

Usage::

    mgr = get_session_manager()
    if mgr.active(conv_id):
        mgr.steer(conv_id, new_message)        # redirect the running loop
    else:
        sess = mgr.get_or_create(conv_id)
        sess.start(lambda s: run_my_grounded_loop(s))   # background

Inside the grounded loop, between steps::

    for steer_msg in session.pop_steering():
        inject_as_observation(f"User steering input: {steer_msg}")

The session is budget/cancel-bounded: the manager evicts finished/expired
sessions and caps concurrency so background tasks never leak.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional


class SessionStatus:
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class ReasoningSession:
    """A single conversation's continuous, steerable background reasoning loop."""

    def __init__(self, conversation_id: str, *, max_steering: int = 32):
        self.conversation_id = conversation_id
        self.status: str = SessionStatus.IDLE
        self.reasoning_state: dict = {}
        self.result: Any = None
        self.error: Optional[str] = None
        self.created_at: float = time.time()
        self.updated_at: float = self.created_at
        self.steer_count: int = 0
        self._steering: "asyncio.Queue[str]" = asyncio.Queue(maxsize=max_steering)
        self._task: Optional[asyncio.Task] = None
        # Output channel: the loop emit()s events; observers (e.g. an SSE
        # /observe endpoint the frontend reconnects to) drain them. A capped
        # backfill log lets a late observer catch up on what it missed.
        self._subscribers: "list[asyncio.Queue]" = []
        self._event_log: list[dict] = []
        self._max_log: int = 200

    # ── lifecycle ────────────────────────────────────────────────────────────
    def is_active(self) -> bool:
        # Active if RUNNING via our OWN background task, OR via an EXTERNALLY-owned
        # loop marked with mark_running()/mark_done() — e.g. the grounded loop a
        # caller already drives through adk.responder.respond. TTL eviction in the
        # manager reaps a session left RUNNING by a crashed external loop.
        if self.status != SessionStatus.RUNNING:
            return False
        return self._task is None or not self._task.done()

    def mark_running(self) -> None:
        """Mark active for an EXTERNALLY-owned loop (the caller drives the task and
        calls ``pop_steering()`` / ``emit()`` itself). Use ``start()`` instead when
        the session should own the background task."""
        self.status = SessionStatus.RUNNING
        self.error = None
        self.updated_at = time.time()

    def mark_done(self, *, error: Optional[str] = None) -> None:
        """Finish an externally-owned loop."""
        self.status = SessionStatus.ERROR if error else SessionStatus.DONE
        self.error = error
        self.updated_at = time.time()

    def start(self, run: Callable[["ReasoningSession"], Awaitable[Any]]) -> asyncio.Task:
        """Launch the background loop. ``run(session)`` is an awaitable that should
        call ``session.pop_steering()`` between steps to honour mid-flight steering.
        Idempotent while already running (returns the existing task)."""
        if self.is_active():
            return self._task  # type: ignore[return-value]
        self.status = SessionStatus.RUNNING
        self.error = None
        self.updated_at = time.time()

        async def _runner() -> None:
            try:
                self.result = await run(self)
                if self.status == SessionStatus.RUNNING:
                    self.status = SessionStatus.DONE
            except asyncio.CancelledError:
                self.status = SessionStatus.CANCELLED
                raise
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                self.status = SessionStatus.ERROR
            finally:
                self.updated_at = time.time()

        # Bind to the RUNNING loop, never asyncio.ensure_future: ensure_future
        # resolves via get_event_loop(), which has NO default in modern
        # pytest-asyncio (and any loop-less context) — it fabricates an orphan
        # loop, the task is created there and never stepped, and the loop that
        # called start() observes a task that is done-without-running.
        # Measured 2026-08-30: supervisor tests failed exactly this way under
        # pytest-asyncio 1.3.0 while asyncio.create_task worked. In production
        # (daemon/awkit, running loop set as default) the two were identical;
        # create_task is correct in both worlds.
        try:
            self._task = asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            self._task = asyncio.ensure_future(_runner())  # legacy: no running loop
        return self._task

    async def join(self, timeout: Optional[float] = None) -> Any:
        """Wait for the background loop to finish (shielded so the wait's timeout
        never cancels the loop). Returns the loop's result."""
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            except Exception:  # noqa: BLE001 — surfaced via .status/.error
                pass
        return self.result

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.status = SessionStatus.CANCELLED
        self.updated_at = time.time()

    # ── steering ─────────────────────────────────────────────────────────────
    def steer(self, message: str) -> bool:
        """Enqueue a steering message for the running loop. Returns False if the
        message is empty or the steering buffer is full (caller should fall back
        to starting a fresh turn)."""
        if not (message or "").strip():
            return False
        try:
            self._steering.put_nowait(message.strip())
        except asyncio.QueueFull:
            return False
        self.steer_count += 1
        self.updated_at = time.time()
        return True

    def has_steering(self) -> bool:
        return not self._steering.empty()

    def pop_steering(self) -> list[str]:
        """Drain ALL queued steering messages (call between loop steps)."""
        out: list[str] = []
        while True:
            try:
                out.append(self._steering.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    # ── output / observe ─────────────────────────────────────────────────────
    def emit(self, event: dict) -> None:
        """Publish an event to all observers (+ the backfill log). Non-blocking;
        a full observer queue drops the event for that observer only."""
        self._event_log.append(event)
        if len(self._event_log) > self._max_log:
            self._event_log = self._event_log[-self._max_log:]
        self.updated_at = time.time()
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def observe(self, *, backfill: bool = True, idle_timeout: float = 0.5):
        """Async-iterate this session's event stream — for an SSE ``/observe``
        endpoint the frontend reconnects to. Yields backfilled events first (so a
        late observer catches up), then live events until the loop ends and the
        buffer drains."""
        q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        try:
            if backfill:
                for e in list(self._event_log):
                    yield e
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=idle_timeout)
                    yield e
                except asyncio.TimeoutError:
                    if not self.is_active() and q.empty():
                        break
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def snapshot(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "status": self.status,
            "active": self.is_active(),
            "steer_count": self.steer_count,
            "pending_steering": self._steering.qsize(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


class ReasoningSessionManager:
    """Registry of per-conversation reasoning sessions, with eviction + a hard
    concurrency cap so background loops never leak."""

    def __init__(self, *, max_sessions: int = 64, ttl_s: float = 1800.0):
        self._sessions: dict[str, ReasoningSession] = {}
        self.max_sessions = max_sessions
        self.ttl_s = ttl_s

    def get(self, conversation_id: str) -> Optional[ReasoningSession]:
        return self._sessions.get(conversation_id)

    def active(self, conversation_id: str) -> bool:
        s = self._sessions.get(conversation_id)
        return bool(s and s.is_active())

    def get_or_create(self, conversation_id: str) -> ReasoningSession:
        self._evict()
        s = self._sessions.get(conversation_id)
        if s is None:
            s = ReasoningSession(conversation_id)
            self._sessions[conversation_id] = s
        return s

    def steer(self, conversation_id: str, message: str) -> bool:
        """Steer the conversation's running loop. False if no active loop (caller
        should start a fresh turn instead)."""
        s = self._sessions.get(conversation_id)
        return bool(s and s.is_active() and s.steer(message))

    def cancel(self, conversation_id: str) -> bool:
        s = self._sessions.get(conversation_id)
        if s:
            s.cancel()
            return True
        return False

    def _evict(self) -> None:
        now = time.time()
        # Drop finished/expired sessions.
        for cid in [cid for cid, s in self._sessions.items()
                    if not s.is_active() and (now - s.updated_at) > self.ttl_s]:
            self._sessions.pop(cid, None)
        # Hard concurrency cap: drop the oldest INACTIVE sessions first.
        if len(self._sessions) > self.max_sessions:
            for cid, s in sorted(self._sessions.items(), key=lambda kv: kv[1].updated_at):
                if len(self._sessions) <= self.max_sessions:
                    break
                if not s.is_active():
                    self._sessions.pop(cid, None)

    def cleanup(self) -> None:
        self._evict()

    def snapshot(self) -> dict:
        return {cid: s.snapshot() for cid, s in self._sessions.items()}


_MANAGER: Optional[ReasoningSessionManager] = None


def get_session_manager() -> ReasoningSessionManager:
    """Process-wide shared registry (one per service)."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ReasoningSessionManager()
    return _MANAGER


__all__ = [
    "ReasoningSession", "ReasoningSessionManager", "SessionStatus",
    "get_session_manager",
]
