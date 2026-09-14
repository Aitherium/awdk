"""Mesh ops toolpack — mesh_* agent tools.

Codifies the AitherMesh node lifecycle as agent-callable tools so any agent (or
the operator) can drive it without hand-SSHing:
  * mesh_list_nodes        — list registered mesh nodes (fleet endpoint registry)
  * mesh_run_on_node       — run a command on a node over vault-backed SSH
  * mesh_register_endpoint — register a node's endpoint + SSH target in the registry
  * mesh_enroll_node       — enroll a Linux node onto the local Headscale control plane
  * mesh_deploy_agent      — deploy an adk agent onto a node pointed at its own inference

All tools fail soft (return a dict, never raise). SSH creds resolve server-side
from the vault by name — callers never pass keys.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("mesh_ops_pack")

PACK_ID = "mesh_ops"

_TOOL_NAMES = [
    "mesh_list_nodes",
    "mesh_run_on_node",
    "mesh_register_endpoint",
    "mesh_enroll_node",
    "mesh_deploy_agent",
]


def register(registry) -> int:
    """Register all mesh_* tools. Returns the number registered. One bad tool
    never sinks the pack."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("mesh_ops pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("mesh_ops: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool != crash
            logger.debug("mesh_ops: skip tool %s: %s", name, exc)

    logger.info("Mesh ops pack registered %d mesh_* tools", n)
    return n
