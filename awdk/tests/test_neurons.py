"""Tests for neuron architecture — auto-firing context gathering."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.neurons import (
    AutoNeuronFire,
    BaseNeuron,
    GraphNeuron,
    MemoryNeuron,
    NeuronPool,
    NeuronResult,
    WebSearchNeuron,
    _AUTO_PATTERNS,
)


# ─── WebSearchNeuron ──────────────────────────────────────────────────────────

class TestWebSearchNeuron:
    @pytest.mark.asyncio
    async def test_fire_returns_results(self):
        neuron = WebSearchNeuron(limit=2)
        mock_results = json.dumps({
            "query": "test",
            "results": [
                {"title": "Result 1", "url": "http://a.com", "snippet": "Snippet 1"},
                {"title": "Result 2", "url": "http://b.com", "snippet": "Snippet 2"},
            ],
        })
        with patch("adk.builtin_tools.web_search", new=AsyncMock(return_value=mock_results)):
            result = await neuron.fire("test query")
        assert result.neuron == "web_search"
        assert "Result 1" in result.content
        assert result.relevance > 0

    @pytest.mark.asyncio
    async def test_fire_handles_error(self):
        neuron = WebSearchNeuron()
        with patch("adk.builtin_tools.web_search", new=AsyncMock(return_value='{"error": "failed"}')):
            result = await neuron.fire("test")
        assert result.content == ""
        assert result.relevance == 0.0

    @pytest.mark.asyncio
    async def test_fire_handles_import_error(self):
        neuron = WebSearchNeuron()
        with patch("adk.builtin_tools.web_search", side_effect=ImportError("no module")):
            result = await neuron.fire("test")
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_fire_handles_exception(self):
        neuron = WebSearchNeuron()
        with patch("adk.builtin_tools.web_search", new=AsyncMock(side_effect=Exception("timeout"))):
            result = await neuron.fire("test")
        assert result.content == ""


# ─── MemoryNeuron ─────────────────────────────────────────────────────────────

class TestMemoryNeuron:
    @pytest.mark.asyncio
    async def test_fire_with_results(self):
        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search = AsyncMock(return_value=["fact 1", "fact 2"])
        neuron = MemoryNeuron(agent=agent)
        result = await neuron.fire("recall something")
        assert "fact 1" in result.content
        assert result.relevance > 0

    @pytest.mark.asyncio
    async def test_fire_no_agent(self):
        neuron = MemoryNeuron(agent=None)
        result = await neuron.fire("test")
        assert result.content == ""
        assert result.relevance == 0.0

    @pytest.mark.asyncio
    async def test_fire_empty_results(self):
        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search = AsyncMock(return_value=[])
        neuron = MemoryNeuron(agent=agent)
        result = await neuron.fire("test")
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_fire_exception_nonfatal(self):
        agent = MagicMock()
        agent.memory = MagicMock()
        agent.memory.search = AsyncMock(side_effect=Exception("db error"))
        neuron = MemoryNeuron(agent=agent)
        result = await neuron.fire("test")
        assert result.content == ""


# ─── GraphNeuron ──────────────────────────────────────────────────────────────

class TestGraphNeuron:
    @pytest.mark.asyncio
    async def test_fire_with_results(self):
        agent = MagicMock()
        node = MagicMock()
        node.label = "AitherOS"
        node.content = "An AI operating system"
        agent._graph = MagicMock()
        agent._graph.search = AsyncMock(return_value=[node])
        neuron = GraphNeuron(agent=agent)
        result = await neuron.fire("what is AitherOS")
        assert "AitherOS" in result.content
        assert result.relevance > 0

    @pytest.mark.asyncio
    async def test_fire_no_graph(self):
        agent = MagicMock()
        agent._graph = None
        neuron = GraphNeuron(agent=agent)
        result = await neuron.fire("test")
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_fire_no_agent(self):
        neuron = GraphNeuron(agent=None)
        result = await neuron.fire("test")
        assert result.content == ""


# ─── NeuronPool ───────────────────────────────────────────────────────────────

class TestNeuronPool:
    def test_default_neurons(self):
        pool = NeuronPool()
        assert "web_search" in pool.neurons
        assert "memory" in pool.neurons
        assert "graph" in pool.neurons

    def test_register_unregister(self):
        pool = NeuronPool()

        class CustomNeuron(BaseNeuron):
            name = "custom"
            async def fire(self, query, **kwargs):
                return NeuronResult(neuron="custom", content="custom result")

        pool.register(CustomNeuron())
        assert "custom" in pool.neurons
        pool.unregister("custom")
        assert "custom" not in pool.neurons

    @pytest.mark.asyncio
    async def test_fire_specific(self):
        pool = NeuronPool()
        mock_results = json.dumps({
            "query": "test",
            "results": [{"title": "R1", "url": "http://x.com", "snippet": "S1"}],
        })
        with patch("adk.builtin_tools.web_search", new=AsyncMock(return_value=mock_results)):
            results = await pool.fire(["web_search"], "test query")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_fire_nonexistent_neuron(self):
        pool = NeuronPool()
        results = await pool.fire(["doesnt_exist"], "test")
        assert results == []

    @pytest.mark.asyncio
    async def test_fire_empty_list(self):
        pool = NeuronPool()
        results = await pool.fire([], "test")
        assert results == []

    @pytest.mark.asyncio
    async def test_fire_all(self):
        pool = NeuronPool()
        # All neurons will fail (no real services) but shouldn't crash
        results = await pool.fire_all("test")
        # Results may be empty since no real backends
        assert isinstance(results, list)

    def test_stats(self):
        pool = NeuronPool()
        stats = pool.stats()
        assert "registered" in stats
        assert "total_fires" in stats
        assert len(stats["registered"]) == 7  # web_search, web, memory, graph, tool/agent/service inventory


# ─── AutoNeuronFire ───────────────────────────────────────────────────────────

class TestAutoNeuronFire:
    def test_detect_web_search(self):
        auto = AutoNeuronFire()
        neurons = auto.detect_neurons("search for the latest AI news")
        assert "web_search" in neurons

    def test_detect_memory(self):
        auto = AutoNeuronFire()
        neurons = auto.detect_neurons("do you remember what we discussed last time?")
        assert "memory" in neurons

    def test_detect_graph(self):
        auto = AutoNeuronFire()
        neurons = auto.detect_neurons("what is related to memory?")
        assert "graph" in neurons

    def test_detect_combined(self):
        auto = AutoNeuronFire()
        neurons = auto.detect_neurons("what is the current status of AitherOS?")
        assert len(neurons) >= 1

    def test_detect_none(self):
        auto = AutoNeuronFire()
        neurons = auto.detect_neurons("hello")
        # No patterns match, no graph = empty
        assert isinstance(neurons, list)

    @pytest.mark.asyncio
    async def test_gather_context_with_results(self):
        agent = MagicMock()
        agent._graph = MagicMock()
        node = MagicMock()
        node.label = "Test"
        node.content = "Test content"
        agent._graph.search = AsyncMock(return_value=[node])
        agent.memory = MagicMock()
        agent.memory.search = AsyncMock(return_value=["memory result"])

        auto = AutoNeuronFire(agent=agent)
        context = await auto.gather_context("remember what we discussed about related topics")
        # Should have content from memory and/or graph
        assert isinstance(context, str)

    @pytest.mark.asyncio
    async def test_gather_context_empty(self):
        auto = AutoNeuronFire()
        context = await auto.gather_context("hello there")
        # No patterns match and no agent = empty
        assert context == ""

    @pytest.mark.asyncio
    async def test_gather_context_caching(self):
        """A repeated query is served from cache rather than re-fired.

        The pool is INJECTED rather than exercised. Driving the real pipeline
        made this assertion depend on a 10s `asyncio.wait_for` and two
        `except Exception` handlers that return an empty NeuronResult — and
        `gather_context` does not cache an empty result, so the second call
        re-fires. It passed alone and failed in the full 4,113-test run,
        reporting a caching bug that was really a timing accident.
        """
        from adk.neurons import NeuronResult

        agent = MagicMock()
        agent._graph = MagicMock()

        pool = MagicMock()
        pool.fire = AsyncMock(return_value=[
            NeuronResult(neuron="graph", content="Cached content", relevance=0.9),
        ])

        auto = AutoNeuronFire(agent=agent, pool=pool)
        ctx1 = await auto.gather_context("what is related to test?")
        ctx2 = await auto.gather_context("what is related to test?")

        assert ctx1 == ctx2
        assert "Cached content" in ctx1
        # The whole point: the second call never reached the pool.
        assert pool.fire.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_result_is_not_cached(self):
        """An empty result is recomputed, not remembered.

        Documented because it is the behaviour that made the test above
        fragile: a neuron that returns nothing leaves the cache untouched, so
        the next identical query fires again. Asserting it means a future
        change to that policy is a visible decision rather than a surprise.
        """
        agent = MagicMock()
        agent._graph = MagicMock()

        pool = MagicMock()
        pool.fire = AsyncMock(return_value=[])

        auto = AutoNeuronFire(agent=agent, pool=pool)
        assert await auto.gather_context("same question") == ""
        assert await auto.gather_context("same question") == ""
        assert pool.fire.call_count == 2

# ─── Agent Integration ────────────────────────────────────────────────────────

class TestAgentNeuronIntegration:
    def test_auto_neurons_initialized(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(name="test", llm=llm, builtin_tools=False, system_prompt="System")
        assert agent._auto_neurons is not None

    def test_auto_neurons_has_pool(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(name="test", llm=llm, builtin_tools=False, system_prompt="System")
        if agent._auto_neurons:
            assert hasattr(agent._auto_neurons, 'pool')


# ─── Export ────────────────────────────────────────────────────────────────────

class TestExport:
    def test_exports(self):
        import adk
        assert hasattr(adk, "NeuronPool")
        assert hasattr(adk, "AutoNeuronFire")
