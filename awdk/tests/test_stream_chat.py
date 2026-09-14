"""Tests for the canonical honest instant-response glue (``adk.responder.stream_chat``)."""

from __future__ import annotations

import pytest

from adk import responder


class _Dec:
    """IntentDecision-like stub."""

    def __init__(self, **k):
        self.__dict__.update(k)


def _classify(dec):
    async def f():
        return dec
    return f


async def _collect(message, dec, *, stream_chunks, grounded):
    events = []

    async def on_ev(e):
        events.append(e)

    async def stream(msgs, mx):
        for c in stream_chunks:
            yield c

    async def oneshot(msgs, mx):
        return "FALLBACK"

    async def run_grounded(decision, on_ev_inner):
        return grounded

    res = await responder.stream_chat(
        message=message, on_event=on_ev, classify=_classify(dec),
        oneshot=oneshot, stream=stream, run_grounded=run_grounded,
        persona="You are Test.", name="Test",
    )
    tokens = "".join(e.get("t", "") for e in events if e.get("event") == "token")
    return res, events, tokens


@pytest.mark.asyncio
async def test_chitchat_is_direct_single_segment():
    """Non-agentic, low-effort → direct answer, no grounded enrich, think stripped."""
    res, events, tokens = await _collect(
        "hi", _Dec(agentic=False, effort=2, requires_grounding=False, grounding_label=""),
        stream_chunks=["Hel", "lo!", "<think>secret</think>", " Hi."],
        grounded={"answer": "SHOULD_NOT_APPEAR", "used_tools": False, "artifacts": []},
    )
    assert res["segments"] == 1
    assert res["answer"] == "Hello! Hi."
    assert "secret" not in tokens
    assert "SHOULD_NOT_APPEAR" not in res["answer"]


@pytest.mark.asyncio
async def test_grounding_acks_then_refines():
    """agentic/requires_grounding → instant honest ack, then grounded refinement."""
    res, events, tokens = await _collect(
        "what is in my calendar",
        _Dec(agentic=True, effort=5, requires_grounding=True, grounding_label="your calendar"),
        stream_chunks=["unused"],
        grounded={"answer": "Your calendar is empty today.", "used_tools": True, "artifacts": []},
    )
    assert res["segments"] == 2
    assert tokens.startswith("Let me check your calendar")
    assert res["answer"] == "Your calendar is empty today."


@pytest.mark.asyncio
async def test_agentic_without_grounding_label_uses_generic_ack():
    res, events, tokens = await _collect(
        "refactor this module",
        _Dec(agentic=True, effort=6, requires_grounding=False, grounding_label=""),
        stream_chunks=["unused"],
        grounded={"answer": "Done.", "used_tools": True, "artifacts": []},
    )
    assert tokens.startswith("On it")
    assert res["segments"] == 2
