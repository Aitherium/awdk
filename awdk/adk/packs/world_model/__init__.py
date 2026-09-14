"""World model pack — observation, surprise, and learn-safely exploration.

Registers three tools:
  - wm_observe: buffer transitions into the world model
  - wm_surprise: compute prediction error (surprise) for anomaly detection
  - wm_status: get model health

Also exposes explore() and CursorWorldAdapter for learn-safely exploration loops
(importable via `from adk.packs.world_model.safe_explore import explore`).

All tools fail-soft dict-returners. In-process fallback (MLPWorldModel) is used
when AITHER_OFFLINE=1 or the remote service is unreachable.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("world_model_pack")

PACK_ID = "world-model"

_TOOL_NAMES = [
    "wm_observe",
    "wm_surprise",
    "wm_status",
    "env_enroll",
]


def register(registry) -> int:
    """Register all wm_* tools. Returns the number registered.

    Each tool fails soft with actionable guidance; one bad tool never sinks
    the pack.
    """
    try:
        from . import tools as t
    except Exception as exc:
        logger.warning(
            "world_model pack unavailable (%s) — 0 tools registered",
            exc
        )
        return 0

    try:
        from .env_enroll import env_enroll as _enroll_fn
        t.env_enroll = _enroll_fn
    except Exception as exc:
        logger.debug("world_model: env_enroll unavailable: %s", exc)

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("world_model: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:
            logger.debug("world_model: skip tool %s: %s", name, exc)

    logger.info("World model pack registered %d wm_* tools", n)
    return n
