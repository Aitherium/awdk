"""Generate docker-compose.addons.yml from addon manifests.

Reads addon manifests, resolves the dependency graph via topological sort,
and emits a Compose file with proper networking, volumes, health checks,
and environment variable rewriting so inter-addon URLs use service names.

Usage::

    from adk.addon_compose import generate_addon_compose
    path = generate_addon_compose(["qdrant", "knowledge-rag"], Path("compose.yml"))
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from adk.addon_manager import load_addon_manifest, load_all_manifests

log = logging.getLogger("adk.addon_compose")

NETWORK_NAME = "aitheros_default"


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------

def _resolve_deps(addon_ids: list[str]) -> list[str]:
    """Return *addon_ids* plus all transitive dependencies, topologically sorted.

    Dependencies appear before the addons that require them.
    Raises ``ValueError`` on cycles or missing manifests.
    """
    # Collect the full set of IDs we need (including transitive deps)
    needed: dict[str, Dict[str, Any]] = {}
    queue = deque(addon_ids)
    while queue:
        aid = queue.popleft()
        if aid in needed:
            continue
        manifest = load_addon_manifest(aid)
        if not manifest:
            raise ValueError(f"Unknown addon: {aid}")
        needed[aid] = manifest
        for dep in manifest.get("dependencies", []):
            if dep not in needed:
                queue.append(dep)

    # Build adjacency list and in-degree map for Kahn's algorithm
    in_degree: dict[str, int] = {aid: 0 for aid in needed}
    dependents: dict[str, list[str]] = defaultdict(list)
    for aid, m in needed.items():
        for dep in m.get("dependencies", []):
            dependents[dep].append(aid)
            in_degree[aid] += 1

    # Kahn's topological sort
    ready = deque(aid for aid, deg in in_degree.items() if deg == 0)
    order: list[str] = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for child in dependents[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)

    if len(order) != len(needed):
        raise ValueError(
            f"Dependency cycle detected among addons: "
            f"{set(needed) - set(order)}"
        )
    return order


# ---------------------------------------------------------------------------
# Interval parsing
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^(\d+)([smh]?)$")


def _parse_interval(raw: str) -> str:
    """Normalise a manifest interval like '30s' into compose-compatible form."""
    m = _INTERVAL_RE.match(raw.strip())
    if not m:
        return "30s"
    val, unit = m.group(1), m.group(2) or "s"
    return f"{val}{unit}"


# ---------------------------------------------------------------------------
# Service name helper
# ---------------------------------------------------------------------------

def _svc_name(addon_id: str) -> str:
    return f"aitheros-addon-{addon_id}"


# ---------------------------------------------------------------------------
# Environment variable rewriting
# ---------------------------------------------------------------------------

def _rewrite_env(
    manifest: Dict[str, Any],
    overrides: Dict[str, Any],
    all_manifests: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    """Build environment dict, rewriting dependency URLs to compose service names."""
    env = dict(manifest.get("env_defaults", {}))
    env.update(overrides.get("env", {}))

    deps = manifest.get("dependencies", [])
    for dep_id in deps:
        dep_manifest = all_manifests.get(dep_id)
        if not dep_manifest:
            continue
        dep_port = dep_manifest.get("default_port", 8000)
        dep_svc = _svc_name(dep_id)
        # Rewrite any env value referencing the dependency by hostname patterns
        for key, val in list(env.items()):
            if not isinstance(val, str):
                continue
            # Replace patterns like http://qdrant:PORT, http://localhost:PORT,
            # or just the bare dep_id hostname
            val = re.sub(
                rf"http://(?:localhost|{re.escape(dep_id)}):{dep_port}",
                f"http://{dep_svc}:{dep_port}",
                val,
            )
            env[key] = val

    return env


# ---------------------------------------------------------------------------
# Compose service builder
# ---------------------------------------------------------------------------

def _build_service(
    manifest: Dict[str, Any],
    all_manifests: Dict[str, Dict[str, Any]],
    overrides: Dict[str, Any],
    tag: str,
) -> Dict[str, Any]:
    """Build a single compose service dict from an addon manifest."""
    addon_id = manifest["id"]
    svc: Dict[str, Any] = {}

    # Image
    image = manifest.get("image", "")
    if image and tag != "latest" and ":" in image:
        image = image.rsplit(":", 1)[0] + f":{tag}"
    elif image and tag != "latest":
        image = f"{image}:{tag}"
    svc["image"] = image
    svc["container_name"] = _svc_name(addon_id)

    # Ports
    port = manifest.get("default_port")
    if port:
        svc["ports"] = [f"{port}:{port}"]

    # Volumes
    volumes_spec = manifest.get("volumes", [])
    if volumes_spec:
        svc["volumes"] = []
        for vol in volumes_spec:
            vol_name = f"aitheros-addon-{addon_id}-{vol['name']}"
            svc["volumes"].append(f"{vol_name}:{vol['path']}")

    # Environment
    env = _rewrite_env(manifest, overrides, all_manifests)
    if env:
        svc["environment"] = {k: str(v) for k, v in env.items()}

    # depends_on (with health condition where possible)
    deps = manifest.get("dependencies", [])
    if deps:
        depends_on = {}
        for dep_id in deps:
            dep_manifest = all_manifests.get(dep_id, {})
            if dep_manifest.get("health_check", {}).get("path"):
                depends_on[_svc_name(dep_id)] = {"condition": "service_healthy"}
            else:
                depends_on[_svc_name(dep_id)] = {"condition": "service_started"}
        svc["depends_on"] = depends_on

    # Health check
    hc = manifest.get("health_check", {})
    hc_path = hc.get("path")
    if hc_path and port:
        interval = _parse_interval(hc.get("interval", "30s"))
        svc["healthcheck"] = {
            "test": ["CMD", "curl", "-sf", f"http://localhost:{port}{hc_path}"],
            "interval": interval,
            "timeout": "5s",
            "start_period": "15s",
            "retries": 3,
        }

    # GPU resources
    resources = manifest.get("resources", {})
    if resources.get("gpu"):
        svc["deploy"] = {
            "resources": {
                "reservations": {
                    "devices": [{
                        "driver": "nvidia",
                        "count": 1,
                        "capabilities": ["gpu"],
                    }]
                }
            }
        }

    # Restart policy + host access
    svc["restart"] = "unless-stopped"
    svc["extra_hosts"] = ["host.docker.internal:host-gateway"]
    svc["networks"] = ["aitheros"]

    return svc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_addon_compose(
    addon_ids: list[str],
    output: Path,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    tag: str = "latest",
) -> Path:
    """Generate a ``docker-compose.addons.yml`` for the requested addons.

    Args:
        addon_ids: Addon IDs to include (dependencies auto-resolved).
        output: File path to write the compose YAML.
        overrides: Per-addon overrides keyed by addon_id.
            Each value may contain ``{"env": {"KEY": "VAL"}}``.
        tag: Docker image tag override (default: latest).

    Returns:
        The *output* path (for chaining).

    Raises:
        ValueError: Unknown addon or dependency cycle.
    """
    overrides = overrides or {}
    ordered = _resolve_deps(addon_ids)

    # Load all needed manifests into a dict
    all_manifests: Dict[str, Dict[str, Any]] = {}
    for aid in ordered:
        m = load_addon_manifest(aid)
        if m:
            all_manifests[aid] = m

    # Build compose structure
    services: Dict[str, Any] = {}
    volumes: Dict[str, Any] = {}

    for aid in ordered:
        manifest = all_manifests[aid]
        addon_overrides = overrides.get(aid, {})
        svc = _build_service(manifest, all_manifests, addon_overrides, tag)
        services[_svc_name(aid)] = svc

        # Collect named volumes
        for vol in manifest.get("volumes", []):
            vol_name = f"aitheros-addon-{aid}-{vol['name']}"
            volumes[vol_name] = None  # use default driver

    compose = {
        "services": services,
    }
    if volumes:
        compose["volumes"] = {k: None for k in volumes}
    compose["networks"] = {
        "aitheros": {
            "name": NETWORK_NAME,
            "external": True,
        }
    }

    # Write with a header comment
    output.parent.mkdir(parents=True, exist_ok=True)
    header = "# Auto-generated by AitherADK addon deployer — do not edit manually\n"
    with open(output, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    log.info("Generated addon compose: %s (%d services)", output, len(services))
    return output


def list_available_addons(plan_tier: Optional[str] = None) -> list[Dict[str, Any]]:
    """Return addon manifests, optionally filtered by plan tier."""
    tier_order = {"free": 0, "developer": 1, "professional": 2, "enterprise": 3}
    tier_level = tier_order.get(plan_tier or "enterprise", 3)
    manifests = load_all_manifests()
    if plan_tier:
        manifests = [
            m for m in manifests
            if tier_order.get(m.get("requires_plan", "free"), 0) <= tier_level
        ]
    return manifests
