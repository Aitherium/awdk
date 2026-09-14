"""Tests for the canonical LLM intent router (adk.intent).

Pure-logic tests with a mock ``llm_complete`` — no network. Validates the one
shared classifier that Genesis, awkit and ADK agents all route through.
"""

import asyncio

from adk.intent import (
    IntentDecision,
    classify_intent,
    keyword_intent,
    depth_for_effort,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_depth_for_effort_mapping():
    assert depth_for_effort(1) == "skip"
    assert depth_for_effort(2) == "gate"
    assert depth_for_effort(5) == "gate"
    assert depth_for_effort(6) == "light"
    assert depth_for_effort(7) == "sase"
    assert depth_for_effort(10) == "sase"


def test_keyword_greeting_is_trivial_conversation():
    d = keyword_intent("hey, how are you?")
    assert d.intent == "conversation"
    assert d.effort == 1
    assert d.agentic is False
    assert d.reasoning_depth == "skip"
    assert d.source == "keyword"


def test_keyword_toolish_is_agentic():
    d = keyword_intent("search the web and email me the summary")
    assert d.agentic is True
    assert d.effort >= 4


def test_keyword_plain_question_not_agentic():
    d = keyword_intent("what is the capital of France")
    assert d.intent == "question"
    assert d.agentic is False


def test_classify_intent_parses_llm_json():
    async def llm(_messages):
        return '{"intent":"conversation","effort":1,"agentic":false,"reasoning_depth":"skip"}'

    d = _run(classify_intent("hi there", llm_complete=llm))
    assert isinstance(d, IntentDecision)
    assert d.intent == "conversation"
    assert d.effort == 1
    assert d.agentic is False
    assert d.source == "llm"


def test_classify_intent_extracts_json_from_noise():
    # Thinking models / code fences must not break parsing.
    async def llm(_messages):
        return 'Sure!\n```json\n{"intent":"question","effort":3,"agentic":false,"reasoning_depth":"gate"}\n```'

    d = _run(classify_intent("what is 2+2", llm_complete=llm))
    assert d.intent == "question"
    assert d.effort == 3
    assert d.source == "llm"


def test_classify_intent_clamps_effort_and_validates_intent():
    async def llm(_messages):
        return '{"intent":"nonsense","effort":99,"agentic":true,"reasoning_depth":"???"}'

    d = _run(classify_intent("do a thing", llm_complete=llm))
    assert d.intent == "question"          # invalid intent → safe default
    assert 1 <= d.effort <= 10             # clamped
    assert d.agentic is True
    assert d.reasoning_depth in ("skip", "gate", "light", "sase")  # repaired


def test_classify_intent_falls_back_when_llm_raises():
    async def llm(_messages):
        raise RuntimeError("LLM down")

    d = _run(classify_intent("hello!", llm_complete=llm))
    assert d.source == "keyword"           # graceful fallback, never raises
    assert d.intent == "conversation"


def test_classify_intent_falls_back_on_non_json():
    async def llm(_messages):
        return "I cannot help with that."

    d = _run(classify_intent("hmm", llm_complete=llm))
    assert d.source == "keyword"


def test_classify_intent_empty_message():
    async def llm(_messages):
        raise AssertionError("should not be called for empty message")

    d = _run(classify_intent("   ", llm_complete=llm))
    assert d.intent == "conversation"
    assert d.effort == 1
