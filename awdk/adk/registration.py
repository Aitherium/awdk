"""Portal registration — makes an ADK agent a citizen of api.aitherium.com.

On startup in workspace mode:
1. Reads agent.yaml for portal configuration
2. POSTs to portal gateway to register/upsert the agent
3. Runs a heartbeat loop to maintain presence
4. Stores portal token locally for subsequent requests
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adk.registration")

_TOKEN_PATH = Path(os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))) / "portal.token"
_HEARTBEAT_INTERVAL = 300  # 5 minutes
_portal_token: Optional[str] = None
_heartbeat_task: Optional[asyncio.Task] = None


def _load_agent_yaml() -> dict:
    """Load agent.yaml from the ADK project root."""
    candidates = [
        Path(os.getenv("AGENT_YAML", "")),
        Path.cwd() / "agent.yaml",
        Path(__file__).resolve().parent.parent / "agent.yaml",
    ]
    for p in candidates:
        if p.exists():
            try:
                import yaml
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning("Failed to load agent.yaml from %s: %s", p, e)
    return {}


def _get_portal_url() -> str:
    """Get the portal gateway URL."""
    return os.getenv(
        "AITHER_PORTAL_URL",
        "https://api.aitherium.com",
    )


def _load_token() -> Optional[str]:
    """Load saved portal token."""
    global _portal_token
    if _portal_token:
        return _portal_token
    if _TOKEN_PATH.exists():
        try:
            _portal_token = _TOKEN_PATH.read_text(encoding="utf-8").strip()
            return _portal_token
        except Exception:
            pass
    return None


def _save_token(token: str) -> None:
    """Save portal token to disk."""
    global _portal_token
    _portal_token = token
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        os.chmod(_TOKEN_PATH, 0o600)
    except (OSError, AttributeError):
        pass


async def register_with_portal(
    agent_spec: Optional[dict] = None,
    server_url: Optional[str] = None,
) -> bool:
    """Register this agent with the portal gateway.

    Args:
        agent_spec: Parsed agent.yaml dict. If None, loads from disk.
        server_url: Local server URL for health checks / embed.

    Returns:
        True if registration succeeded.
    """
    spec = agent_spec or _load_agent_yaml()
    portal_meta = spec.get("portal", {})
    if not portal_meta:
        logger.debug("No portal section in agent.yaml — skipping registration")
        return False

    portal_url = _get_portal_url()
    app_id = portal_meta.get("app_id", spec.get("name", "adk-agent"))
    local_url = server_url or portal_meta.get("url", "http://localhost:8080")

    payload = {
        "app_id": app_id,
        "name": spec.get("name", app_id),
        "description": spec.get("description", ""),
        "url": local_url,
        "embed_url": portal_meta.get("embed_url", f"{local_url}/?embedded=true"),
        "health_endpoint": portal_meta.get("health_endpoint", "/api/health"),
        "capabilities": portal_meta.get("capabilities", []),
        "icon": portal_meta.get("icon", "bot"),
        "category": portal_meta.get("category", "general"),
        "version": spec.get("version", "1.0.0"),
        "registered_at": time.time(),
    }

    # Include existing token for re-registration
    token = _load_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{portal_url}/api/v1/agents/upsert",
                json=payload,
                headers=headers,
            )
            if resp.status_code < 300:
                data = resp.json()
                new_token = data.get("token")
                if new_token:
                    _save_token(new_token)
                logger.info(
                    "Registered with portal: %s -> %s (status=%d)",
                    app_id, portal_url, resp.status_code,
                )
                return True
            logger.warning(
                "Portal registration returned %d: %s",
                resp.status_code, resp.text[:200],
            )
    except ImportError:
        logger.debug("httpx not installed — portal registration unavailable")
    except Exception as e:
        logger.warning("Portal registration failed: %s", e)

    return False


async def _heartbeat_loop(agent_spec: dict, server_url: str) -> None:
    """Re-register periodically to maintain presence."""
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            await register_with_portal(agent_spec, server_url)
        except Exception as e:
            logger.debug("Heartbeat registration failed: %s", e)


async def start_registration(
    agent_spec: Optional[dict] = None,
    server_url: Optional[str] = None,
) -> bool:
    """Register and start heartbeat loop.

    Returns True if initial registration succeeded.
    """
    global _heartbeat_task
    spec = agent_spec or _load_agent_yaml()
    url = server_url or spec.get("portal", {}).get("url", "http://localhost:8080")

    success = await register_with_portal(spec, url)

    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = asyncio.create_task(_heartbeat_loop(spec, url))

    return success


async def stop_registration() -> None:
    """Stop the heartbeat loop."""
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        _heartbeat_task = None
