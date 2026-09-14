"""Tests for event emission wiring into agent, forge, and server."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_llm(content="Hello!", tool_calls=None):
    resp = MagicMock()
    resp.content = content
    resp.model = "test-model"
    resp.tokens_used = 10
    resp.latency_ms = 50.0
    resp.tool_calls = tool_calls or []
    resp.finish_reason = "stop"
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=resp)
    llm.provider_name = "test"
    return llm


class TestAgentEvents:
    """Events fire during agent.chat()."""

    @pytest.mark.asyncio
    async def test_chat_request_event(self):
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("chat_request", lambda e: received.append(e))

        llm = _make_mock_llm()
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        await agent.chat("Hello world")
        assert len(received) == 1
        assert received[0]["agent"] == "test"
        assert "Hello" in received[0]["message"]

    @pytest.mark.asyncio
    async def test_chat_response_event(self):
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("chat_response", lambda e: received.append(e))

        llm = _make_mock_llm(content="Answer")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        await agent.chat("Question")
        assert len(received) == 1
        assert received[0]["agent"] == "test"
        assert received[0]["tokens_used"] == 10

    @pytest.mark.asyncio
    async def test_events_fire_in_order(self):
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        emitter = EventEmitter()
        events = []
        emitter.subscribe_all(lambda e: events.append(e["type"]))

        llm = _make_mock_llm()
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        await agent.chat("Hi")
        assert events[0] == "chat_request"
        assert events[-1] == "chat_response"

    @pytest.mark.asyncio
    async def test_tool_call_events(self):
        """Tool events include tool name and arguments."""
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        from adk.llm import LLMResponse, ToolCall
        emitter = EventEmitter()
        tool_events = []
        emitter.subscribe("tool_call", lambda e: tool_events.append(e))
        emitter.subscribe("tool_result", lambda e: tool_events.append(e))

        # First call returns tool_call, second returns final answer
        tc = MagicMock()
        tc.id = "tc1"
        tc.name = "search"
        tc.arguments = {"query": "test"}
        resp1 = MagicMock()
        resp1.content = ""
        resp1.tool_calls = [tc]
        resp1.tokens_used = 5
        resp1.model = "test"
        resp1.latency_ms = 10

        resp2 = MagicMock()
        resp2.content = "Found results"
        resp2.tool_calls = []
        resp2.tokens_used = 10
        resp2.model = "test"
        resp2.latency_ms = 20

        llm = AsyncMock()
        llm.chat = AsyncMock(side_effect=[resp1, resp2])
        llm.provider_name = "test"

        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        # Register a mock tool
        agent._tools.register(lambda query="": "result", name="search", description="Search")
        await agent.chat("Search for test")
        assert any(e["type"] == "tool_call" and e["tool"] == "search" for e in tool_events)
        assert any(e["type"] == "tool_result" and e["tool"] == "search" for e in tool_events)

    @pytest.mark.asyncio
    async def test_events_nonfatal(self):
        """Agent works if event emission fails."""
        from adk.agent import AitherAgent
        llm = _make_mock_llm(content="Works")
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        broken_emitter = MagicMock()
        broken_emitter.emit = AsyncMock(side_effect=RuntimeError("broken"))
        agent._events = broken_emitter
        resp = await agent.chat("Hello")
        assert resp.content == "Works"


class TestForgeEvents:
    """Events fire during forge.dispatch()."""

    @pytest.mark.asyncio
    async def test_forge_dispatch_event(self):
        from adk.forge import AgentForge, ForgeSpec
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("forge_dispatch", lambda e: received.append(e))

        mock_agent = MagicMock()
        mock_agent.chat = AsyncMock(return_value=MagicMock(
            content="Done", tokens_used=5, tool_calls_made=[], latency_ms=10,
            model="test", session_id="s1",
        ))
        registry = MagicMock()
        registry.get = MagicMock(return_value=mock_agent)
        registry.route = MagicMock(return_value=None)
        forge = AgentForge(registry=registry)
        forge._events = emitter

        spec = ForgeSpec(agent_type="demiurge", task="Write code")
        await forge.dispatch(spec)
        assert len(received) == 1
        assert received[0]["agent"] == "demiurge"

    @pytest.mark.asyncio
    async def test_forge_complete_event(self):
        from adk.forge import AgentForge, ForgeSpec
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("forge_complete", lambda e: received.append(e))

        mock_agent = MagicMock()
        mock_agent.chat = AsyncMock(return_value=MagicMock(
            content="Done", tokens_used=5, tool_calls_made=[], latency_ms=10,
            model="test", session_id="s1",
        ))
        registry = MagicMock()
        registry.get = MagicMock(return_value=mock_agent)
        registry.route = MagicMock(return_value=None)
        forge = AgentForge(registry=registry)
        forge._events = emitter

        spec = ForgeSpec(agent_type="demiurge", task="Write code")
        result = await forge.dispatch(spec)
        assert result.status == "completed"
        assert len(received) == 1
        assert received[0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_forge_events_include_agent_name(self):
        from adk.forge import AgentForge, ForgeSpec
        from adk.events import EventEmitter
        emitter = EventEmitter()
        all_events = []
        emitter.subscribe_all(lambda e: all_events.append(e))

        mock_agent = MagicMock()
        mock_agent.chat = AsyncMock(return_value=MagicMock(
            content="Done", tokens_used=5, tool_calls_made=[], latency_ms=10,
            model="test", session_id="s1",
        ))
        registry = MagicMock()
        registry.get = MagicMock(return_value=mock_agent)
        registry.route = MagicMock(return_value=None)
        forge = AgentForge(registry=registry)
        forge._events = emitter

        spec = ForgeSpec(agent_type="atlas", task="Find services")
        await forge.dispatch(spec)
        for e in all_events:
            assert e.get("agent") == "atlas"

    @pytest.mark.asyncio
    async def test_forge_events_nonfatal(self):
        """Forge works if event emission fails."""
        from adk.forge import AgentForge, ForgeSpec
        mock_agent = MagicMock()
        mock_agent.chat = AsyncMock(return_value=MagicMock(
            content="Done", tokens_used=5, tool_calls_made=[], latency_ms=10,
            model="test", session_id="s1",
        ))
        registry = MagicMock()
        registry.get = MagicMock(return_value=mock_agent)
        registry.route = MagicMock(return_value=None)
        forge = AgentForge(registry=registry)
        broken_emitter = MagicMock()
        broken_emitter.emit = AsyncMock(side_effect=RuntimeError("broken"))
        forge._events = broken_emitter

        spec = ForgeSpec(agent_type="test", task="Normal task")
        result = await forge.dispatch(spec)
        assert result.status == "completed"


class TestEventPayloads:
    """Event payloads contain expected data."""

    @pytest.mark.asyncio
    async def test_chat_request_payload(self):
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("chat_request", lambda e: received.append(e))

        llm = _make_mock_llm()
        agent = AitherAgent(
            name="myagent", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        await agent.chat("Test message")
        event = received[0]
        assert event["type"] == "chat_request"
        assert event["agent"] == "myagent"
        assert "session_id" in event
        assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_chat_response_has_metrics(self):
        from adk.agent import AitherAgent
        from adk.events import EventEmitter
        emitter = EventEmitter()
        received = []
        emitter.subscribe("chat_response", lambda e: received.append(e))

        llm = _make_mock_llm()
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._events = emitter
        await agent.chat("Hi")
        event = received[0]
        assert "tokens_used" in event
        assert "latency_ms" in event
        assert "model" in event
