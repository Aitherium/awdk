"""Persistent graph store: nodes + edges + embeddings, JSON-backed.

Reuses :class:`adk.graph_retriever.GraphNode` / :class:`GraphEdge` so the store
and the retriever speak the same types. The JSON shape mirrors the existing
``company_knowledge_graph.json`` (a flat nodes/edges document) for portability;
embeddings live in ``node.metadata['embedding']`` and namespaces in
``node.metadata['namespace']``.
"""

from __future__ import annotations

import json
from pathlib import Path

from adk.graph_retriever import GraphEdge, GraphNode

_VERSION = 1


class GraphStore:
    """In-memory graph with JSON persistence and adjacency for traversal."""

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        self._out: dict[str, list[tuple[str, str, float]]] = {}
        self._in: dict[str, list[tuple[str, str, float]]] = {}

    # ── construction ────────────────────────────────────────────────────
    def add_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node
        self._out.setdefault(node.id, [])
        self._in.setdefault(node.id, [])

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.src not in self._nodes or edge.dst not in self._nodes:
            return  # never store a dangling edge
        self._edges.append(edge)
        self._out.setdefault(edge.src, []).append((edge.dst, edge.relation, edge.weight))
        self._in.setdefault(edge.dst, []).append((edge.src, edge.relation, edge.weight))

    # ── queries ─────────────────────────────────────────────────────────
    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    def all_edges(self) -> list[GraphEdge]:
        return list(self._edges)

    def nodes_in_namespace(self, namespace: str | None) -> list[GraphNode]:
        if namespace is None:
            return self.all_nodes()
        return [n for n in self._nodes.values() if n.metadata.get("namespace") == namespace]

    def neighbors(self, node_id: str, *, direction: str = "both") -> list[tuple[str, str, float]]:
        out: list[tuple[str, str, float]] = []
        if direction in ("out", "both"):
            out.extend(self._out.get(node_id, []))
        if direction in ("in", "both"):
            out.extend(self._in.get(node_id, []))
        return out

    def in_degree(self, node_id: str) -> int:
        return len(self._in.get(node_id, []))

    def __len__(self) -> int:
        return len(self._nodes)

    # ── persistence ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "version": _VERSION,
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "nodes": [
                {"id": n.id, "content": n.content, "score": n.score, "metadata": n.metadata}
                for n in self._nodes.values()
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "relation": e.relation, "weight": e.weight}
                for e in self._edges
            ],
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "GraphStore":
        store = cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for nd in data.get("nodes", []):
            store.add_node(GraphNode(id=nd["id"], content=nd.get("content", ""),
                                     score=float(nd.get("score", 0.0)),
                                     metadata=dict(nd.get("metadata", {}))))
        for ed in data.get("edges", []):
            store.add_edge(GraphEdge(src=ed["src"], dst=ed["dst"],
                                     relation=ed.get("relation", "related"),
                                     weight=float(ed.get("weight", 1.0))))
        return store
