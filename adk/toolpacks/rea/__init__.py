"""REA toolpack — reverse-engineer a target with the pinned rea CLI, then prove a rebuild.

Wraps rea-agents (MIT, https://github.com/morluto/rea) as a subprocess; never vendored.
Analysis is gated by a target-authorization policy (owned|licensed|interop).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("rea_pack")

PACK_ID = "rea"
_TOOL_NAMES = ["rea_status", "rea_analyze_javascript", "rea_ledger_close"]


def register(registry) -> int:
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001
        logger.warning("rea pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("rea: skip tool %s: %s", name, exc)
    return n
