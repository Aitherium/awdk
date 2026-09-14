"""AitherOS registry pack — auto-generated.

Tool registration for registry service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("registry_pack")

PACK_ID = "registry"

_TOOL_NAMES = [
    "reg_list_services",
    "reg_get_service",
    "reg_health_check",
    "reg_resolve_url",
    "reg_query_by_tag",
]


def register(registry) -> int:
    """Register all registry_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("registry pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("registry: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("registry: skip tool %s: %s", name, exc)

    logger.info("Service Registry pack registered %d "
                "registry_* tools", n)
    return n
