"""
Elysium Connect — One-command bootstrap to join your desktop AitherOS mesh.
============================================================================

Connects a laptop/remote ADK node to a running AitherOS desktop instance
("Elysium") for distributed inference, mesh hosting, and tool sharing.

Usage::

    from adk.elysium_connect import connect_to_desktop

    result = await connect_to_desktop("http://desktop:8001")
    # result: {"ok": True, "node_id": "...", "mesh_joined": True, ...}

CLI::

    adk connect --elysium http://desktop:8001
    adk connect --elysium http://desktop:8001 --token <node_token>
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from adk.config import load_saved_config, save_saved_config

logger = logging.getLogger("adk.elysium_connect")

# Service ports on the desktop (direct mode)
_DESKTOP_PORTS = {
    "genesis": 8001,
    "mesh": 8125,
    "tunnel": 8310,
    "microscheduler": 8150,
    "node": 8080,
}


def _hardware_inventory() -> dict[str, Any]:
    """Collect local hardware info for mesh registration."""
    import sys

    info: dict[str, Any] = {
        "hostname": platform.node(),
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 1,
    }

    # GPU detection
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            gpus = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
            info["gpu_count"] = len(gpus)
            info["gpus"] = gpus
    except Exception:
        info["gpu_count"] = 0

    # Capabilities based on hardware
    caps = ["mcp"]
    if info.get("gpu_count", 0) > 0:
        caps.append("inference")
    info["capabilities"] = caps

    return info


async def connect_to_desktop(
    url: str,
    token: str | None = None,
    node_name: str | None = None,
    skip_wireguard: bool = False,
) -> dict[str, Any]:
    """Connect this node to a desktop AitherOS instance.

    Steps:
        1. Health check Genesis on the desktop
        2. Get or use a node token
        3. Join the AitherMesh
        4. Optionally set up WireGuard tunnel
        5. Save config to ~/.aither/config.json

    Args:
        url: Desktop Genesis URL (e.g. http://192.168.1.10:8001)
        token: Pre-generated node token. If None, requests one from Genesis.
        node_name: Node name for mesh registration. Defaults to hostname.
        skip_wireguard: Skip WireGuard setup even if available.

    Returns:
        Result dict with connection details.
    """
    url = url.rstrip("/")
    name = node_name or platform.node()
    hw = _hardware_inventory()
    result: dict[str, Any] = {"ok": False, "url": url}

    # ── 1. Health check ──────────────────────────────────────────────
    logger.info("Checking Genesis at %s...", url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code != 200:
                result["error"] = f"Genesis health check failed: HTTP {resp.status_code}"
                return result
    except Exception as e:
        result["error"] = f"Cannot reach Genesis at {url}: {e}"
        return result

    logger.info("Genesis reachable at %s", url)

    # ── 2. Get node token ────────────────────────────────────────────
    if not token:
        logger.info("Requesting node token from Genesis...")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{url}/admin/nodes/create",
                    json={
                        "node_name": name,
                        "capabilities": hw["capabilities"],
                        "hardware": {
                            "os": hw["os"],
                            "arch": hw["arch"],
                            "cpu_count": hw["cpu_count"],
                            "gpu_count": hw.get("gpu_count", 0),
                        },
                    },
                )
                if resp.status_code in (200, 201):
                    data = resp.json()
                    token = data.get("token") or data.get("node_token", "")
                    result["node_id"] = data.get("node_id", "")
                    logger.info("Got node token: %s...", token[:16] if token else "none")
                else:
                    logger.warning("Token request failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("Token request error: %s", e)

    if not token:
        # Try loading from saved config
        saved = load_saved_config()
        token = saved.get("node_token", "")

    result["token"] = token or ""

    # ── 3. Join mesh ─────────────────────────────────────────────────
    # Extract base host from Genesis URL for mesh port
    base_host = url.rsplit(":", 1)[0]  # http://192.168.1.10
    mesh_url = f"{base_host}:{_DESKTOP_PORTS['mesh']}"

    mesh_joined = False
    try:
        from adk.federation import FederationClient

        fed = FederationClient(host=base_host, mode="direct")
        if token:
            fed._creds.token = token
        mesh_joined = await fed.join_mesh(
            capabilities=hw["capabilities"],
            role="client",
        )
        if mesh_joined:
            result["node_id"] = result.get("node_id") or fed._node_id
            logger.info("Joined mesh as %s", result["node_id"])
    except Exception as e:
        logger.warning("Mesh join via federation failed: %s, trying direct...", e)

    if not mesh_joined:
        # Direct mesh join fallback
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{mesh_url}/aithernet/nodes/join",
                    json={
                        "hostname": name,
                        "wg_public_key": f"adk-{name}",
                        "services": hw["capabilities"],
                        "metadata": {
                            "type": "adk_laptop",
                            "platform": hw["os"],
                            "gpu_count": hw.get("gpu_count", 0),
                        },
                    },
                    headers={
                        "Authorization": f"Bearer {token}" if token else "",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["node_id"] = data.get("node_id", result.get("node_id", ""))
                    mesh_joined = True
                    logger.info("Joined mesh directly: %s", result["node_id"])
        except Exception as e:
            logger.warning("Direct mesh join failed: %s", e)

    result["mesh_joined"] = mesh_joined

    # ── 4. WireGuard (optional) ──────────────────────────────────────
    wg_configured = False
    if not skip_wireguard:
        wg_configured = await _setup_wireguard(base_host, token, result)

    result["wireguard"] = wg_configured

    # ── 5. Save config ───────────────────────────────────────────────
    core_llm_url = f"{base_host}:{_DESKTOP_PORTS['microscheduler']}/v1"

    # If WireGuard is up, prefer the WG IP for LLM routing
    if wg_configured and result.get("wg_ip"):
        core_llm_url = f"http://{result['wg_ip']}:{_DESKTOP_PORTS['microscheduler']}/v1"

    config_data = {
        "elysium_url": url,
        "elysium_base_host": base_host,
        "node_token": token or "",
        "node_id": result.get("node_id", ""),
        "core_llm_url": core_llm_url,
        "inference_mode": "dual",
        "mesh_enabled": True,
        "mesh_url": mesh_url,
    }

    saved_path = save_saved_config(config_data)
    result["config_saved"] = str(saved_path)

    # Also set env vars for the current process
    os.environ["AITHER_CORE_LLM_URL"] = core_llm_url
    if token:
        os.environ["AITHER_NODE_TOKEN"] = token

    result["ok"] = True
    result["core_llm_url"] = core_llm_url
    return result


async def _setup_wireguard(
    base_host: str,
    token: str | None,
    result: dict[str, Any],
) -> bool:
    """Attempt WireGuard tunnel setup with the desktop."""
    # Check if WireGuard is available
    wg_bin = shutil.which("wg") or shutil.which("wireguard")
    if platform.system() == "Windows":
        wg_bin = wg_bin or shutil.which("wireguard.exe") or shutil.which("wg.exe")

    if not wg_bin:
        logger.info("WireGuard not found in PATH, skipping tunnel setup")
        return False

    tunnel_url = f"{base_host}:{_DESKTOP_PORTS['tunnel']}"

    try:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{tunnel_url}/tunnel/peers",
                json={
                    "name": platform.node(),
                    "capabilities": ["mcp", "inference"],
                },
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning("Tunnel peer request failed: %s", resp.status_code)
                return False

            wg_data = resp.json()

        # Save WG config
        wg_conf_dir = Path.home() / ".aither"
        wg_conf_dir.mkdir(parents=True, exist_ok=True)
        wg_conf_path = wg_conf_dir / "wg-elysium.conf"

        wg_config = wg_data.get("config", "")
        if not wg_config:
            logger.warning("No WireGuard config in tunnel response")
            return False

        wg_conf_path.write_text(wg_config, encoding="utf-8")
        result["wg_config"] = str(wg_conf_path)
        result["wg_ip"] = wg_data.get("ip", "")

        # Try to bring up the tunnel
        try:
            subprocess.run(
                ["wg-quick", "up", str(wg_conf_path)],
                capture_output=True, text=True, timeout=15,
            )
            logger.info("WireGuard tunnel up: %s", result.get("wg_ip", ""))
            return True
        except Exception as e:
            logger.info(
                "Could not auto-start WireGuard (may need admin): %s\n"
                "  Manual setup: wg-quick up %s", e, wg_conf_path,
            )
            return False

    except Exception as e:
        logger.warning("WireGuard setup failed: %s", e)
        return False


async def disconnect_from_desktop() -> dict[str, Any]:
    """Disconnect from the desktop mesh and clean up."""
    result: dict[str, Any] = {"ok": True}
    saved = load_saved_config()

    # Leave mesh
    mesh_url = saved.get("mesh_url", "")
    node_id = saved.get("node_id", "")
    token = saved.get("node_token", "")

    if mesh_url and node_id:
        try:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{mesh_url}/heartbeat",
                    json={"node_id": node_id, "status": "offline"},
                    headers=headers,
                )
            result["mesh_left"] = True
        except Exception:
            result["mesh_left"] = False

    # Tear down WireGuard
    wg_conf_path = Path.home() / ".aither" / "wg-elysium.conf"
    if wg_conf_path.exists():
        try:
            subprocess.run(
                ["wg-quick", "down", str(wg_conf_path)],
                capture_output=True, text=True, timeout=10,
            )
            result["wireguard_down"] = True
        except Exception:
            result["wireguard_down"] = False

    # Clear elysium-specific config (preserve other keys)
    elysium_keys = [
        "elysium_url", "elysium_base_host", "node_token", "node_id",
        "core_llm_url", "inference_mode", "mesh_enabled", "mesh_url",
    ]
    cleared = load_saved_config()
    for k in elysium_keys:
        cleared.pop(k, None)

    config_path = Path.home() / ".aither" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(cleared, indent=2), encoding="utf-8")
    result["config_cleared"] = True

    # Clear env vars
    for var in ("AITHER_CORE_LLM_URL", "AITHER_NODE_TOKEN"):
        os.environ.pop(var, None)

    return result
