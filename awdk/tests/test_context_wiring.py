"""Tests for context manager wiring into agent loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_llm(content="Hello!"):
    resp = MagicMock()
    resp.content = content
    resp.model = "test-model"
    resp.tokens_used = 10
    resp.latency_ms = 50.0
    resp.tool_calls = []
    resp.finish_reason = "stop"
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=resp)
    llm.provider_name = "test"
    return llm


class TestContextManagerWiring:
    """ContextManager is used in agent.chat() for message building."""

    @pytest.mark.asyncio
    async def test_context_manager_initialized(self):
        from adk.agent import AitherAgent
        llm = _make_mock_llm()
        agent = AitherAgent(name="test", llm=llm, builtin_tools=False)
        assert agent._context_mgr is not None

    @pytest.mark.asyncio
    async def test_context_manager_used_in_chat(self):
        """Verify the context manager build() is called during chat."""
        from adk.agent import AitherAgent
        from adk.context import ContextManager
        llm = _make_mock_llm(content="Hi there")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="You are helpful.",
        )
        # Replace context manager with a spy
        spy_ctx = MagicMock(spec=ContextManager)
        spy_ctx.build.return_value = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        agent._context_mgr = spy_ctx
        await agent.chat("Hello")
        spy_ctx.clear.assert_called_once()
        # The system prompt is the configured prompt PLUS the live [AGENT HOST]
        # state block ("agents know the time", 3.7.2). Assert the contract —
        # prompt first, host block appended — rather than the exact bytes: the
        # block carries the wall clock and hostname, which differ every run,
        # and an equality assert here is how 3.7.2's mirror CI went red twice.
        spy_ctx.add_system.assert_called_once()
        sys_prompt = spy_ctx.add_system.call_args[0][0]
        assert sys_prompt.startswith("You are helpful.")
        assert "[AGENT HOST" in sys_prompt
        spy_ctx.add_user.assert_called_once_with("Hello")
        spy_ctx.build.assert_called_once()

    @pytest.mark.asyncio
    async def test_truncation_with_long_history(self):
        """Context manager truncates long history to fit token budget."""
        from adk.agent import AitherAgent
        from adk.context import ContextManager
        llm = _make_mock_llm(content="Response")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        # Use a real context manager with tiny budget
        agent._context_mgr = ContextManager(max_tokens=200, preserve_turns=1, reserve_for_response=50)

        # Build long history
        history = [
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Message {i} " * 20}
            for i in range(20)
        ]
        resp = await agent.chat("Latest question", history=history)
        # Should have called LLM with truncated messages
        call_args = llm.chat.call_args
        messages = call_args[0][0]
        # Truncated: fewer than 20 history + system + user = 22
        assert len(messages) < 22

    @pytest.mark.asyncio
    async def test_preserves_system_and_latest(self):
        """System prompt and latest user message are always preserved."""
        from adk.agent import AitherAgent
        from adk.context import ContextManager
        llm = _make_mock_llm(content="Answer")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="IMPORTANT SYSTEM PROMPT",
        )
        agent._context_mgr = ContextManager(max_tokens=200, preserve_turns=1, reserve_for_response=50)
        agent._graph = None  # Disable graph memory injection
        agent._auto_neurons = None  # Disable neuron injection
        history = [
            {"role": "user", "content": f"Old message {i} " * 30}
            for i in range(10)
        ]
        resp = await agent.chat("Latest question", history=history)
        call_args = llm.chat.call_args
        messages = call_args[0][0]
        contents = [m.content for m in messages]
        assert any("IMPORTANT SYSTEM PROMPT" in c for c in contents)
        assert any("Latest question" in c for c in contents)

    @pytest.mark.asyncio
    async def test_fallback_when_context_manager_fails(self):
        """Agent falls back to manual message assembly if ContextManager errors."""
        from adk.agent import AitherAgent
        llm = _make_mock_llm(content="Still works")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        # Break the context manager
        agent._context_mgr = MagicMock()
        agent._context_mgr.clear = MagicMock(side_effect=RuntimeError("broken"))
        resp = await agent.chat("Hello")
        assert resp.content == "Still works"
        llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_when_context_manager_none(self):
        """Agent works fine without a context manager."""
        from adk.agent import AitherAgent
        llm = _make_mock_llm(content="Works")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._context_mgr = None
        resp = await agent.chat("Hello")
        assert resp.content == "Works"

    @pytest.mark.asyncio
    async def test_context_manager_with_stored_history(self):
        """Context manager uses stored history when no explicit history passed."""
        from adk.agent import AitherAgent
        from adk.context import ContextManager
        llm = _make_mock_llm(content="OK")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        # Mock memory to return stored history
        agent.memory.get_history = AsyncMock(return_value=[
            {"role": "user", "content": "Previous Q"},
            {"role": "assistant", "content": "Previous A"},
        ])
        spy_ctx = MagicMock(spec=ContextManager)
        spy_ctx.build.return_value = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Previous Q"},
            {"role": "assistant", "content": "Previous A"},
            {"role": "user", "content": "New Q"},
        ]
        agent._context_mgr = spy_ctx
        await agent.chat("New Q")
        # Should have added stored history
        assert spy_ctx.add.call_count == 2  # Two stored messages

    @pytest.mark.asyncio
    async def test_config_max_context_used(self):
        """ContextManager uses config.max_context for token budget."""
        from adk.agent import AitherAgent
        from adk.config import Config
        config = Config()
        config.max_context = 4096
        llm = _make_mock_llm()
        agent = AitherAgent(
            name="test", llm=llm, config=config,
            builtin_tools=False, system_prompt="System",
        )
        assert agent._context_mgr is not None
        assert agent._context_mgr.max_tokens == 4096
