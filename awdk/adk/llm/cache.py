"""Provider-neutral response cache for exact-match query results.

Lives above the provider layer so that identical requests (model, messages,
tools, temperature) are transparently served from cache, bypassing the provider
entirely. Supports pluggable backends (in-memory LRU by default; Redis/Supabase
drops in later).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from typing import Awaitable, Callable, Protocol

from .base import LLMResponse, Message


class CacheBackend(Protocol):
    """A pluggable cache backend interface.

    Any backend (in-memory, Redis, Supabase, etc.) implements these three
    methods to integrate with ResponseCache.
    """

    async def get(self, key: str) -> LLMResponse | None:
        """Retrieve a cached response by key. Returns None on miss."""
        ...

    async def set(self, key: str, value: LLMResponse) -> None:
        """Store a response under a key."""
        ...

    async def __len__(self) -> int:
        """Return the current number of cached items."""
        ...


class InMemoryLRUBackend(CacheBackend):
    """In-memory LRU cache with configurable max size.

    Automatically evicts least-recently-used items when capacity is exceeded.
    Thread-safe via asyncio.Lock (handles concurrent access within a single
    event loop).
    """

    def __init__(self, maxsize: int = 512):
        self.maxsize = maxsize
        self._cache: OrderedDict[str, LLMResponse] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> LLMResponse | None:
        """Retrieve and bump LRU order."""
        async with self._lock:
            if key in self._cache:
                # Move to end (most recently used)
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    async def set(self, key: str, value: LLMResponse) -> None:
        """Store or update, evicting oldest if over capacity."""
        async with self._lock:
            if key in self._cache:
                # Update and move to end
                self._cache.move_to_end(key)
            self._cache[key] = value
            # Evict oldest if over capacity
            while len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)

    async def __len__(self) -> int:
        """Return current cache size."""
        async with self._lock:
            return len(self._cache)


def cache_key(
    model: str,
    messages: list[Message],
    tools: list[dict] | None = None,
    temperature: float = 0.7,
    **extra,
) -> str:
    """Generate a stable, deterministic cache key.

    Canonical form: SHA256(JSON of (model, messages, tools, temperature)).
    - Messages are order-sensitive: (role, content, name, tool_call_id) tuple per message.
    - Tools are sorted by key to normalize dict key ordering.
    - Temperature is rounded to 3 decimals for stability across float rounding.
    - Extra kwargs are ignored (cache key is defined by core LLM parameters only).

    Returns a hex-encoded SHA256 hash suitable as a cache key.
    """
    # Normalize messages to tuples (order matters)
    msg_tuples = [
        (m.role, m.content, m.name, m.tool_call_id)
        for m in messages
    ]

    # Normalize tools: sort keys so dict order doesn't matter
    normalized_tools = None
    if tools:
        normalized_tools = [
            {k: v for k, v in sorted(t.items())}
            for t in tools
        ]

    # Build canonical form
    canonical = {
        "model": model,
        "messages": msg_tuples,
        "tools": normalized_tools,
        "temperature": round(temperature, 3),
    }

    # JSON serialize (sorted keys for stability)
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))

    # Hash
    return hashlib.sha256(canonical_json.encode()).hexdigest()


def should_cache(resp: LLMResponse) -> bool:
    """Determine if a response should be stored in cache.

    Do NOT cache:
    - Responses with tool_calls (must re-execute)
    - Responses that errored or were truncated (finish_reason in {"error", "length"})

    Returns True if the response is safe to cache and reuse.
    """
    # Tool calls must execute fresh
    if resp.tool_calls:
        return False
    # Error/truncation conditions shouldn't be cached
    if resp.finish_reason in ("error", "length"):
        return False
    return True


class ResponseCache:
    """Provider-neutral response cache for exact-match query results.

    Caches full LLMResponse objects keyed by (model, messages, tools, temperature).
    On hit, returns a COPY to prevent mutation of the cached original. On miss,
    calls the provided async function, caches the result, and returns it.

    Supports concurrent requests for the same key (single-flight protection:
    only one call is made, others await the result).

    Usage::

        cache = ResponseCache()
        resp = await cache.get_or_call(
            key=cache_key(model, messages, tools, temp),
            call=lambda: provider.chat(messages, model=model, ...),
        )
    """

    def __init__(self, backend: CacheBackend | None = None):
        """Initialize with an optional backend. Defaults to InMemoryLRUBackend(512)."""
        self.backend = backend or InMemoryLRUBackend(maxsize=512)
        # Single-flight protection: {key: (event, result_holder)} for in-flight
        self._in_flight: dict[str, tuple[asyncio.Event, list]] = {}
        self._lock = asyncio.Lock()

    async def get_or_call(
        self,
        key: str,
        call: Callable[[], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        """Retrieve or compute a response.

        On cache hit, returns a COPY of the cached response with cache_status="response_hit".
        On cache miss, awaits call(), stores the result (if should_cache), and returns it.
        Concurrent identical keys await a single call (single-flight).

        Args:
            key: Cache key (typically from cache_key()).
            call: Async callable that returns an LLMResponse.

        Returns:
            LLMResponse with cache_status set appropriately.
        """
        # Try cache first
        cached = await self.backend.get(key)
        if cached is not None:
            # Return a copy with cache_status marker
            result = _copy_response(cached)
            result.cache_status = "response_hit"
            return result

        # Check if another task is already fetching this key
        async with self._lock:
            if key in self._in_flight:
                # Another task is fetching; wait for the result
                event, result_holder = self._in_flight[key]
            else:
                # We're first; start the fetch
                event = asyncio.Event()
                result_holder = []
                self._in_flight[key] = (event, result_holder)
                # Schedule the actual fetch
                asyncio.create_task(self._do_fetch(key, call, event, result_holder))

        # Wait for the result
        await event.wait()

        # Result should be in the holder
        if result_holder:
            return result_holder[0]

        # Shouldn't reach here, but fail gracefully
        raise RuntimeError(f"cache result lost for {key}")

    async def _do_fetch(
        self,
        key: str,
        call: Callable[[], Awaitable[LLMResponse]],
        event: asyncio.Event,
        result_holder: list,
    ) -> None:
        """Internal: fetch a response and notify waiters."""
        try:
            result = await call()

            # Only cache if appropriate
            if should_cache(result):
                await self.backend.set(key, result)

            # Store result for waiters
            result_holder.append(result)
        finally:
            # Signal all waiters
            event.set()

            # Clean up in-flight tracking
            async with self._lock:
                self._in_flight.pop(key, None)

    async def clear(self) -> None:
        """Clear all cached entries."""
        # InMemoryLRUBackend doesn't have a clear, so we recreate it
        if isinstance(self.backend, InMemoryLRUBackend):
            async with self.backend._lock:
                self.backend._cache.clear()
        # For other backends, they'd need a clear() method


def _copy_response(resp: LLMResponse) -> LLMResponse:
    """Create a shallow copy of an LLMResponse.

    Safe for mutation (doesn't affect the cached original). Tool calls and
    content are not deep-copied (they're immutable-ish for our purposes).
    """
    return LLMResponse(
        content=resp.content,
        model=resp.model,
        tokens_used=resp.tokens_used,
        prompt_tokens=resp.prompt_tokens,
        completion_tokens=resp.completion_tokens,
        latency_ms=resp.latency_ms,
        tool_calls=resp.tool_calls,
        finish_reason=resp.finish_reason,
        effort_level=resp.effort_level,
        cache_status=resp.cache_status,
        cache_read_tokens=resp.cache_read_tokens,
        cache_write_tokens=resp.cache_write_tokens,
    )
