"""MicroScheduler heartbeat loop for personal agents.

After `adk onboard --quick`, this module starts a background daemon that:
  1. Every 30 seconds, POSTs a heartbeat to MicroScheduler (:8150)
  2. Reports the agent_id, capabilities, current model, and system metrics
  3. Registers the endpoint in the AitherOS fleet (visible in Portal → Fleet)

This is separate from the node heartbeat (which goes to the portal hub).
MicroScheduler is the agent registry that powers the Fleet dashboard.

Usage:
    from adk.microscheduler_heartbeat import start_microscheduler_heartbeat
    await start_microscheduler_heartbeat(
        agent_id="personal-agent-jason-laptop",
        microscheduler_url="http://aitheros-microscheduler:8150",
        interval=30,
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any

import httpx

log = logging.getLogger("adk.microscheduler_heartbeat")

_AITHER_DIR = Path.home() / ".aither"
_CONFIG_FILE = _AITHER_DIR / "config.json"


def _load_config() -> Dict[str, Any]:
    """Load ~/.aither/config.json (agent configuration)."""
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _get_system_metrics() -> Dict[str, float]:
    """Collect basic system metrics for the heartbeat."""
    try:
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
        }
    except ImportError:
        return {"cpu_percent": 0.0, "memory_percent": 0.0}


async def start_microscheduler_heartbeat(
    agent_id: str,
    microscheduler_url: Optional[str] = None,
    interval: int = 30,
    capabilities: Optional[list[str]] = None,
    current_model: Optional[str] = None,
) -> None:
    """Start background heartbeat loop to MicroScheduler.

    Args:
        agent_id: Unique agent identifier (e.g., 'personal-agent-laptop-001')
        microscheduler_url: MicroScheduler base URL (default from env or localhost:8150)
        interval: Heartbeat interval in seconds (default 30)
        capabilities: List of agent capabilities (e.g., ['research', 'coding'])
        current_model: Current LLM model in use (e.g., 'bonsai-4b', 'ollama:llama2')

    Returns: None (runs indefinitely in background)

    Raises: No exceptions — failures are logged but don't break the loop.
    """
    # Resolve microscheduler URL
    if not microscheduler_url:
        microscheduler_url = os.environ.get(
            "AITHER_MICROSCHEDULER_URL",
            "http://localhost:8150"
        )
    microscheduler_url = microscheduler_url.rstrip("/")

    # Default capabilities
    if not capabilities:
        capabilities = ["agent", "llm", "research"]

    # Resolve model
    if not current_model:
        config = _load_config()
        current_model = config.get("active_model", "unknown")

    log.info(
        "Starting MicroScheduler heartbeat (agent_id=%s, url=%s, interval=%ds)",
        agent_id, microscheduler_url, interval,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                await asyncio.sleep(interval)

                # Build heartbeat payload
                metrics = _get_system_metrics()
                heartbeat = {
                    "agent_id": agent_id,
                    "name": agent_id.replace("_", "-").title(),
                    "kind": "agent",
                    "capabilities": capabilities,
                    "current_task": None,  # Only set if agent is actively working
                    "current_model": current_model,
                    "resource_usage": metrics,
                    "metadata": {
                        "source": "adk-personal-agent",
                        "sdk_version": "1.0.0+personal",
                        "timestamp": int(time.time()),
                    },
                }

                # POST to MicroScheduler
                response = await client.post(
                    f"{microscheduler_url}/agents/heartbeat",
                    json=heartbeat,
                )

                if response.status_code == 200:
                    log.debug(
                        "Heartbeat sent (agent_id=%s, status_code=%d)",
                        agent_id, response.status_code,
                    )
                else:
                    log.warning(
                        "Heartbeat failed (agent_id=%s, status_code=%d): %s",
                        agent_id, response.status_code, response.text[:200],
                    )

            except asyncio.CancelledError:
                log.info("MicroScheduler heartbeat loop cancelled (agent_id=%s)", agent_id)
                break
            except httpx.ConnectError:
                log.debug(
                    "MicroScheduler unreachable (%s) — will retry",
                    microscheduler_url,
                )
            except Exception as e:
                log.debug("Heartbeat error (agent_id=%s): %s", agent_id, e)


def get_agent_id_for_personal_agent() -> str:
    """Generate a unique agent_id for this personal agent enrollment.

    Format: personal-agent-<hostname>-<random-suffix>
    This ensures multiple personal agents on different machines get unique IDs.
    """
    import socket
    import uuid

    hostname = socket.gethostname().lower().replace(" ", "-")
    suffix = str(uuid.uuid4())[:8]
    return f"personal-agent-{hostname}-{suffix}"


# Singleton to track the background task (for testing/cleanup)
_heartbeat_task: Optional[asyncio.Task[None]] = None


async def start_heartbeat_in_background(
    agent_id: Optional[str] = None,
    microscheduler_url: Optional[str] = None,
    interval: int = 30,
    **kwargs: Any,
) -> str:
    """Start heartbeat in background (does not await it).

    Returns: The agent_id that was registered.
    """
    global _heartbeat_task

    if not agent_id:
        agent_id = get_agent_id_for_personal_agent()

    # Start the background task
    try:
        _heartbeat_task = asyncio.create_task(
            start_microscheduler_heartbeat(
                agent_id=agent_id,
                microscheduler_url=microscheduler_url,
                interval=interval,
                **kwargs,
            )
        )
        log.info("Background heartbeat task created (agent_id=%s)", agent_id)
        return agent_id
    except RuntimeError as e:
        # No running event loop — this is OK in many contexts
        log.debug(
            "Cannot start heartbeat in background (no event loop): %s", e
        )
        return agent_id


def stop_heartbeat() -> None:
    """Stop the background heartbeat task (for cleanup)."""
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
        log.info("Background heartbeat task cancelled")


# ────────────────────────────────────────────────────────────────────────────
# Threading-based heartbeat for synchronous contexts (e.g., CLI)
# ────────────────────────────────────────────────────────────────────────────

_heartbeat_thread: Optional[Any] = None


def start_heartbeat_threaded(
    agent_id: Optional[str] = None,
    microscheduler_url: Optional[str] = None,
    interval: int = 30,
    capabilities: Optional[list[str]] = None,
    current_model: Optional[str] = None,
) -> str:
    """Start heartbeat in a background thread (for synchronous contexts like CLI).

    This is the recommended approach for `adk onboard` since it's a CLI command
    without an active event loop.

    Returns: The agent_id that was registered.
    """
    global _heartbeat_thread
    import threading

    if not agent_id:
        agent_id = get_agent_id_for_personal_agent()

    # Create the event loop and run the heartbeat in a thread
    def _run_heartbeat_thread():
        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    start_microscheduler_heartbeat(
                        agent_id=agent_id,
                        microscheduler_url=microscheduler_url,
                        interval=interval,
                        capabilities=capabilities,
                        current_model=current_model,
                    )
                )
            finally:
                loop.close()
        except Exception as e:
            log.error("Heartbeat thread error: %s", e)

    # Start as daemon thread (will not block program exit)
    _heartbeat_thread = threading.Thread(
        target=_run_heartbeat_thread,
        daemon=True,
        name=f"microscheduler-heartbeat-{agent_id}",
    )
    _heartbeat_thread.start()
    log.info("Background heartbeat thread started (agent_id=%s)", agent_id)

    return agent_id
