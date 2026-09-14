"""Image bootstrap toolpack — hardware-aware IMAGE GENERATION deployment tools.

The node_bootstrap twin for the visual plane. Registers 6 imagegen_* tools:
detect hardware and report the VRAM capability band, resolve recipes (ComfyUI
6/12/24GB, SANA Sprint, CPU-only, Apple Metal native, vast.ai burst), plan
deployments, apply them, register the backend with Genesis, and verify the
endpoint actually has models loaded. All tools fail-soft dict-returners.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("image_bootstrap_pack")

PACK_ID = "image_bootstrap"

_TOOL_NAMES = [
    "imagegen_detect_hardware",
    "imagegen_resolve_recipe",
    "imagegen_plan_deployment",
    "imagegen_apply",
    "imagegen_register_backend",
    "imagegen_verify",
    "imagegen_setup",
]


def register(registry) -> int:
    """Register all imagegen_* tools. Returns the number registered.

    Each tool fails soft with actionable guidance; one bad tool never sinks
    the pack.
    """
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("image_bootstrap pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("image_bootstrap: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("image_bootstrap: skip tool %s: %s", name, exc)

    logger.info("Image bootstrap pack registered %d imagegen_* tools", n)
    return n
