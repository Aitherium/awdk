"""Tests for LLM provider layer."""

import json
import pytest
import httpx
import respx
from unittest.mock import AsyncMock, MagicMock, patch

from adk.llm.base import Message, LLMResponse, ToolCall, messages_to_dicts, LLMProvider
from adk.llm.ollama import OllamaProvider
from adk.llm.openai_compat import OpenAIProvider
from adk.llm.anthropic import AnthropicProvider
from adk.llm import LLMRouter


# ─── Base types ───

class TestMessage:
    def test_create(self):
        m = Message(role="user", content="hello")
        assert m.role == "user"
        assert m.content == "hello"

    def test_with_tool_call_id(self):
        m = Message(role="tool", content="result", tool_call_id="tc_1")
        assert m.tool_call_id == "tc_1"

    def test_messages_to_dicts(self):
        msgs = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        dicts = messages_to_dicts(msgs)
        assert len(dicts) == 2
        assert dicts[0] == {"role": "system", "content": "sys"}

    def test_messages_to_dicts_with_name(self):
        msgs = [Message(role="user", content="hi", name="bob")]
        dicts = messages_to_dicts(msgs)
        assert dicts[0]["name"] == "bob"


class TestLLMResponse:
    def test_defaults(self):
        r = LLMResponse(content="hello")
        assert r.content == "hello"
        assert r.tokens_used == 0
        assert r.tool_calls == []

    def test_with_tool_calls(self):
        tc = ToolCall(id="1", name="search", arguments={"q": "test"})
        r = LLMResponse(content="", tool_calls=[tc])
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "search"


# ─── Ollama ───

class TestOllamaProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_chat(self):
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={
                "message": {"role": "assistant", "content": "Hello there!"},
                "model": "llama3.2",
                "eval_count": 10,
                "prompt_eval_count": 5,
            })
        )
        provider = OllamaProvider()
        resp = await provider.chat([Message(role="user", content="hi")])
        assert resp.content == "Hello there!"
        assert resp.model == "llama3.2"
        assert resp.tokens_used == 15

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_models(self):
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={
                "models": [{"name": "llama3.2"}, {"name": "deepseek-r1:14b"}]
            })
        )
        provider = OllamaProvider()
        models = await provider.list_models()
        assert "llama3.2" in models
        assert len(models) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_with_tools(self):
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "search", "arguments": {"q": "test"}}}],
                },
                "model": "llama3.2",
            })
        )
        provider = OllamaProvider()
        resp = await provider.chat(
            [Message(role="user", content="search for test")],
            tools=[{"type": "function", "function": {"name": "search"}}],
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2"}]})
        )
        provider = OllamaProvider()
        assert await provider.health_check() is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self):
        respx.get("http://localhost:11434/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        provider = OllamaProvider()
        assert await provider.health_check() is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_non_200(self):
        """health_check returns False on non-200 status (e.g. 500)."""
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )
        provider = OllamaProvider()
        assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_uses_fast_timeout(self):
        """health_check uses a 5s timeout, not the default 120s."""
        provider = OllamaProvider(timeout=300.0)  # Deliberately large default
        with patch("adk.llm.ollama.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            await provider.health_check()
            # Verify the client was created with 5s, not 300s
            MockClient.assert_called_once()
            call_kwargs = MockClient.call_args
            assert call_kwargs.kwargs.get("timeout") == 5.0 or call_kwargs[1].get("timeout") == 5.0

    def test_custom_host(self):
        provider = OllamaProvider(host="http://myhost:11434")
        assert provider.host == "http://myhost:11434"


# ─── OpenAI-compatible ───

class TestOpenAIProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_chat(self):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "Hi!"}, "finish_reason": "stop"}],
                "model": "gpt-4o-mini",
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="hi")])
        assert resp.content == "Hi!"
        assert resp.tokens_used == 7

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "calc", "arguments": '{"expr": "2+2"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="calc 2+2")])
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].arguments == {"expr": "2+2"}

    # ── thinking-model empty-content rescue ──
    # Live signature (qwen3.6-27B-NVFP4 on a DGX Spark, 2026-07-26): the whole
    # max_tokens budget is spent in the reasoning channel and `content` comes
    # back null. Without the rescue these are successful 200s with empty answers.

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_rescues_reasoning_when_content_null(self):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {"role": "assistant", "content": None,
                                "reasoning": "the answer is 4"},
                    "finish_reason": "length",
                }],
                "model": "qwen36-27b-dgx",
                "usage": {"prompt_tokens": 8, "completion_tokens": 200, "total_tokens": 208},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="2+2?")])
        assert resp.content == "the answer is 4"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_rescues_legacy_reasoning_content_field(self):
        """vLLM < 0.8 and the DeepSeek API name the field `reasoning_content`."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {"role": "assistant", "content": "",
                                "reasoning_content": "legacy channel"},
                    "finish_reason": "length",
                }],
                "model": "deepseek-reasoner",
                "usage": {"prompt_tokens": 3, "completion_tokens": 50, "total_tokens": 53},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="hi")])
        assert resp.content == "legacy channel"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_real_content_wins_over_reasoning(self):
        """Guards the Kimi trap: never let reasoning displace a real answer."""
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{
                    "message": {"role": "assistant", "content": "4",
                                "reasoning": "let me think... 2+2..."},
                    "finish_reason": "stop",
                }],
                "model": "qwen36-27b-dgx",
                "usage": {"prompt_tokens": 8, "completion_tokens": 30, "total_tokens": 38},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="2+2?")])
        assert resp.content == "4"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_stream_flushes_reasoning_when_no_content(self):
        sse = (
            'data: {"choices":[{"delta":{"reasoning":"thinking "},"finish_reason":null}],"model":"q"}\n\n'
            'data: {"choices":[{"delta":{"reasoning":"hard"},"finish_reason":null}],"model":"q"}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"length"}],"model":"q"}\n\n'
            'data: [DONE]\n\n'
        )
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=sse)
        )
        provider = OpenAIProvider(api_key="sk-test")
        chunks = [c async for c in provider.chat_stream([Message(role="user", content="hi")])]
        assert "".join(c.content for c in chunks) == "thinking hard"
        assert any(c.done for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_stream_does_not_duplicate_when_content_present(self):
        """Reasoning must be dropped, not appended, once real content streamed."""
        sse = (
            'data: {"choices":[{"delta":{"reasoning":"thinking"},"finish_reason":null}],"model":"q"}\n\n'
            'data: {"choices":[{"delta":{"content":"4"},"finish_reason":null}],"model":"q"}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"model":"q"}\n\n'
            'data: [DONE]\n\n'
        )
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=sse)
        )
        provider = OpenAIProvider(api_key="sk-test")
        chunks = [c async for c in provider.chat_stream([Message(role="user", content="hi")])]
        assert "".join(c.content for c in chunks) == "4"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_stream_parses_aitheros_typed_sse(self):
        """MicroScheduler's /v1 facade answers stream=true with AitherOS-typed
        events, not OpenAI chunks. Parsing only the OpenAI shape made that a
        perfectly silent EMPTY stream (zero chunks, no error)."""
        sse = (
            'event: session_start\n'
            'data: {"type":"session_start","model":"aither-orchestrator"}\n\n'
            'event: token\n'
            'data: {"type":"token","t":"Hel","n":1}\n\n'
            'event: token\n'
            'data: {"type":"token","t":"lo","n":2}\n\n'
            'event: answer\n'
            'data: {"type":"answer","answer":"Hello"}\n\n'
            'event: complete\n'
            'data: {"type":"complete","duration_ms":10,"model":"aither-orchestrator"}\n\n'
        )
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=sse)
        )
        provider = OpenAIProvider(api_key="sk-test")
        chunks = [c async for c in provider.chat_stream([Message(role="user", content="hi")])]
        # tokens streamed; the trailing "answer" event must NOT double the text
        assert "".join(c.content for c in chunks) == "Hello"
        assert any(c.done for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_stream_typed_sse_answer_only(self):
        """A typed stream with no token events still yields the final answer."""
        sse = (
            'event: answer\n'
            'data: {"type":"answer","answer":"42"}\n\n'
            'event: complete\n'
            'data: {"type":"complete","duration_ms":5}\n\n'
        )
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, text=sse)
        )
        provider = OpenAIProvider(api_key="sk-test")
        chunks = [c async for c in provider.chat_stream([Message(role="user", content="hi")])]
        assert "".join(c.content for c in chunks) == "42"
        assert any(c.done for c in chunks)

    @respx.mock
    @pytest.mark.asyncio
    async def test_list_models(self):
        respx.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(200, json={
                "data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}]
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        models = await provider.list_models()
        assert "gpt-4o" in models

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_healthy(self):
        respx.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})
        )
        provider = OpenAIProvider(api_key="sk-test")
        assert await provider.health_check() is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self):
        respx.get("https://api.openai.com/v1/models").mock(
            side_effect=httpx.ConnectError("refused")
        )
        provider = OpenAIProvider(api_key="sk-test")
        assert await provider.health_check() is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_non_200(self):
        """health_check returns False on 401/500/etc."""
        respx.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        provider = OpenAIProvider(api_key="sk-bad")
        assert await provider.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_uses_fast_timeout(self):
        """health_check uses a 5s timeout, not the default 120s."""
        provider = OpenAIProvider(api_key="sk-test", timeout=300.0)
        with patch("adk.llm.openai_compat.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(
                return_value=MagicMock(status_code=200)
            )
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            await provider.health_check()
            MockClient.assert_called_once()
            call_kwargs = MockClient.call_args
            assert call_kwargs.kwargs.get("timeout") == 5.0 or call_kwargs[1].get("timeout") == 5.0

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_check_vllm_local(self):
        """health_check works for vLLM on localhost (OpenAI-compat format)."""
        respx.get("http://localhost:8200/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "meta-llama/Llama-3.2-8B"}]})
        )
        provider = OpenAIProvider(base_url="http://localhost:8200/v1", api_key="not-needed")
        assert await provider.health_check() is True

    def test_custom_base_url(self):
        provider = OpenAIProvider(base_url="http://localhost:8000/v1")
        assert provider.base_url == "http://localhost:8000/v1"

    @respx.mock
    @pytest.mark.asyncio
    async def test_null_content_handled(self):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}],
                "model": "gpt-4o-mini",
                "usage": {"total_tokens": 0},
            })
        )
        provider = OpenAIProvider(api_key="sk-test")
        resp = await provider.chat([Message(role="user", content="hi")])
        assert resp.content == ""


# ─── Anthropic ───

class TestAnthropicProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_chat(self):
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={
                "content": [{"type": "text", "text": "Hello!"}],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 5, "output_tokens": 3},
                "stop_reason": "end_turn",
            })
        )
        provider = AnthropicProvider(api_key="sk-ant-test")
        resp = await provider.chat([Message(role="user", content="hi")])
        assert resp.content == "Hello!"
        assert resp.tokens_used == 8

    @respx.mock
    @pytest.mark.asyncio
    async def test_system_message_extraction(self):
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={
                "content": [{"type": "text", "text": "OK"}],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            })
        )
        provider = AnthropicProvider(api_key="sk-ant-test")
        await provider.chat([
            Message(role="system", content="You are helpful"),
            Message(role="user", content="hi"),
        ])
        body = json.loads(route.calls[0].request.content)
        assert body["system"] == "You are helpful"
        assert len(body["messages"]) == 1  # system extracted

    @respx.mock
    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={
                "content": [
                    {"type": "text", "text": "Let me search."},
                    {"type": "tool_use", "id": "tu_1", "name": "search", "input": {"q": "test"}},
                ],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
        )
        provider = AnthropicProvider(api_key="sk-ant-test")
        resp = await provider.chat([Message(role="user", content="search test")])
        assert "Let me search" in resp.content
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"

    async def test_list_models(self):
        provider = AnthropicProvider()
        models = await provider.list_models()
        assert "claude-sonnet-4-6" in models


# ─── LLMRouter ───

class TestLLMRouter:
    def test_explicit_provider(self):
        router = LLMRouter(provider="ollama")
        assert router.provider_name == "ollama"

    def test_model_for_effort(self):
        router = LLMRouter(provider="openai", api_key="sk-test")
        assert router.model_for_effort(1) == "gpt-4o-mini"    # small
        assert router.model_for_effort(5) == "gpt-4o"          # medium
        assert router.model_for_effort(9) == "o1"              # large

    def test_model_override(self):
        router = LLMRouter(provider="ollama", model="custom-model")
        assert router.model_for_effort(5) == "custom-model"

    @respx.mock
    @pytest.mark.asyncio
    async def test_chat_delegates(self):
        respx.post("http://localhost:11434/api/chat").mock(
            return_value=httpx.Response(200, json={
                "message": {"content": "hi"},
                "model": "llama3.2",
            })
        )
        router = LLMRouter(provider="ollama")
        resp = await router.chat([Message(role="user", content="hi")])
        assert resp.content == "hi"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            LLMRouter(provider="nonexistent")

    @respx.mock
    @pytest.mark.asyncio
    async def test_auto_detect_ollama(self):
        respx.get("http://localhost:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "llama3.2"}]})
        )
        router = LLMRouter()
        provider = await router.get_provider()
        assert router.provider_name == "ollama"

    @respx.mock
    @pytest.mark.asyncio
    async def test_auto_detect_falls_back_to_env(self, monkeypatch):
        respx.get("http://localhost:11434/api/tags").mock(side_effect=httpx.ConnectError("refused"))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        router = LLMRouter()
        provider = await router.get_provider()
        assert router.provider_name == "openai"


# ─── Non-leading system demotion (DeepSeek 400 fix) ───

class TestDemoteNonLeadingSystem:
    def _demote(self, dicts):
        from adk.llm.openai_compat import _demote_nonleading_system
        return _demote_nonleading_system(dicts)

    def test_leading_system_kept(self):
        out = self._demote([{"role": "system", "content": "sys"},
                            {"role": "user", "content": "hi"}])
        assert out[0]["role"] == "system"

    def test_consecutive_leading_systems_kept(self):
        out = self._demote([{"role": "system", "content": "a"},
                            {"role": "system", "content": "b"},
                            {"role": "user", "content": "hi"}])
        assert [m["role"] for m in out] == ["system", "system", "user"]

    def test_midconversation_system_demoted_to_user(self):
        # the exact shape that 400'd DeepSeek: a mid-loop steering nudge
        out = self._demote([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "r", "tool_call_id": "t1"},
            {"role": "system", "content": "[DIMINISHING RETURNS] ..."},
        ])
        assert out[-1]["role"] == "user"
        assert "[DIMINISHING RETURNS]" in out[-1]["content"]
        assert out[0]["role"] == "system"  # leading prompt untouched

    def test_does_not_mutate_input(self):
        src = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"},
               {"role": "system", "content": "mid"}]
        self._demote(src)
        assert src[2]["role"] == "system"  # original unchanged


class TestEnsureOkSurfacesBody:
    def test_400_includes_provider_body(self):
        from adk.llm.openai_compat import _ensure_ok
        req = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        resp = httpx.Response(
            400, request=req,
            json={"error": {"message": "This model's maximum context length is 65536 tokens"}},
        )
        with pytest.raises(httpx.HTTPStatusError) as ei:
            _ensure_ok(resp)
        assert "maximum context length" in str(ei.value)

    def test_2xx_does_not_raise(self):
        from adk.llm.openai_compat import _ensure_ok
        req = httpx.Request("POST", "https://x/v1/chat/completions")
        _ensure_ok(httpx.Response(200, request=req, json={"ok": True}))


# ─── Transient-error retry (502/503/429/529) ───

class TestTransientRetry:
    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_retries_502_then_succeeds(self):
        route = respx.post("https://api.test/v1/chat/completions").mock(side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 3, "prompt_tokens": 2, "completion_tokens": 1},
                "model": "m"}),
        ])
        p = OpenAIProvider(base_url="https://api.test/v1", api_key="sk", default_model="m")
        with patch("adk.llm.openai_compat.asyncio.sleep", new_callable=AsyncMock):
            resp = await p.chat([Message(role="user", content="hi")])
        assert resp.content == "ok"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_exhausts_and_raises_with_body(self):
        respx.post("https://api.test/v1/chat/completions").mock(
            return_value=httpx.Response(503, text="overloaded detail"))
        p = OpenAIProvider(base_url="https://api.test/v1", api_key="sk", default_model="m")
        with patch("adk.llm.openai_compat.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await p.chat([Message(role="user", content="hi")])
        assert "overloaded detail" in str(ei.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_openai_400_not_retried(self):
        route = respx.post("https://api.test/v1/chat/completions").mock(
            return_value=httpx.Response(400, text="bad request"))
        p = OpenAIProvider(base_url="https://api.test/v1", api_key="sk", default_model="m")
        with patch("adk.llm.openai_compat.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError):
                await p.chat([Message(role="user", content="hi")])
        assert route.call_count == 1  # 4xx is not transient → no retry

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_retries_502_then_succeeds(self):
        route = respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "claude", "stop_reason": "end_turn"}),
        ])
        p = AnthropicProvider(api_key="sk", default_model="claude")
        with patch("adk.llm.anthropic.asyncio.sleep", new_callable=AsyncMock):
            resp = await p.chat([Message(role="user", content="hi")])
        assert resp.content == "ok"
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_anthropic_exhausts_and_raises_with_body(self):
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(529, text="overloaded"))
        p = AnthropicProvider(api_key="sk", default_model="claude")
        with patch("adk.llm.anthropic.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await p.chat([Message(role="user", content="hi")])
        assert "overloaded" in str(ei.value)


# ─── thinking-mode wiring ───

class TestChatTemplateKwargsWiring:
    """Every OpenAIProvider construction must carry the per-model
    chat_template_kwargs default.

    qwen3.6 with thinking ENABLED burns its whole budget in the reasoning
    channel and returns content=None. Measured live 2026-07-26 against
    qwen3.6-27B-NVFP4 on a DGX Spark: without the kwarg, max_tokens=220 gave
    finish_reason="length", 220 tokens, content None; WITH it, the same prompt
    returned finish_reason="stop" in 42 tokens with a clean 253-char answer.

    Only 2 of the 7 construction sites passed it — notably NOT the DGX
    auto-detect path — so adk talking straight to the DGX got empty answers.
    An AST check (not a grep) so a new construction cannot silently omit it.
    """

    def test_every_openai_provider_construction_passes_ctk_by_model(self):
        import ast
        import pathlib

        src_path = pathlib.Path(__file__).resolve().parents[1] / "adk" / "llm" / "__init__.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))

        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "OpenAIProvider":
                continue
            if not any(kw.arg == "ctk_by_model" for kw in node.keywords):
                missing.append(node.lineno)

        assert not missing, (
            f"OpenAIProvider constructed without ctk_by_model at line(s) {missing} "
            f"in {src_path.name} — a qwen model reached through that path will run "
            f"with thinking ON and return empty content."
        )

    def test_default_ctk_disables_thinking_for_qwen(self):
        from adk.llm import _DEFAULT_CTK_BY_MODEL

        assert _DEFAULT_CTK_BY_MODEL.get("qwen", {}).get("enable_thinking") is False

    def test_resolve_ctk_matches_the_live_dgx_model_id(self):
        """The served id is 'qwen36-27b-dgx'; matching is substring + case-insensitive."""
        from adk.llm import _DEFAULT_CTK_BY_MODEL
        from adk.llm.openai_compat import OpenAIProvider

        p = OpenAIProvider(api_key="x", ctk_by_model=_DEFAULT_CTK_BY_MODEL)
        assert p._resolve_ctk("qwen36-27b-dgx") == {"enable_thinking": False}
        assert p._resolve_ctk("Qwen3.6-27B-NVFP4") == {"enable_thinking": False}
