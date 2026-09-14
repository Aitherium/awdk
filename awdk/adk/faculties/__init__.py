"""
adk.faculties — Local Knowledge Graphs for Every Agent
========================================================

Battle-tested graph faculties extracted from AitherOS, adapted for
standalone use. No AitherOS services required.

Provides:
  - CodeGraph: AST-based Python code indexer with call graph + semantic search
  - EmbeddingProvider: Pluggable embedding backends (sentence-transformers/Ollama/Elysium/feature-hash)
  - BaseFacultyGraph: Abstract base with pickle persistence + HMAC integrity

For persistent agent memory, use :class:`adk.graph_memory.GraphMemory`.

Usage:
    from adk.faculties import CodeGraph

    cg = CodeGraph()
    await cg.index_codebase("./my-project")
    results = await cg.query("authentication middleware", max_results=5)
"""

from adk.faculties.base import BaseFacultyGraph, GraphSyncConfig
from adk.faculties.embeddings import EmbeddingProvider, get_embedding_provider

# Lazy imports for heavy modules
def __getattr__(name):
    if name == "CodeGraph":
        from adk.faculties.code_graph import CodeGraph
        return CodeGraph
    if name == "MemoryGraph":
        # Back-compat shim: resolve to the canonical GraphMemory
        from adk.graph_memory import GraphMemory as MemoryGraph
        return MemoryGraph
    raise AttributeError(f"module 'adk.faculties' has no attribute {name!r}")


__all__ = [
    "BaseFacultyGraph",
    "GraphSyncConfig",
    "EmbeddingProvider",
    "get_embedding_provider",
    "CodeGraph",
]
