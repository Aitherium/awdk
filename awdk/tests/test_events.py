"""Tests for adk/events.py — EventEmitter pub/sub."""

import sys
import asyncio
import pytest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.events import EventEmitter, EventType, get_emitter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_emitter_singleton():
    import adk.events as ev_mod
    ev_mod._instance = None
    yield
    ev_mod._instance = None


@pytest.fixture
def emitter():
    return EventEmitter()


# ---------------------------------------------------------------------------
# EventType enum
# ---------------------------------------------------------------------------

class TestEventType:
    def test_event_types_are_strings(self):
        assert EventType.TOOL_CALL == "tool_call"
        assert EventType.CHAT_REQUEST == "chat_request"
        assert EventType.FORGE_DISPATCH == "forge_dispatch"

    def test_all_event_types_unique(self):
        values = [e.value for e in EventType]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Subscribe / Emit
# ---------------------------------------------------------------------------

class TestSubscribeAndEmit:
    @pytest.mark.asyncio
    async def test_subscribe_and_emit_sync_handler(self, emitter):
        received = []

        def handler(event):
            received.append(event)

        emitter.subscribe(EventType.TOOL_CALL, handler)
        notified = await emitter.emit(EventType.TOOL_CALL, tool="web_search")

        assert notified == 1
        assert len(received) == 1
        assert received[0]["type"] == "tool_call"
        assert received[0]["tool"] == "web_search"
        assert "timestamp" in received[0]

    @pytest.mark.asyncio
    async def test_subscribe_and_emit_async_handler(self, emitter):
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(EventType.CHAT_RESPONSE, handler)
        notified = await emitter.emit(EventType.CHAT_RESPONSE, tokens=100)

        assert notified == 1
        assert received[0]["tokens"] == 100

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, emitter):
        count = {"a": 0, "b": 0}

        def handler_a(event):
            count["a"] += 1

        def handler_b(event):
            count["b"] += 1

        emitter.subscribe(EventType.LLM_CALL, handler_a)
        emitter.subscribe(EventType.LLM_CALL, handler_b)
        notified = await emitter.emit(EventType.LLM_CALL, model="llama")

        assert notified == 2
        assert count["a"] == 1
        assert count["b"] == 1

    @pytest.mark.asyncio
    async def test_emit_with_no_subscribers(self, emitter):
        notified = await emitter.emit(EventType.LLM_ERROR, error="bad")
        assert notified == 0

    @pytest.mark.asyncio
    async def test_subscribe_with_string_key(self, emitter):
        received = []
        emitter.subscribe("custom_event", lambda e: received.append(e))
        await emitter.emit("custom_event", data="test")
        assert len(received) == 1
        assert received[0]["type"] == "custom_event"

    @pytest.mark.asyncio
    async def test_emit_with_enum_value(self, emitter):
        received = []
        emitter.subscribe(EventType.AGENT_STARTED, lambda e: received.append(e))
        await emitter.emit(EventType.AGENT_STARTED, agent="atlas")
        assert received[0]["agent"] == "atlas"

    @pytest.mark.asyncio
    async def test_different_event_types_isolated(self, emitter):
        calls_a = []
        calls_b = []
        emitter.subscribe(EventType.TOOL_CALL, lambda e: calls_a.append(e))
        emitter.subscribe(EventType.TOOL_RESULT, lambda e: calls_b.append(e))

        await emitter.emit(EventType.TOOL_CALL, tool="search")
        assert len(calls_a) == 1
        assert len(calls_b) == 0


# ---------------------------------------------------------------------------
# Wildcard subscribers
# ---------------------------------------------------------------------------

class TestWildcard:
    @pytest.mark.asyncio
    async def test_subscribe_all(self, emitter):
        received = []
        emitter.subscribe_all(lambda e: received.append(e))

        await emitter.emit(EventType.TOOL_CALL, tool="a")
        await emitter.emit(EventType.LLM_CALL, model="b")
        await emitter.emit("custom", data="c")

        assert len(received) == 3
        types = {r["type"] for r in received}
        assert "tool_call" in types
        assert "llm_call" in types
        assert "custom" in types

    @pytest.mark.asyncio
    async def test_wildcard_plus_specific(self, emitter):
        all_events = []
        tool_events = []

        emitter.subscribe_all(lambda e: all_events.append(e))
        emitter.subscribe(EventType.TOOL_CALL, lambda e: tool_events.append(e))

        await emitter.emit(EventType.TOOL_CALL, tool="search")
        await emitter.emit(EventType.LLM_CALL, model="llama")

        assert len(all_events) == 2
        assert len(tool_events) == 1


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------

class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe_stops_notifications(self, emitter):
        count = {"value": 0}

        def handler(event):
            count["value"] += 1

        emitter.subscribe(EventType.TOOL_CALL, handler)
        await emitter.emit(EventType.TOOL_CALL, tool="a")
        assert count["value"] == 1

        emitter.unsubscribe(EventType.TOOL_CALL, handler)
        await emitter.emit(EventType.TOOL_CALL, tool="b")
        assert count["value"] == 1  # Not incremented

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_handler_is_safe(self, emitter):
        def handler(event):
            pass

        # Should not raise
        emitter.unsubscribe(EventType.TOOL_CALL, handler)

    @pytest.mark.asyncio
    async def test_unsubscribe_with_string_key(self, emitter):
        handler = MagicMock()
        emitter.subscribe("custom", handler)
        emitter.unsubscribe("custom", handler)
        await emitter.emit("custom", data="test")
        handler.assert_not_called()


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    @pytest.mark.asyncio
    async def test_bad_handler_doesnt_break_others(self, emitter):
        received = []

        def bad_handler(event):
            raise ValueError("handler crashed!")

        def good_handler(event):
            received.append(event)

        emitter.subscribe(EventType.TOOL_CALL, bad_handler)
        emitter.subscribe(EventType.TOOL_CALL, good_handler)

        notified = await emitter.emit(EventType.TOOL_CALL, tool="test")
        # bad_handler raises but good_handler still runs
        assert len(received) == 1
        # Only good_handler counted as notified
        assert notified == 1

    @pytest.mark.asyncio
    async def test_bad_async_handler_doesnt_break_others(self, emitter):
        received = []

        async def bad_handler(event):
            raise RuntimeError("async crash!")

        async def good_handler(event):
            received.append(event)

        emitter.subscribe(EventType.LLM_CALL, bad_handler)
        emitter.subscribe(EventType.LLM_CALL, good_handler)

        notified = await emitter.emit(EventType.LLM_CALL, model="test")
        assert len(received) == 1


# ---------------------------------------------------------------------------
# Event counting / stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_event_count_increments(self, emitter):
        assert emitter.stats["total_events"] == 0
        await emitter.emit(EventType.TOOL_CALL, tool="a")
        await emitter.emit(EventType.LLM_CALL, model="b")
        assert emitter.stats["total_events"] == 2

    @pytest.mark.asyncio
    async def test_subscriber_count(self, emitter):
        emitter.subscribe(EventType.TOOL_CALL, lambda e: None)
        emitter.subscribe(EventType.LLM_CALL, lambda e: None)
        emitter.subscribe_all(lambda e: None)
        assert emitter.stats["subscriber_count"] == 3

    @pytest.mark.asyncio
    async def test_event_types_tracked(self, emitter):
        emitter.subscribe(EventType.TOOL_CALL, lambda e: None)
        emitter.subscribe("custom_type", lambda e: None)
        types = emitter.stats["event_types"]
        assert "tool_call" in types
        assert "custom_type" in types


# ---------------------------------------------------------------------------
# emit_sync
# ---------------------------------------------------------------------------

class TestEmitSync:
    def test_emit_sync_no_event_loop(self, emitter):
        # Should not raise even without a running event loop
        # (it catches RuntimeError)
        emitter.emit_sync(EventType.TOOL_CALL, tool="test")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_emitter_returns_same_instance(self):
        e1 = get_emitter()
        e2 = get_emitter()
        assert e1 is e2

    def test_get_emitter_creates_instance(self):
        emitter = get_emitter()
        assert isinstance(emitter, EventEmitter)
