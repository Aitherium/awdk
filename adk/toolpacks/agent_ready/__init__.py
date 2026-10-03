"""Agent Ready tool pack — register agent_ready_* tools on an adk agent.

Checks and fixes how a website presents itself to AI agents: content signals,
Link headers, markdown negotiation, the well-known discovery documents. Every
tool is keyless and standard-library only, and fails soft: an unreachable site
is a verdict with a reason, never an exception into the agent loop.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("agent_ready_pack")

PACK_ID = "agent_ready"

_TOOL_NAMES = ["agent_ready_probe", "agent_ready_scan", "agent_ready_worker_template"]


def register(registry) -> int:
    """Register every agent_ready_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("agent_ready pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("agent_ready: skip tool %s: %s", name, exc)
    logger.info("Agent Ready pack registered %d agent_ready_* tools", n)
    return n
