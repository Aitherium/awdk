"""Assemble parsed documents into an embedded, governed graph store."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from adk.graph_retriever import GraphEdge, GraphNode
from adk.graph_rag.config import DEFAULT_NAMESPACE
from adk.graph_rag.corpus_loader import RawDoc
from adk.graph_rag.governance import GovernedIngest
from adk.graph_rag.graph_store import GraphStore
from adk.graph_rag.parsers import ParsedNode, parse

# An embedder is either an adk.core.vector_memory.Embedder or an async text→vec callable.
Embedder = Any
EmbedFn = Callable[[str], Awaitable[list[float]]]


async def _embed(embedder: Embedder, text: str) -> list[float]:
    if hasattr(embedder, "embed"):
        return await embedder.embed(text)
    return await embedder(text)


def _snapshot(node_id: str, pn: ParsedNode, namespace: str) -> dict:
    return {
        "id": node_id,
        "type": pn.type.value,
        "namespace": namespace,
        "title": pn.title or pn.key,
        "content": pn.content,
    }


async def build_graph(
    corpus: dict[str, RawDoc],
    *,
    embedder: Embedder,
    namespace: str = DEFAULT_NAMESPACE,
    store: GraphStore | None = None,
    stable_ids: Any,
    governed: GovernedIngest | None = None,
) -> GraphStore:
    """Parse → merge → embed → (govern) → store. Returns the populated store.

    ``stable_ids`` is a :class:`adk.graph_rag.governance.StableNodeID` providing
    content-independent ids keyed by each node's natural key. ``governed`` (if
    given) records the trial layer (ledger / conflict / tombstone).
    """
    store = store if store is not None else GraphStore()

    # Snapshots of nodes already in the store (for FORGET/tombstone on finalize).
    prior_snapshots: dict[str, dict] = {
        n.id: {"id": n.id, "content": n.content, **n.metadata}
        for n in store.nodes_in_namespace(namespace)
    }

    # 1. Parse all docs and merge nodes by natural key (longest content wins).
    merged: dict[str, ParsedNode] = {}
    raw_edges: list[tuple[str, str, str, float]] = []
    for doc in corpus.values():
        result = parse(doc)
        for pn in result.nodes:
            existing = merged.get(pn.key)
            if existing is None or len(pn.content) > len(existing.content):
                if existing is not None:
                    pn.metadata = {**existing.metadata, **pn.metadata}
                merged[pn.key] = pn
        for pe in result.edges:
            raw_edges.append((pe.src_key, pe.dst_key, pe.relation, pe.weight))

    node_keys = set(merged.keys())
    # Stable ids are keyed per-namespace so ids never collide across namespaces and
    # FORGET detection stays scoped (see GovernedIngest.key_prefix).
    prefix = f"{namespace}\x00"
    key_to_id = {key: stable_ids.id_for(prefix + key) for key in node_keys}
    if governed is not None:
        governed.key_prefix = prefix

    # 2. Embed + store nodes (with governance observation).
    for key, pn in merged.items():
        node_id = key_to_id[key]
        embedding = await _embed(embedder, pn.content or pn.title or key)
        prev = store.get_node(node_id)
        snap = _snapshot(node_id, pn, namespace)
        if governed is not None:
            governed.observe(
                prefix + key, node_id, pn.content, snap,
                prev_snapshot=({"id": node_id, "content": prev.content} if prev else None),
                embedding=embedding,
                prev_embedding=(prev.metadata.get("embedding") if prev else None),
            )
        store.add_node(GraphNode(
            id=node_id, content=pn.content, score=0.0,
            metadata={
                "namespace": namespace,
                "type": pn.type.value,
                "title": pn.title or key,
                "natural_key": key,
                "embedding": embedding,
                "importance": 0.5,
                **{k: v for k, v in pn.metadata.items() if k != "embedding"},
            },
        ))

    # 3. Resolve + dedupe edges (drop dangling dst).
    seen_edges: set[tuple[str, str, str]] = set()
    for src_key, dst_key, relation, weight in raw_edges:
        if src_key not in key_to_id or dst_key not in key_to_id:
            continue
        src_id, dst_id = key_to_id[src_key], key_to_id[dst_key]
        if src_id == dst_id:
            continue
        edge_key = (src_id, dst_id, relation)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        store.add_edge(GraphEdge(src=src_id, dst=dst_id, relation=relation, weight=weight))

    # 4. Migration ordering: link consecutive SQL files (succeeds).
    sql_files = sorted(
        (k for k in node_keys if k.startswith("file:") and k.endswith(".sql"))
    )
    for prev_key, next_key in zip(sql_files, sql_files[1:]):
        store.add_edge(GraphEdge(src=key_to_id[next_key], dst=key_to_id[prev_key],
                                 relation="succeeds", weight=0.5))

    # 5. Importance from in-degree.
    for node in store.nodes_in_namespace(namespace):
        node.metadata["importance"] = min(1.0, 0.5 + 0.04 * store.in_degree(node.id))

    if governed is not None:
        governed.finalize(prior_snapshots)

    return store
