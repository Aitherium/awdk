"""Integration layer for the aw* family of packages.

This module provides a capabilities API that reports which aw* packages are
installed and what capabilities they unlock for the agent. Each package is
optional, degradation is VISIBLE (never silent), and the agent works fine
without any of them.

Usage:
    from adk.integrations import capabilities

    caps = capabilities()
    for cap in caps['installed']:
        print(f"{cap['name']}: {cap['description']}")
"""

from __future__ import annotations

import importlib
from typing import Any


def _check_module(name: str) -> bool:
    """Check if a module is installed without raising ImportError."""
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


#: What each brick UNLOCKS in adk -- adk's own tool names, which the registry cannot
#: know. Everything else about the brick (that it exists, its import name, its
#: description) is read from the vendored family.json (adk.brick_family, generated
#: from ecosystem.yaml). awgraph, awgit and awrelay are hard deps of awdk; the rest
#: are optional.
_UNLOCKS: dict[str, list[str]] = {
    "awgraph": ["code_search", "call_graph"],
    "awgit": ["lease", "stage_mine"],
    "awrelay": ["relay_send", "relay_history"],
    "awm": ["remember", "recall"],
    "awrecover": ["snapshot", "restore"],
    "awseal": ["sign", "verify"],
    "awshare": ["publish", "fetch"],
    "awnest": ["attest", "declare_agent"],
    "awbrowse": ["browse_page", "browse_fill_form"],
    "awfind": ["find_search"],
}


def _family() -> list[tuple[str, str, str, list[str]]]:
    """(name, module, description, unlocks) for each brick adk integrates, in
    registry order. Raises when family.json is missing (FamilyDataMissingError) or no
    longer names a brick adk wires up -- either way the report would be wrong."""
    from adk import brick_family

    fam = brick_family.by_id()
    unknown = sorted(set(_UNLOCKS) - set(fam))
    if unknown:
        raise LookupError(
            f"adk.integrations wires {unknown}, which the family index "
            f"({brick_family.DATA_PATH}) does not register; regenerate family.json "
            f"or drop them here")
    return [
        (bid, fam[bid].get("module") or bid, fam[bid].get("tagline") or bid,
         list(_UNLOCKS[bid]))
        for bid in fam if bid in _UNLOCKS
    ]


def capabilities() -> dict[str, Any]:
    """Report the aw* family capabilities installed in this session.

    Returns:
        A dict with:
        - installed: list of {name, version, description, unlocks}
        - missing: list of {name, description, why_needed}
        - extras_to_install: suggested pip extra to get missing features
    """
    installed = []
    missing = []

    for name, module, description, unlocks in _family():
        if _check_module(module):
            installed.append({
                "name": name,
                "module": module,
                "description": description,
                "unlocks": unlocks,
            })
        else:
            missing.append({
                "name": name,
                "module": module,
                "description": description,
                "unlocks": unlocks,
            })

    # Map missing packages to the extras that install them
    extras_map = {
        "awm": "memory",
        "awrecover": "snapshots",
        "awseal": "seal",
        "awshare": "share",
        "awnest": "nest",
        "awbrowse": "senses",
        "awfind": "senses",
    }

    extras_needed = set()
    for pkg in missing:
        extra = extras_map.get(pkg["name"])
        if extra:
            extras_needed.add(extra)

    return {
        "installed": installed,
        "missing": missing,
        "extras_to_install": sorted(extras_needed),
        "suggest": (f"pip install awdk[{','.join(sorted(extras_needed))}]"
                    if extras_needed else None),
    }


def require_capability(capability: str) -> None:
    """Raise an error if a capability is not available.

    Args:
        capability: The capability name (e.g. "awm", "awbrowse")

    Raises:
        ImportError: If the capability is not installed.
    """
    caps = capabilities()
    installed_names = {p["name"] for p in caps["installed"]}

    if capability not in installed_names:
        missing_pkg = None
        for pkg in caps["missing"]:
            if pkg["name"] == capability:
                missing_pkg = pkg
                break

        if missing_pkg:
            raise ImportError(
                f"{capability} not available: {missing_pkg['description']}\n"
                f"Install with: pip install awdk[{caps['suggest'].split('[')[1].rstrip(']')}]"
            )
        else:
            raise ImportError(f"Unknown capability: {capability}")
