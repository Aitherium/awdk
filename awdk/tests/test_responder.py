"""Tests for the canonical instant-response loop (adk.responder).

Mock first-pass streamer + enrich coroutine — no network. Validates the
"answer now, enrich in the background, auto-continue" event flow shared by
Genesis, awkit and ADK agents.
"""

import asyncio

from adk.responder import (
    respond,
    materially_different,
    additive_delta,
    text_similarity,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _collector():
    events = []
    async def on_event(evt):
        events.append(evt)
    return events, on_event


def _stream(chunks):
    async def gen():
        for c in chunks:
            yield c
    return gen


# ── refinement decision helpers ──────────────────────────────────────────────

def test_materially_different_rules():
    # identical, no tools → no refinement
    assert materially_different("hello", "hello", used_tools=False, has_artifacts=False) is False
    # tools used → always refine
    assert materially_different("hi", "hi", used_tools=True, has_artifacts=False) is True
    # artifacts produced → always refine
    assert materially_different("hi", "hi", used_tools=False, has_artifacts=True) is True
    # empty first pass → grounded is the answer
    assert materially_different("", "the answer", used_tools=False, has_artifacts=False) is True
    # diverged content (no tools) → refine
    assert materially_different("yes", "actually no, it's complicated because", used_tools=False, has_artifacts=False) is True


def test_additive_delta_strips_echo():
    assert additive_delta("Hello.", "Hello. Here is more.") == "Here is more."
    assert additive_delta("abc", "xyz") == "xyz"


def test_text_similarity_bounds():
    assert text_similarity("", "x") == 0.0
    assert text_similarity("same", "same") == 1.0


# ── orchestration ─────────────────────────────────────────────────────────────

def test_conversational_single_segment_no_refine():
    events, on_event = _collector()

    async def enrich(_on):
        return {"answer": "", "used_tools": False, "artifacts": []}  # trivial → no grounding

    res = _run(respond(
        message="hi",
        on_event=on_event,
        first_pass_stream=_stream(["Hello", "! 👋"]),
        enrich=enrich,
    ))
    kinds = [e.get("event") for e in events]
    assert "answer_segment" in kinds
    assert "complete" in kinds
    assert res["segments"] == 1
    # exactly one answer_segment (the initial), no refinement
    segs = [e for e in events if e.get("event") == "answer_segment"]
    assert len(segs) == 1 and segs[0]["kind"] == "initial"
    assert res["answer"] == "Hello! 👋"


def test_tool_grounded_emits_refinement():
    events, on_event = _collector()

    async def enrich(on_enrich):
        await on_enrich({"type": "tool", "name": "web_search", "args": {}})
        await on_enrich({"type": "tool_result", "name": "web_search", "result": "fresh data"})
        return {"answer": "The latest data shows X grew 12%.", "used_tools": True, "artifacts": []}

    res = _run(respond(
        message="latest numbers?",
        on_event=on_event,
        first_pass_stream=_stream(["Let me give you a rough idea."]),
        enrich=enrich,
    ))
    segs = [e for e in events if e.get("event") == "answer_segment"]
    assert res["segments"] == 2
    assert [s["kind"] for s in segs] == ["initial", "refinement"]
    # the live tool events from enrich were forwarded
    assert any(e.get("type") == "tool" for e in events)
    assert any(e.get("type") == "tool_result" for e in events)
    assert "The latest data" in res["answer"]


def test_empty_first_pass_uses_direct_fallback():
    events, on_event = _collector()

    async def enrich(_on):
        return {"answer": "", "used_tools": False, "artifacts": []}

    async def direct():
        return "Fallback answer."

    res = _run(respond(
        message="hi",
        on_event=on_event,
        first_pass_stream=_stream([]),   # streamed nothing
        enrich=enrich,
        direct_answer=direct,
    ))
    assert res["answer"] == "Fallback answer."
    # tokens were emitted from the fallback
    assert any(e.get("event") == "token" for e in events)


def test_event_ordering_initial_first_complete_last():
    events, on_event = _collector()

    async def enrich(_on):
        return {"answer": "", "used_tools": False, "artifacts": []}

    _run(respond(
        message="hey",
        on_event=on_event,
        first_pass_stream=_stream(["hi"]),
        enrich=enrich,
    ))
    names = [e.get("event") for e in events if e.get("event")]
    assert names[0] == "answer_segment"      # first thing the user sees
    assert names[-1] == "complete"           # terminal
    assert "segment_end" in names
