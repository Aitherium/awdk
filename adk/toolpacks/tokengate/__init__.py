"""TokenGate toolpack — ask a long document, get the answer plus the exact passages.

Registers two ``tokengate_*`` tools. ``tokengate_ask`` sends a document and a
question to the TokenGate document lane and returns the lane's answer, the ranked
evidence passages (with character offsets into the document that was sent), and
the measured token budget. ``tokengate_status`` reports whether the lane is up,
loaded, and holding a GPU lease.

The pack is self-contained: it speaks the lane's plain-HTTP contract with the
stdlib and imports nothing from the AitherOS monorepo, so a ``pip install awdk``
gets a working client for any lane that implements the contract (see README.md).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tokengate_pack")

PACK_ID = "tokengate"

_TOOL_NAMES = ["tokengate_ask", "tokengate_status"]


def register(registry) -> int:
    """Register all tokengate_* tools. Returns the number registered.

    Each tool fails soft with a typed error dict; one bad tool never sinks the
    pack and a lane that is down never raises into the agent's loop.
    """
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("tokengate pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("tokengate: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool != crash
            logger.debug("tokengate: skip tool %s: %s", name, exc)

    logger.info("TokenGate pack registered %d tokengate_* tools", n)
    return n
