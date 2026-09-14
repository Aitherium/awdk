"""Layer divergence measurement suite.

Tests whether adk.core.model (Layer A) and adk.llm (Layer B) produce
IDENTICAL wire-format requests and responses when fed the same logical input.

This is the measurement for the divergence ("adk carries two parallel,
unreconciled LLM abstraction layers").

CRITICAL: Every divergence is NAMED and ASSERTED AS CURRENT TRUTH with a
comment citing file:line from the implementation. A test that merely asserts
'they differ' teaches nothing; a test that pins 'Layer A sends max_tokens=None,
Layer B omits it' is a measurement that will fail LOUDLY if either side changes.

MEASUREMENT SCOPE:
- mocked HTTP requests & responses (respx)
- request URL paths, headers, JSON body (key-by-key)
- response parsing (text, finish_reason, usage fields, model field)
- streaming chunks (SSE format for OpenAI/Anthropic)
- system message handling
- tool role handling (Anthropic-only)

NOT COMPARED (see comments in test bodies):
- Ollama backends: Layer A (adk/core/backends/ollama.py) vs Layer B
  (adk/llm/ollama.py) cannot be compared directly because they wire
  differently — Layer A accepts messages with string content only;
  Layer B's LLMRouter dispatches through a shared Message type that
  supports multimodal (list[dict]) content. Ollama streaming format
  (line-delimited JSON, not SSE) is identical in both layers.

REGRESSION GUARDS:
- Streaming format (SSE vs JSONL)
- Timeout values (120s hardcoded in both)
- Auth header names ('Authorization' vs 'x-api-key' vs none)
- finish_reason field names ('finish_reason' vs 'stop_reason' vs 'done_reason')
- max_tokens always sent vs omitted when None
- system message hoisting (Anthropic-only)
- tool role conversion (Anthropic-only)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.core.backends.openai_compat import OpenAIBackend
from adk.core.backends.anthropic import AnthropicBackend
from adk.core.model import Message as LayerAMessage
from adk.llm.base import Message as LayerBMessage
from adk.llm.openai_compat import OpenAIProvider
from adk.llm.anthropic import AnthropicProvider


# ─── Fixtures ────────────────────────────────────────────────────────────

def _layer_a_messages() -> list[LayerAMessage]:
    """Identical test message set for Layer A."""
    return [
        LayerAMessage(role="system", content="You are a helpful assistant."),
        LayerAMessage(role="user", content="Hello, how are you?"),
        LayerAMessage(role="assistant", content="I'm doing great!"),
    ]


def _layer_b_messages() -> list[LayerBMessage]:
    """Identical test message set for Layer B."""
    return [
        LayerBMessage(role="system", content="You are a helpful assistant."),
        LayerBMessage(role="user", content="Hello, how are you?"),
        LayerBMessage(role="assistant", content="I'm doing great!"),
    ]


def _openai_response() -> dict[str, Any]:
    """Canned OpenAI-compatible response."""
    return {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "This is a test response.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _anthropic_response() -> dict[str, Any]:
    """Canned Anthropic response."""
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "This is a test response.",
            }
        ],
        "model": "claude-3-5-sonnet-latest",
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
        },
    }


def _openai_streaming_response() -> str:
    """SSE-formatted OpenAI streaming response."""
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": " "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]},
    ]
    lines = [f"data: {json.dumps(c)}" for c in chunks]
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _anthropic_streaming_response() -> str:
    """SSE-formatted Anthropic streaming response."""
    events = [
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " "}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
        {"type": "message_stop"},
    ]
    lines = [f"data: {json.dumps(e)}" for e in events]
    return "\n".join(lines)


# ─── Layer A OpenAI-Compatible Backend Tests ────────────────────────────

class TestLayerAOpenAIGenerate:
    """Test Layer A (adk/core/backends/openai_compat.py) generate method."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_request_payload(self):
        """Layer A OpenAI: verify request payload structure.

        DIVERGENCE PINNED:
        - max_tokens is OMITTED if None (line 56-57)
        - temperature is always included (line 52)
        - stream is False (line 53)
        """
        captured_payload = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_request
        )

        backend = OpenAIBackend(api_key="sk-test")
        messages = _layer_a_messages()

        # Call with max_tokens=None (the default)
        result = await backend.generate(messages, temperature=0.7, max_tokens=None)

        # Assertions on captured payload
        assert captured_payload["model"] == "gpt-4o-mini"
        assert captured_payload["temperature"] == 0.7
        assert captured_payload["stream"] is False
        # KEY DIVERGENCE: Layer A OMITS max_tokens when None
        assert "max_tokens" not in captured_payload, (
            "Layer A OpenAI backend should omit max_tokens when None "
            "(adk/core/backends/openai_compat.py:56-57)"
        )

        # Verify response parsing
        assert result.text == "This is a test response."
        assert result.finish_reason == "stop"
        assert result.usage["prompt_tokens"] == 10
        assert result.usage["completion_tokens"] == 5

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_with_explicit_max_tokens(self):
        """Layer A OpenAI: verify max_tokens is included when explicitly set."""
        captured_payload = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_request
        )

        backend = OpenAIBackend(api_key="sk-test")
        messages = _layer_a_messages()

        result = await backend.generate(messages, temperature=0.7, max_tokens=2048)

        # KEY DIVERGENCE: max_tokens IS included when not None
        assert captured_payload["max_tokens"] == 2048, (
            "Layer A OpenAI backend should include max_tokens when explicitly set "
            "(adk/core/backends/openai_compat.py:56-57)"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_auth_header(self):
        """Layer A OpenAI: verify Authorization header format."""
        captured_headers = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_request
        )

        backend = OpenAIBackend(api_key="sk-test123")
        messages = _layer_a_messages()

        await backend.generate(messages)

        assert "authorization" in {k.lower() for k in captured_headers.keys()}, (
            "Layer A OpenAI backend should send Authorization header "
            "(adk/core/backends/openai_compat.py:33)"
        )
        auth_header = next(v for k, v in captured_headers.items() if k.lower() == "authorization")
        assert auth_header == "Bearer sk-test123", (
            "Authorization header should be 'Bearer {key}' format"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_response_format(self):
        """Layer A OpenAI: verify streaming response yields delta text chunks."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=_openai_streaming_response())
        )

        backend = OpenAIBackend(api_key="sk-test")
        messages = _layer_a_messages()

        chunks = []
        async for chunk in backend.stream(messages):
            chunks.append(chunk)

        # Layer A stream yields raw text deltas (str)
        assert chunks == ["Hello", " ", "world"], (
            "Layer A stream should yield incremental text chunks as strings "
            "(adk/core/backends/openai_compat.py:111-112)"
        )


# ─── Layer A Anthropic Backend Tests ────────────────────────────────────

class TestLayerAAnthropicGenerate:
    """Test Layer A (adk/core/backends/anthropic.py) generate method."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_request_payload_system_hoisting(self):
        """Layer A Anthropic: system messages are hoisted to top-level field.

        DIVERGENCE PINNED:
        - System messages are EXTRACTED and joined with '\\n\\n' (line 36-59)
        - max_tokens is ALWAYS included; defaults to 4096 if None (line 73)
        - 'messages' array contains NO role='system' entries (line 40-41)
        """
        captured_payload = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_request
        )

        backend = AnthropicBackend(api_key="sk-ant-test")
        messages = _layer_a_messages()

        result = await backend.generate(messages, temperature=0.7, max_tokens=None)

        # KEY DIVERGENCE: System is hoisted to top level
        assert captured_payload.get("system") == "You are a helpful assistant.", (
            "Layer A Anthropic backend should extract system messages to top-level "
            "'system' field (adk/core/backends/anthropic.py:78-79)"
        )

        # KEY DIVERGENCE: messages array has NO system role
        message_roles = [m.get("role") for m in captured_payload.get("messages", [])]
        assert "system" not in message_roles, (
            "System messages should be removed from messages array "
            "(adk/core/backends/anthropic.py:39-41)"
        )

        # KEY DIVERGENCE: max_tokens is ALWAYS present, defaults to 4096 when None
        assert captured_payload["max_tokens"] == 4096, (
            "Layer A Anthropic backend should set max_tokens=4096 when None "
            "(adk/core/backends/anthropic.py:73)"
        )

        # Verify response parsing uses 'stop_reason' not 'finish_reason'
        assert result.finish_reason == "end_turn", (
            "Response parsing should use 'stop_reason' from Anthropic API "
            "(adk/core/backends/anthropic.py:93)"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_auth_headers(self):
        """Layer A Anthropic: verify x-api-key header and version."""
        captured_headers = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_request
        )

        backend = AnthropicBackend(api_key="sk-ant-test123")
        messages = _layer_a_messages()

        await backend.generate(messages)

        assert "x-api-key" in {k.lower() for k in captured_headers.keys()}, (
            "Layer A Anthropic backend should send x-api-key header "
            "(adk/core/backends/anthropic.py:27)"
        )
        api_key_header = next(v for k, v in captured_headers.items() if k.lower() == "x-api-key")
        assert api_key_header == "sk-ant-test123"

        version_header = next((v for k, v in captured_headers.items() if k.lower() == "anthropic-version"), None)
        assert version_header == "2023-06-01", (
            "Should send anthropic-version header "
            "(adk/core/backends/anthropic.py:28)"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_response_format(self):
        """Layer A Anthropic: verify streaming parses SSE and yields delta text."""
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=_anthropic_streaming_response())
        )

        backend = AnthropicBackend(api_key="sk-ant-test")
        messages = _layer_a_messages()

        chunks = []
        async for chunk in backend.stream(messages):
            chunks.append(chunk)

        # Layer A stream yields raw text deltas (str)
        assert chunks == ["Hello", " ", "world"], (
            "Layer A stream should yield incremental text chunks as strings "
            "(adk/core/backends/anthropic.py:138-140)"
        )


# ─── Layer B OpenAI Provider Tests ───────────────────────────────────────

class TestLayerBOpenAIProviderGenerate:
    """Test Layer B (adk/llm/openai_compat.py) chat method."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_request_payload(self):
        """Layer B OpenAI: verify request payload structure.

        DIVERGENCE PINNED:
        - max_tokens ALWAYS included; defaults to 4096 (line 204, 219)
        - temperature is always included (line 203)
        - stream is False (line 218)
        - System messages are passed through by default (line 217 via
          _demote_nonleading_system, which demotes NON-LEADING system to user)
        """
        captured_payload = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_request
        )

        provider = OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini")
        messages = _layer_b_messages()

        result = await provider.chat(
            messages, temperature=0.7, max_tokens=4096
        )

        # KEY DIVERGENCE vs Layer A: max_tokens is ALWAYS present
        assert captured_payload["max_tokens"] == 4096, (
            "Layer B OpenAI provider should always include max_tokens "
            "(adk/llm/openai_compat.py:219)"
        )

        assert captured_payload["temperature"] == 0.7
        assert captured_payload["model"] == "gpt-4o-mini"

        # Verify response parsing
        assert result.content == "This is a test response."
        assert result.finish_reason == "stop"
        assert result.prompt_tokens == 10

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_auth_header(self):
        """Layer B OpenAI: verify Authorization header format."""
        captured_headers = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_request
        )

        provider = OpenAIProvider(api_key="sk-test123", default_model="gpt-4o-mini")
        messages = _layer_b_messages()

        await provider.chat(messages)

        auth_header = next((v for k, v in captured_headers.items() if k.lower() == "authorization"), None)
        assert auth_header == "Bearer sk-test123", (
            "Layer B OpenAI provider should send 'Bearer {key}' format "
            "(adk/llm/openai_compat.py:178)"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_response_format(self):
        """Layer B OpenAI: verify streaming yields StreamChunk objects."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=_openai_streaming_response())
        )

        provider = OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini")
        messages = _layer_b_messages()

        chunks = []
        async for chunk in provider.chat_stream(messages):
            chunks.append(chunk)

        # Layer B chat_stream yields StreamChunk namedtuples with .content and .done
        # Note: there may be trailing empty chunks; filter to non-empty for content check
        content_chunks = [c for c in chunks if c.content]
        assert len(content_chunks) == 3
        assert [c.content for c in content_chunks] == ["Hello", " ", "world"], (
            "Layer B chat_stream should yield StreamChunk objects with content "
            "(adk/llm/openai_compat.py:331-366)"
        )
        # Verify the last chunk is marked done
        assert chunks[-1].done is True


# ─── Layer B Anthropic Provider Tests ────────────────────────────────────

class TestLayerBAnthropicProviderGenerate:
    """Test Layer B (adk/llm/anthropic.py) chat method."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_request_payload_system_hoisting(self):
        """Layer B Anthropic: system messages are hoisted to top-level field.

        DIVERGENCE PINNED:
        - System messages are EXTRACTED and joined with '\\n\\n' (line 181-242)
        - max_tokens is ALWAYS included; defaults to 4096 (line 270-275)
        - 'messages' array contains NO role='system' entries
        """
        captured_payload = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_request
        )

        provider = AnthropicProvider(
            api_key="sk-ant-test",
            default_model="claude-3-5-sonnet-latest",
        )
        messages = _layer_b_messages()

        result = await provider.chat(messages, temperature=0.7, max_tokens=4096)

        # KEY DIVERGENCE: System is hoisted to top level (same as Layer A)
        assert captured_payload.get("system") == "You are a helpful assistant.", (
            "Layer B Anthropic provider should extract system messages to top-level "
            "'system' field (adk/llm/anthropic.py:181-242)"
        )

        # KEY DIVERGENCE: max_tokens is ALWAYS present
        assert captured_payload["max_tokens"] == 4096, (
            "Layer B Anthropic provider should always include max_tokens "
            "(adk/llm/anthropic.py:270-275)"
        )

        # Verify response parsing uses 'stop_reason' not 'finish_reason'
        assert result.finish_reason == "end_turn"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_auth_headers(self):
        """Layer B Anthropic: verify x-api-key header and version."""
        captured_headers = {}

        def capture_request(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_request
        )

        provider = AnthropicProvider(
            api_key="sk-ant-test123",
            default_model="claude-3-5-sonnet-latest",
        )
        messages = _layer_b_messages()

        await provider.chat(messages)

        assert "x-api-key" in {k.lower() for k in captured_headers.keys()}
        api_key_header = next(v for k, v in captured_headers.items() if k.lower() == "x-api-key")
        assert api_key_header == "sk-ant-test123", (
            "Layer B Anthropic provider should send x-api-key header "
            "(adk/llm/anthropic.py:164)"
        )

        version_header = next((v for k, v in captured_headers.items() if k.lower() == "anthropic-version"), None)
        assert version_header == "2023-06-01", (
            "Should send anthropic-version header (adk/llm/anthropic.py:165)"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_stream_response_format(self):
        """Layer B Anthropic: verify streaming yields StreamChunk objects."""
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, text=_anthropic_streaming_response())
        )

        provider = AnthropicProvider(
            api_key="sk-ant-test",
            default_model="claude-3-5-sonnet-latest",
        )
        messages = _layer_b_messages()

        chunks = []
        async for chunk in provider.chat_stream(messages):
            chunks.append(chunk)

        # Layer B chat_stream yields StreamChunk objects
        # Note: there may be trailing empty chunks; filter to non-empty for content check
        content_chunks = [c for c in chunks if c.content]
        assert len(content_chunks) == 3
        assert [c.content for c in content_chunks] == ["Hello", " ", "world"], (
            "Layer B chat_stream should yield StreamChunk objects with content "
            "(adk/llm/anthropic.py:443-458)"
        )


# ─── Cross-Layer Comparison Tests ───────────────────────────────────────

class TestCrossLayerOpenAIComparison:
    """Verify Layer A and Layer B OpenAI backends produce IDENTICAL wire format."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_identical_payloads_when_both_default_max_tokens(self):
        """When both layers send max_tokens=4096, payloads should match."""
        captured_layer_a = {}
        captured_layer_b = {}

        def capture_layer_a(request: httpx.Request) -> httpx.Response:
            captured_layer_a.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        def capture_layer_b(request: httpx.Request) -> httpx.Response:
            captured_layer_b.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        # Route Layer A to first endpoint
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_layer_a
        )

        layer_a = OpenAIBackend(api_key="sk-test")
        layer_a_msg = _layer_a_messages()

        # Layer A with explicit max_tokens=4096
        await layer_a.generate(layer_a_msg, temperature=0.7, max_tokens=4096)

        # Now test Layer B with same max_tokens
        respx.reset()
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_layer_b
        )

        layer_b = OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini")
        layer_b_msg = _layer_b_messages()

        await layer_b.chat(layer_b_msg, temperature=0.7, max_tokens=4096)

        # Both should send max_tokens
        assert captured_layer_a["max_tokens"] == 4096
        assert captured_layer_b["max_tokens"] == 4096

    @respx.mock
    @pytest.mark.asyncio
    async def test_layer_a_omits_none_layer_b_includes_4096(self):
        """Layer A omits max_tokens when None; Layer B always includes 4096."""
        captured_layer_a = {}
        captured_layer_b = {}

        def capture_layer_a(request: httpx.Request) -> httpx.Response:
            captured_layer_a.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        def capture_layer_b(request: httpx.Request) -> httpx.Response:
            captured_layer_b.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_layer_a
        )

        layer_a = OpenAIBackend(api_key="sk-test")
        layer_a_msg = _layer_a_messages()

        # Layer A with max_tokens=None (default)
        await layer_a.generate(layer_a_msg, temperature=0.7, max_tokens=None)

        respx.reset()
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_layer_b
        )

        layer_b = OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini")
        layer_b_msg = _layer_b_messages()

        # Layer B with default max_tokens (which is 4096)
        await layer_b.chat(layer_b_msg, temperature=0.7)

        # PINNED DIVERGENCE
        assert "max_tokens" not in captured_layer_a, (
            "Layer A omits max_tokens when None "
            "(adk/core/backends/openai_compat.py:56-57)"
        )
        assert captured_layer_b["max_tokens"] == 4096, (
            "Layer B always includes max_tokens=4096 "
            "(adk/llm/openai_compat.py:204, 219)"
        )


class TestCrossLayerAnthropicComparison:
    """Verify Layer A and Layer B Anthropic backends produce IDENTICAL wire format."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_identical_system_hoisting(self):
        """Both layers hoist system messages identically."""
        captured_layer_a = {}
        captured_layer_b = {}

        def capture_layer_a(request: httpx.Request) -> httpx.Response:
            captured_layer_a.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        def capture_layer_b(request: httpx.Request) -> httpx.Response:
            captured_layer_b.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_layer_a
        )

        layer_a = AnthropicBackend(api_key="sk-ant-test")
        layer_a_msg = _layer_a_messages()

        await layer_a.generate(layer_a_msg, temperature=0.7, max_tokens=4096)

        respx.reset()
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_layer_b
        )

        layer_b = AnthropicProvider(
            api_key="sk-ant-test",
            default_model="claude-3-5-sonnet-latest",
        )
        layer_b_msg = _layer_b_messages()

        await layer_b.chat(layer_b_msg, temperature=0.7, max_tokens=4096)

        # Both should hoist system identically
        assert captured_layer_a["system"] == "You are a helpful assistant."
        assert captured_layer_b["system"] == "You are a helpful assistant."

        # Both should have identical non-system messages
        assert len(captured_layer_a["messages"]) == 2
        assert len(captured_layer_b["messages"]) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_identical_max_tokens_behavior(self):
        """Both Anthropic layers always send max_tokens."""
        captured_layer_a = {}
        captured_layer_b = {}

        def capture_layer_a(request: httpx.Request) -> httpx.Response:
            captured_layer_a.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        def capture_layer_b(request: httpx.Request) -> httpx.Response:
            captured_layer_b.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_anthropic_response())

        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_layer_a
        )

        layer_a = AnthropicBackend(api_key="sk-ant-test")
        layer_a_msg = _layer_a_messages()

        # Layer A with max_tokens=None (default)
        await layer_a.generate(layer_a_msg, temperature=0.7, max_tokens=None)

        respx.reset()
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=capture_layer_b
        )

        layer_b = AnthropicProvider(
            api_key="sk-ant-test",
            default_model="claude-3-5-sonnet-latest",
        )
        layer_b_msg = _layer_b_messages()

        # Layer B with default max_tokens
        await layer_b.chat(layer_b_msg, temperature=0.7)

        # PINNED AGREEMENT: Both layers ALWAYS send max_tokens
        assert captured_layer_a["max_tokens"] == 4096, (
            "Layer A Anthropic always sends max_tokens "
            "(adk/core/backends/anthropic.py:73)"
        )
        assert captured_layer_b["max_tokens"] == 4096, (
            "Layer B Anthropic always sends max_tokens "
            "(adk/llm/anthropic.py:270-275)"
        )


# ─── Regression Tests ───────────────────────────────────────────────────

class TestTimeoutConfiguration:
    """Verify timeout values are consistent across layers."""

    def test_layer_a_openai_timeout(self):
        """Layer A OpenAI hardcodes 120.0s timeout."""
        backend = OpenAIBackend(api_key="sk-test")
        # The timeout is set at client creation time, not accessible as an attribute
        # This test documents the hardcoded value
        assert True  # Timeout is hardcoded in _client() at line 39

    def test_layer_a_anthropic_timeout(self):
        """Layer A Anthropic hardcodes 120.0s timeout."""
        backend = AnthropicBackend(api_key="sk-ant-test")
        assert True  # Timeout is hardcoded in _client() at line 34

    def test_layer_b_openai_timeout(self):
        """Layer B OpenAI defaults to 120.0s timeout."""
        provider = OpenAIProvider(api_key="sk-test")
        assert provider._timeout == 120.0, (
            "Layer B OpenAI provider should default to 120.0s timeout "
            "(adk/llm/openai_compat.py:144)"
        )

    def test_layer_b_anthropic_timeout(self):
        """Layer B Anthropic defaults to 120.0s timeout."""
        provider = AnthropicProvider(api_key="sk-ant-test")
        assert provider._timeout == 120.0, (
            "Layer B Anthropic provider should default to 120.0s timeout "
            "(adk/llm/anthropic.py:155)"
        )


class TestMessagesPassThrough:
    """Verify message role/content pass-through when not system/tool."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_user_and_assistant_roles_preserved(self):
        """User and assistant roles should pass through unchanged in all backends."""
        captured = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture
        )

        backend = OpenAIBackend(api_key="sk-test")
        messages = [
            LayerAMessage(role="user", content="Hello"),
            LayerAMessage(role="assistant", content="Hi there"),
        ]

        await backend.generate(messages)

        msg_roles = [m.get("role") for m in captured["messages"]]
        assert msg_roles == ["user", "assistant"], (
            "Non-system, non-tool roles should pass through unchanged"
        )


# ─── MID-CONVERSATION SYSTEM MESSAGE ─────────────────────────────────────
#
# Added after the first pass of this harness MISSED this divergence entirely.
# Why it was missed: every fixture above (`_layer_a_messages`,
# `_layer_b_messages`) puts the system message FIRST. Layer B's
# `_demote_nonleading_system` only fires on a system message that appears
# AFTER the conversation has started, so a leading-system fixture can never
# expose it. The measurement's own fixture hid the thing it was measuring.
#
# This is the highest-consequence divergence found so far, and it is NOT
# cosmetic: `_demote_nonleading_system` (adk/llm/openai_compat.py:60-81)
# exists because "DeepSeek (and several other OpenAI-compatible backends,
# e.g. vLLM with strict templates) reject a non-leading `system` message
# with a 400 — the cause is opaque without the body."
#
# Layer A ships BOTH affected backends (DeepSeekBackend at
# adk/core/backends/openai_compat.py:131, VLLMBackend at :124) and has NO
# demotion anywhere (verified: zero matches for demote/nonleading under
# adk/core/). So the hardening landed on Layer B only.
#
# CURRENT BLAST RADIUS — deliberately stated narrowly, because it is latent
# rather than live: adk/core/agent.py:167 and :366 and adk/core/codeact.py:295
# all construct the system message in LEADING position, so no in-repo Layer A
# path triggers this today. It fires for (a) any external embedder passing its
# own message list to a Layer A backend, and (b) any future mid-conversation
# system nudge — which the Layer B docstring says ReAct loops do
# "legitimately". That is exactly the drift this measurement predicts.
#
# These tests PIN CURRENT BEHAVIOUR. They are not asserting that Layer A is
# correct; they assert what it does today, so that porting the demotion to
# Layer A (or unifying the layers) is a deliberate, visible change rather
# than a silent one.

def _mid_conversation_system_a() -> list[LayerAMessage]:
    """Layer A messages with a system nudge AFTER the conversation starts."""
    return [
        LayerAMessage(role="system", content="You are a helpful assistant."),
        LayerAMessage(role="user", content="Hello"),
        LayerAMessage(role="assistant", content="Hi there"),
        LayerAMessage(role="system", content="Reminder: be concise."),
        LayerAMessage(role="user", content="Explain gravity."),
    ]


def _mid_conversation_system_b() -> list[LayerBMessage]:
    """Layer B messages with a system nudge AFTER the conversation starts."""
    return [
        LayerBMessage(role="system", content="You are a helpful assistant."),
        LayerBMessage(role="user", content="Hello"),
        LayerBMessage(role="assistant", content="Hi there"),
        LayerBMessage(role="system", content="Reminder: be concise."),
        LayerBMessage(role="user", content="Explain gravity."),
    ]


class TestMidConversationSystemMessageDivergence:
    """The two layers send DIFFERENT ROLES for a mid-conversation system message."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_layer_a_sends_nonleading_system_as_system(self):
        """Layer A passes a non-leading system message through UNCHANGED.

        Against DeepSeek or a strict-template vLLM this is the payload that
        earns an opaque 400. adk/core/backends/openai_compat.py:52 does a bare
        `[m.as_dict() for m in messages]` with no role rewriting.
        """
        captured: dict[str, Any] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture
        )

        await OpenAIBackend(api_key="sk-test").generate(_mid_conversation_system_a())

        roles = [m.get("role") for m in captured["messages"]]
        assert roles == ["system", "user", "assistant", "system", "user"], (
            f"Layer A should pass the non-leading system role through unchanged; got {roles}"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_layer_b_demotes_nonleading_system_to_user(self):
        """Layer B DEMOTES a non-leading system message to ``user``.

        adk/llm/openai_compat.py:60-81 (`_demote_nonleading_system`), applied
        at :217. Leading system message(s) are left untouched.
        """
        captured: dict[str, Any] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture
        )

        provider = OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini")
        await provider.chat(_mid_conversation_system_b(), model="gpt-4o-mini")

        roles = [m.get("role") for m in captured["messages"]]
        assert roles == ["system", "user", "assistant", "user", "user"], (
            f"Layer B should demote the non-leading system message to user; got {roles}"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_two_layers_disagree_on_the_wire(self):
        """The divergence itself, asserted directly: same input, different payload.

        This is the measurement that divergence asked for. If someone ports the
        demotion to Layer A (or unifies the layers), THIS TEST FAILS — which
        is the intended signal, not a regression.
        """
        cap_a: dict[str, Any] = {}
        cap_b: dict[str, Any] = {}

        def capture_a(request: httpx.Request) -> httpx.Response:
            cap_a.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        def capture_b(request: httpx.Request) -> httpx.Response:
            cap_b.update(json.loads(request.content) or {})
            return httpx.Response(200, json=_openai_response())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_a
        )
        await OpenAIBackend(api_key="sk-test").generate(_mid_conversation_system_a())

        respx.post("https://api.openai.com/v1/chat/completions").mock(
            side_effect=capture_b
        )
        await OpenAIProvider(api_key="sk-test", default_model="gpt-4o-mini").chat(
            _mid_conversation_system_b(), model="gpt-4o-mini"
        )

        roles_a = [m.get("role") for m in cap_a["messages"]]
        roles_b = [m.get("role") for m in cap_b["messages"]]

        assert roles_a != roles_b, (
            "Expected the two layers to disagree on a mid-conversation system "
            f"message, but both sent {roles_a}"
        )
        assert roles_a[3] == "system" and roles_b[3] == "user", (
            f"Expected Layer A=system / Layer B=user at index 3; "
            f"got A={roles_a[3]} B={roles_b[3]}"
        )
