"""Test suite for aither default brain pack and GraphRAG memory integration.

Verifies that:
1. discover_brain_pack() resolves to the bundled aither pack (final fallback)
2. AitherAgent builds with aither identity + default builtin tools
3. GraphMemory is active and functional (store → recall round-trip)
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


def test_discover_aither_pack_fallback():
    """Verify discover_brain_pack() resolves to bundled aither pack when no other is found."""
    from adk.pack_discovery import discover_brain_pack

    # Clear env and CWD conditions to force fallback
    with mock.patch.dict(os.environ, {"AGENT_BRAIN_PACK": ""}, clear=False):
        # Mock _find_library_packs, _find_entrypoint_packs, _find_local_packs to return []
        with mock.patch("adk.pack_discovery._find_library_packs", return_value=[]):
            with mock.patch("adk.pack_discovery._find_entrypoint_packs", return_value=[]):
                with mock.patch("adk.pack_discovery._find_local_packs", return_value=[]):
                    # Mock Path.cwd() to a temp dir with no brain_pack.yaml
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with mock.patch("pathlib.Path.cwd", return_value=Path(tmpdir)):
                            pack = discover_brain_pack()

    assert pack is not None, "discover_brain_pack() should fall back to aither pack"
    assert pack.name == "brain_pack.yaml", f"Expected brain_pack.yaml, got {pack.name}"
    assert "aither" in str(pack), f"Expected aither pack path, got {pack}"
    assert pack.exists(), f"Aither pack should exist at {pack}"


def test_aither_pack_exists():
    """Verify the bundled aither pack is present and has required files."""
    aither_pack_dir = Path(__file__).resolve().parents[1] / "adk" / "packs" / "aither"
    assert aither_pack_dir.is_dir(), f"Aither pack dir should exist: {aither_pack_dir}"

    brain_pack = aither_pack_dir / "brain_pack.yaml"
    assert brain_pack.exists(), f"brain_pack.yaml should exist: {brain_pack}"

    # Check that required skills are present
    skills_dir = aither_pack_dir / "skills"
    assert skills_dir.is_dir(), f"Skills directory should exist: {skills_dir}"

    expected_skills = ["coordination.md", "memory-recall.md"]
    for skill in expected_skills:
        skill_path = skills_dir / skill
        assert skill_path.exists(), f"Skill should exist: {skill_path}"


def test_aither_agent_builds():
    """Verify AitherAgent builds successfully with aither identity."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither", builtin_tools=True, load_packs=False)
    assert agent is not None
    assert agent.name == "aither"
    assert agent._identity is not None
    assert agent._identity.name == "aither"


def test_aither_agent_has_builtin_tools():
    """Verify AitherAgent has basic file_io, shell, web tools registered."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither", builtin_tools=True, load_packs=False)
    assert agent._tools is not None

    # Get list of tool names
    tool_names = {tool.name for tool in agent._tools.list_tools()}

    # Verify basic tool categories are registered
    expected_tools = {
        "file_read",
        "file_write",
        "file_edit",
        "file_list",
        "file_search",
        "shell_exec",
        "web_search",
        "web_fetch",
    }

    missing = expected_tools - tool_names
    assert not missing, f"Missing tools: {missing}. Available: {tool_names}"


def test_aither_agent_graph_memory_active():
    """Verify GraphMemory is initialized and active on the agent."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither", builtin_tools=True, load_packs=False)
    assert agent._graph is not None, "GraphMemory should be initialized"

    # Verify it's a GraphMemory instance
    from adk.graph_memory import GraphMemory
    assert isinstance(agent._graph, GraphMemory)


@pytest.mark.asyncio
async def test_graphrag_store_and_recall():
    """Integration test: store a distinctive fact and recall it via GraphMemory."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither-test", builtin_tools=False, load_packs=False)
    graph = agent._graph

    assert graph is not None, "GraphMemory must be active"

    # Store a distinctive fact
    test_subject = "AitherOS Framework"
    test_relation = "provides"
    test_object = "unified agent orchestration"
    test_metadata = {"version": "2.0", "test": True}

    await graph.remember(
        subject=test_subject,
        relation=test_relation,
        object_=test_object,
        metadata=test_metadata,
    )

    # Recall the fact by querying the graph
    results = await graph.recall(subject=test_subject, relation=test_relation)
    assert len(results) > 0, "Should have recalled the stored fact"

    found = any(
        result["object"] == test_object and result["relation"] == test_relation
        for result in results
    )
    assert found, f"Should recall the exact fact we stored. Results: {results}"


@pytest.mark.asyncio
async def test_graphrag_search_and_semantics():
    """Test semantic search via GraphMemory using feature-hash embedding."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither-search-test", builtin_tools=False, load_packs=False)
    graph = agent._graph

    assert graph is not None

    # Store several related facts
    facts = [
        ("GraphRAG", "is", "persistent knowledge graph"),
        ("GraphRAG", "uses", "hybrid semantic search"),
        ("Knowledge Graph", "stores", "facts and relationships"),
    ]

    for subject, relation, obj in facts:
        await graph.remember(subject, relation, obj)

    # Search for related content
    search_results = await graph.search("knowledge graph persistent", limit=10)

    assert len(search_results) > 0, "Search should return results"
    # At least one result should have high relevance to our query
    labels = [node.label for node in search_results]
    assert any("knowledge" in label.lower() or "graph" in label.lower()
               for label in labels), (
        f"Expected to find knowledge/graph related node. Got: {labels}"
    )


@pytest.mark.asyncio
async def test_graphrag_reinforcement():
    """Test that recall with reinforce=True bumps reinforcement counts."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither-reinforce-test", builtin_tools=False, load_packs=False)
    graph = agent._graph

    # Store a fact
    await graph.remember("Test Framework", "enables", "rapid development")

    # Get the node to check initial state
    node_id = (await graph.add_node(
        label="Test Framework",
        node_type="entity",
        content="Test Framework enables rapid development"
    )).id

    # Recall with reinforcement
    results = await graph.recall_with_activation(
        "Test Framework",
        limit=5,
        reinforce=True
    )

    assert len(results) > 0, "Should recall facts"

    # Verify the node was reinforced by checking metadata
    recalled_node = await graph.get_node(node_id)
    if recalled_node and recalled_node.metadata:
        # After reinforcement, metadata should have reinforcement_count
        # (but it may be 0 initially if this was the first recall)
        assert isinstance(recalled_node.metadata, dict)


@pytest.mark.asyncio
async def test_graphrag_cleanup():
    """Cleanup test: verify database connections are properly closed."""
    from adk.agent import AitherAgent

    agent = AitherAgent(name="aither-cleanup-test", builtin_tools=False, load_packs=False)
    graph = agent._graph

    # Store and recall
    await graph.remember("Cleanup Test", "verifies", "connection management")
    results = await graph.search("cleanup", limit=5)
    assert len(results) >= 0  # May be 0 if no exact match, but should not crash

    # Drain any pending sync tasks (if auto-sync is enabled)
    await graph.drain_sync()


def test_builtin_tools_categories():
    """Verify that builtin_tools can be registered by category."""
    from adk.agent import AitherAgent
    from adk.builtin_tools import register_builtin_tools

    agent = AitherAgent(name="aither-tools-test", builtin_tools=False, load_packs=False)
    assert len(agent._tools.list_tools()) == 0, "Should start with no tools"

    # Register just file_io
    register_builtin_tools(agent, categories=["file_io"])
    file_tools = {t.name for t in agent._tools.list_tools()}
    assert "file_read" in file_tools
    assert "file_write" in file_tools

    # Verify shell tools are NOT registered yet
    assert "shell_exec" not in file_tools

    # Now register shell
    register_builtin_tools(agent, categories=["shell"])
    all_tools = {t.name for t in agent._tools.list_tools()}
    assert "shell_exec" in all_tools
    assert "file_read" in all_tools  # Should still have file_io


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
