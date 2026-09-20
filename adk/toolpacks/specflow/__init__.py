"""Specflow tool pack — spec-driven change workflow as spec_* agent tools.

A change is a directory under ``openspec/changes/<change-id>/``: ``proposal.md``
(why, what, capabilities, prioritised user stories) -> ``specs/<capability>/
spec.md`` (a delta of ADDED / MODIFIED / REMOVED requirements with WHEN/THEN
scenarios) -> ``design.md`` (optional) -> ``tasks.md``. Living specs accumulate
under ``openspec/specs/<capability>/spec.md``; archiving merges a change's
deltas into them and moves the change under ``openspec/changes/archive/``.

The shape is adapted from OpenSpec (https://github.com/Fission-AI/OpenSpec,
MIT) and the constitution / prioritised-story rules from spec-kit
(https://github.com/github/spec-kit, MIT). Nothing upstream is vendored; the
parser is ``deltas.py``, the tools are ``tools.py``, the CLI is ``cli.py``.
"""
from __future__ import annotations

import logging

from .tools import (
    TOOLS,
    spec_archive,
    spec_export,
    spec_new,
    spec_status,
    spec_tasks,
    spec_validate,
)

logger = logging.getLogger("specflow")

PACK_ID = "specflow"

__all__ = [
    "PACK_ID",
    "register",
    "spec_archive",
    "spec_export",
    "spec_new",
    "spec_status",
    "spec_tasks",
    "spec_validate",
]


def register(registry) -> int:
    """Register the six spec_* tools. Free tier: no gate, no network."""
    n = 0
    for fn in TOOLS:
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:
            logger.debug("specflow: skip tool %s: %s", getattr(fn, "__name__", "?"), exc)
    logger.info("Specflow registered %d spec_* tools", n)
    return n
