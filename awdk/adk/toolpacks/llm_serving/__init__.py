"""LLM serving toolpack — install vLLM + serve the fleet models, quant-optimized.

Registers 6 llm_* tools: detect which fleet models fit + their optimal quant,
resolve a model role to a recipe + hardware-optimized quant, plan the vLLM serve,
apply it, register the backend, and verify with a real chat round-trip. All tools
fail-soft dict-returners.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("llm_serving_pack")

PACK_ID = "llm_serving"

_TOOL_NAMES = [
    "llm_detect_hardware",
    "llm_resolve",
    "llm_plan_deployment",
    "llm_apply",
    "llm_register_backend",
    "llm_verify",
]


def register(registry) -> int:
    """Register all llm_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("llm_serving pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("llm_serving: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("llm_serving: skip tool %s: %s", name, exc)

    logger.info("LLM serving pack registered %d llm_* tools", n)
    return n
