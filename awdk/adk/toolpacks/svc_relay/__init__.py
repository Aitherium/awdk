"""AitherOS relay pack — auto-generated.

Tool registration for relay service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("relay_pack")

PACK_ID = "relay"

_TOOL_NAMES = [
    "relay_send_message",
    "relay_list_messages",
    "relay_get_message",
    "relay_mark_read",
    "relay_forward",
]


def register(registry) -> int:
    """Register all relay_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("relay pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("relay: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("relay: skip tool %s: %s", name, exc)

    logger.info("Service Relay pack registered %d "
                "relay_* tools", n)
    return n
