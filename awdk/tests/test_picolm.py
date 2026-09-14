"""Tests for the PicoLM edge inference provider."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.llm.base import Message, ToolCall
from adk.llm.picolm import PicoLMProvider, _build_chatml


# ── ChatML Builder ───────────────────────────────────────────────────────


class TestBuildChatML:
    def test_basic_user_message(self):
        msgs = [Message(role="user", content="Hello")]
        result = _build_chatml(msgs)
        assert "<|im_start|>user\nHello<|im_end|>" in result
        assert result.endswith("<|im_start|>assistant\n")

    def test_system_and_user(self):
        msgs = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hi"),
        ]
        result = _build_chatml(msgs)
        assert "<|im_start|>system\nYou are helpful.<|im_end|>" in result
        assert "<|im_start|>user\nHi<|im_end|>" in result
        assert result.index("system") < result.index("user")

    def test_multi_turn(self):
        msgs = [
            Message(role="system", content="Be brief."),
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello!"),
            Message(role="user", content="Bye"),
        ]
        result = _build_chatml(msgs)
        assert result.count("<|im_start|>") == 5  # 4 messages + trailing assistant
        assert result.count("<|im_end|>") == 4

    def test_empty_messages(self):
        result = _build_chatml([])
        assert result == "<|im_start|>assistant\n"


# ── Provider Initialization ──────────────────────────────────────────────


class TestPicoLMInit:
    def test_defaults(self):
        p = PicoLMProvider()
        assert p.binary == ""
        assert p.model == ""
        assert p.threads == 4

    def test_explicit_params(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf", threads=8)
        assert p.binary == "/bin/picolm"
        assert p.model == "/m/test.gguf"
        assert p.threads == 8
        assert p.default_model == "test.gguf"

    def test_env_vars(self):
        with patch.dict(os.environ, {
            "PICOLM_BINARY": "/env/picolm",
            "PICOLM_MODEL": "/env/model.gguf",
            "PICOLM_THREADS": "16",
            "PICOLM_CACHE": "/tmp/cache",
        }):
            p = PicoLMProvider()
            assert p.binary == "/env/picolm"
            assert p.model == "/env/model.gguf"
            assert p.threads == 16
            assert p.cache_dir == "/tmp/cache"

    def test_explicit_overrides_env(self):
        with patch.dict(os.environ, {"PICOLM_BINARY": "/env/picolm"}):
            p = PicoLMProvider(binary="/explicit/picolm")
            assert p.binary == "/explicit/picolm"


# ── Health Check ─────────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_no_binary(self):
        p = PicoLMProvider()
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_no_model(self):
        p = PicoLMProvider(binary="/bin/picolm")
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_files_exist(self, tmp_path):
        binary = tmp_path / "picolm"
        binary.write_text("fake")
        model = tmp_path / "model.gguf"
        model.write_text("fake")
        p = PicoLMProvider(binary=str(binary), model=str(model))
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_files_missing(self):
        p = PicoLMProvider(binary="/nonexistent/picolm", model="/nonexistent/model.gguf")
        assert await p.health_check() is False


# ── List Models ──────────────────────────────────────────────────────────


class TestListModels:
    @pytest.mark.asyncio
    async def test_list_with_model(self):
        p = PicoLMProvider(model="/models/phi-2.gguf")
        models = await p.list_models()
        assert models == ["phi-2.gguf"]

    @pytest.mark.asyncio
    async def test_list_no_model(self):
        p = PicoLMProvider()
        models = await p.list_models()
        assert models == []


# ── Chat (subprocess mocking) ───────────────────────────────────────────


class TestChat:
    @pytest.mark.asyncio
    async def test_no_binary_raises(self):
        p = PicoLMProvider(model="/m/test.gguf")
        with pytest.raises(RuntimeError, match="binary not configured"):
            await p.chat([Message(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_no_model_raises(self):
        p = PicoLMProvider(binary="/bin/picolm")
        with pytest.raises(RuntimeError, match="model not configured"):
            await p.chat([Message(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_basic_chat(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Hello world!", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await p.chat([Message(role="user", content="hi")])
            assert resp.content == "Hello world!"
            assert resp.finish_reason == "stop"
            assert resp.model == "test.gguf"
            assert resp.tokens_used > 0

    @pytest.mark.asyncio
    async def test_chat_with_system_prompt(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Response", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat([
                Message(role="system", content="Be brief"),
                Message(role="user", content="hi"),
            ])
            # Verify stdin contains ChatML
            call_args = mock_proc.communicate.call_args
            stdin = call_args[1]["input"].decode()
            assert "<|im_start|>system" in stdin
            assert "Be brief" in stdin

    @pytest.mark.asyncio
    async def test_chat_custom_params(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf", threads=8)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat(
                [Message(role="user", content="hi")],
                temperature=0.5,
                max_tokens=512,
            )
            cmd = mock_exec.call_args[0]
            assert "-n" in cmd
            idx = cmd.index("-n")
            assert cmd[idx + 1] == "512"
            assert "-t" in cmd
            idx = cmd.index("-t")
            assert cmd[idx + 1] == "0.5"
            assert "-j" in cmd
            idx = cmd.index("-j")
            assert cmd[idx + 1] == "8"

    @pytest.mark.asyncio
    async def test_json_mode_with_tools(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat(
                [Message(role="user", content="search for cats")],
                tools=[{"type": "function", "function": {"name": "search"}}],
            )
            cmd = mock_exec.call_args[0]
            assert "--json" in cmd

    @pytest.mark.asyncio
    async def test_tool_call_parsing_multi(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        tool_response = json.dumps({
            "tool_calls": [
                {"function": {"name": "search", "arguments": {"q": "cats"}}},
            ],
            "content": "",
        })
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(tool_response.encode(), b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await p.chat(
                [Message(role="user", content="search")],
                tools=[{"type": "function"}],
            )
            assert len(resp.tool_calls) == 1
            assert resp.tool_calls[0].name == "search"
            assert resp.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_tool_call_parsing_single(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        tool_response = json.dumps({
            "name": "get_weather",
            "arguments": {"city": "NYC"},
        })
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(tool_response.encode(), b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await p.chat(
                [Message(role="user", content="weather")],
                tools=[{"type": "function"}],
            )
            assert len(resp.tool_calls) == 1
            assert resp.tool_calls[0].name == "get_weather"
            assert resp.tool_calls[0].arguments == {"city": "NYC"}
            assert resp.content == ""
            assert resp.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_invalid_json_with_tools(self):
        """Non-JSON output with tools enabled is treated as plain text."""
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Just plain text", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            resp = await p.chat(
                [Message(role="user", content="hi")],
                tools=[{"type": "function"}],
            )
            assert resp.content == "Just plain text"
            assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"segfault"))
        mock_proc.returncode = 1

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="exited with code 1"):
                await p.chat([Message(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_timeout(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf", timeout=0.1)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="timed out"):
                await p.chat([Message(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_binary_not_found(self):
        p = PicoLMProvider(binary="/nonexistent/picolm", model="/m/test.gguf")

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="binary not found"):
                await p.chat([Message(role="user", content="hi")])


# ── KV Cache ─────────────────────────────────────────────────────────────


class TestKVCache:
    @pytest.mark.asyncio
    async def test_cache_with_system_prompt(self, tmp_path):
        p = PicoLMProvider(
            binary="/bin/picolm",
            model="/m/test.gguf",
            cache_dir=str(tmp_path),
        )
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat([
                Message(role="system", content="You are helpful."),
                Message(role="user", content="hi"),
            ])
            cmd = mock_exec.call_args[0]
            assert "--cache" in cmd
            cache_idx = cmd.index("--cache")
            cache_path = cmd[cache_idx + 1]
            assert str(tmp_path) in cache_path
            assert cache_path.endswith(".kvc")

    @pytest.mark.asyncio
    async def test_no_cache_without_system(self, tmp_path):
        p = PicoLMProvider(
            binary="/bin/picolm",
            model="/m/test.gguf",
            cache_dir=str(tmp_path),
        )
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat([Message(role="user", content="hi")])
            cmd = mock_exec.call_args[0]
            assert "--cache" not in cmd

    @pytest.mark.asyncio
    async def test_no_cache_without_dir(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await p.chat([
                Message(role="system", content="test"),
                Message(role="user", content="hi"),
            ])
            cmd = mock_exec.call_args[0]
            assert "--cache" not in cmd


# ── Streaming ────────────────────────────────────────────────────────────


class TestStreaming:
    @pytest.mark.asyncio
    async def test_chat_stream_wraps_chat(self):
        p = PicoLMProvider(binary="/bin/picolm", model="/m/test.gguf")
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Hello!", b""))
        mock_proc.returncode = 0

        with patch("adk.llm.picolm.asyncio.create_subprocess_exec", return_value=mock_proc):
            chunks = []
            async for chunk in p.chat_stream([Message(role="user", content="hi")]):
                chunks.append(chunk)
            assert len(chunks) == 1
            assert chunks[0].content == "Hello!"
            assert chunks[0].done is True


# ── LLMRouter Integration ───────────────────────────────────────────────


class TestRouterIntegration:
    def test_create_picolm_provider(self):
        from adk.llm import LLMRouter
        router = LLMRouter(provider="picolm", base_url="/bin/picolm", model="/m/test.gguf")
        assert router._provider_name == "picolm"

    @pytest.mark.asyncio
    async def test_auto_detect_with_picolm_env(self, tmp_path):
        binary = tmp_path / "picolm"
        binary.write_text("fake")
        model = tmp_path / "model.gguf"
        model.write_text("fake")

        with patch.dict(os.environ, {
            "PICOLM_BINARY": str(binary),
            "PICOLM_MODEL": str(model),
        }, clear=False):
            from adk.llm import LLMRouter
            router = LLMRouter()
            # Mock out vLLM/Ollama so they fail
            router._try_vllm = AsyncMock(return_value=None)
            router._try_ollama = AsyncMock(return_value=None)
            # Clear any API keys
            with patch.dict(os.environ, {
                "AITHER_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
            }):
                provider = await router._auto_detect()
                assert router._provider_name == "picolm"

    def test_effort_models(self):
        from adk.llm import _EFFORT_MODELS
        assert "picolm" in _EFFORT_MODELS
        assert _EFFORT_MODELS["picolm"]["small"] == "picolm"
