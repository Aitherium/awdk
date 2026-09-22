"""The orchestrator contract (spec section 2): strict JSON, one retry, hard failure."""
from __future__ import annotations

import json

import pytest
from adk.orchestrate import (
    OrchestratorContractError,
    extract_json,
    render_plan,
    route_task,
    validate,
)

MODELS = {"bonsai2-27b", "deepseek-v4-flash", "aither-orchestrator-8b"}

# Verbatim from aither-orchestrator-8b, 2026-09-22: right shape, wrong vocabulary.
LIVE = ('{"intent":"code_edit","effort":2,"route":{"lane":"reasoning","model_hint":"identify the '
        'source of the KeyError in BaseState.__new__ and modify the code","fallback":[]},'
        '"plan":[{"step":1,"lane":"code_edit","goal":"Modify BaseState.__new__ to handle '
        'computed variables and prevent KeyError"}]}')


def scripted(*answers):
    seen: list[list[dict]] = []

    async def call(messages):
        seen.append(messages)
        return answers[len(seen) - 1]

    return call, seen


def test_the_live_answer_validates_after_normalization():
    d, fatal = validate(json.loads(LIVE), models=MODELS)
    assert not fatal and d is not None
    assert d.intent == "code_edit" and d.effort == 2 and d.route["lane"] == "reasoning"
    assert d.route["model_hint"] == ""  # a sentence is not a model id
    assert d.plan == [{"step": 1, "lane": "reasoning", "goal":
                       "Modify BaseState.__new__ to handle computed variables and prevent "
                       "KeyError"}]
    assert any("model_hint" in n for n in d.normalized)
    assert any("'code_edit' -> 'reasoning'" in n for n in d.normalized)


def test_extract_json_tolerates_fences_and_prose_but_not_nothing():
    assert extract_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("I would route this to the reasoning lane.")


@pytest.mark.parametrize("obj,why", [
    ({"intent": "banter", "effort": 1, "plan": [{"goal": "x"}]}, "intent"),
    ({"intent": "qa", "effort": "high", "plan": [{"goal": "x"}]}, "effort"),
    ({"intent": "qa", "effort": 1, "plan": []}, "plan"),
    ({"intent": "qa", "effort": 1, "plan": [{"lane": "chat"}]}, "no usable step"),
    (["not", "an", "object"], "not a JSON object"),
])
def test_fatal_problems_are_named(obj, why):
    d, fatal = validate(obj, models=MODELS)
    assert d is None and any(why in p for p in fatal), fatal


def test_effort_is_clamped_and_unknown_fallbacks_dropped():
    d, _ = validate({"intent": "planning", "effort": 7,
                     "route": {"lane": "deep", "model_hint": "bonsai2-27b",
                               "fallback": ["deepseek-v4-flash", "gpt-9", "cloud:deepseek-v4-pro"]},
                     "plan": [{"goal": "plan it"}]}, models=MODELS)
    assert d.effort == 3 and d.route["lane"] == "reasoning"
    assert d.route["model_hint"] == "bonsai2-27b"
    assert d.route["fallback"] == ["deepseek-v4-flash", "cloud:deepseek-v4-pro"]
    assert d.escalation == {"stuck_threshold": 8, "next_effort": 3}


@pytest.mark.asyncio
async def test_first_answer_valid_is_one_call():
    call, seen = scripted(LIVE)
    d = await route_task(call, "fix it", models=MODELS)
    assert d.attempts == 1 and len(seen) == 1
    assert seen[0][0]["role"] == "system" and "ONLY one JSON object" in seen[0][0]["content"]


@pytest.mark.asyncio
async def test_invalid_then_valid_retries_once_with_the_schema_and_the_problem():
    call, seen = scripted("I'd send this to reasoning.", LIVE)
    d = await route_task(call, "fix it", models=MODELS)
    assert d.attempts == 2 and len(seen) == 2
    retry = seen[1][-1]["content"]
    assert "broke the contract" in retry and "not JSON" in retry and "ONLY one JSON object" in retry


@pytest.mark.asyncio
async def test_two_failures_are_a_hard_error_with_telemetry_never_a_fallback():
    call, seen = scripted("prose", '{"intent": "nope", "effort": 1, "plan": [{"goal": "g"}]}')
    with pytest.raises(OrchestratorContractError) as ei:
        await route_task(call, "fix it", models=MODELS)
    assert len(seen) == 2
    tel = ei.value.telemetry
    assert tel["attempts"] == 2 and any("intent" in p for p in tel["problems"])
    assert len(tel["answers"]) == 2


def test_render_plan_names_intent_effort_lane_and_steps():
    d, _ = validate(json.loads(LIVE), models=MODELS)
    text = render_plan(d)
    assert text.startswith("## Orchestrator plan (intent code_edit, effort E2, lane reasoning)")
    assert "1. [reasoning] Modify BaseState.__new__" in text


def test_a_reasoning_scratchpad_is_stripped_before_parsing():
    """Measured 2026-09-22: aither-orchestrator-8b opens with <think>...</think>, which made
    a well-formed answer read as 'not JSON'."""
    stripped = extract_json('<think>Let me consider the lanes.</think>\n' + LIVE)
    assert stripped["intent"] == "code_edit"
    # An UNCLOSED block (the token cap cut it) leaves nothing to parse -- and says so.
    with pytest.raises(ValueError):
        extract_json('<think>cut off mid-thought, no JSON ever arrived')
