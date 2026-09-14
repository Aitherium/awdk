"""Provider-neutral response cache (OQ16 lever 2).

Tests the ResponseCache class: exact-match caching, LRU eviction, single-flight
protection, copy-not-mutate semantics, and appropriate cache filtering.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm.cache import (
    ResponseCache,
    InMemoryLRUBackend,
    cache_key,
    should_cache,
    _copy_response,
)
from adk.llm.base import LLMResponse, Message, ToolCall


# ── cache_key: determinism & order-sensitivity ──────────────────────────────

def test_cache_key_is_deterministic():
    """Same inputs produce the same key."""
    msgs = [Message(role="user", content="hello")]
    key1 = cache_key("gpt-4", msgs, temperature=0.7)
    key2 = cache_key("gpt-4", msgs, temperature=0.7)
    assert key1 == key2


def test_cache_key_message_order_matters():
    """Reordering messages changes the key."""
    msg_a = Message(role="user", content="a")
    msg_b = Message(role="user", content="b")

    key1 = cache_key("gpt-4", [msg_a, msg_b])
    key2 = cache_key("gpt-4", [msg_b, msg_a])

    assert key1 != key2


def test_cache_key_temperature_rounded():
    """Temperature is rounded to 3 decimals."""
    msgs = [Message(role="user", content="q")]
    key1 = cache_key("gpt-4", msgs, temperature=0.7001)
    key2 = cache_key("gpt-4", msgs, temperature=0.7002)
    # Both round to 0.700
    assert key1 == key2


def test_cache_key_tools_dict_order_normalized():
    """Tools dict key ordering doesn't affect the key."""
    msgs = [Message(role="user", content="q")]
    # Same tool but with keys in different order at the top level
    tool_dict = {"function": {"name": "a", "description": "desc", "parameters": {}}}

    # Manually create dicts with different key order (Python 3.7+ preserves order,
    # but our normalize step sorts keys, so both should hash identically)
    import json
    tool_json = json.dumps(tool_dict)
    tool_a = json.loads(tool_json)  # Standard order

    # Create one with reversed key access patterns (but same content)
    tool_b_data = {"parameters": {}, "description": "desc", "name": "a"}
    tool_b = {"function": tool_b_data}

    key1 = cache_key("gpt-4", msgs, tools=[tool_a])
    key2 = cache_key("gpt-4", msgs, tools=[tool_b])

    assert key1 == key2


def test_cache_key_includes_model():
    """Different models produce different keys."""
    msgs = [Message(role="user", content="q")]
    key1 = cache_key("gpt-4", msgs)
    key2 = cache_key("gpt-3.5", msgs)
    assert key1 != key2


def test_cache_key_includes_tools():
    """Presence/absence of tools changes the key."""
    msgs = [Message(role="user", content="q")]
    key1 = cache_key("gpt-4", msgs, tools=None)
    key2 = cache_key("gpt-4", msgs, tools=[{"function": {"name": "search"}}])
    assert key1 != key2


def test_cache_key_message_name_included():
    """Message.name is part of the key."""
    msg_a = Message(role="user", content="q")
    msg_b = Message(role="user", content="q", name="alice")

    key1 = cache_key("gpt-4", [msg_a])
    key2 = cache_key("gpt-4", [msg_b])

    assert key1 != key2


def test_cache_key_message_tool_call_id_included():
    """Message.tool_call_id is part of the key."""
    msg_a = Message(role="tool", content="result")
    msg_b = Message(role="tool", content="result", tool_call_id="call_123")

    key1 = cache_key("gpt-4", [msg_a])
    key2 = cache_key("gpt-4", [msg_b])

    assert key1 != key2


# ── should_cache: filtering logic ────────────────────────────────────────────

def test_should_cache_success_response():
    """Normal responses should be cached."""
    resp = LLMResponse(content="answer", finish_reason="stop")
    assert should_cache(resp) is True


def test_should_cache_tool_calls_not_cached():
    """Responses with tool_calls must not be cached."""
    resp = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="1", name="search", arguments={})],
        finish_reason="tool_calls",
    )
    assert should_cache(resp) is False


def test_should_cache_error_responses_not_cached():
    """Error responses should not be cached."""
    resp = LLMResponse(content="error", finish_reason="error")
    assert should_cache(resp) is False


def test_should_cache_truncated_responses_not_cached():
    """Truncated (length) responses should not be cached."""
    resp = LLMResponse(content="partial", finish_reason="length")
    assert should_cache(resp) is False


# ── _copy_response: mutation safety ──────────────────────────────────────────

def test_copy_response_does_not_share_reference():
    """Copying creates independent objects."""
    orig = LLMResponse(
        content="answer",
        model="gpt-4",
        tokens_used=100,
        cache_status="",
    )
    copied = _copy_response(orig)

    # Mutate the copy
    copied.cache_status = "hit"
    copied.tokens_used = 200

    # Original unchanged
    assert orig.cache_status == ""
    assert orig.tokens_used == 100


# ── InMemoryLRUBackend: LRU eviction ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_inmemory_backend_stores_and_retrieves():
    """Backend stores and retrieves responses."""
    backend = InMemoryLRUBackend(maxsize=2)
    resp = LLMResponse(content="answer")

    await backend.set("key1", resp)
    retrieved = await backend.get("key1")

    assert retrieved is resp


@pytest.mark.asyncio
async def test_inmemory_backend_lru_eviction():
    """Oldest item is evicted when maxsize exceeded."""
    backend = InMemoryLRUBackend(maxsize=2)

    resp1 = LLMResponse(content="1")
    resp2 = LLMResponse(content="2")
    resp3 = LLMResponse(content="3")

    await backend.set("a", resp1)
    await backend.set("b", resp2)
    await backend.set("c", resp3)  # Evicts "a"

    assert await backend.get("a") is None
    assert await backend.get("b") is not None
    assert await backend.get("c") is not None


@pytest.mark.asyncio
async def test_inmemory_backend_move_to_end_on_access():
    """Accessing an item bumps its LRU order."""
    backend = InMemoryLRUBackend(maxsize=2)

    resp1 = LLMResponse(content="1")
    resp2 = LLMResponse(content="2")
    resp3 = LLMResponse(content="3")

    await backend.set("a", resp1)
    await backend.set("b", resp2)
    # Access "a" to make it recently used
    await backend.get("a")
    await backend.set("c", resp3)  # Evicts "b", not "a"

    assert await backend.get("a") is not None
    assert await backend.get("b") is None
    assert await backend.get("c") is not None


@pytest.mark.asyncio
async def test_inmemory_backend_len():
    """Backend reports current size."""
    backend = InMemoryLRUBackend(maxsize=3)
    assert await backend.__len__() == 0

    await backend.set("a", LLMResponse(content="1"))
    assert await backend.__len__() == 1

    await backend.set("b", LLMResponse(content="2"))
    assert await backend.__len__() == 2


# ── ResponseCache: main contract ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hit_returns_copy_with_status():
    """Cache hit returns a copy with cache_status='response_hit'."""
    cache = ResponseCache()
    call_count = 0

    async def fake_call():
        nonlocal call_count
        call_count += 1
        return LLMResponse(content="answer", model="gpt-4")

    key = "test_key"

    # First call: miss
    resp1 = await cache.get_or_call(key, fake_call)
    assert call_count == 1
    assert resp1.cache_status == ""

    # Second call: hit
    resp2 = await cache.get_or_call(key, fake_call)
    assert call_count == 1  # Call not made again
    assert resp2.cache_status == "response_hit"

    # Verify they're not the same object
    assert resp1 is not resp2
    assert resp1.content == resp2.content


@pytest.mark.asyncio
async def test_cache_miss_stores_result():
    """Cache miss calls the callable and stores the result."""
    cache = ResponseCache()
    resp = LLMResponse(content="answer", model="gpt-4", tokens_used=100)

    async def fake_call():
        return resp

    key = "test_key"
    result = await cache.get_or_call(key, fake_call)

    assert result.content == "answer"
    # Verify it's cached (by checking a second call doesn't invoke)
    result2 = await cache.get_or_call(key, fake_call)
    assert result2.cache_status == "response_hit"


@pytest.mark.asyncio
async def test_cache_does_not_cache_tool_calls():
    """Responses with tool_calls are not cached."""
    cache = ResponseCache()
    call_count = 0

    async def fake_call():
        nonlocal call_count
        call_count += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name="search", arguments={})],
            finish_reason="tool_calls",
        )

    key = "test_key"

    resp = await cache.get_or_call(key, fake_call)
    assert call_count == 1
    assert resp.tool_calls

    # Second call: still a miss (tool_calls not cached)
    _ = await cache.get_or_call(key, fake_call)
    assert call_count == 2  # Called again


@pytest.mark.asyncio
async def test_cache_does_not_cache_errors():
    """Error responses are not cached."""
    cache = ResponseCache()
    call_count = 0

    async def fake_call():
        nonlocal call_count
        call_count += 1
        return LLMResponse(content="error", finish_reason="error")

    key = "test_key"

    _ = await cache.get_or_call(key, fake_call)
    assert call_count == 1

    # Second call: still a miss
    _ = await cache.get_or_call(key, fake_call)
    assert call_count == 2


@pytest.mark.asyncio
async def test_cache_concurrent_identical_keys_single_flight():
    """Concurrent requests for the same key result in a single call."""
    import asyncio

    cache = ResponseCache()
    call_count = 0
    call_started = asyncio.Event()

    async def fake_call():
        nonlocal call_count
        call_count += 1
        call_started.set()
        # Simulate work
        await asyncio.sleep(0.01)
        return LLMResponse(content="answer")

    key = "test_key"

    # Fire two concurrent requests
    async def request():
        return await cache.get_or_call(key, fake_call)

    task1 = asyncio.create_task(request())
    task2 = asyncio.create_task(request())

    # Wait for both
    await asyncio.gather(task1, task2)

    # Only one call should have been made (single-flight)
    assert call_count == 1

    # Both results should be successful
    r1 = await task1
    r2 = await task2
    assert r1.content == "answer"
    assert r2.content == "answer"


@pytest.mark.asyncio
async def test_cache_clear():
    """clear() empties the cache."""
    cache = ResponseCache()
    call_count = 0

    async def fake_call():
        nonlocal call_count
        call_count += 1
        return LLMResponse(content=f"call {call_count}")

    key = "test_key"

    # Prime the cache
    resp1 = await cache.get_or_call(key, fake_call)
    assert call_count == 1

    # Clear
    await cache.clear()

    # Next call is a miss
    resp2 = await cache.get_or_call(key, fake_call)
    assert call_count == 2
    assert resp1.content != resp2.content
