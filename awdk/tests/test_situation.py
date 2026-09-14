"""Tests for adk.situation and its wiring — the clock an agent should simply have.

The bug this guards (2026-08-23): asked "what time is it" through AitherShell,
the ADK daemon took ~18 s and illustrated its answer with "Tuesday, June 4,
2025" — a date it invented, because no system prompt carried a clock, and the
daemon's /chat/stream handler silently DROPPED the `system_additions` the shell
sent. Every assertion below fails against that code.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from adk.llm.base import StreamChunk
from adk.situation import (
    HOST_HEADER,
    render_host_block,
    sanitize_additions,
    self_test,
    situation_block,
)


def test_self_test_is_clean():
    failures = self_test()
    assert failures == [], "\n".join(failures)


def test_host_block_is_cache_safe_and_honest():
    from datetime import datetime, timedelta, timezone
    fixed = datetime(2026, 8, 23, 6, 11, 5, tzinfo=timezone(timedelta(hours=-7), "PDT"))
    blk = render_host_block(fixed, hostname="BOX", system="Windows", release="11",
                            cwd="C:\\x", user="u")
    lines = blk.splitlines()
    assert lines[0] == HOST_HEADER
    assert lines[1] == "local time: Sunday 2026-08-23 06:11 (UTC-07:00, PDT)"   # minute precision
    assert lines[2] == "utc: 2026-08-23T13:11Z"
    assert "do not call a tool" in blk and "never invent a date" in blk
    sparse = render_host_block(fixed, hostname="", system="", release="", cwd="", user="")
    assert not any(ln.startswith(("host:", "os:", "user:", "cwd:")) for ln in sparse.splitlines())


def test_additions_are_bounded_and_follow_the_host_block():
    adds = sanitize_additions(["  a ", None, 3, "", "b" * 10_000] + ["c"] * 50)
    assert adds[0] == "a" and len(adds[1]) == 4000 and len(adds) <= 8
    out = situation_block(["[USER'S SHELL] local time: X"], env={})
    assert out.startswith("\n\n" + HOST_HEADER)
    assert out.index(HOST_HEADER) < out.index("[USER'S SHELL]")
    # Kill switch drops the host block but never the caller's additions.
    assert situation_block(None, env={"ADK_SITUATION": "0"}) == ""
    assert situation_block(["keep me"], env={"ADK_SITUATION": "0"}).endswith("keep me")


# ── Agent wiring ──────────────────────────────────────────────────────────────

def _llm(chunks):
    from unittest.mock import AsyncMock, MagicMock
    captured: dict = {}

    async def _stream(messages, *a, **k):
        captured["messages"] = messages
        for c in chunks:
            yield c

    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    resp = MagicMock(content="x", model="m", tokens_used=1, latency_ms=1.0,
                     tool_calls=[], tool_calls_made=[], finish_reason="stop", session_id="s")
    llm.chat = AsyncMock(return_value=resp)
    return llm, captured


@pytest.mark.asyncio
async def test_chat_stream_system_prompt_ends_with_host_block_and_additions():
    from adk.agent import AitherAgent
    llm, cap = _llm([StreamChunk(content="hi", done=True, model="t")])
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")
    async for _ in agent.chat_stream("what time is it",
                                     system_additions=["[USER'S SHELL] local time: Q"]):
        pass
    sys_msg = cap["messages"][0]
    assert sys_msg.role == "system"
    c = sys_msg.content
    assert c.startswith("IDENTITY")                         # identity stays the cacheable prefix
    assert HOST_HEADER in c and c.index(HOST_HEADER) > c.index("IDENTITY")
    assert c.rstrip().endswith("local time: Q")              # caller block is LAST


@pytest.mark.asyncio
async def test_stream_react_system_prompt_carries_situation_after_tools():
    from adk.agent import AitherAgent
    llm, cap = _llm([StreamChunk(content="<think>k</think>FINAL: now", done=True, model="t")])
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")

    @agent.tool
    def noop() -> str:
        """does nothing"""
        return "ok"

    events = []
    resp = await agent.stream_react("what time is it", on_event=events.append,
                                    system_additions=["[USER'S SHELL] local time: Q"])
    assert resp.content == "now"
    c = cap["messages"][0].content
    assert c.index("Available tools:") < c.index(HOST_HEADER) < c.index("[USER'S SHELL]")


# ── Server wiring: system_additions reaches the agent, and events relay LIVE ──

@pytest.mark.asyncio
async def test_aitheros_stream_relays_live_and_forwards_additions():
    from adk.agent import AitherAgent
    from adk.server import _aitheros_stream

    gate = asyncio.Event()
    seen: dict = {}

    async def _stream(messages, *a, **k):
        seen["messages"] = messages
        yield StreamChunk(content="FINAL: first", done=False, model="t")
        await gate.wait()                       # the turn is NOT finished yet
        yield StreamChunk(content=" second", done=True, model="t")

    from unittest.mock import AsyncMock
    llm = AsyncMock()
    llm.chat_stream = _stream
    llm.provider_name = "test"
    agent = AitherAgent(name="t", llm=llm, builtin_tools=False, system_prompt="IDENTITY")

    @agent.tool
    def noop() -> str:
        """does nothing"""
        return "ok"

    async def get_agent(_name):
        return agent

    gen = _aitheros_stream(get_agent, "what time is it", "sid", None, False,
                           system_additions=["[USER'S SHELL] local time: Q"])
    frames = []
    # Pull frames until the FIRST token arrives; with the old collect-then-replay
    # this would block forever here because nothing was yielded until stream_react
    # returned — and it cannot return until `gate` is set below.
    first_token = None
    async def _pull_until_token():
        nonlocal first_token
        async for f in gen:
            frames.append(f)
            if f.startswith("event: token"):
                first_token = f
                return
    await asyncio.wait_for(_pull_until_token(), timeout=5.0)
    assert first_token is not None and "first" in first_token
    gate.set()
    async for f in gen:
        frames.append(f)
    joined = "".join(frames)
    assert "event: answer" in joined and "event: complete" in joined
    ans = [json.loads(f.split("data: ", 1)[1]) for f in frames if f.startswith("event: answer")][0]
    assert ans["answer"] == "first second"
    # The caller's block reached the system prompt (it used to be dropped).
    assert "[USER'S SHELL] local time: Q" in seen["messages"][0].content
