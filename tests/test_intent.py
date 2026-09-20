"""Tests for the canonical LLM intent router (adk.intent).

Pure-logic tests with a mock ``llm_complete`` — no network. Validates the one
shared classifier that Genesis, awkit and ADK agents all route through.
"""

import asyncio

import pytest

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


# ── awrise (wakes) — the keyword fallback must classify these agentic even
# with the LLM router down, per the six-pillars phrase table. Command-shaped
# (mutate) utterances land in the higher-effort tool bucket (3-6); read-shaped
# (status/explain/history) utterances land in the low-effort bucket (1-2) but
# are STILL agentic — "list my wakes" needs a tool call just as much as "add
# a wake" does, only cheaper. A few utterances that carry no wake keyword and
# no recognizable verb at all (e.g. "is nightly-backup enabled" style names
# with no domain noun) are a known, documented limit of a cheap regex
# fallback — those are covered where they DO carry a recognizable signal.

@pytest.mark.parametrize("utterance", [
    "add a wake called nightly-backup that runs backup.sh every day at 2am",
    "change nightly-backup to run every 6 hours instead",
    "set nightly-backup's description to 'DR export'",
    "change nightly-backup to instead run drain-and-sync.sh nightly",
    "enable nightly-backup",
    "turn off the nightly-backup wake",
    "delete the nightly-backup wake",
    "run nightly-backup right now",
    "run the fleet gates every 4 hours",
    "disable the backup job",
    "pause job1",
])
def test_keyword_wake_commands_are_agentic_mutate_tier(utterance):
    d = keyword_intent(utterance)
    assert d.agentic is True, utterance
    assert d.intent == "command", utterance
    assert 3 <= d.effort <= 6, (utterance, d.effort)
    assert d.requires_grounding is True, utterance
    assert d.source == "keyword"


@pytest.mark.parametrize("utterance", [
    "what wakes do I have",
    "list my wakes",
    "is nightly-backup enabled",
    "explain what nightly-backup does",
    "show me the run history for nightly-backup",
    "has nightly-backup ever failed",
    "what fired overnight",
])
def test_keyword_wake_reads_are_agentic_low_effort(utterance):
    d = keyword_intent(utterance)
    assert d.agentic is True, utterance
    assert 1 <= d.effort <= 6, (utterance, d.effort)
    assert d.requires_grounding is True, utterance
    assert d.source == "keyword"


def test_keyword_wake_noun_alone_grounds_as_your_wakes():
    d = keyword_intent("what wakes do I have")
    assert d.grounding_label == "your wakes"


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
