"""Brain pack + skill pack discovery for workspace mode.

Discovers installed packs via:
1. ``AGENT_BRAIN_PACK`` env var (explicit path)
2. ``brain_pack.yaml`` in current directory
3. Python entry points group ``aither.brain_packs``
4. ``~/.aither/packs/`` and ``~/.aitheros/packs/`` directory scan
5. Bundled ``adk/workspace-agent.yaml`` fallback

A "pack" is a pip-installable package that ships:
- ``brain_pack.yaml``  — persona, features, doc_types, UI labels, safety
- ``agent.yaml``       — capabilities, portal config, enabled_domains
- ``skills/``          — skill assets (.md files)
- ``packs/``           — tool pack manifests

Customers install: ``pip install awdk aither-pack-myapp``
Then run: ``adk-workspace`` — auto-discovers the brain pack.

Entry point registration in the pack's pyproject.toml:
    [project.entry-points."aither.brain_packs"]
    myapp = "aither_pack_myapp:get_pack_dir"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "discover_brain_pack",
    "discover_agent_yaml",
    "discover_pack_dir",
    "list_available_packs",
    "load_agent_spec",
]

logger = logging.getLogger("adk.pack_discovery")


def _find_library_packs() -> list[dict]:
    """Scan Library/packs/ for brain packs (monorepo / Genesis container)."""
    packs = []
    # Try multiple candidate paths: monorepo layout and Docker container layout
    candidates = [
        Path(os.getenv("AITHER_LIBRARY", "")) / "packs" if os.getenv("AITHER_LIBRARY") else None,
        Path(__file__).resolve().parents[2] / "AitherOS" / "Library" / "packs",  # monorepo
        Path("/app/AitherOS/Library/packs"),  # Docker
    ]
    for packs_dir in candidates:
        if packs_dir is None or not packs_dir.is_dir():
            continue
        for candidate in sorted(packs_dir.iterdir()):
            if candidate.is_dir() and (candidate / "brain_pack.yaml").exists():
                packs.append({
                    "name": candidate.name,
                    "dir": candidate,
                    "source": "library",
                })
                logger.info("Found library brain pack: %s -> %s", candidate.name, candidate)
        break  # Use the first valid packs_dir
    return packs


def _find_entrypoint_packs() -> list[dict]:
    """Discover brain packs registered via Python entry points."""
    packs = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        # Python 3.12+ returns a SelectableGroups; 3.10-3.11 returns dict
        if hasattr(eps, "select"):
            brain_eps = eps.select(group="aither.brain_packs")
        else:
            brain_eps = eps.get("aither.brain_packs", [])

        for ep in brain_eps:
            try:
                get_dir = ep.load()
                pack_dir = Path(get_dir())
                if pack_dir.exists():
                    packs.append({
                        "name": ep.name,
                        "dir": pack_dir,
                        "source": "entrypoint",
                    })
                    logger.info(
                        "Discovered brain pack via entry point: %s -> %s",
                        ep.name,
                        pack_dir,
                    )
            except Exception as e:
                logger.debug("Failed to load entry point %s: %s", ep.name, e)
    except Exception as e:
        logger.debug("Entry point discovery failed: %s", e)
    return packs


def _find_local_packs() -> list[dict]:
    """Scan ~/.aither/packs/ and ~/.aitheros/packs/ for brain packs."""
    packs = []
    candidates = [
        Path(os.getenv("AITHER_PACKS_DIR", os.path.expanduser("~/.aither/packs"))),
        Path.home() / ".aitheros" / "packs",  # ADK CLI install target
    ]
    seen: set[str] = set()
    for packs_dir in candidates:
        if not packs_dir.is_dir():
            continue
        for candidate in sorted(packs_dir.iterdir()):
            if candidate.is_dir() and candidate.name not in seen:
                bp = candidate / "brain_pack.yaml"
                if bp.exists():
                    seen.add(candidate.name)
                    packs.append({
                        "name": candidate.name,
                        "dir": candidate,
                        "source": "local",
                    })
                    logger.info("Found local brain pack: %s -> %s", candidate.name, candidate)
    return packs


#: The bundled pack used as the default when nothing else is selected. Kept as a
#: named constant so the enumeration below and the fallbacks in
#: ``discover_brain_pack``/``discover_pack_dir`` cannot drift apart.
DEFAULT_BUNDLED_PACK = "aither"


def _find_bundled_packs() -> list[dict]:
    """Scan the packs shipped INSIDE the adk wheel (``adk/packs/``).

    Without this, every pack we ship is invisible to anyone who installed adk
    normally: the other scans resolve only in the monorepo (``Library/packs``),
    in a container, from a separately pip-installed pack (entry points), or from
    a directory the user populated themselves (``~/.aither/packs``). A plain
    ``pip install aither-adk`` matches none of those, so ``list_available_packs``
    returned nothing for the packs the wheel had just placed on disk — the files
    were there and there was no way to find out.

    Enumeration only. Which pack is the DEFAULT is decided by
    ``DEFAULT_BUNDLED_PACK``, never by this scan's ordering: picking the first
    directory alphabetically would silently hand new installs a different agent
    than the one they got before.
    """
    packs = []
    packs_dir = Path(__file__).resolve().parent / "packs"
    if not packs_dir.is_dir():
        return packs
    for candidate in sorted(packs_dir.iterdir()):
        if candidate.is_dir() and (candidate / "brain_pack.yaml").exists():
            packs.append({
                "name": candidate.name,
                "dir": candidate,
                "source": "bundled",
            })
    return packs


def discover_brain_pack() -> Optional[Path]:
    """Find the best brain pack YAML to use.

    Returns the path to a brain_pack.yaml, or None if only defaults should be used.

    Priority:
    1. AGENT_BRAIN_PACK env var
    2. brain_pack.yaml in CWD
    3. Entry-point registered packs (first found)
    4. ~/.aither/packs/<name>/brain_pack.yaml (first found)
    5. Bundled aither pack (shipped with ADK)
    6. None (use defaults)
    """
    # 1. Explicit env var
    env_path = os.getenv("AGENT_BRAIN_PACK")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            logger.info("Brain pack from AGENT_BRAIN_PACK: %s", p)
            return p
        logger.warning("AGENT_BRAIN_PACK=%s not found", env_path)

    # 2. CWD
    cwd_pack = Path.cwd() / "brain_pack.yaml"
    if cwd_pack.exists():
        logger.info("Brain pack from CWD: %s", cwd_pack)
        return cwd_pack

    # 3. Library/packs/ (monorepo / Genesis container)
    lib_packs = _find_library_packs()
    if lib_packs:
        bp = lib_packs[0]["dir"] / "brain_pack.yaml"
        if bp.exists():
            return bp

    # 4. Entry points
    ep_packs = _find_entrypoint_packs()
    if ep_packs:
        bp = ep_packs[0]["dir"] / "brain_pack.yaml"
        if bp.exists():
            return bp

    # 5. Local packs dir
    local_packs = _find_local_packs()
    if local_packs:
        bp = local_packs[0]["dir"] / "brain_pack.yaml"
        if bp.exists():
            return bp

    # 6. Bundled aither pack (shipped with ADK) — default fallback
    aither_pack = (
        Path(__file__).resolve().parent / "packs" / DEFAULT_BUNDLED_PACK / "brain_pack.yaml"
    )
    if aither_pack.exists():
        logger.info("Brain pack from bundled aither pack: %s", aither_pack)
        return aither_pack

    return None


def discover_agent_yaml() -> Optional[Path]:
    """Find the best agent.yaml to use.

    Same priority as discover_brain_pack but looks for agent.yaml.
    Falls back to the bundled workspace-agent.yaml in the ADK package.
    """
    # Explicit env
    env_path = os.getenv("AGENT_YAML")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    # CWD
    cwd = Path.cwd() / "agent.yaml"
    if cwd.exists():
        return cwd

    # Library packs
    lib_packs = _find_library_packs()
    if lib_packs:
        ay = lib_packs[0]["dir"] / "agent.yaml"
        if ay.exists():
            return ay

    # Entry-point pack
    ep_packs = _find_entrypoint_packs()
    if ep_packs:
        ay = ep_packs[0]["dir"] / "agent.yaml"
        if ay.exists():
            return ay

    # Local packs
    local_packs = _find_local_packs()
    if local_packs:
        ay = local_packs[0]["dir"] / "agent.yaml"
        if ay.exists():
            return ay

    # Bundled fallback (shipped with ADK)
    bundled = Path(__file__).resolve().parent.parent / "agent.yaml"
    if bundled.exists():
        return bundled

    # Package data fallback
    pkg_bundled = Path(__file__).resolve().parent / "workspace-agent.yaml"
    if pkg_bundled.exists():
        return pkg_bundled

    return None


def discover_pack_dir() -> Optional[Path]:
    """Find the pack directory (contains skills/, packs/, brain_pack.yaml).

    Used by workspace mode to set AGENT_BRAIN_PACK and load skills/tool packs.
    """
    # Library packs (monorepo / container)
    lib_packs = _find_library_packs()
    if lib_packs:
        return lib_packs[0]["dir"]

    # Entry-point packs
    ep_packs = _find_entrypoint_packs()
    if ep_packs:
        return ep_packs[0]["dir"]

    # Local packs
    local_packs = _find_local_packs()
    if local_packs:
        return local_packs[0]["dir"]

    # CWD if it has a brain_pack.yaml
    if (Path.cwd() / "brain_pack.yaml").exists():
        return Path.cwd()

    # Bundled default, so a plain `pip install aither-adk` resolves a pack dir
    # instead of None. Pinned by name to agree with discover_brain_pack.
    for p in _find_bundled_packs():
        if p["name"] == DEFAULT_BUNDLED_PACK:
            return p["dir"]

    return None


def list_available_packs() -> list[dict]:
    """List all discoverable packs (for CLI/UI display)."""
    # Bundled LAST: dedup keeps the first of each name, so a pack the user
    # installed or wrote themselves still wins over the copy in our wheel.
    packs = (
        _find_library_packs()
        + _find_entrypoint_packs()
        + _find_local_packs()
        + _find_bundled_packs()
    )

    # Deduplicate by name
    seen = set()
    unique = []
    for p in packs:
        if p["name"] not in seen:
            seen.add(p["name"])
            unique.append(p)
    return unique


def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay dict into base dict.

    For scalar keys, overlay wins. For lists (capabilities, enabled_domains),
    overlay REPLACES (not merged).
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            # Scalar or list: overlay wins (replaces entirely for lists)
            result[key] = value
    return result


def load_agent_spec(agent_yaml_path: Optional[Path] = None) -> dict[str, Any]:
    """Load agent.yaml and deep-merge in agent.yaml.local if it exists.

    Args:
        agent_yaml_path: Optional explicit path to agent.yaml. If None, uses discover_agent_yaml.

    Returns:
        Merged spec dict. Local values override base values (scalars replace; lists replace).
        Returns {} if no agent.yaml found.

    Raises:
        No exceptions — failures are logged and return empty dict.
    """
    import yaml

    try:
        # Resolve the base agent.yaml
        yaml_path = agent_yaml_path or discover_agent_yaml()
        if not yaml_path or not yaml_path.exists():
            logger.debug("No agent.yaml found")
            return {}

        # Load base spec
        try:
            base = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("Failed to load agent.yaml at %s: %s", yaml_path, e)
            return {}

        # Check for local override
        local_yaml = yaml_path.parent / "agent.yaml.local"
        if local_yaml.exists():
            try:
                local = yaml.safe_load(local_yaml.read_text(encoding="utf-8")) or {}
                logger.debug("Merging agent.yaml.local from %s", local_yaml)
                base = _deep_merge_dicts(base, local)
            except Exception as e:
                logger.warning("Failed to load agent.yaml.local at %s: %s", local_yaml, e)

        return base
    except Exception as e:
        logger.warning("load_agent_spec failed: %s", e)
        return {}
