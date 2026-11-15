"""NVIDIA CUDA docs tool pack — hand-written, fail-soft.

Unlike the generated NVIDIA packs, this one implements an OAuth
authorization-code + PKCE flow (no device grant exists on the server) and
discovers the search tool by shape. With no credential the pack still imports
and every tool reports ``not_authenticated`` with a fix.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("cuda_pack")

PACK_ID = "nvidia_cuda"

_TOOL_NAMES = [
    "cuda_docs_search",
    "cuda_docs_tools",
    "cuda_docs_status",
    "cuda_docs_login",
    "cuda_docs_logout",
]


def register(registry) -> int:
    """Register every cuda_docs_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("nvidia_cuda pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("nvidia_cuda: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("nvidia_cuda: skip tool %s: %s", name, exc)

    logger.info("NVIDIA CUDA docs pack registered %d tools", n)
    return n
