"""AitherOS directory pack — auto-generated.

Tool registration for directory service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("directory_pack")

PACK_ID = "directory"

_TOOL_NAMES = [
    "dir_verify_session",
    "dir_get_permissions",
    "dir_check_entitlement",
    "dir_list_roles",
    "dir_health",
]


def register(registry) -> int:
    """Register all directory_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("directory pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("directory: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("directory: skip tool %s: %s", name, exc)

    logger.info("Service Directory pack registered %d "
                "directory_* tools", n)
    return n
