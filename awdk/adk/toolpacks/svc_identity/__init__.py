"""AitherOS identity pack — auto-generated.

Tool registration for identity service endpoints. This pack is OPTIONAL and
fails gracefully when not authenticated — an agent with no credential still
registers these tools with "not configured" status, never crashes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("identity_pack")

PACK_ID = "identity"

_TOOL_NAMES = [
    "ident_create_session",
    "ident_refresh_session",
    "ident_revoke_session",
    "ident_get_user_info",
    "ident_health",
]


def register(registry) -> int:
    """Register all identity_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("identity pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("identity: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("identity: skip tool %s: %s", name, exc)

    logger.info("Service Identity pack registered %d "
                "identity_* tools", n)
    return n
