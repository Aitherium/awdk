"""Node bootstrap toolpack — hardware-aware inference deployment tools.

Registers 7 node_* tools: detect hardware, resolve recipes, plan deployments,
apply deployments (local or remote), enroll with control plane, register backends,
and verify endpoint health + inference capability. All tools fail-soft dict-returners.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("node_bootstrap_pack")

PACK_ID = "node_bootstrap"

_TOOL_NAMES = [
    "node_detect_hardware",
    "node_resolve_recipe",
    "node_plan_deployment",
    "node_apply",
    "node_enroll",
    "node_register_backend",
    "node_verify",
]


def register(registry) -> int:
    """Register all node_* tools. Returns the number registered.

    Each tool fails soft with actionable guidance; one bad tool never sinks
    the pack.
    """
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("node_bootstrap pack unavailable (%s) — 0 tools registered",
                      exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("node_bootstrap: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("node_bootstrap: skip tool %s: %s", name, exc)

    logger.info("Node bootstrap pack registered %d node_* tools", n)
    return n
