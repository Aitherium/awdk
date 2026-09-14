"""Tests for the shared adk continuation primitive (adk.llm.continuation)."""

from __future__ import annotations

import pytest

from adk.llm.base import LLMResponse, Message
from adk.llm.continuation import (
    continuation_enabled,
    run_continuation,
    stitch,
)


def _resp(content, finish="length", completion_tokens=0):
    return LLMResponse(content=content, finish_reason=finish, completion_tokens=completion_tokens)


def _chat_fn(rounds):
    """Return a chat_fn yielding the queued responses in order."""
    seq = list(rounds)
    calls = {"n": 0, "messages": []}

    async def _fn(messages):
        i = calls["n"]
        calls["n"] += 1
        calls["messages"].append(messages)
        return seq[i] if i < len(seq) else _resp("", "stop")

    return _fn, calls


# ---------------------------------------------------------------------------
# stitch
# ---------------------------------------------------------------------------

def test_stitch_seamless_concat():
    assert stitch("The quick brown fox", " jumps over") == "The quick brown fox jumps over"


def test_stitch_overlap_dedup():
    assert stitch("A fox over the lazy", "the lazy dog runs") == "A fox over the lazy dog runs"


def test_stitch_restart_keeps_longer():
    acc = "HEADER content here"
    chunk = "HEADER content here and a great deal more text following on"
    assert stitch(acc, chunk) == chunk


def test_stitch_empty_inputs():
    assert stitch("", "abc") == "abc"
    assert stitch("abc", "") == "abc"


# ---------------------------------------------------------------------------
# run_continuation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_continuation_when_not_truncated():
    fn, calls = _chat_fn([])
    first = _resp("done", finish="stop")
    out = await run_continuation(fn, [Message(role="user", content="x")], first)
    assert out is first
    assert calls["n"] == 0  # chat_fn never called


@pytest.mark.asyncio
async def test_single_round_completes():
    fn, calls = _chat_fn([_resp(" and the rest.", finish="stop", completion_tokens=7)])
    first = _resp("The beginning", finish="length", completion_tokens=10)
    out = await run_continuation(fn, [Message(role="user", content="x")], first)
    assert out.finish_reason == "stop"
    assert out.content == "The beginning and the rest."
    assert out.completion_tokens == 17  # summed across rounds
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_multi_round_until_stop():
    fn, _ = _chat_fn([
        _resp(" part two", finish="length"),
        _resp(" part three.", finish="stop"),
    ])
    first = _resp("part one", finish="length")
    out = await run_continuation(
        fn, [Message(role="user", content="x")], first, max_continuations=5,
    )
    assert out.finish_reason == "stop"
    assert out.content == "part one part two part three."


@pytest.mark.asyncio
async def test_budget_cap_keeps_length():
    fn, _ = _chat_fn([
        _resp(" a", finish="length"),
        _resp(" b", finish="length"),
        _resp(" c", finish="length"),
    ])
    first = _resp("start", finish="length")
    out = await run_continuation(
        fn, [Message(role="user", content="x")], first, max_continuations=2,
    )
    assert out.finish_reason == "length"   # bound hit → best-effort partial
    assert out.content == "start a b"       # first + 2 rounds


@pytest.mark.asyncio
async def test_does_not_mutate_base_messages():
    base = [Message(role="user", content="x")]
    fn, calls = _chat_fn([_resp(" tail", finish="stop")])
    await run_continuation(fn, base, _resp("head", finish="length"))
    assert len(base) == 1  # caller's history untouched
    # the continuation built its own prefill (assistant partial + continue user)
    assert len(calls["messages"][0]) == 3
    assert calls["messages"][0][-1].role == "user"
    assert calls["messages"][0][-2].role == "assistant"


@pytest.mark.asyncio
async def test_no_growth_breaks():
    fn, calls = _chat_fn([_resp("", finish="length")])  # empty continuation
    out = await run_continuation(fn, [Message(role="user", content="x")], _resp("partial", finish="length"))
    assert out.content == "partial"
    assert calls["n"] == 1  # tried once, then stopped on empty


@pytest.mark.asyncio
async def test_continuation_failure_preserves_partial():
    async def _boom(_messages):
        raise RuntimeError("backend down")
    out = await run_continuation(_boom, [Message(role="user", content="x")], _resp("partial answer", finish="length"))
    assert out.content == "partial answer"  # never raises; partial preserved


@pytest.mark.asyncio
async def test_killswitch_disables(monkeypatch):
    monkeypatch.setenv("ADK_LLM_CONTINUATION", "off")
    assert continuation_enabled() is False
    fn, calls = _chat_fn([_resp(" more", finish="stop")])
    first = _resp("truncated", finish="length")
    out = await run_continuation(fn, [Message(role="user", content="x")], first)
    assert out is first
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_provider_chat_with_continuation_inherits():
    """Every LLMProvider gets chat_with_continuation for free."""
    from adk.llm.base import LLMProvider

    class _FakeProvider(LLMProvider):
        def __init__(self):
            self._seq = [
                _resp("first half", finish="length"),
                _resp(" second half.", finish="stop"),
            ]
            self._i = 0

        async def chat(self, messages, **kwargs):
            r = self._seq[self._i] if self._i < len(self._seq) else _resp("", "stop")
            self._i += 1
            return r

        async def list_models(self):
            return ["fake"]

    p = _FakeProvider()
    out = await p.chat_with_continuation([Message(role="user", content="x")], max_continuations=3)
    assert out.finish_reason == "stop"
    assert out.content == "first half second half."
