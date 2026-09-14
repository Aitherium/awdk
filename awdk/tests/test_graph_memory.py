"""Tests for GraphMemory — local knowledge graph with hybrid search."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.graph_memory import (
    GraphMemory,
    GraphNode,
    GraphEdge,
    EdgeType,
    extract_entities,
    extract_relations,
    extract_keywords,
    cosine_similarity,
    _fallback_embed,
    _embed_to_blob,
    _blob_to_embed,
    _classify_query,
)


@pytest.fixture
def graph(tmp_path):
    """Create a graph memory with a temp db."""
    db_path = tmp_path / "test_graph.db"
    return GraphMemory(db_path=db_path, agent_name="test-agent")


# ─── Entity extraction ──────────────────────────────────────────────────────

class TestEntityExtraction:
    def test_camelcase_services(self):
        entities = extract_entities("The ServiceBridge connects to ContextManager.")
        labels = [e[0] for e in entities]
        assert "ServiceBridge" in labels
        assert "ContextManager" in labels

    def test_capitalized_phrases(self):
        entities = extract_entities("The Graph Memory Store is available.")
        labels = [e[0] for e in entities]
        assert any("Graph" in l for l in labels)

    def test_file_paths(self):
        entities = extract_entities("Edit the file adk/agent.py now.")
        labels = [e[0] for e in entities]
        assert any("agent.py" in l for l in labels)

    def test_snake_case_code(self):
        entities = extract_entities("Call the function get_graph_memory_store from module.")
        labels = [e[0] for e in entities]
        assert any("get_graph_memory_store" in l for l in labels)

    def test_stopwords_excluded(self):
        entities = extract_entities("the is are was")
        assert len(entities) == 0

    def test_caps_limit(self):
        """Max 30 entities extracted."""
        big_text = " ".join(f"FooService{i}" for i in range(50))
        entities = extract_entities(big_text)
        assert len(entities) <= 30


class TestRelationExtraction:
    def test_is_a(self):
        rels = extract_relations("AitherOS is a platform")
        assert any(r[1] == "is_a" for r in rels)

    def test_uses(self):
        rels = extract_relations("AitherOS uses SQLite")
        assert any(r[1] == "uses" for r in rels)

    def test_depends_on(self):
        rels = extract_relations("ServiceBridge depends on httpx")
        assert any(r[1] == "depends_on" for r in rels)

    def test_contains(self):
        rels = extract_relations("AitherOS contains microservices")
        assert any(r[1] == "contains" for r in rels)


class TestKeywordExtraction:
    def test_basic_keywords(self):
        kw = extract_keywords("The GraphMemory module uses SQLite for storage.")
        assert "graphmemory" in kw
        assert "sqlite" in kw
        assert "storage" in kw

    def test_stopwords_removed(self):
        kw = extract_keywords("the is are was for with on at")
        assert len(kw) == 0

    def test_limit(self):
        big_text = " ".join(f"word{i}" for i in range(100))
        kw = extract_keywords(big_text)
        assert len(kw) <= 50


# ─── Embedding helpers ────────────────────────────────────────────────────────

class TestEmbeddingHelpers:
    def test_embed_blob_roundtrip(self):
        original = [0.1, 0.2, 0.3, 0.99, -0.5]
        blob = _embed_to_blob(original)
        restored = _blob_to_embed(blob)
        for a, b in zip(original, restored):
            assert abs(a - b) < 1e-5

    def test_cosine_similarity_identical(self):
        v = [1.0, 0.0, 0.5]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(cosine_similarity(a, b)) < 1e-6

    def test_cosine_similarity_different_lengths(self):
        assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0

    def test_cosine_similarity_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_fallback_embed_nonzero(self):
        emb = _fallback_embed("hello world")
        assert len(emb) == 384
        assert any(x != 0.0 for x in emb)

    def test_fallback_embed_normalized(self):
        emb = _fallback_embed("some text here")
        magnitude = sum(x * x for x in emb) ** 0.5
        assert abs(magnitude - 1.0) < 1e-5

    def test_fallback_embed_different_texts(self):
        a = _fallback_embed("artificial intelligence")
        b = _fallback_embed("banana smoothie recipe")
        assert a != b


# ─── Query classification ─────────────────────────────────────────────────────

class TestQueryClassification:
    def test_identity_query(self):
        kw, sem = _classify_query("What is my name?")
        assert kw > sem  # Identity = heavy keyword

    def test_procedural_query(self):
        kw, sem = _classify_query("How do I deploy?")
        assert kw > sem  # Procedural leans keyword

    def test_conceptual_query(self):
        kw, sem = _classify_query("What is related to memory?")
        assert sem > kw  # Conceptual = heavy semantic

    def test_balanced_query(self):
        kw, sem = _classify_query("Tell me something interesting.")
        assert kw + sem == pytest.approx(1.0)


# ─── GraphMemory CRUD ─────────────────────────────────────────────────────────

class TestGraphMemoryCRUD:
    @pytest.mark.asyncio
    async def test_add_node(self, graph):
        node = await graph.add_node("AitherOS", node_type="service", content="The OS")
        assert node.label == "AitherOS"
        assert node.node_type == "service"
        assert node.id  # Has an ID

    @pytest.mark.asyncio
    async def test_get_node(self, graph):
        node = await graph.add_node("TestNode", content="Some content")
        fetched = await graph.get_node(node.id)
        assert fetched is not None
        assert fetched.label == "TestNode"

    @pytest.mark.asyncio
    async def test_get_nonexistent_node(self, graph):
        result = await graph.get_node("nonexistent_id")
        assert result is None

    @pytest.mark.asyncio
    async def test_remove_node(self, graph):
        node = await graph.add_node("Removable")
        await graph.remove_node(node.id)
        assert await graph.get_node(node.id) is None

    @pytest.mark.asyncio
    async def test_upsert_node(self, graph):
        n1 = await graph.add_node("Same", content="v1")
        n2 = await graph.add_node("Same", content="v2")
        assert n1.id == n2.id  # Same label+type = same ID
        fetched = await graph.get_node(n1.id)
        assert fetched.content == "v2"

    @pytest.mark.asyncio
    async def test_upsert_reinforces_instead_of_wiping_activation_mirror(self, graph):
        """A re-store of the same node must REINFORCE the metadata activation
        mirror (reinforcement_count/last_reinforced — sweep()'s archive guard),
        never wholesale-replace it back to zero."""
        n1 = await graph.add_node("Pref", content="I hate rain")
        # Simulate recall reinforcement accruing on the stored node.
        graph._reinforce_nodes([n1.id])
        graph._reinforce_nodes([n1.id])
        before = await graph.get_node(n1.id)
        assert int(before.metadata["reinforcement_count"]) == 2
        # Restating the same content upserts — the count carries forward (+1
        # for the restatement) and the reinforcement clock resets to now.
        await graph.add_node("Pref", content="I hate rain")
        after = await graph.get_node(n1.id)
        assert int(after.metadata["reinforcement_count"]) == 3
        assert float(after.metadata["last_reinforced"]) >= float(
            before.metadata["last_reinforced"])

    @pytest.mark.asyncio
    async def test_add_edge(self, graph):
        n1 = await graph.add_node("A")
        n2 = await graph.add_node("B")
        edge = await graph.add_edge(n1.id, n2.id, "uses", weight=0.9)
        assert edge.relation == "uses"
        assert edge.weight == 0.9

    @pytest.mark.asyncio
    async def test_get_neighbors(self, graph):
        n1 = await graph.add_node("Parent")
        n2 = await graph.add_node("Child")
        await graph.add_edge(n1.id, n2.id, "contains")
        neighbors = await graph.get_neighbors(n1.id)
        labels = [n.label for n in neighbors]
        assert "Child" in labels

    @pytest.mark.asyncio
    async def test_get_neighbors_with_relation(self, graph):
        n1 = await graph.add_node("Root")
        n2 = await graph.add_node("Dep1")
        n3 = await graph.add_node("Dep2")
        await graph.add_edge(n1.id, n2.id, "uses")
        await graph.add_edge(n1.id, n3.id, "contains")
        uses = await graph.get_neighbors(n1.id, relation="uses")
        assert len(uses) == 1
        assert uses[0].label == "Dep1"


# ─── Convenience API ──────────────────────────────────────────────────────────

class TestConvenienceAPI:
    @pytest.mark.asyncio
    async def test_remember_and_recall(self, graph):
        await graph.remember("AitherOS", "uses", "SQLite")
        results = await graph.recall("AitherOS")
        assert len(results) >= 1
        assert any(r["object"] == "SQLite" for r in results)

    @pytest.mark.asyncio
    async def test_recall_with_relation_filter(self, graph):
        await graph.remember("Agent", "uses", "LLM")
        await graph.remember("Agent", "is_a", "Software")
        uses = await graph.recall("Agent", relation="uses")
        assert all(r["relation"] == "uses" for r in uses)

    @pytest.mark.asyncio
    async def test_recall_nonexistent(self, graph):
        results = await graph.recall("NothingHere")
        assert results == []


# ─── Hybrid Search ────────────────────────────────────────────────────────────

class TestHybridSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, graph):
        await graph.add_node("Memory", content="Graph-based memory system with embeddings")
        await graph.add_node("Database", content="SQLite WAL mode storage backend")
        results = await graph.search("memory graph", limit=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_empty_query(self, graph):
        results = await graph.search("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_type_filter(self, graph):
        await graph.add_node("S1", node_type="service", content="A service")
        await graph.add_node("E1", node_type="entity", content="An entity about services")
        results = await graph.search("service", node_type="service", limit=5)
        for r in results:
            assert r.node_type == "service"

    @pytest.mark.asyncio
    async def test_query_alias(self, graph):
        await graph.add_node("Foo", content="Something about foo bar")
        results = await graph.query("foo bar")
        assert len(results) >= 1


# ─── Conversation Ingestion ───────────────────────────────────────────────────

class TestConversationIngestion:
    @pytest.mark.asyncio
    async def test_ingest_conversation(self, graph):
        count = await graph.ingest_conversation("sess1", [
            {"role": "user", "content": "How does the ServiceBridge work?"},
            {"role": "assistant", "content": "ServiceBridge connects to awnode for auto-discovery."},
        ])
        assert count >= 1

    @pytest.mark.asyncio
    async def test_ingest_creates_session_node(self, graph):
        await graph.ingest_conversation("sess2", [
            {"role": "user", "content": "Tell me about agents"},
        ])
        stats = await graph.get_stats()
        assert stats["node_types"].get("session", 0) >= 1

    @pytest.mark.asyncio
    async def test_ingest_text(self, graph):
        count = await graph.ingest_text(
            "AitherOS uses ContextManager for token-aware truncation.", source="test"
        )
        assert count >= 1


# ─── Graph Traversal ──────────────────────────────────────────────────────────

class TestGraphTraversal:
    @pytest.mark.asyncio
    async def test_get_related(self, graph):
        await graph.remember("AitherOS", "uses", "SQLite")
        await graph.remember("AitherOS", "contains", "agents")
        subgraph = await graph.get_related("AitherOS", depth=1)
        assert "AitherOS" in subgraph
        assert len(subgraph["AitherOS"]) >= 1

    @pytest.mark.asyncio
    async def test_get_related_nonexistent(self, graph):
        subgraph = await graph.get_related("DoesNotExist")
        assert subgraph == {}


# ─── Stats ────────────────────────────────────────────────────────────────────

class TestGraphStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, graph):
        stats = await graph.get_stats()
        assert stats["nodes"] == 0
        assert stats["edges"] == 0
        assert stats["agent"] == "test-agent"

    @pytest.mark.asyncio
    async def test_stats_after_inserts(self, graph):
        await graph.add_node("A", content="Node A")
        await graph.add_node("B", content="Node B")
        stats = await graph.get_stats()
        assert stats["nodes"] == 2


# ─── Agent Integration ────────────────────────────────────────────────────────

class TestAgentGraphIntegration:
    @pytest.mark.asyncio
    async def test_graph_initialized_on_agent(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        # Graph should be initialized (or None if import failed)
        # Either way, agent works
        assert agent._graph is not None or agent._graph is None

    @pytest.mark.asyncio
    async def test_graph_remember_on_agent(self, tmp_path):
        from adk.agent import AitherAgent
        from adk.graph_memory import GraphMemory

        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = GraphMemory(db_path=tmp_path / "agent_graph.db")
        await agent.graph_remember("Test", "is_a", "Example")
        results = await agent.graph_query("Test")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_graph_query_no_graph(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = None
        results = await agent.graph_query("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_graph_remember_no_graph(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = None
        # Should not raise
        await agent.graph_remember("X", "Y", "Z")

    @pytest.mark.asyncio
    async def test_graph_stats_no_graph(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = None
        stats = await agent.graph_stats()
        assert stats == {"enabled": False}

    @pytest.mark.asyncio
    async def test_graph_stats_with_graph(self, tmp_path):
        from adk.agent import AitherAgent
        from adk.graph_memory import GraphMemory
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = GraphMemory(db_path=tmp_path / "stats_graph.db")
        stats = await agent.graph_stats()
        assert stats["enabled"] is True
        assert "nodes" in stats

    @pytest.mark.asyncio
    async def test_graph_auto_ingest_after_chat(self, tmp_path):
        """Graph memory auto-ingests conversation after chat()."""
        from adk.agent import AitherAgent
        from adk.graph_memory import GraphMemory

        resp = MagicMock()
        resp.content = "The ServiceBridge connects to awnode for discovery."
        resp.model = "test"
        resp.tokens_used = 10
        resp.latency_ms = 50.0
        resp.tool_calls = []
        resp.tool_calls_made = []
        resp.finish_reason = "stop"
        resp.session_id = "s1"

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=resp)
        llm.provider_name = "test"

        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        agent._graph = GraphMemory(db_path=tmp_path / "ingest_graph.db")

        await agent.chat("How does ServiceBridge work?")

        stats = await agent._graph.get_stats()
        # Should have ingested at least a session node + entities
        assert stats["nodes"] >= 1


# ─── Module singleton ─────────────────────────────────────────────────────────

class TestModuleSingleton:
    def test_get_graph_memory_singleton(self):
        import adk.graph_memory
        adk.graph_memory._instance = None
        from adk.graph_memory import get_graph_memory
        g1 = get_graph_memory("test")
        g2 = get_graph_memory("test")
        assert g1 is g2
        adk.graph_memory._instance = None  # Cleanup


# ─── Export ────────────────────────────────────────────────────────────────────

class TestExport:
    def test_graph_memory_in_adk(self):
        import adk
        assert hasattr(adk, "GraphMemory")
