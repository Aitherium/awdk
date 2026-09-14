"""Provider-agnostic prompt-caching contract (OQ16 lever 1).

Caching is expressed once via ``chat(cache=True)`` and realized per provider:
Anthropic inserts ``cache_control`` breakpoints (explicit); OpenAI/DeepSeek cache
automatically and we only normalize their usage fields. Every provider maps its
own cache accounting onto ``LLMResponse.cache_read_tokens/cache_write_tokens`` so
the tokens-saved meter never branches on backend.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm.anthropic import AnthropicProvider, _apply_prompt_cache
from adk.llm import anthropic as anthropic_mod
from adk.llm.openai_compat import OpenAIProvider, _read_cached_tokens
from adk.llm.base import LLMProvider, Message, ProviderCapabilities


class _FakeResp:
    def __init__(self, data: dict):
        self._data = data

    def json(self) -> dict:
        return self._data


def _capture(captured: dict, data: dict):
    """Return a fake _post_with_retry that records the payload and replies `data`."""
    async def _fake(_client, _url, payload):
        captured["payload"] = payload
        return _FakeResp(data)
    return _fake


# ── Capabilities ────────────────────────────────────────────────────────────

def test_capabilities_declared_per_provider():
    assert AnthropicProvider().capabilities().prompt_cache == "explicit"
    assert OpenAIProvider().capabilities().prompt_cache == "automatic"
    # ABC default is the conservative no-cache profile.
    assert ProviderCapabilities().prompt_cache == "none"
    assert ProviderCapabilities().batch is False


def test_base_default_capabilities_none():
    class _Bare(LLMProvider):
        async def chat(self, *a, **k): ...
        async def list_models(self): return []
    assert _Bare().capabilities().prompt_cache == "none"


# ── Anthropic: explicit breakpoints ──────────────────────────────────────────

def test_apply_prompt_cache_marks_system_and_last_tool():
    payload = {
        "system": "STABLE RULES",
        "tools": [{"name": "a"}, {"name": "b"}],
    }
    _apply_prompt_cache(payload)
    assert payload["system"] == [
        {"type": "text", "text": "STABLE RULES", "cache_control": {"type": "ephemeral"}}
    ]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["tools"][0]


def test_apply_prompt_cache_does_not_mutate_caller_tools():
    shared = [{"name": "a"}, {"name": "b"}]
    payload = {"system": "X", "tools": shared}
    _apply_prompt_cache(payload)
    # Caller's original list/dicts must stay clean (copied before marking).
    assert "cache_control" not in shared[-1]


async def test_anthropic_cache_true_sends_breakpoints_and_reads_usage(monkeypatch):
    captured: dict = {}
    fake_data = {
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 5,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, fake_data))

    prov = AnthropicProvider(api_key="x")
    resp = await prov.chat(
        [Message(role="system", content="RULES"), Message(role="user", content="q")],
        tools=[{"function": {"name": "t", "description": "", "parameters": {}}}],
        cache=True,
    )

    # Request carried the breakpoint…
    assert isinstance(captured["payload"]["system"], list)
    assert captured["payload"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["payload"]["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # …and the response normalized the savings.
    assert resp.cache_read_tokens == 900
    assert resp.cache_write_tokens == 0
    assert resp.cache_status == "hit"


async def test_anthropic_cache_false_omits_breakpoints(monkeypatch):
    captured: dict = {}
    fake_data = {"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(anthropic_mod, "_post_with_retry", _capture(captured, fake_data))

    prov = AnthropicProvider(api_key="x")
    resp = await prov.chat([Message(role="system", content="RULES")], cache=False)

    assert captured["payload"]["system"] == "RULES"  # plain string, no blocks
    assert resp.cache_read_tokens == 0
    assert resp.cache_status == ""


# ── OpenAI / DeepSeek: automatic, just normalize ─────────────────────────────

def test_read_cached_tokens_handles_both_schemas():
    assert _read_cached_tokens({"prompt_tokens_details": {"cached_tokens": 640}}) == 640
    assert _read_cached_tokens({"prompt_cache_hit_tokens": 512}) == 512   # DeepSeek
    assert _read_cached_tokens({"prompt_tokens": 100}) == 0               # no cache info


async def test_openai_normalizes_cached_tokens(monkeypatch):
    captured: dict = {}
    fake_data = {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "total_tokens": 50,
            "prompt_tokens": 40,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 32},
        },
    }
    prov = OpenAIProvider(api_key="x")
    monkeypatch.setattr(prov, "_post_with_retry", _capture(captured, fake_data))

    resp = await prov.chat([Message(role="system", content="RULES")], cache=True)

    # No request-side cache flag for this family…
    assert "cache_control" not in str(captured["payload"])
    # …but the automatic cache savings are surfaced uniformly.
    assert resp.cache_read_tokens == 32
    assert resp.cache_status == "hit"
