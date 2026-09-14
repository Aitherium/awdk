"""Split inference toolpack — multi-node llama.cpp RPC model sharding.

Registers 5 split_* tools: detect the device topology (local + RPC backends),
resolve a split recipe against combined VRAM, plan the build/start sequence,
apply it, and PROVE the split is real rather than a silent local-only fallback.
All tools fail-soft dict-returners.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("split_inference_pack")

PACK_ID = "split_inference"

_TOOL_NAMES = [
    "split_detect_topology",
    "split_resolve_recipe",
    "split_plan_deployment",
    "split_apply",
    "split_verify",
]


def register(registry) -> int:
    """Register all split_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("split_inference pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("split_inference: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("split_inference: skip tool %s: %s", name, exc)

    logger.info("Split inference pack registered %d split_* tools", n)
    return n
