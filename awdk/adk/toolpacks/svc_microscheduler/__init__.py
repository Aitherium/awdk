"""AitherOS microscheduler pack — auto-generated.

Tool registration for microscheduler service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("microscheduler_pack")

PACK_ID = "microscheduler"

_TOOL_NAMES = [
    "sched_enqueue_request",
    "sched_get_status",
    "sched_cancel",
    "sched_queue_depth",
    "sched_health",
]


def register(registry) -> int:
    """Register all microscheduler_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("microscheduler pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("microscheduler: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("microscheduler: skip tool %s: %s", name, exc)

    logger.info("Service Microscheduler pack registered %d "
                "microscheduler_* tools", n)
    return n
