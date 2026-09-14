"""Tests for graph retrieval interface and in-memory implementation."""

import sys
from pathlib import Path

# Make adk importable in dev
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from adk.graph_retriever import (
    GraphEdge,
    GraphNode,
    InMemoryGraphRetriever,
    Subgraph,
)


@pytest.mark.asyncio
async def test_graphnode_basic():
    """Test GraphNode creation."""
    node = GraphNode(id="fact_1", content="The sky is blue", score=0.95)
    assert node.id == "fact_1"
    assert node.content == "The sky is blue"
    assert node.score == 0.95
    assert node.metadata == {}


@pytest.mark.asyncio
async def test_graphedge_basic():
    """Test GraphEdge creation."""
    edge = GraphEdge(src="fact_1", dst="fact_2", relation="supports", weight=0.8)
    assert edge.src == "fact_1"
    assert edge.dst == "fact_2"
    assert edge.relation == "supports"
    assert edge.weight == 0.8


@pytest.mark.asyncio
async def test_subgraph_empty():
    """Test Subgraph with no nodes."""
    sg = Subgraph(nodes=[], edges=[])
    assert sg.nodes == []
    assert sg.edges == []
    context = sg.to_context()
    assert context == ""


@pytest.mark.asyncio
async def test_subgraph_to_context_single_node():
    """Test to_context with a single node."""
    nodes = [GraphNode(id="n1", content="Fact one", score=0.9)]
    sg = Subgraph(nodes=nodes, edges=[])
    context = sg.to_context(max_chars=1000)
    assert "Facts" in context
    assert "[n1]" in context
    assert "Fact one" in context
    assert "(relevance: 0.90)" in context


@pytest.mark.asyncio
async def test_subgraph_to_context_sorting():
    """Test that to_context sorts nodes by score (highest first)."""
    nodes = [
        GraphNode(id="n1", content="Low score", score=0.3),
        GraphNode(id="n2", content="High score", score=0.9),
        GraphNode(id="n3", content="Medium score", score=0.6),
    ]
    sg = Subgraph(nodes=nodes, edges=[])
    context = sg.to_context(max_chars=1000)
    lines = context.split("\n")
    # Check order: n2 should appear before n3, which appears before n1
    idx_n2 = next(i for i, l in enumerate(lines) if "[n2]" in l)
    idx_n3 = next(i for i, l in enumerate(lines) if "[n3]" in l)
    idx_n1 = next(i for i, l in enumerate(lines) if "[n1]" in l)
    assert idx_n2 < idx_n3 < idx_n1


@pytest.mark.asyncio
async def test_subgraph_to_context_truncation():
    """Test that to_context truncates at max_chars."""
    nodes = [
        GraphNode(id=f"n{i}", content=f"Fact {i}" * 50, score=float(i) / 10)
        for i in range(10)
    ]
    sg = Subgraph(nodes=nodes, edges=[])
    context = sg.to_context(max_chars=100)
    assert len(context) <= 120  # Some slack for the truncation marker
    assert "(truncated)" in context


@pytest.mark.asyncio
async def test_subgraph_to_context_with_edges():
    """Test to_context includes edges."""
    nodes = [
        GraphNode(id="n1", content="Fact one", score=0.9),
        GraphNode(id="n2", content="Fact two", score=0.8),
    ]
    edges = [
        GraphEdge(src="n1", dst="n2", relation="supports", weight=0.7),
    ]
    sg = Subgraph(nodes=nodes, edges=edges)
    context = sg.to_context(max_chars=1000)
    assert "Relations" in context
    assert "n1" in context and "n2" in context
    assert "supports" in context


@pytest.mark.asyncio
async def test_subgraph_to_context_edge_dedup():
    """Test that to_context deduplicates edges."""
    nodes = [
        GraphNode(id="n1", content="Fact one", score=0.9),
        GraphNode(id="n2", content="Fact two", score=0.8),
    ]
    edges = [
        GraphEdge(src="n1", dst="n2", relation="supports", weight=0.7),
        GraphEdge(src="n1", dst="n2", relation="supports", weight=0.7),
    ]
    sg = Subgraph(nodes=nodes, edges=edges)
    context = sg.to_context(max_chars=1000)
    # Count how many times the edge appears
    count = context.count("n1 --[supports]--> n2")
    assert count == 1, "Edge should appear exactly once (deduplicated)"


@pytest.mark.asyncio
async def test_in_memory_retriever_empty():
    """Test InMemoryGraphRetriever with no data."""
    retriever = InMemoryGraphRetriever()
    sg = await retriever.subgraph_search("query")
    assert sg.nodes == []
    assert sg.edges == []


@pytest.mark.asyncio
async def test_in_memory_retriever_seed_by_substring():
    """Test seed search by substring match."""
    retriever = InMemoryGraphRetriever()
    retriever.add_node(GraphNode(id="n1", content="The cat sat on the mat", score=0.9))
    retriever.add_node(GraphNode(id="n2", content="The dog ran away", score=0.7))
    retriever.add_node(GraphNode(id="n3", content="A bird flew overhead", score=0.5))

    sg = await retriever.subgraph_search("cat", k_seeds=5)
    assert len(sg.nodes) == 1
    assert sg.nodes[0].id == "n1"


@pytest.mark.asyncio
async def test_in_memory_retriever_case_insensitive():
    """Test that substring matching is case-insensitive."""
    retriever = InMemoryGraphRetriever()
    retriever.add_node(GraphNode(id="n1", content="The Cat Sat", score=0.9))
    retriever.add_node(GraphNode(id="n2", content="dog", score=0.7))

    sg = await retriever.subgraph_search("CAT", k_seeds=5)
    assert len(sg.nodes) == 1
    assert sg.nodes[0].id == "n1"


@pytest.mark.asyncio
async def test_in_memory_retriever_k_seeds():
    """Test k_seeds limit."""
    retriever = InMemoryGraphRetriever()
    for i in range(10):
        retriever.add_node(GraphNode(id=f"n{i}", content="test", score=0.9 - i * 0.05))

    sg = await retriever.subgraph_search("test", k_seeds=3)
    assert len(sg.nodes) == 3
    # Should be the three highest scores
    node_ids = {n.id for n in sg.nodes}
    assert node_ids == {"n0", "n1", "n2"}


@pytest.mark.asyncio
async def test_in_memory_retriever_expansion_1hop():
    """Test 1-hop expansion."""
    retriever = InMemoryGraphRetriever()
    # Create a seed node and some neighbors
    retriever.add_node(GraphNode(id="seed", content="seed node", score=0.9))
    retriever.add_node(GraphNode(id="n1", content="neighbor 1", score=0.5))
    retriever.add_node(GraphNode(id="n2", content="neighbor 2", score=0.5))
    retriever.add_edge(GraphEdge(src="seed", dst="n1", relation="related"))
    retriever.add_edge(GraphEdge(src="seed", dst="n2", relation="related"))

    sg = await retriever.subgraph_search("seed", k_seeds=1, k_hops=1, limit=10)
    assert len(sg.nodes) == 3
    node_ids = {n.id for n in sg.nodes}
    assert node_ids == {"seed", "n1", "n2"}


@pytest.mark.asyncio
async def test_in_memory_retriever_expansion_2hops():
    """Test 2-hop expansion."""
    retriever = InMemoryGraphRetriever()
    # seed -> n1 -> n3
    # seed -> n2
    retriever.add_node(GraphNode(id="seed", content="seed", score=0.9))
    retriever.add_node(GraphNode(id="n1", content="n1", score=0.5))
    retriever.add_node(GraphNode(id="n2", content="n2", score=0.5))
    retriever.add_node(GraphNode(id="n3", content="n3", score=0.3))
    retriever.add_edge(GraphEdge(src="seed", dst="n1"))
    retriever.add_edge(GraphEdge(src="seed", dst="n2"))
    retriever.add_edge(GraphEdge(src="n1", dst="n3"))

    sg = await retriever.subgraph_search("seed", k_seeds=1, k_hops=2, limit=10)
    assert len(sg.nodes) == 4
    node_ids = {n.id for n in sg.nodes}
    assert node_ids == {"seed", "n1", "n2", "n3"}


@pytest.mark.asyncio
async def test_in_memory_retriever_limit():
    """Test that limit caps the number of nodes."""
    retriever = InMemoryGraphRetriever()
    # Create a seed and many neighbors
    retriever.add_node(GraphNode(id="seed", content="seed", score=0.9))
    for i in range(20):
        retriever.add_node(GraphNode(id=f"n{i}", content=f"n{i}", score=0.5))
        retriever.add_edge(GraphEdge(src="seed", dst=f"n{i}"))

    sg = await retriever.subgraph_search("seed", k_seeds=1, k_hops=1, limit=5)
    assert len(sg.nodes) <= 5


@pytest.mark.asyncio
async def test_in_memory_retriever_deduplication():
    """Test that nodes are not duplicated in result."""
    retriever = InMemoryGraphRetriever()
    retriever.add_node(GraphNode(id="seed", content="seed", score=0.9))
    retriever.add_node(GraphNode(id="n1", content="n1", score=0.5))
    # Both seed and n1 already exist in seeds
    retriever.add_edge(GraphEdge(src="seed", dst="n1"))
    retriever.add_edge(GraphEdge(src="n1", dst="seed"))

    sg = await retriever.subgraph_search("seed", k_seeds=2, k_hops=1, limit=10)
    # Should have seed + n1, not duplicates
    node_ids = {n.id for n in sg.nodes}
    assert len(node_ids) == len(sg.nodes), "Nodes should be deduplicated"


@pytest.mark.asyncio
async def test_in_memory_retriever_no_orphan_edges():
    """Test that only edges within the result set are included."""
    retriever = InMemoryGraphRetriever()
    retriever.add_node(GraphNode(id="seed", content="seed", score=0.9))
    retriever.add_node(GraphNode(id="n1", content="n1", score=0.5))
    retriever.add_node(GraphNode(id="orphan", content="orphan", score=0.3))
    retriever.add_edge(GraphEdge(src="seed", dst="n1"))
    retriever.add_edge(GraphEdge(src="orphan", dst="seed"))

    # Limit to 2 nodes (seed + n1), exclude orphan
    sg = await retriever.subgraph_search("seed", k_seeds=1, k_hops=1, limit=2)
    # Should only have seed->n1 edge, not the orphan->seed edge
    for edge in sg.edges:
        assert edge.src in {n.id for n in sg.nodes}
        assert edge.dst in {n.id for n in sg.nodes}
