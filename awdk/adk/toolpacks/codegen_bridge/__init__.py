"""Codegen bridge toolpack — Qwen3.8 as a one-shot code-generation sub-tool.

Registers 1 tool: codegen_generate. See tools.py for the full design rationale —
this is the "Bonsai drives, Qwen3.8 codegens" role split measured and validated in
.PLANS/bonsai-27b-awdk-coder-2026-08-22.md (phases 10-12).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("codegen_bridge_pack")

PACK_ID = "codegen_bridge"

_TOOL_NAMES = [
    "codegen_generate",
    "codegen_diagnose_failure",
    "codegen_reasoning_trace",
    "repl_exec",
    "repl_reset",
    "recurse_query",
]


def register(registry) -> int:
    """Register all codegen_* tools. Returns the number registered."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("codegen_bridge pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("codegen_bridge: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool != crash
            logger.debug("codegen_bridge: skip tool %s: %s", name, exc)

    logger.info("Codegen bridge pack registered %d codegen_* tools", n)
    return n
