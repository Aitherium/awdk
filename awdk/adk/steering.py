"""Mid-turn steering for active agent sessions.

Allows clients to inject follow-up messages or hints into an in-flight agent
turn via /chat/steer, similar to Genesis steering.py. Changes are drained
between tool iterations so the agent can react before the next LLM call.

Steering message types:
  - append: user follow-up message, visible in chat history
  - hint: invisible system-level context nudge, NOT in history
  - cancel: abort the running turn (not yet implemented)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

logger = logging.getLogger("adk.steering")

# session_id → asyncio.Queue of steering messages for SSE output
_steering_queues: dict[str, asyncio.Queue] = {}

# session_id → list of pending steering messages for pipeline injection
_steering_inputs: dict[str, list[str]] = {}

# session_id → list of pending steering hints (invisible, not in history)
_steering_hints: dict[str, list[str]] = {}

# Eviction bounds (prevent unbounded growth in long-lived processes)
_STEERING_MAX_SESSIONS = 1000


def _evict_overflow() -> None:
    """Drop oldest-inserted sessions if dicts exceed capacity."""
    for _d in (_steering_inputs, _steering_hints, _steering_queues):
        try:
            while len(_d) > _STEERING_MAX_SESSIONS:
                _d.pop(next(iter(_d)), None)
        except Exception:  # noqa: BLE001
            break


@dataclass(frozen=True)
class SteeringMessage:
    """A steering message injected via /chat/steer."""
    action: str
    message: str
    ts: float


async def register_steering_queue(session_id: str) -> asyncio.Queue:
    """Register an async queue for a session's steering messages.

    Called at the start of a /chat/stream turn. Returns the queue so the
    caller can drain and emit steering events to the SSE stream.
    """
    _evict_overflow()
    q: asyncio.Queue = asyncio.Queue()
    _steering_queues[session_id] = q
    return q


def unregister_steering_queue(session_id: str) -> None:
    """Unregister a session's steering queue.

    Called when the turn ends. Cleans up queued messages + input/hint lists.
    """
    _steering_queues.pop(session_id, None)
    _steering_inputs.pop(session_id, None)
    _steering_hints.pop(session_id, None)


async def queue_steering_message(
    session_id: str,
    action: str,
    message: str,
) -> bool:
    """Inject a steering message into an active session.

    Called by POST /chat/steer. Routes to the session's queue for SSE output,
    and also to _steering_inputs / _steering_hints for pipeline injection.

    Returns True if the session is active (queue found), False otherwise.
    """
    q = _steering_queues.get(session_id)
    if q is None:
        return False

    steering = SteeringMessage(action=action, message=message, ts=time.time())
    try:
        await q.put(steering)
    except asyncio.QueueFull:
        logger.warning("Steering queue full for session %s", session_id)
        return False

    # Also append to pipeline-accessible input lists so the agent loop can
    # drain and inject between tool iterations (same as Genesis).
    if action == "append" and message:
        _steering_inputs.setdefault(session_id, []).append(message)
    elif action == "hint" and message:
        _steering_hints.setdefault(session_id, []).append(message)

    logger.debug("Queued steering %s for session %s", action, session_id)
    return True


def drain_steering_inputs(session_id: str) -> list[str]:
    """Drain and return all pending steering messages for a session.

    Called by the agent loop between ReAct tool iterations to pick up
    follow-up messages injected via /chat/steer. Messages are removed
    from the list after draining (pop semantics).
    """
    msgs = _steering_inputs.pop(session_id, [])
    if msgs:
        logger.debug("Drained %d steering inputs for session %s", len(msgs), session_id)
    return msgs


def drain_steering_hints(session_id: str) -> list[str]:
    """Drain and return all pending steering hints for a session.

    Called by the agent loop between ReAct tool iterations. Unlike inputs,
    hints are injected as system-level context (not user messages) and are
    invisible in the conversation history.
    """
    hints = _steering_hints.pop(session_id, [])
    if hints:
        logger.debug("Drained %d steering hints for session %s", len(hints), session_id)
    return hints


async def drain_steering_queue(session_id: str, max_wait_ms: int = 100) -> list[SteeringMessage]:
    """Drain all pending steering messages from the SSE queue (non-blocking).

    Called by SSE handlers to emit queued steering events. Waits up to
    max_wait_ms for the first message, then drains the rest immediately.
    """
    q = _steering_queues.get(session_id)
    if q is None:
        return []

    messages: list[SteeringMessage] = []
    try:
        # Wait up to max_wait_ms for the first message
        try:
            msg = q.get_nowait()
            messages.append(msg)
        except asyncio.QueueEmpty:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=max_wait_ms / 1000.0)
                messages.append(msg)
            except asyncio.TimeoutError:
                pass

        # Drain remaining messages immediately
        while True:
            try:
                msg = q.get_nowait()
                messages.append(msg)
            except asyncio.QueueEmpty:
                break
    except Exception as e:
        logger.warning("Error draining steering queue for %s: %s", session_id, e)

    return messages


@asynccontextmanager
async def steering_session(session_id: str):
    """Context manager for steering queue lifecycle.

    Registers the queue on entry, cleans up on exit. Ensures that steering
    is available for the entire duration of a chat turn, even if it returns
    early or raises an exception.

    Usage:
        async with steering_session(sid):
            # ... chat logic ...
    """
    try:
        await register_steering_queue(session_id)
        yield
    finally:
        unregister_steering_queue(session_id)
