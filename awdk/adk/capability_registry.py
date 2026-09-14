"""AitherCapability registry — query interface for generated capability mirrors.

This module reads the generated capability mirror
(adk/aither_capability_generated.py) and provides query functions. It is fail-soft:
a missing or unreadable mirror returns an empty set with a logged warning.

The generated mirror is the ONLY source of truth for adk (which ships to PyPI and
cannot import the monorepo `lib` package). A capability that lives in the manifest
and reaches this registry MUST also appear in the TypeScript and JavaScript mirrors
— that round-trip is asserted by check_capability_protocol_parity.py (CP004).

Usage:
    from adk.capability_registry import all_capabilities, find_capability, by_agent
    caps = all_capabilities()
    agent_caps = by_agent("demiurge")
    tool_cap = find_capability("acp")
"""

from __future__ import annotations

import logging
from typing import List, Optional

# Try to import the generated mirror; fail-soft if it's missing or broken.
try:
    from adk.aither_capability_generated import CAPABILITIES, Capability
except ImportError as e:
    logging.warning(
        "Capability mirror not found or unreadable; "
        "returning empty set. Error: %s", e
    )
    # Define stub to allow module to import
    CAPABILITIES = []

    class Capability:  # type: ignore
        """Stub Capability when mirror is unavailable."""
        pass


logger = logging.getLogger(__name__)


def all_capabilities() -> List[Capability]:
    """Return all capabilities.

    Returns:
        List of all Capability objects from the generated mirror.
        Empty list if mirror is unavailable.
    """
    return list(CAPABILITIES)


def find_capability(cap_id: str) -> Optional[Capability]:
    """Find a capability by ID.

    Args:
        cap_id: The capability ID to search for.

    Returns:
        The Capability object if found, None otherwise.
    """
    for cap in CAPABILITIES:
        if cap.id == cap_id:
            return cap
    return None


def by_agent(agent: str) -> List[Capability]:
    """Find all capabilities owned by an agent.

    Args:
        agent: The owning agent name.

    Returns:
        List of Capability objects owned by the agent. Empty list if none found.
    """
    return [c for c in CAPABILITIES if c.owning_agent == agent]


def by_kind(kind: str) -> List[Capability]:
    """Find all capabilities of a given kind.

    Args:
        kind: The capability kind (e.g., 'toolpack', 'service', 'app', 'builtin').

    Returns:
        List of Capability objects of the given kind. Empty list if none found.
    """
    return [c for c in CAPABILITIES if c.kind == kind]


def available() -> List[Capability]:
    """Return all available capabilities.

    Returns:
        List of Capability objects with available=True.
    """
    return [c for c in CAPABILITIES if c.available]


def unavailable() -> List[Capability]:
    """Return all unavailable capabilities.

    Returns:
        List of Capability objects with available=False.
    """
    return [c for c in CAPABILITIES if not c.available]


def by_tool_pattern(pattern: str) -> List[Capability]:
    """Find capabilities providing tools that match a pattern.

    The pattern is a simple substring match (e.g., 'mesh_*' matches 'mesh_list').
    Capabilities with wildcard tools (e.g., ['mesh_*']) will match any prefix.

    Args:
        pattern: A tool name or prefix to search for.

    Returns:
        List of Capability objects containing matching tools.
    """
    results = []
    for cap in CAPABILITIES:
        for tool in cap.tools:
            # Handle wildcard patterns: 'mesh_*' matches 'mesh_list'
            if tool.endswith("*"):
                prefix = tool[:-1]  # Remove trailing '*'
                if pattern.startswith(prefix):
                    results.append(cap)
                    break
            elif tool == pattern:
                results.append(cap)
                break
    return results


def by_tier(tier: str) -> List[Capability]:
    """Find all capabilities at a given tier.

    Args:
        tier: The tier name (e.g., 'free', 'pro', 'enterprise').

    Returns:
        List of Capability objects at the given tier.
    """
    return [c for c in CAPABILITIES if c.tier == tier]


def all_agents() -> set[str]:
    """Return all unique agent IDs from capabilities.

    Returns:
        Set of owning agent names.
    """
    return {c.owning_agent for c in CAPABILITIES}


def all_tools() -> set[str]:
    """Return all unique tool names from capabilities.

    Note: This expands wildcard patterns (e.g., 'mesh_*' becomes a single entry).

    Returns:
        Set of all tool names/patterns from all capabilities.
    """
    tools = set()
    for cap in CAPABILITIES:
        tools.update(cap.tools)
    return tools


def count() -> int:
    """Return the total number of capabilities.

    Returns:
        Count of capabilities in the mirror.
    """
    return len(CAPABILITIES)
