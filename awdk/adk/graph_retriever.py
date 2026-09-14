"""Graph retrieval interface for bounded subgraph search.

Provides a provider-neutral retrieval interface so grounding can fetch a BOUNDED
relevant subgraph instead of flat top-k (reduces tokens stuffed per reasoning step).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GraphNode:
    """A single node in a knowledge graph."""
    id: str
    content: str
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    """A directed edge connecting two nodes."""
    src: str
    dst: str
    relation: str = "related"
    weight: float = 1.0


@dataclass
class Subgraph:
    """A bounded subgraph: seed nodes + 1-hop neighbors."""
    nodes: list[GraphNode]
    edges: list[GraphEdge]

    def to_context(self, max_chars: int = 8192) -> str:
        """Render a compact, deterministic text block for LLM context.

        Order: seed facts first (highest score), then edges, deduplicated.
        Truncates to max_chars deterministically.
        """
        if not self.nodes:
            return ""

        # Sort nodes by score descending (seeds first)
        sorted_nodes = sorted(self.nodes, key=lambda n: (-n.score, n.id))

        lines = []
        lines.append("# Facts")
        char_count = 8  # "# Facts\n"
        truncated = False

        # Add nodes
        for node in sorted_nodes:
            node_line = f"- [{node.id}] {node.content}"
            if node.score > 0:
                node_line += f" (relevance: {node.score:.2f})"
            line_len = len(node_line) + 1  # +1 for newline
            if char_count + line_len > max_chars:
                truncated = True
                break
            lines.append(node_line)
            char_count += line_len

        # Edges section (only if we have room)
        if self.edges and char_count < max_chars * 0.8:
            lines.append("\n# Relations")
            char_count += 12  # "\n# Relations\n"
            # Dedupe and sort edges for determinism
            seen_edges = set()
            sorted_edges = sorted(self.edges, key=lambda e: (e.src, e.dst, e.relation))
            for edge in sorted_edges:
                edge_key = (edge.src, edge.dst, edge.relation)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                edge_line = f"- {edge.src} --[{edge.relation}]--> {edge.dst}"
                if edge.weight != 1.0:
                    edge_line += f" (weight: {edge.weight:.2f})"
                line_len = len(edge_line) + 1  # +1 for newline
                if char_count + line_len > max_chars:
                    truncated = True
                    break
                lines.append(edge_line)
                char_count += line_len

        result = "\n".join(lines)
        if truncated:
            result += "\n... (truncated)"
        return result


class GraphRetriever(ABC):
    """Abstract base for graph-aware retrieval."""

    @abstractmethod
    async def subgraph_search(
        self,
        query: str,
        *,
        k_seeds: int = 5,
        k_hops: int = 1,
        limit: int = 12,
        intent_type: str | None = None,
    ) -> Subgraph:
        """Search for a bounded relevant subgraph.

        Args:
            query: The search query (natural language or embedding).
            k_seeds: Number of seed nodes to retrieve.
            k_hops: Depth of expansion (1 = direct neighbors only).
            limit: Max total nodes in result.
            intent_type: Optional intent type (CODE/CONVERSATION/DEFAULT) for intent-aware scoring.

        Returns:
            A Subgraph with seed nodes and up to k_hops neighbors.
        """
        ...


class InMemoryGraphRetriever(GraphRetriever):
    """Simple in-memory reference implementation for testing.

    Stores nodes and edges in memory. Seed search uses substring/simple matching.
    Expansion follows edges up to k_hops, deduplicates by node id, caps at limit.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        # Build adjacency for faster expansion
        self._adj_out: dict[str, list[tuple[str, str, float]]] = {}  # src -> [(dst, rel, weight)]

    def add_node(self, node: GraphNode) -> None:
        """Add a node to the graph."""
        self._nodes[node.id] = node
        if node.id not in self._adj_out:
            self._adj_out[node.id] = []

    def add_edge(self, edge: GraphEdge) -> None:
        """Add an edge to the graph."""
        self._edges.append(edge)
        if edge.src not in self._adj_out:
            self._adj_out[edge.src] = []
        self._adj_out[edge.src].append((edge.dst, edge.relation, edge.weight))

    async def subgraph_search(
        self,
        query: str,
        *,
        k_seeds: int = 5,
        k_hops: int = 1,
        limit: int = 12,
        intent_type: str | None = None,
    ) -> Subgraph:
        """Search in-memory graph.

        Seeds: substring match on node.content, sorted by descending score.
        Expansion: BFS up to k_hops, deduped by id, capped at limit.

        Optional intent-aware rescoring:
        - CODE intent: multiply scores by 1.2 (boost technical content)
        - CONVERSATION intent: filter weak nodes (score < 0.5)
        - Missing/DEFAULT intent: neutral (no rescoring)
        """
        # Find seed nodes (substring match)
        query_lower = query.lower()
        candidates = [
            n for n in self._nodes.values()
            if query_lower in n.content.lower()
        ]

        # Intent-aware rescoring (fail-open: no intent → neutral). Compute an
        # EFFECTIVE score for ranking only — NEVER mutate node.score in place:
        # nodes are shared/cached, so `n.score *= 1.2` is non-idempotent and
        # compounds every time the same node is re-scored across queries.
        intent_norm = (intent_type or "").strip().lower() if intent_type else ""
        if intent_norm in ("conversation", "web_research"):
            # CONVERSATION/web intent: drop weak matches
            candidates = [n for n in candidates if n.score >= 0.5]

        def _eff_score(n) -> float:
            # CODE intent boosts technical relevance for RANKING without mutating n
            return n.score * 1.2 if intent_norm == "code" else n.score

        # Sort by effective score descending, then by id for determinism
        candidates.sort(key=lambda n: (-_eff_score(n), n.id))
        seed_nodes = candidates[:k_seeds]

        if not seed_nodes:
            return Subgraph(nodes=[], edges=[])

        # BFS expansion
        result_nodes = {n.id: n for n in seed_nodes}
        frontier = {n.id for n in seed_nodes}
        result_edges = []

        for hop in range(k_hops):
            if len(result_nodes) >= limit:
                break
            next_frontier = set()
            for src_id in frontier:
                if len(result_nodes) >= limit:
                    break
                for dst_id, rel, weight in self._adj_out.get(src_id, []):
                    if dst_id in result_nodes:
                        continue  # Already have this node
                    if dst_id not in self._nodes:
                        continue  # Invalid edge
                    if len(result_nodes) >= limit:
                        break
                    # Add the neighbor
                    result_nodes[dst_id] = self._nodes[dst_id]
                    next_frontier.add(dst_id)
                    # Record the edge
                    result_edges.append(GraphEdge(src=src_id, dst=dst_id, relation=rel, weight=weight))

            frontier = next_frontier
            if not frontier:
                break

        # Collect edges within the result set (for display)
        all_result_edges = []
        for edge in self._edges:
            if edge.src in result_nodes and edge.dst in result_nodes:
                all_result_edges.append(edge)

        return Subgraph(nodes=list(result_nodes.values()), edges=all_result_edges)
