"""Tests for the Anthropic advisor tool (beta ``advisor-tool-2026-03-01``).

Covers the provider wiring (tool injection, beta header, response parsing, usage
split, native message round-trip) and the config/helper building blocks. The
executor stays a cheap model (e.g. Haiku); the advisor is Opus — these tests
mock the HTTP layer, so no live key is needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm import anthropic as anthropic_mod
from adk.llm.anthropic import AnthropicProvider
from adk.llm.advisor import (
    ADVISOR_BETA,
    AdvisorConfig,
    advisor_brevity_line,
    steering_system_block,
    strip_advisor_blocks,
    validate_pair,
)
from adk.llm.base import Message


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data

    def json(self) -> dict:
        return self._data


def _capture(captured: dict, data: dict):
    """Fake ``_post_with_retry`` that records the client + payload and replies."""
    async def _fake(client, url, payload):
        captured["client"] = client
        captured["url"] = url
        captured["payload"] = payload
        return _FakeResp(data)
    return _fake


_OK = {
    "model": "claude-haiku-4-5-20251001",
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 4},
}


# ── Config + helpers ─────────────────────────────────────────────────────────

def test_advisor_config_coerce():
    assert AdvisorConfig.coerce(None) is None
    inst = AdvisorConfig(enabled=True)
    assert AdvisorConfig.coerce(inst) is inst
    fromdict = AdvisorConfig.coerce({"enabled": True, "max_uses": 3})
    assert fromdict.enabled and fromdict.max_uses == 3
    # Unknown keys are ignored, garbage degrades to None (never raises).
    assert AdvisorConfig.coerce({"enabled": True, "bogus": 1}).enabled
    assert AdvisorConfig.coerce(42) is None


def test_advisor_tool_dict_shape():
    cfg = AdvisorConfig(enabled=True, advisor_model="claude-opus-4-8",
                        max_uses=4, max_tokens=2048, caching_ttl="5m")
    t = cfg.tool_dict()
    assert t["type"] == "advisor_20260301"
    assert t["name"] == "advisor"
    assert t["model"] == "claude-opus-4-8"
    assert t["max_uses"] == 4
    assert t["max_tokens"] == 2048
    assert t["caching"] == {"type": "ephemeral", "ttl": "5m"}
    # Carries the internal no-cache marker (stripped before send).
    assert t["__no_cache_control"] is True


def test_advisor_tool_dict_omits_unset_optionals():
    t = AdvisorConfig(enabled=True).tool_dict()
    assert "max_uses" not in t
    assert "caching" not in t
    assert t["max_tokens"] == 2048  # default present


def test_validate_pair():
    # Valid: Haiku executor + Opus 4.8 advisor.
    assert validate_pair("claude-haiku-4-5-20251001", "claude-opus-4-8") is None
    assert validate_pair("claude-sonnet-4-6", "claude-opus-4-7") is None
    # Invalid advisor (Opus 4.6 is executor-only, not an advisor).
    assert validate_pair("claude-haiku-4-5", "claude-opus-4-6") is not None
    # Invalid executor.
    assert validate_pair("gpt-4o", "claude-opus-4-8") is not None


def test_strip_advisor_blocks_keeps_tool_use():
    blocks = [
        {"type": "text", "text": "hi"},
        {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "srv_1",
         "content": {"type": "advisor_result", "text": "plan"}},
        {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
    ]
    kept = strip_advisor_blocks(blocks)
    types = [b["type"] for b in kept]
    assert types == ["text", "tool_use"]   # advisor pair dropped, tool_use kept
    assert strip_advisor_blocks(None) is None
    assert strip_advisor_blocks([]) is None


def test_steering_fragments():
    cfg = AdvisorConfig(enabled=True, brevity_words=80)
    block = steering_system_block(cfg)
    assert "advisor" in block.lower()
    assert "under 80 words" in advisor_brevity_line(cfg.brevity_words)


# ── Tool injection + beta header ─────────────────────────────────────────────

async def test_advisor_injects_tool_and_beta_header(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, _OK))
    prov = AnthropicProvider(api_key="x", default_model="claude-haiku-4-5-20251001")

    await prov.chat(
        [Message(role="user", content="tune the device")],
        advisor=AdvisorConfig(enabled=True, max_uses=4, max_tokens=2048),
    )

    tools = captured["payload"]["tools"]
    advisor_tools = [t for t in tools if t.get("type") == "advisor_20260301"]
    assert len(advisor_tools) == 1
    assert advisor_tools[0]["model"] == "claude-opus-4-8"
    assert advisor_tools[0]["max_uses"] == 4
    # The internal marker must NOT reach the wire.
    assert "__no_cache_control" not in advisor_tools[0]
    # Beta header present when enabled.
    assert captured["client"].headers.get("anthropic-beta") == ADVISOR_BETA


async def test_advisor_rides_alongside_function_tools(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, _OK))
    prov = AnthropicProvider(api_key="x", default_model="claude-haiku-4-5-20251001")

    await prov.chat(
        [Message(role="user", content="q")],
        tools=[{"function": {"name": "search", "description": "", "parameters": {}}}],
        advisor=AdvisorConfig(enabled=True),
        cache=True,
    )
    tools = captured["payload"]["tools"]
    names_types = [(t.get("name"), t.get("type")) for t in tools]
    assert ("search", None) in names_types
    assert any(t.get("type") == "advisor_20260301" for t in tools)
    # The prompt-cache breakpoint marks the function tool, NOT the advisor tool.
    advisor_tool = [t for t in tools if t.get("type") == "advisor_20260301"][0]
    func_tool = [t for t in tools if t.get("name") == "search"][0]
    assert "cache_control" not in advisor_tool
    assert func_tool.get("cache_control") == {"type": "ephemeral"}


async def test_advisor_off_is_byte_identical(monkeypatch):
    """Disabled advisor → no tool, no beta header, identical payload to no-kwarg."""
    cap_off: dict = {}
    cap_none: dict = {}
    prov = AnthropicProvider(api_key="x", default_model="claude-haiku-4-5-20251001")
    msgs = [Message(role="system", content="RULES"), Message(role="user", content="hi")]

    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(cap_off, _OK))
    await prov.chat(list(msgs), advisor=AdvisorConfig(enabled=False))

    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(cap_none, _OK))
    await prov.chat(list(msgs))

    assert cap_off["payload"] == cap_none["payload"]
    assert "tools" not in cap_off["payload"]
    assert cap_off["client"].headers.get("anthropic-beta") is None


# ── Response parsing: result variants + usage split ──────────────────────────

async def test_advisor_result_parsed(monkeypatch):
    data = {
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv_1",
             "content": {"type": "advisor_result", "text": "Use a channel pattern.",
                         "stop_reason": "end_turn"}},
            {"type": "text", "text": "Here is the implementation."},
        ],
        "usage": {
            "input_tokens": 50, "output_tokens": 20,
            "iterations": [
                {"type": "message", "input_tokens": 50, "output_tokens": 8},
                {"type": "advisor_message", "model": "claude-opus-4-8",
                 "input_tokens": 800, "output_tokens": 1600},
                {"type": "message", "input_tokens": 1300, "output_tokens": 12},
            ],
        },
    }
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, data))
    prov = AnthropicProvider(api_key="x", default_model="claude-haiku-4-5-20251001")

    resp = await prov.chat([Message(role="user", content="build worker pool")],
                           advisor=AdvisorConfig(enabled=True))

    assert resp.advisor_calls == 1
    assert resp.advisor_text == "Use a channel pattern."
    assert resp.advisor_stop_reason == "end_turn"
    assert resp.advisor_error == ""
    # Advisor (Opus) tokens summed apart from the executor's.
    assert resp.advisor_input_tokens == 800
    assert resp.advisor_output_tokens == 1600
    # Executor billing excludes advisor tokens.
    assert resp.tokens_used == 70  # 50 + 20 top-level (executor only)
    assert resp.content == "Here is the implementation."
    # Raw blocks preserved for round-trip.
    assert any(b["type"] == "advisor_tool_result" for b in resp.raw_content_blocks)


async def test_advisor_redacted_result(monkeypatch):
    data = {
        "model": "m",
        "content": [
            {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv_1",
             "content": {"type": "advisor_redacted_result",
                         "encrypted_content": "OPAQUE", "stop_reason": "end_turn"}},
            {"type": "text", "text": "ok"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, data))
    prov = AnthropicProvider(api_key="x")
    resp = await prov.chat([Message(role="user", content="q")],
                           advisor=AdvisorConfig(enabled=True))
    assert resp.advisor_calls == 1
    assert resp.advisor_text == ""           # encrypted — not readable
    assert resp.advisor_stop_reason == "end_turn"


async def test_advisor_error_result(monkeypatch):
    data = {
        "model": "m",
        "content": [
            {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
            {"type": "advisor_tool_result", "tool_use_id": "srv_1",
             "content": {"type": "advisor_tool_result_error",
                         "error_code": "max_uses_exceeded"}},
            {"type": "text", "text": "continuing without advice"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, data))
    prov = AnthropicProvider(api_key="x")
    resp = await prov.chat([Message(role="user", content="q")],
                           advisor=AdvisorConfig(enabled=True))
    assert resp.advisor_error == "max_uses_exceeded"
    assert resp.content == "continuing without advice"  # request itself didn't fail


# ── Native message conversion (the round-trip fix) ───────────────────────────

def test_convert_messages_plain_unchanged():
    prov = AnthropicProvider()
    system, conv = prov._convert_messages([
        Message(role="system", content="SYS"),
        Message(role="user", content="hi"),
    ])
    assert system == "SYS"
    assert conv == [{"role": "user", "content": "hi"}]


def test_convert_messages_tool_result_native():
    prov = AnthropicProvider()
    _, conv = prov._convert_messages([
        Message(role="tool", content="42", tool_call_id="tu_1"),
    ])
    # role:"tool" → user + native tool_result (the old string flattening 400'd).
    assert conv[0]["role"] == "user"
    assert conv[0]["content"][0] == {
        "type": "tool_result", "tool_use_id": "tu_1", "content": "42",
    }


def test_convert_messages_assistant_tool_calls_native():
    prov = AnthropicProvider()
    _, conv = prov._convert_messages([
        Message(role="assistant", content="let me search",
                tool_calls=[{"id": "tu_1", "type": "function",
                             "function": {"name": "search", "arguments": '{"q": "x"}'}}]),
    ])
    blocks = conv[0]["content"]
    assert blocks[0] == {"type": "text", "text": "let me search"}
    assert blocks[1] == {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "x"}}


def test_convert_messages_content_blocks_passthrough():
    prov = AnthropicProvider()
    native = [
        {"type": "text", "text": "plan applied"},
        {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "srv_1",
         "content": {"type": "advisor_result", "text": "advice"}},
    ]
    _, conv = prov._convert_messages([
        Message(role="assistant", content="ignored-when-blocks-present",
                content_blocks=native),
    ])
    assert conv[0]["content"] is native  # verbatim round-trip


async def test_advisor_blocks_roundtrip_into_payload(monkeypatch):
    """An assistant turn carrying advisor content_blocks is sent back verbatim."""
    captured: dict = {}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, _OK))
    prov = AnthropicProvider(api_key="x")
    native = [
        {"type": "server_tool_use", "name": "advisor", "id": "srv_1", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "srv_1",
         "content": {"type": "advisor_result", "text": "advice"}},
        {"type": "tool_use", "id": "tu_1", "name": "search", "input": {}},
    ]
    await prov.chat(
        [
            Message(role="user", content="go"),
            Message(role="assistant", content="", content_blocks=native),
            Message(role="tool", content="result", tool_call_id="tu_1"),
        ],
        advisor=AdvisorConfig(enabled=True),
    )
    sent = captured["payload"]["messages"]
    # The advisor blocks survived into the request unchanged…
    assert sent[1]["content"] is native
    # …and the tool result paired natively against the tool_use id.
    assert sent[2]["content"][0]["tool_use_id"] == "tu_1"


# ── Agent-level wiring (steering + threading + usage surfacing) ──────────────

async def test_agent_threads_advisor_and_surfaces_usage(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from adk.agent import AitherAgent
    from adk.llm.base import LLMResponse
    from adk.memory import Memory

    router = MagicMock()
    router.provider_name = "mock"
    router.chat = AsyncMock(return_value=LLMResponse(
        content="device tuned", model="claude-haiku-4-5-20251001", tokens_used=14,
        advisor_calls=1, advisor_input_tokens=800, advisor_output_tokens=1600,
    ))
    agent = AitherAgent(
        "test", llm=router,
        memory=Memory(db_path=tmp_path / "t.db", agent_name="test"),
    )

    resp = await agent.chat(
        "tune the on-vehicle device",
        advisor=AdvisorConfig(enabled=True, brevity_words=80),
    )

    # Advisor config threaded through to the LLM call.
    adv = router.chat.call_args.kwargs.get("advisor")
    assert isinstance(adv, AdvisorConfig) and adv.enabled

    # Steering injected: a system turn mentions the advisor; the user turn carries
    # the brevity line (these reach the LLM only, not memory).
    sent_msgs = router.chat.call_args.args[0]
    assert any(m.role == "system" and "advisor" in (m.content or "").lower()
               for m in sent_msgs)
    assert any(m.role == "user" and "under 80 words" in (m.content or "")
               for m in sent_msgs)

    # Advisor (Opus) usage surfaced on the AgentResponse for downstream metering.
    assert resp.advisor_calls == 1
    assert resp.advisor_input_tokens == 800
    assert resp.advisor_output_tokens == 1600
