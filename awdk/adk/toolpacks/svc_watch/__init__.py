"""AitherOS watch pack — auto-generated.

Tool registration for watch service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("watch_pack")

PACK_ID = "watch"

_TOOL_NAMES = [
    "watch_service_health",
    "watch_metrics",
    "watch_list_alerts",
    "watch_acknowledge_alert",
    "watch_recent_incidents",
]


def register(registry) -> int:
    """Register all watch_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("watch pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("watch: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("watch: skip tool %s: %s", name, exc)

    logger.info("Service Watch pack registered %d "
                "watch_* tools", n)
    return n
