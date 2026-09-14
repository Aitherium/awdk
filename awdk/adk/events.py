"""Event system — lightweight pub/sub for ADK agent orchestration.

A standalone equivalent of AitherOS FluxEmitter. Agents and tools can
emit events; subscribers react asynchronously without blocking.

Usage:
    from adk.events import get_emitter, EventType

    emitter = get_emitter()

    # Subscribe
    async def on_tool_call(event):
        print(f"Tool called: {event['tool']}")
    emitter.subscribe(EventType.TOOL_CALL, on_tool_call)

    # Emit
    await emitter.emit(EventType.TOOL_CALL, tool="web_search", agent="atlas")
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, List

logger = logging.getLogger("adk.events")


class EventType(str, Enum):
    """Core event types for ADK agent orchestration."""
    # Agent lifecycle
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    AGENT_SPAWNED = "agent_spawned"

    # Chat/LLM
    CHAT_REQUEST = "chat_request"
    CHAT_RESPONSE = "chat_response"
    LLM_CALL = "llm_call"
    LLM_ERROR = "llm_error"

    # Tools
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"

    # Security
    SANDBOX_BLOCK = "sandbox_block"
    LOOP_GUARD_BREAK = "loop_guard_break"
    QUOTA_BREACH = "quota_breach"

    # Forge
    FORGE_DISPATCH = "forge_dispatch"
    FORGE_COMPLETE = "forge_complete"
    FORGE_FAILED = "forge_failed"

    # Services
    SERVICE_CONNECTED = "service_connected"
    SERVICE_DISCONNECTED = "service_disconnected"

    # Custom
    CUSTOM = "custom"


class EventEmitter:
    """Async pub/sub event bus for ADK.

    Thread-safe for subscription; async for emission.
    Subscribers that raise are logged but never break the emitter.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._wildcard_subscribers: List[Callable] = []
        self._event_count = 0

    def subscribe(self, event_type: EventType | str, handler: Callable) -> None:
        """Subscribe to an event type. Handler receives dict with event data."""
        key = event_type.value if isinstance(event_type, EventType) else event_type
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(handler)

    def subscribe_all(self, handler: Callable) -> None:
        """Subscribe to ALL events (wildcard)."""
        self._wildcard_subscribers.append(handler)

    def unsubscribe(self, event_type: EventType | str, handler: Callable) -> None:
        """Remove a subscriber."""
        key = event_type.value if isinstance(event_type, EventType) else event_type
        handlers = self._subscribers.get(key, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, event_type: EventType | str, **data) -> int:
        """Emit an event. Returns number of handlers notified.

        All handlers are called concurrently. Failing handlers are
        logged but do not prevent other handlers from executing.
        """
        key = event_type.value if isinstance(event_type, EventType) else event_type
        self._event_count += 1

        event = {
            "type": key,
            "timestamp": time.time(),
            **data,
        }

        handlers = list(self._subscribers.get(key, [])) + list(self._wildcard_subscribers)
        if not handlers:
            return 0

        notified = 0
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
                notified += 1
            except Exception as exc:
                logger.warning("Event handler failed for %s: %s", key, exc)

        return notified

    def emit_sync(self, event_type: EventType | str, **data) -> None:
        """Fire-and-forget emit (creates task, doesn't await).

        Safe to call from sync code if an event loop is running.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.emit(event_type, **data))
            else:
                loop.run_until_complete(self.emit(event_type, **data))
        except RuntimeError:
            pass  # No event loop — skip

    @property
    def stats(self) -> dict:
        return {
            "total_events": self._event_count,
            "subscriber_count": sum(len(h) for h in self._subscribers.values()) + len(self._wildcard_subscribers),
            "event_types": list(self._subscribers.keys()),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: EventEmitter | None = None


def get_emitter() -> EventEmitter:
    """Get or create the module-level EventEmitter singleton."""
    global _instance
    if _instance is None:
        _instance = EventEmitter()
    return _instance


# ─────────────────────────────────────────────────────────────────────────────
# Agent-activity helpers — standalone replacement for FluxEmitter's
# inject_agent_activity / clear_agent_activity.  In a full AitherOS deployment
# these drive the live dashboard; standalone they emit a local CUSTOM event and
# are otherwise harmless no-ops.
# ─────────────────────────────────────────────────────────────────────────────


def inject_agent_activity(agent: str, activity: Any = None, **extra) -> None:
    """Record what an agent is currently doing (fire-and-forget, never raises)."""
    try:
        get_emitter().emit_sync(
            EventType.CUSTOM, kind="agent_activity", agent=agent, activity=activity, **extra
        )
    except Exception:  # telemetry must never break the caller
        pass


def clear_agent_activity(agent: str, **extra) -> None:
    """Clear an agent's current-activity marker (fire-and-forget, never raises)."""
    try:
        get_emitter().emit_sync(
            EventType.CUSTOM, kind="agent_activity_clear", agent=agent, **extra
        )
    except Exception:
        pass
