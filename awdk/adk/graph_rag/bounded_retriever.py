"""Bounded subgraph retriever over a :class:`GraphStore`.

Implements the existing :class:`adk.graph_retriever.GraphRetriever` ABC so it
drops into any grounding path that expects ``subgraph_search`` →
:class:`adk.graph_retriever.Subgraph`. Seeds via vector similarity with a
relevance floor (keeps off-topic namespaces out), then BFS-expands along edges.
"""

from __future__ import annotations

from typing import Any

from adk.core.vector_memory import InMemoryVectorStore
from adk.graph_retriever import GraphEdge, GraphNode, GraphRetriever, Subgraph
from adk.graph_rag.config import (
    DEFAULT_K_HOPS,
    DEFAULT_K_SEEDS,
    DEFAULT_LIMIT,
    RELEVANCE_FLOOR,
)
from adk.graph_rag.graph_store import GraphStore


async def _embed(embedder: Any, text: str) -> list[float]:
    if hasattr(embedder, "embed"):
        return await embedder.embed(text)
    return await embedder(text)


def _excerpt(node: GraphNode, max_chars: int = 280) -> str:
    ntype = node.metadata.get("type", "node")
    title = node.metadata.get("title", "")
    body = node.content.strip().replace("\n", " ")
    text = f"({ntype}) {title}".strip()
    if body and body != title:
        text = f"{text} - {body}"
    return text[:max_chars]


class CorpusGraphRetriever(GraphRetriever):
    def __init__(
        self,
        store: GraphStore,
        embedder: Any,
        *,
        floor: float = RELEVANCE_FLOOR,
        vector_store: InMemoryVectorStore | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.floor = floor
        self._vs = vector_store
        self._built = False

    async def _ensure_index(self) -> None:
        if self._built:
            return
        vs = self._vs if self._vs is not None else InMemoryVectorStore()
        for node in self.store.all_nodes():
            emb = node.metadata.get("embedding")
            if emb:
                await vs.add(node.id, emb, {
                    "namespace": node.metadata.get("namespace"),
                    "type": node.metadata.get("type"),
                })
        self._vs = vs
        self._built = True

    async def subgraph_search(
        self,
        query: str,
        *,
        k_seeds: int = DEFAULT_K_SEEDS,
        k_hops: int = DEFAULT_K_HOPS,
        limit: int = DEFAULT_LIMIT,
        namespace: str | None = None,
    ) -> Subgraph:
        await self._ensure_index()
        assert self._vs is not None
        qvec = await _embed(self.embedder, query)
        hits = await self._vs.search(qvec, k=max(k_seeds * 4, k_seeds))

        seeds = []
        for hit in hits:
            if namespace is not None and hit.payload.get("namespace") != namespace:
                continue
            if hit.score < self.floor:
                continue
            seeds.append(hit)
            if len(seeds) >= k_seeds:
                break
        if not seeds:
            return Subgraph(nodes=[], edges=[])

        # display ids = readable natural keys; map internal id → display id
        display: dict[str, str] = {}

        def disp(internal_id: str) -> str:
            node = self.store.get_node(internal_id)
            label = node.metadata.get("natural_key", internal_id) if node else internal_id
            display[internal_id] = label
            return label

        result: dict[str, GraphNode] = {}
        for hit in seeds:
            node = self.store.get_node(hit.id)
            if node is None:
                continue
            result[hit.id] = GraphNode(id=disp(hit.id), content=_excerpt(node),
                                       score=round(hit.score, 4),
                                       metadata={"type": node.metadata.get("type")})

        frontier = set(result.keys())
        for _ in range(k_hops):
            if len(result) >= limit:
                break
            nxt: set[str] = set()
            for src in frontier:
                if len(result) >= limit:
                    break
                for dst, _rel, _w in self.store.neighbors(src, direction="both"):
                    if dst in result or len(result) >= limit:
                        continue
                    node = self.store.get_node(dst)
                    if node is None:
                        continue
                    result[dst] = GraphNode(id=disp(dst), content=_excerpt(node), score=0.0,
                                            metadata={"type": node.metadata.get("type")})
                    nxt.add(dst)
            frontier = nxt
            if not frontier:
                break

        edges: list[GraphEdge] = []
        seen: set[tuple[str, str, str]] = set()
        for edge in self.store.all_edges():
            if edge.src in result and edge.dst in result:
                k = (edge.src, edge.dst, edge.relation)
                if k in seen:
                    continue
                seen.add(k)
                edges.append(GraphEdge(src=display[edge.src], dst=display[edge.dst],
                                       relation=edge.relation, weight=edge.weight))

        return Subgraph(nodes=list(result.values()), edges=edges)
