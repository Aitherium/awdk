"""Genesis graph tools must not block the event loop, nor be offered without a backend.

Measured on the daemon host: graph_search / graph_memory_query ran a synchronous
httpx call on the event loop; each refused connect held the loop ~4.5 s, and one turn
of ten such calls froze every other request until the watchdog replaced the daemon.
"""
from __future__ import annotations

import asyncio
import inspect
import time

from adk import builtin_tools as bt


def test_genesis_graph_tools_are_async():
    for fn in bt._GENESIS_ONLY_GRAPH_TOOLS:
        assert inspect.iscoroutinefunction(fn), fn.__name__


def test_graph_search_does_not_block_the_loop(monkeypatch):
    def slow_request(*_a, **_k):
        time.sleep(0.6)
        return '{"error": "refused"}'

    monkeypatch.setattr(bt, "_graph_request", slow_request)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        t = asyncio.create_task(ticker())
        out = await bt.graph_search("what time is it")
        t.cancel()
        return out, ticks

    out, ticks = asyncio.run(run())
    assert "refused" in out
    assert ticks >= 5, f"event loop starved during the call ({ticks} ticks)"


def test_graph_category_never_empty_without_genesis(monkeypatch):
    """Every agent profile declares 'graph'; emptying it made the daemon watchdog
    replace the daemon every two minutes. Dead backends leave via the registry breaker."""
    monkeypatch.delenv("AITHER_GENESIS_URL", raising=False)
    bt._init_graph_tools()
    offered = bt.TOOL_CATEGORIES["graph"]
    assert all(t in offered for t in bt._GENESIS_ONLY_GRAPH_TOOLS)
