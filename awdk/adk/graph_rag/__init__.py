"""Graph-based RAG service-pack for awdk.

Ingest a corpus (Markdown / SQL / TypeScript) into an embedded knowledge graph,
retrieve a *bounded subgraph* (facts + relations) instead of flat top-k, and
govern (re-)ingestion with a "memory you can put on trial" layer (audit ledger +
conflict-on-write + reversible forget).

Portable: pure stdlib + the existing adk graph/vector primitives. No monorepo
dependency.

High-level entry points:

    from adk.graph_rag import ingest_corpus, open_retriever

    stats = await ingest_corpus(corpus_root, graph_path, embedder=embed, namespace="codebase")
    retriever = open_retriever(graph_path, embedder=embed)
    subgraph = await retriever.subgraph_search("how does approval work?", namespace="codebase")
    print(subgraph.to_context())
"""

from __future__ import annotations

from typing import Any

from adk.graph_rag.bounded_retriever import CorpusGraphRetriever
from adk.graph_rag.config import CONFLICT_SIMILARITY, DEFAULT_NAMESPACE, NodeType, EdgeType
from adk.graph_rag.corpus_loader import RawDoc, load_corpus
from adk.graph_rag.governance import (
    ConflictDetector,
    GovernanceArtifacts,
    GovernedIngest,
    MutationLedger,
    StableNodeID,
    TombstoneStore,
)
from adk.graph_rag.graph_builder import build_graph
from adk.graph_rag.graph_store import GraphStore


async def ingest_corpus(
    corpus_root: str,
    output_path: str,
    *,
    embedder: Any,
    namespace: str = "codebase",
    govern: bool = True,
    include_languages: set[str] | None = None,
) -> dict:
    """Ingest a corpus directory into a graph index at ``output_path``.

    Re-ingestion is incremental + audited: existing nodes keep their ids, changed
    nodes are superseded (with a conflict verdict), and removed nodes are
    tombstoned. Returns a stats dict.
    """
    from pathlib import Path

    corpus = load_corpus(corpus_root, include_languages=include_languages)

    store = GraphStore.load(output_path) if Path(output_path).exists() else GraphStore()
    artifacts = GovernanceArtifacts.beside(output_path)
    stable_ids = StableNodeID(artifacts.stable_id_path, persist=True)
    governed = GovernedIngest(
        ledger=MutationLedger(artifacts.ledger_path, persist=govern),
        detector=ConflictDetector(CONFLICT_SIMILARITY),
        tombstones=TombstoneStore(artifacts.tombstone_path, persist=govern),
        stable_ids=stable_ids,
        source=f"ingest:{namespace}",
        enabled=govern,
    )

    store = await build_graph(
        corpus, embedder=embedder, namespace=namespace,
        store=store, stable_ids=stable_ids, governed=governed,
    )
    store.save(output_path)

    return {
        "namespace": namespace,
        "files": len(corpus),
        "nodes": len(store),
        "edges": len(store.all_edges()),
        "conflicts": [
            {"node_id": c.node_id, "kind": c.kind, "reason": c.reason}
            for c in governed.conflicts
        ],
        "ledger": governed.ledger.stats() if govern else {"total": 0},
        "output": output_path,
    }


def open_retriever(graph_path: str, *, embedder: Any, floor: float | None = None) -> CorpusGraphRetriever:
    """Load a persisted graph index and return a ready-to-query retriever."""
    store = GraphStore.load(graph_path)
    if floor is None:
        return CorpusGraphRetriever(store, embedder)
    return CorpusGraphRetriever(store, embedder, floor=floor)


__all__ = [
    "ingest_corpus",
    "open_retriever",
    "CorpusGraphRetriever",
    "GraphStore",
    "build_graph",
    "load_corpus",
    "RawDoc",
    "NodeType",
    "EdgeType",
    "DEFAULT_NAMESPACE",
    "ConflictDetector",
    "GovernanceArtifacts",
    "GovernedIngest",
    "MutationLedger",
    "StableNodeID",
    "TombstoneStore",
]
