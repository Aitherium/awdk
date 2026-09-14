"""AitherZero toolpack — az_* tools for self-service infra config + provisioning.

Registers 7 az_* tools that let an awdk agent manage the AitherZero surface:
inventory the automation-scripts + playbooks (az_inventory / az_describe_script),
regenerate the schema from any (public or private) inventory (az_export_schema),
generate + validate a config.local.psd1 (az_generate_config / az_validate_config), plan a
playbook (az_plan_playbook), and scaffold a new automation-script (az_scaffold_script).
All tools fail soft (return dicts, never raise).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aitherzero_pack")

PACK_ID = "aitherzero"

_TOOL_NAMES = [
    "az_inventory",
    "az_describe_script",
    "az_export_schema",
    "az_generate_config",
    "az_validate_config",
    "az_plan_playbook",
    "az_scaffold_script",
]


def register(registry) -> int:
    """Register all az_* tools. Returns the number registered. One bad tool never
    sinks the pack."""
    try:
        from . import tools as t
    except Exception as exc:  # noqa: BLE001 — import failure = 0 tools
        logger.warning("aitherzero pack unavailable (%s) — 0 tools registered", exc)
        return 0

    n = 0
    for name in _TOOL_NAMES:
        fn = getattr(t, name, None)
        if not callable(fn):
            logger.debug("aitherzero: missing tool %s", name)
            continue
        try:
            registry.register(fn)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one bad tool ≠ crash
            logger.debug("aitherzero: skip tool %s: %s", name, exc)

    logger.info("AitherZero pack registered %d az_* tools", n)
    return n
