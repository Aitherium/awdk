"""NVIDIA Nemotron Orchestrator tool pack — auto-generated from the component spec.

Optional and fail-soft: with no NVIDIA credential the pack still imports and
every tool reports not_authenticated. Regenerate, never hand-edit.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nemotron_pack")

PACK_ID = "nvidia_nemotron"

_TOOL_NAMES = [
    "nemotron_chat",
    "nemotron_status",
    "nemotron_config",
]


def register(registry) -> int:
    """Register every nemotron_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("nvidia_nemotron pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("nvidia_nemotron: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("nvidia_nemotron: skip tool %s: %s", name, exc)

    logger.info("NVIDIA Nemotron Orchestrator pack registered %d tools", n)
    return n
