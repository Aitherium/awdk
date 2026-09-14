"""Tests for AitherAgent."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.agent import AitherAgent, AgentResponse
from adk.config import Config
from adk.identity import Identity
from adk.llm.base import LLMResponse, Message, ToolCall
from adk.tools import ToolRegistry
from adk.memory import Memory


@pytest.fixture
def mock_llm():
    """Create a mock LLM router."""
    router = MagicMock()
    router.provider_name = "mock"
    router.chat = AsyncMock(return_value=LLMResponse(
        content="Mock response",
        model="mock-model",
        tokens_used=10,
        latency_ms=50.0,
    ))
    return router


@pytest.fixture
def tmp_memory(tmp_path):
    return Memory(db_path=tmp_path / "test.db", agent_name="test")


class TestAgentCreation:
    def test_create_with_name(self):
        agent = AitherAgent("test_agent")
        assert agent.name == "test_agent"

    def test_create_with_identity_string(self):
        agent = AitherAgent(identity="aither")
        assert agent.name == "aither"

    def test_create_with_identity_object(self):
        ident = Identity(name="custom", description="A custom agent")
        agent = AitherAgent(identity=ident)
        assert agent.name == "custom"

    def test_create_with_system_prompt(self):
        agent = AitherAgent("test", system_prompt="You are helpful.")
        assert agent.system_prompt == "You are helpful."

    def test_system_prompt_from_identity(self):
        agent = AitherAgent(identity="aither")
        assert "aither" in agent.system_prompt.lower()

    def test_custom_config(self):
        cfg = Config()
        cfg.llm_backend = "openai"
        agent = AitherAgent("test", config=cfg)
        assert agent.config.llm_backend == "openai"


class TestAgentChat:
    @pytest.mark.asyncio
    async def test_simple_chat(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        resp = await agent.chat("Hello")
        assert resp.content == "Mock response"
        assert resp.model == "mock-model"
        mock_llm.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_stores_history(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        await agent.chat("Hello", session_id="s1")
        history = await tmp_memory.get_history("s1")
        assert len(history) == 2  # user + assistant
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_chat_with_explicit_history(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        agent._graph = None  # Disable graph injection for message count test
        agent._auto_neurons = None  # Disable neuron injection
        history = [
            {"role": "user", "content": "prev msg"},
            {"role": "assistant", "content": "prev resp"},
        ]
        await agent.chat("New message", history=history)
        # Should include system + history + new message
        call_args = mock_llm.chat.call_args
        messages = call_args[0][0]
        assert len(messages) == 4  # system + 2 history + user

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, tmp_memory):
        # First call returns tool call, second call returns final response
        tool_call_resp = LLMResponse(
            content="",
            model="mock",
            tool_calls=[ToolCall(id="tc_1", name="search", arguments={"q": "test"})],
        )
        final_resp = LLMResponse(content="Found results!", model="mock")

        mock_llm = MagicMock()
        mock_llm.provider_name = "mock"
        mock_llm.chat = AsyncMock(side_effect=[tool_call_resp, final_resp])

        tools = ToolRegistry()
        tools.register(lambda q: f"Results for {q}", name="search", description="Search")

        agent = AitherAgent("test", llm=mock_llm, tools=[tools], memory=tmp_memory)
        resp = await agent.chat("Search for test")
        assert resp.content == "Found results!"
        assert "search" in resp.tool_calls_made


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_run_wraps_task(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        resp = await agent.run("Build a REST API")
        assert resp.content == "Mock response"
        # The task message may be in the first or second call (steering retry)
        all_messages = []
        for call in mock_llm.chat.call_args_list:
            all_messages.extend(call[0][0])
        assert any("Build a REST API" in m.content for m in all_messages)


class TestAgentMemory:
    @pytest.mark.asyncio
    async def test_remember_and_recall(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        await agent.remember("key1", "value1")
        result = await agent.recall("key1")
        assert result == "value1"

    @pytest.mark.asyncio
    async def test_recall_missing(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)
        result = await agent.recall("nonexistent")
        assert result is None


class TestAgentSession:
    def test_new_session(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        old_session = agent._session_id
        new_session = agent.new_session()
        assert new_session != old_session


class TestAgentSwarm:
    @pytest.mark.asyncio
    async def test_swarm_success(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "completed",
            "architect_plan": "Plan here",
            "code": "print('hello')",
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent.swarm("Build a feature")
            assert result["status"] == "completed"
            assert "Plan here" in result["architect_plan"]

    @pytest.mark.asyncio
    async def test_swarm_timeout(self, mock_llm):
        import httpx
        agent = AitherAgent("test", llm=mock_llm)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent.swarm("task", max_seconds=10)
            assert result["status"] == "failed"
            assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_swarm_connection_error(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent.swarm("task")
            assert result["status"] == "failed"
            assert "Connection refused" in result["error"]

    @pytest.mark.asyncio
    async def test_swarm_http_error(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent.swarm("task")
            assert result["status"] == "failed"
            assert "500" in result["error"]


class TestAgentCodeSearch:
    @pytest.mark.asyncio
    async def test_code_search_returns_list(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        mock_result = '{"results": [{"file": "a.py", "symbol": "foo", "snippet": "def foo():", "score": 0.9}], "count": 1, "source": "repowise"}'
        with patch("adk.builtin_tools.repowise_search", return_value=mock_result):
            results = await agent.code_search("foo function")
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["file"] == "a.py"

    @pytest.mark.asyncio
    async def test_code_search_fallback_on_parse_error(self, mock_llm):
        agent = AitherAgent("test", llm=mock_llm)
        with patch("adk.builtin_tools.repowise_search", return_value="not json"):
            results = await agent.code_search("query")
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["snippet"] == "not json"


class TestAgentToolDecorator:
    @pytest.mark.asyncio
    async def test_tool_decorator(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)

        @agent.tool
        def greet(name: str) -> str:
            """Say hello."""
            return f"Hello {name}"

        assert agent._tools.get("greet") is not None

    @pytest.mark.asyncio
    async def test_tool_decorator_with_args(self, mock_llm, tmp_memory):
        agent = AitherAgent("test", llm=mock_llm, memory=tmp_memory)

        @agent.tool(name="custom", description="Custom greeting")
        def greet(name: str) -> str:
            return f"Hi {name}"

        assert agent._tools.get("custom") is not None
