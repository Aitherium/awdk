"""Thin wrapper over adk.graph_memory entity extraction, adapted to graph-RAG.

Reuses the existing CamelCase / multi-word / file / snake_case extractors so we
don't reinvent NER, and turns the hits into ``symbol`` nodes + ``references``
edges from a given source node.
"""

from __future__ import annotations

from adk.graph_memory import extract_entities, extract_keywords  # noqa: F401 (re-exported)
from adk.graph_rag.config import NodeType
from adk.graph_rag.parsers import ParsedEdge, ParsedNode

# Service/class-like entity types are worth linking; generic prose nouns are not.
_LINKWORTHY = frozenset({"service", "file", "code"})


def symbol_fragments(
    source_key: str,
    text: str,
    *,
    max_symbols: int = 8,
) -> tuple[list[ParsedNode], list[ParsedEdge]]:
    """Extract notable identifiers from ``text`` and link them to ``source_key``."""
    nodes: list[ParsedNode] = []
    edges: list[ParsedEdge] = []
    seen: set[str] = set()
    for label, etype in extract_entities(text):
        if etype not in _LINKWORTHY:
            continue
        key = f"symbol:{label.lower()}"
        if key in seen:
            continue
        seen.add(key)
        nodes.append(ParsedNode(key=key, type=NodeType.SYMBOL, content=label,
                                title=label, metadata={"symbol_type": etype}))
        edges.append(ParsedEdge(src_key=source_key, dst_key=key, relation="references",
                                weight=0.6))
        if len(seen) >= max_symbols:
            break
    return nodes, edges


__all__ = ["symbol_fragments", "extract_entities", "extract_keywords"]
