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


def capabilities() -> dict[str, Any]:
    """Report the aw* family capabilities installed in this session.

    Returns:
        A dict with:
        - installed: list of {name, version, description, unlocks}
        - missing: list of {name, description, why_needed}
        - extras_to_install: suggested pip extra to get missing features
    """
    # The family, in (name, module, description, unlocks) tuples.
    # Hard deps (awgraph, awgit, awrelay) are always present.
    # The rest are optional.
    _FAMILY = [
        ("awgraph", "awgraph", "Code graph: what the code is and dependencies", ["code_search", "call_graph"]),
        ("awgit", "awgit", "Edit ops and leases: version control for shared checkout", ["lease", "stage_mine"]),
        ("awrelay", "awrelay", "Agent messaging: post findings to shared channels", ["relay_send", "relay_history"]),
        ("awm", "awm", "Portable scoped memory: tenant:user:project", ["remember", "recall"]),
        ("awrecover", "awrecover", "Snapshots with all-or-nothing restore", ["snapshot", "restore"]),
        ("awseal", "awseal", "Sign artifacts for verification", ["sign", "verify"]),
        ("awshare", "awshare", "Publish and fetch with verification", ["publish", "fetch"]),
        ("awnest", "awnest", "Human attestation and gates", ["attest", "declare_agent"]),
        ("awbrowse", "awbrowse", "Browser automation: navigate, fill, extract", ["browse_page", "browse_fill_form"]),
        ("awfind", "awfind", "Search client: ranked results", ["find_search"]),
    ]

    installed = []
    missing = []

    for name, module, description, unlocks in _FAMILY:
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
        "suggest": f"pip install awdk[{','.join(sorted(extras_needed))}]" if extras_needed else None,
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
