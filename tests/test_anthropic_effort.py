"""awdk effort (1-10) -> Anthropic thinking / output_config.effort (plan B3).

Until 2026-11-14 the Anthropic provider never sent a thinking or effort setting,
so an effort-9 turn reasoned exactly like an effort-2 turn, and the fixed
temperature 0.7 went to models that reject sampling parameters. These tests pin
the mapping on the wire (HTTP is mocked; no key needed), the router handing
effort only to providers that map it, and the agent skipping its routing LLM call
when the caller already chose an effort (plan B8).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm import anthropic as anthropic_mod
from adk.llm.anthropic import (
    THINKING_MIN_MAX_TOKENS,
    AnthropicProvider,
    model_caps,
    reasoning_settings,
)
from adk.llm.base import LLMResponse, Message


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data

    def json(self) -> dict:
        return self._data


_OK = {"model": "m", "content": [{"type": "text", "text": "ok"}],
       "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}}


def _capture(captured: dict, data: dict = _OK):
    async def _fake(client, url, payload):
        captured["payload"] = payload
        captured["timeout"] = client.timeout.read
        return _FakeResp(data)
    return _fake


async def _sent(monkeypatch, model: str, effort, **kw) -> dict:
    cap: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(cap))
    p = AnthropicProvider(api_key="k")
    await p.chat([Message(role="user", content="hi")], model=model,
                 **({"effort": effort} if effort is not None else {}), **kw)
    return cap


# ── the mapping table ────────────────────────────────────────────────────────

@pytest.mark.parametrize("effort,want", [
    (1, {"effort": "low", "thinking": None}),
    (3, {"effort": "low", "thinking": None}),
    (4, {"effort": None, "thinking": None}),
    (6, {"effort": None, "thinking": None}),
    (7, {"effort": "high", "thinking": {"type": "adaptive"}}),
    (8, {"effort": "high", "thinking": {"type": "adaptive"}}),
    (9, {"effort": "xhigh", "thinking": {"type": "adaptive"}}),
    (10, {"effort": "max", "thinking": {"type": "adaptive"}}),
    (None, {"effort": None, "thinking": None}),
])
def test_effort_maps_to_thinking_and_effort_on_opus_5_5(effort, want):
    assert reasoning_settings("claude-opus-5-5", effort) == want


def test_a_model_without_xhigh_gets_high():
    assert reasoning_settings("claude-opus-4-6", 9) == {
        "effort": "high", "thinking": {"type": "adaptive"}}


def test_a_budget_model_gets_a_budget_and_no_effort_field():
    rs = reasoning_settings("claude-haiku-4-5-20251001", 10)
    assert rs["effort"] is None
    assert rs["thinking"]["type"] == "enabled" and rs["thinking"]["budget_tokens"] >= 1024
    assert reasoning_settings("claude-haiku-4-5", 2) == {"effort": None, "thinking": None}


@pytest.mark.parametrize("model,sampling,forced", [
    ("claude-opus-5-5", False, False),
    ("claude-sonnet-5-5", False, False),
    ("claude-haiku-5-5", False, True),
    ("claude-fable-5-2", False, False),
    ("claude-opus-4-8", False, True),
    ("claude-opus-4-6", True, True),
    ("claude-sonnet-4-6", True, True),
    ("claude-haiku-4-5-20251001", True, True),
    ("anthropic.claude-opus-5-5", False, False),
    ("some-local-model", True, True),
])
def test_model_caps(model, sampling, forced):
    caps = model_caps(model)
    assert caps["sampling"] is sampling
    assert caps["forced_tools"] is forced


# ── on the wire ──────────────────────────────────────────────────────────────

async def test_low_effort_sends_low_and_no_thinking(monkeypatch):
    cap = await _sent(monkeypatch, "claude-opus-5-5", 2)
    pl = cap["payload"]
    assert pl["output_config"] == {"effort": "low"}
    assert "thinking" not in pl
    assert "temperature" not in pl, "Opus 5.5 rejects sampling parameters"


async def test_mid_effort_sends_the_model_default(monkeypatch):
    pl = (await _sent(monkeypatch, "claude-sonnet-5-5", 5))["payload"]
    assert "output_config" not in pl and "thinking" not in pl


async def test_high_effort_thinks_with_room_and_time(monkeypatch):
    cap = await _sent(monkeypatch, "claude-opus-5-5", 9, max_tokens=4096)
    pl = cap["payload"]
    assert pl["thinking"] == {"type": "adaptive"}
    assert pl["output_config"] == {"effort": "xhigh"}
    assert pl["max_tokens"] >= THINKING_MIN_MAX_TOKENS
    assert "temperature" not in pl and "top_p" not in pl
    assert cap["timeout"] >= anthropic_mod.THINKING_TIMEOUT_S


async def test_thinking_drops_temperature_even_where_sampling_is_allowed(monkeypatch):
    pl = (await _sent(monkeypatch, "claude-opus-4-6", 8, top_p=0.9))["payload"]
    assert pl["thinking"] == {"type": "adaptive"}
    assert "temperature" not in pl and "top_p" not in pl


async def test_a_legacy_model_without_effort_is_unchanged(monkeypatch):
    pl = (await _sent(monkeypatch, "claude-sonnet-4-6", None))["payload"]
    assert pl["temperature"] == 0.7
    assert "thinking" not in pl and "output_config" not in pl


async def test_required_tool_choice_becomes_auto_where_forced_is_a_400(monkeypatch):
    tools = [{"name": "f", "description": "", "parameters": {"type": "object"}}]
    pl = (await _sent(monkeypatch, "claude-opus-5-5", None, tools=tools,
                      tool_choice="required"))["payload"]
    assert pl["tool_choice"] == {"type": "auto"}
    pl = (await _sent(monkeypatch, "claude-sonnet-4-6", None, tools=tools,
                      tool_choice="required"))["payload"]
    assert pl["tool_choice"] == {"type": "any"}


async def test_thinking_blocks_ride_back_verbatim(monkeypatch):
    data = {"model": "claude-opus-5-5", "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                        {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]}
    cap: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(cap, data))
    resp = await AnthropicProvider(api_key="k").chat(
        [Message(role="user", content="hi")], model="claude-opus-5-5", effort=8)
    assert [b["type"] for b in resp.raw_content_blocks] == ["thinking", "tool_use"]
    assert resp.tool_calls and resp.tool_calls[0].name == "f"


async def test_a_plain_reply_keeps_no_raw_blocks(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(cap))
    resp = await AnthropicProvider(api_key="k").chat(
        [Message(role="user", content="hi")], model="claude-opus-5-5")
    assert resp.raw_content_blocks == []


# ── the router hands effort only to providers that map it ───────────────────

def test_router_effort_kw_is_opt_in():
    from adk.llm import LLMRouter

    class Plain:
        pass

    assert LLMRouter._effort_kw(AnthropicProvider(api_key="k"), 9) == {"effort": 9}
    assert LLMRouter._effort_kw(AnthropicProvider(api_key="k"), None) == {}
    assert LLMRouter._effort_kw(Plain(), 9) == {}, "other providers forward unknown kwargs"


def test_current_anthropic_tier_ids():
    from adk.llm import _EFFORT_MODELS
    assert _EFFORT_MODELS["anthropic"] == {
        "small": "claude-haiku-5-5", "medium": "claude-sonnet-5-5", "large": "claude-opus-5-5"}


# ── B8: a caller-set effort skips the routing LLM call ──────────────────────

def _agent(tmp_path):
    from adk.agent import AitherAgent
    from adk.memory import Memory

    router = MagicMock()
    router.provider_name = "mock"
    router.chat = AsyncMock(return_value=LLMResponse(
        content='{"intent":"analysis","effort":4,"agentic":false,"reasoning_depth":"gate"}',
        model="m"))
    agent = AitherAgent("t", llm=router,
                        memory=Memory(db_path=tmp_path / "t.db", agent_name="t"))
    return agent, router


async def test_classify_intent_with_effort_makes_no_llm_call(tmp_path):
    agent, router = _agent(tmp_path)
    d = await agent.classify_intent("compare these two designs", effort=8)
    assert router.chat.await_count == 0
    assert d.effort == 8 and d.reasoning_depth == "sase" and d.source == "caller"


async def test_classify_intent_without_effort_still_asks_the_llm(tmp_path):
    agent, router = _agent(tmp_path)
    d = await agent.classify_intent("compare these two designs")
    assert router.chat.await_count == 1
    assert d.source == "llm" and d.effort == 4
