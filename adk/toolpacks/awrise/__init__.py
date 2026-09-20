"""awrise toolpack — awrise_* agent tools.

Gives any agent (or the operator, via natural language on awsh) the wakes
(recurring background job) scheduler without hand-typing ``awrise`` on the
host: list/inspect/explain/history are read-only; enable/disable/run-now
change an already-vetted job's state; add/set-command PROPOSE a change via a
decision card (``awrise_confirm`` answers it). Every tool goes through the
harness daemon's entitlement-gated ``/wakes`` window — never the CLI directly
and never ``AWRISE_HOME`` on disk. See ``tools.py`` for the full doctrine.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("awrise_pack")

PACK_ID = "awrise"


def register(registry) -> int:
    """Register all awrise_* tools. Returns the number registered. One bad
    tool never sinks the pack."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("awrise pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in t._TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("awrise: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool != crash
            logger.debug("awrise: skip tool %s: %s", name, exc)

    logger.info("awrise pack registered %d awrise_* tools", n)
    return n
