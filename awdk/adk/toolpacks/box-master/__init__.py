"""Box Master tool pack — local discovery, activation, and learning.

The pack registers five tools:

  * explore(query, domain="any", k=8) — search the gateway /discover endpoint
    for tool cards matching a query across local/cloud/both domains.
  * activate(ref, domain) — fetch full tool details via /discover/detail,
    auto-register the schema into the agent's runtime registry, and make it
    callable immediately via MCPBridge.
  * system_report() — introspect the box: hardware, OS, network, memory config.
  * fs_map(root, depth) — walk a filesystem tree to depth levels; used to
    understand code layout before exploring codebases.
  * learn(query, ref, outcome) — feed agent outcomes to the world-model and
    local GraphMemory so future agents can recall your discoveries.

Registration is unconditional; all tools fail soft with actionable guidance
when the gateway is unreachable or credentials are missing.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("box_master_pack")

PACK_ID = "box-master"

_TOOL_NAMES = [
    "explore",
    "activate",
    "system_report",
    "fs_map",
    "learn",
]


def register(registry) -> int:
    """Register every box-master tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("box-master pack unavailable (%s) — 0 tools registered",
                       exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("box-master: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool no sink pack
            logger.debug("box-master: skip tool %s: %s", name, exc)
    logger.info("Box Master pack registered %d tools", n)
    return n
