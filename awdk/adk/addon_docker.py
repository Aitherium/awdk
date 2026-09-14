"""Docker wrapper for addon container management.

Uses the ``docker`` Python SDK (docker-py) for programmatic container
lifecycle operations.  All functions are synchronous — the caller
(AddonManager) wraps them in async context.

Install: ``pip install docker>=7.0``
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("adk.addon_docker")

try:
    import docker
    from docker.errors import ImageNotFound, NotFound, APIError
    _DOCKER_AVAILABLE = True
except ImportError:
    _DOCKER_AVAILABLE = False


def _get_client() -> "docker.DockerClient":
    if not _DOCKER_AVAILABLE:
        raise RuntimeError(
            "docker package not installed. Run: pip install docker>=7.0"
        )
    return docker.from_env()


def pull_image(image: str) -> None:
    """Pull a Docker image (no-op if already present and up-to-date)."""
    client = _get_client()
    log.info("Pulling image %s ...", image)
    try:
        client.images.pull(image)
        log.info("Image %s pulled successfully", image)
    except Exception as e:
        log.warning("Image pull failed for %s: %s (trying with local cache)", image, e)


def start_container(
    image: str,
    name: str,
    port: int,
    env: Optional[Dict[str, str]] = None,
    volumes: Optional[Dict[str, Dict[str, str]]] = None,
    network: str = "aitheros_default",
) -> str:
    """Start a container and return its ID.

    If a container with the same name exists, it is stopped and removed first.
    """
    client = _get_client()

    # Remove existing container with same name
    try:
        existing = client.containers.get(name)
        log.info("Removing existing container %s", name)
        existing.stop(timeout=10)
        existing.remove(force=True)
    except NotFound:
        pass
    except Exception as e:
        log.warning("Could not remove existing container %s: %s", name, e)

    # Ensure network exists
    try:
        client.networks.get(network)
    except NotFound:
        log.info("Creating network %s", network)
        client.networks.create(network, driver="bridge")

    container = client.containers.run(
        image,
        name=name,
        detach=True,
        ports={f"{port}/tcp": port},
        environment=env or {},
        volumes=volumes or {},
        network=network,
        restart_policy={"Name": "unless-stopped"},
    )
    log.info("Started container %s (id=%s)", name, container.short_id)
    return container.id


def stop_container(container_id: str, timeout: int = 10) -> None:
    """Stop and remove a container by ID."""
    client = _get_client()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=timeout)
        container.remove(force=True)
        log.info("Stopped and removed container %s", container_id[:12])
    except NotFound:
        log.info("Container %s not found (already removed?)", container_id[:12])
    except Exception as e:
        log.warning("Failed to stop container %s: %s", container_id[:12], e)


def container_logs(container_id: str, tail: int = 100) -> str:
    """Return recent logs from a container."""
    client = _get_client()
    try:
        container = client.containers.get(container_id)
        return container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except NotFound:
        return f"Container {container_id[:12]} not found"
    except Exception as e:
        return f"Error reading logs: {e}"


def container_health(container_id: str) -> Dict[str, Any]:
    """Return health and status info for a container."""
    client = _get_client()
    try:
        container = client.containers.get(container_id)
        state = container.attrs.get("State", {})
        return {
            "id": container.short_id,
            "name": container.name,
            "status": container.status,
            "running": state.get("Running", False),
            "health": state.get("Health", {}).get("Status", "unknown"),
            "started_at": state.get("StartedAt", ""),
            "exit_code": state.get("ExitCode", -1),
        }
    except NotFound:
        return {"id": container_id[:12], "status": "not_found", "running": False}
    except Exception as e:
        return {"id": container_id[:12], "status": "error", "error": str(e)}


def list_addon_containers(prefix: str = "aitheros-addon-") -> List[Dict[str, Any]]:
    """List all running addon containers (by name prefix)."""
    client = _get_client()
    results = []
    for container in client.containers.list(all=True):
        if container.name.startswith(prefix):
            results.append({
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "image": container.image.tags[0] if container.image.tags else "unknown",
            })
    return results
