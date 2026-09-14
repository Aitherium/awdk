"""AitherOS nexus pack — auto-generated.

Tool registration for nexus service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus_pack")

PACK_ID = "nexus"

_TOOL_NAMES = [
    "nexus_search",
    "nexus_ingest",
    "nexus_list_collections",
    "nexus_health",
]


def register(registry) -> int:
    """Register all nexus_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("nexus pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("nexus: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("nexus: skip tool %s: %s", name, exc)

    logger.info("Service Nexus pack registered %d "
                "nexus_* tools", n)
    return n
