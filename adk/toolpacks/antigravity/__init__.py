"""antigravity toolpack — ``antigravity_*`` agent tools for two-way orchestration
between awdk / awsh / AitherOS and Google Antigravity 2.0 / CLI.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("antigravity_pack")

PACK_ID = "antigravity"


def register(registry) -> int:
    """Register all ``antigravity_*`` tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("antigravity pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in t._TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("antigravity: skip tool %s: %s", name, exc)

    logger.info("antigravity pack registered %d antigravity_* tools", n)
    return n
