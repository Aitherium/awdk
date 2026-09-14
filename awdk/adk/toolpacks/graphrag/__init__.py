"""GraphRAG toolpack — set up embeddings + GraphRAG (rag_* tools).

Registers 7 rag_* tools: detect hardware, resolve an embedder role, plan + apply
the vLLM embedder, verify it returns the RIGHT vector dimension, ingest a corpus
into a local knowledge graph, and verify retrieval actually returns ingested
content. All tools fail-soft dict-returners.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("graphrag_pack")

PACK_ID = "graphrag"

_TOOL_NAMES = [
    "rag_detect_hardware",
    "rag_resolve_embedder",
    "rag_plan_embedder",
    "rag_apply_embedder",
    "rag_verify_embedder",
    "rag_ingest",
    "rag_verify_retrieval",
]


def register(registry) -> int:
    """Register all rag_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001
        logger.warning("graphrag pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("graphrag: skip %s: %s", name, exc)
    logger.info("GraphRAG pack registered %d rag_* tools", n)
    return n
