"""Per-language parsers turning raw documents into graph fragments.

Each parser emits :class:`ParseResult` (nodes + edges keyed by *natural keys* —
stable, human-readable strings like ``table:global_outbox`` or
``section:agents/priya/rule.md#leave-rules``). The graph builder resolves those
keys to stable node ids, deduplicates, embeds, and drops dangling edges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from adk.graph_rag.config import NodeType
from adk.graph_rag.corpus_loader import RawDoc


@dataclass
class ParsedNode:
    key: str                 # natural key, unique across the corpus
    type: NodeType
    content: str             # text to embed + surface
    title: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedEdge:
    src_key: str
    dst_key: str
    relation: str
    weight: float = 1.0


@dataclass
class ParseResult:
    nodes: list[ParsedNode] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)


def slugify(text: str) -> str:
    """A compact, deterministic anchor slug for headings."""
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:80] or "section"


def file_key(rel_path: str) -> str:
    return f"file:{rel_path}"


def parse(doc: RawDoc) -> ParseResult:
    """Dispatch a raw document to the right language parser."""
    # imported lazily to avoid a circular import at module load
    if doc.language == "markdown":
        from adk.graph_rag.parsers.markdown_parser import parse_markdown

        return parse_markdown(doc.rel_path, doc.content)
    if doc.language == "sql":
        from adk.graph_rag.parsers.sql_parser import parse_sql

        return parse_sql(doc.rel_path, doc.content)
    if doc.language == "typescript":
        from adk.graph_rag.parsers.typescript_parser import parse_typescript

        return parse_typescript(doc.rel_path, doc.content)
    # unknown language → just a file node
    return ParseResult(nodes=[ParsedNode(key=file_key(doc.rel_path),
                                         type=NodeType.FILE, content=doc.rel_path)])


__all__ = ["ParsedNode", "ParsedEdge", "ParseResult", "parse", "slugify", "file_key"]
