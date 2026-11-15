"""NVIDIA NIM (hosted build.nvidia.com API) tool pack — auto-generated from the component spec.

Optional and fail-soft: with no NVIDIA credential the pack still imports and
every tool reports not_authenticated. Regenerate, never hand-edit.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nim_pack")

PACK_ID = "nvidia_nim"

_TOOL_NAMES = [
    "nim_list_models",
    "nim_chat",
    "nim_check_health",
]


def register(registry) -> int:
    """Register every nim_* tool. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools, not a crash
        logger.warning("nvidia_nim pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("nvidia_nim: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool must not sink the pack
            logger.debug("nvidia_nim: skip tool %s: %s", name, exc)

    logger.info("NVIDIA NIM (hosted build.nvidia.com API) pack registered %d tools", n)
    return n
