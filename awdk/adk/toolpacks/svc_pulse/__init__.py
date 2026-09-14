"""AitherOS pulse pack — auto-generated.

Tool registration for pulse service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("pulse_pack")

PACK_ID = "pulse"

_TOOL_NAMES = [
    "pulse_subscribe",
    "pulse_emit",
    "pulse_query_recent",
    "pulse_circuit_status",
    "pulse_health",
]


def register(registry) -> int:
    """Register all pulse_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("pulse pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("pulse: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("pulse: skip tool %s: %s", name, exc)

    logger.info("Service Pulse pack registered %d "
                "pulse_* tools", n)
    return n
