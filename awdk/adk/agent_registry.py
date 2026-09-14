"""Local agent registry — discovery for AitherShell and sovereign bootstrap.

Generated agents call ``register_local_agent`` on startup so that
AitherShell and the sovereign fleet resync loop can discover them
without requiring full portal registration.

Registry file: ``~/.aither/agents.json``
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("adk.agent_registry")

_AITHER_DIR = Path.home() / ".aither"
_AGENTS_FILE = _AITHER_DIR / "agents.json"
_IDENTITIES_DIR = _AITHER_DIR / "identities"


def _ensure_dirs() -> None:
    _AITHER_DIR.mkdir(parents=True, exist_ok=True)
    _IDENTITIES_DIR.mkdir(parents=True, exist_ok=True)


def get_or_create_instance_id(project_dir: Path, agent_name: str = "") -> str:
    """Return a stable per-machine instance identifier for this project.

    Stored at ``<project_dir>/.aither/instance_id``.  Format::

        {agent_name}-{sha256(hostname+mac+path)[:8]}-{random8}

    Different from ``agent_id`` (agent TYPE) — this is the instance/node
    identity, unique per machine + project directory.
    """
    import hashlib
    import secrets
    import socket
    import uuid

    dot_aither = project_dir / ".aither"
    dot_aither.mkdir(parents=True, exist_ok=True)
    id_file = dot_aither / "instance_id"

    if id_file.exists():
        stored = id_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    name = agent_name or project_dir.name
    fingerprint = f"{socket.gethostname()}-{uuid.getnode()}-{project_dir}"
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:8]
    rand = secrets.token_hex(4)
    instance_id = f"{name}-{digest}-{rand}"

    id_file.write_text(instance_id, encoding="utf-8")
    log.info("Generated instance_id: %s", instance_id)
    return instance_id


def _load_registry() -> Dict[str, Any]:
    """Load the agents registry from disk."""
    if not _AGENTS_FILE.exists():
        return {"agents": {}, "updated_at": ""}
    try:
        with _AGENTS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "agents" not in data:
            data = {"agents": data, "updated_at": ""}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read agents.json: %s", e)
        return {"agents": {}, "updated_at": ""}


def _save_registry(data: Dict[str, Any]) -> None:
    """Write the agents registry to disk."""
    _ensure_dirs()
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _AGENTS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def register_local_agent(
    name: str,
    port: int,
    *,
    identity_path: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    health_endpoint: str = "/api/health",
    url: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a local agent for AitherShell discovery.

    1. Writes entry to ~/.aither/agents.json (port, health URL, status)
    2. Copies identity YAML to ~/.aither/identities/<name>.yaml if provided
    3. If AITHER_REGISTER_GATEWAY=true, also registers with local gateway

    Returns the agent entry that was written.
    """
    _ensure_dirs()

    agent_url = url or f"http://localhost:{port}"
    entry = {
        "name": name,
        "port": port,
        "url": agent_url,
        "health_url": f"{agent_url}{health_endpoint}",
        "health_endpoint": health_endpoint,
        "capabilities": capabilities or [],
        "status": "active",
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metadata": metadata or {},
    }
    if instance_id:
        entry["instance_id"] = instance_id

    # Copy identity YAML if provided
    if identity_path:
        src = Path(identity_path).expanduser()
        if src.exists():
            dest = _IDENTITIES_DIR / f"{name}.yaml"
            shutil.copy2(src, dest)
            entry["identity_path"] = str(dest)

    # Update registry
    registry = _load_registry()
    registry["agents"][name] = entry
    _save_registry(registry)

    log.info("Registered local agent '%s' at %s", name, agent_url)

    # Optional: register with local gateway
    if os.environ.get("AITHER_REGISTER_GATEWAY", "").lower() in ("true", "1"):
        try:
            _register_with_gateway(name, entry)
        except Exception as e:
            log.debug("Gateway registration skipped: %s", e)

    return entry


def unregister_local_agent(name: str) -> bool:
    """Remove an agent from the local registry."""
    registry = _load_registry()
    if name in registry["agents"]:
        del registry["agents"][name]
        _save_registry(registry)
        # Remove identity file
        identity = _IDENTITIES_DIR / f"{name}.yaml"
        if identity.exists():
            identity.unlink()
        log.info("Unregistered local agent '%s'", name)
        return True
    return False


def list_local_agents() -> Dict[str, Any]:
    """Return all registered local agents."""
    return _load_registry().get("agents", {})


def get_local_agent(name: str) -> Optional[Dict[str, Any]]:
    """Get a specific agent entry."""
    return _load_registry().get("agents", {}).get(name)


def _register_with_gateway(name: str, entry: Dict[str, Any]) -> None:
    """Best-effort registration with local AitherOS gateway."""
    import httpx

    gateway_url = os.environ.get("AITHER_GATEWAY_URL", "http://localhost:8001")
    try:
        with httpx.Client(timeout=5) as client:
            client.post(
                f"{gateway_url}/fleet/agents",
                json={
                    "name": name,
                    "url": entry["url"],
                    "capabilities": entry.get("capabilities", []),
                    "status": "active",
                },
            )
    except Exception as e:
        log.debug("Gateway registration failed: %s", e)
