"""Plan B Ledger toolpack — one ledger, two faces (digital + paper).

Registers 7 planb_* tools: status, NL capture (bonsai-27b via llama.cpp with
deterministic fallback), exact entry, bills roster, printable checkpoint sheet,
paper reconcile with conflict detection, demo seed. All fail-soft dict-returners.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("planb_ledger_pack")

PACK_ID = "planb_ledger"

_TOOL_NAMES = [
    "planb_status",
    "planb_capture",
    "planb_add_entry",
    "planb_set_bills",
    "planb_print_sheet",
    "planb_reconcile",
    "planb_sync",
    "planb_seed_demo",
]


def register(registry) -> int:
    """Register all planb_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("planb_ledger pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("planb_ledger: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("planb_ledger: skip tool %s: %s", name, exc)

    logger.info("Plan B Ledger pack registered %d planb_* tools", n)
    return n
