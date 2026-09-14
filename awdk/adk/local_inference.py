"""Local inference endpoint discovery and registration.

Enables agents to automatically discover and register local inference endpoints
(llama.cpp, vLLM, etc.) with MicroScheduler so they can be used for LLM calls
without manual configuration.

Discovery priority:
  1. AITHER_LOCAL_LLM_URL environment variable
  2. MicroScheduler at 127.0.0.1:8150 (if reachable)
  3. Self-hosted loopback ports (SELFHOST_PORTS below)
  4. None found

Each source is verified with a REAL /v1/models or /health call to confirm
the endpoint is actually serving, not just accepting TCP connections. The
port scan additionally REQUIRES a model in /v1/models: a bare /health 200 is
what every dev web server on :8080 answers, and a router that adopted one of
those as its brain would fail every chat.

Until 2026-09-12 this module was dead code: nothing on the agent path called
discover_local_endpoint(), so a Bonsai started by aitherium.com/install-bonsai.sh
(:8080) or `adk bonsai-local` (:8090) was invisible to LLMRouter, `adk status`,
`adk up` and `adk start` unless the user ran `adk backend set` by hand. It is
now the ONE place the self-host ladder lives; LLMRouter._try_local_selfhost,
`adk status`, the `adk up` preflight and `adk start` all call it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from adk._tls import tls_verify

try:
    from httpx import AsyncClient, ConnectError, HTTPError, ReadTimeout
except ImportError:
    AsyncClient = None  # type: ignore

logger = logging.getLogger("adk.local_inference")

# Loopback ports a self-hosted OpenAI-compatible server is known to bind, in
# probe order. Same ladder aitherium.com's local-node probe uses
# (AitherVeil use-local-node.ts), so the browser and the agent agree on what
# "your local node" means:
#   8080  aitherium.com/install-bonsai.sh, phone.sh (llama-server --alias bonsai-selfhost)
#   8090  `adk bonsai-local` (Docker), awnode gateway
#   8092  aither-llamacpp-bonsai container
#   8889  selfhost-bonsai skill
#   8081  install-bonsai.sh --port 8081 (the port it suggests when 8080 is taken)
#   8000  bare vllm / adk server
# Edit this tuple, not the callers.
SELFHOST_PORTS: tuple[int, ...] = (8080, 8090, 8092, 8889, 8081, 8000)


@dataclass
class LocalInferenceDiscovery:
    """Result of discovering a local inference endpoint."""

    found: bool
    """Whether an endpoint was discovered."""

    endpoint_url: Optional[str] = None
    """OpenAI-compatible base URL (e.g., http://127.0.0.1:8080)."""

    model: Optional[str] = None
    """Model name or ID served by the endpoint."""

    source: Optional[str] = None
    """Where the endpoint was discovered: env | microscheduler | port | none."""

    details: Optional[str] = None
    """Human-readable details (error messages, probed URLs, etc.)."""

    def __str__(self) -> str:
        if not self.found:
            return f"No local endpoint found. {self.details or ''}"
        return (
            f"Local endpoint: {self.endpoint_url} (source: {self.source}, "
            f"model: {self.model})"
        )


async def _probe_endpoint(
    url: str, timeout: float = 3.0, require_model: bool = False
) -> tuple[bool, Optional[str]]:
    """Probe an endpoint to verify it's actually serving.

    Returns (healthy, model_name) where:
      - healthy=True if /v1/models or /health succeeded
      - model_name is the first model ID from /v1/models, or None

    With require_model=True a /health-only answer is NOT healthy: the caller
    is scanning ports it does not own, and only a server that lists a model
    can be assumed to complete a chat.

    Never raises; returns (False, None) on any probe failure.
    """
    if AsyncClient is None:
        logger.warning("httpx not available; skipping endpoint probe")
        return False, None

    url = url.rstrip("/")

    try:
        async with AsyncClient(timeout=timeout, verify=tls_verify()) as client:
            # Try /v1/models first (OpenAI-compatible standard)
            try:
                resp = await client.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and "data" in data:
                        models = data.get("data", [])
                        if isinstance(models, list) and len(models) > 0:
                            first_model = models[0]
                            if isinstance(first_model, dict):
                                model_id = first_model.get("id")
                            else:
                                model_id = str(first_model)
                            logger.debug(
                                f"Probed {url}/v1/models: found {len(models)} "
                                f"models, first={model_id}"
                            )
                            return True, model_id
                    logger.debug(
                        f"Probed {url}/v1/models: 200 but no usable models in response"
                    )
            except (HTTPError, ReadTimeout) as e:
                logger.debug(f"Probed {url}/v1/models: {type(e).__name__}")

            if require_model:
                return False, None

            # Fallback to /health
            try:
                resp = await client.get(f"{url}/health")
                if resp.status_code == 200:
                    logger.debug(f"Probed {url}/health: OK")
                    return True, None
                logger.debug(f"Probed {url}/health: {resp.status_code}")
            except (HTTPError, ReadTimeout) as e:
                logger.debug(f"Probed {url}/health: {type(e).__name__}")

    except ConnectError as e:
        logger.debug(f"Could not connect to {url}: {e}")
    except Exception as e:
        logger.debug(f"Unexpected error probing {url}: {type(e).__name__}: {e}")

    return False, None


async def discover_local_endpoint() -> LocalInferenceDiscovery:
    """Discover a local OpenAI-compatible inference endpoint.

    Tries endpoints in priority order:
      1. AITHER_LOCAL_LLM_URL environment variable (explicit override)
      2. MicroScheduler at 127.0.0.1:8150 if reachable (can route to registered backends)
      3. Common llama-server ports on loopback (8080, 8081, 8000)

    Each endpoint is verified with a real /v1/models or /health probe.
    Returns immediately on first success.

    Returns:
        LocalInferenceDiscovery with found=True and populated fields if an
        endpoint is discovered, or found=False with details explaining why.
    """
    logger.debug("Starting local endpoint discovery")
    probed_urls = []

    # 1. Check explicit override env var
    env_url = os.environ.get("AITHER_LOCAL_LLM_URL", "").strip()
    if env_url:
        logger.info(f"Found AITHER_LOCAL_LLM_URL={env_url}")
        probed_urls.append(env_url)
        healthy, model = await _probe_endpoint(env_url)
        if healthy:
            return LocalInferenceDiscovery(
                found=True,
                endpoint_url=env_url,
                model=model,
                source="env",
                details="Discovered via AITHER_LOCAL_LLM_URL",
            )
        logger.warning(
            f"AITHER_LOCAL_LLM_URL={env_url} did not respond to health check"
        )

    # 2. Check MicroScheduler (routes to any registered backends, can query models)
    scheduler_url = "http://127.0.0.1:8150"
    probed_urls.append(scheduler_url)
    logger.debug(f"Probing MicroScheduler at {scheduler_url}")
    try:
        async with AsyncClient(timeout=2.0, verify=tls_verify()) as client:
            resp = await client.get(f"{scheduler_url}/llm/backends/snapshot")
            if resp.status_code == 200:
                data = resp.json()
                backends = data.get("backends", {})
                if backends:
                    logger.info(
                        f"MicroScheduler reported {len(backends)} healthy backends"
                    )
                    # MicroScheduler is reachable and has backends. Use it as the
                    # endpoint so LLM calls route through the scheduler.
                    # The scheduler will pick an available backend for each call.
                    first_backend_name = next(iter(backends.keys()), None)
                    first_backend = backends.get(first_backend_name, {})
                    model = first_backend.get("served_model") or first_backend_name
                    return LocalInferenceDiscovery(
                        found=True,
                        endpoint_url=scheduler_url,
                        model=model,
                        source="microscheduler",
                        details=f"MicroScheduler with {len(backends)} healthy backends",
                    )
                logger.debug("MicroScheduler has no healthy backends")
    except Exception as e:
        logger.debug(
            f"MicroScheduler probe failed ({type(e).__name__}): "
            f"{str(e)[:60]}"
        )

    # 3. Self-hosted loopback ports (SELFHOST_PORTS). /v1/models must list a
    # model — see _probe_endpoint(require_model=True).
    for port in SELFHOST_PORTS:
        url = f"http://127.0.0.1:{port}"
        probed_urls.append(url)
        logger.debug(f"Probing self-host port {port}")
        healthy, model = await _probe_endpoint(url, timeout=1.5, require_model=True)
        if healthy:
            logger.info(f"Found healthy endpoint at {url}")
            return LocalInferenceDiscovery(
                found=True,
                endpoint_url=url,
                model=model,
                source="port",
                details=f"Discovered on port {port}",
            )

    # Nothing found
    logger.info(
        "No local inference endpoint found. Probed: "
        f"{', '.join(probed_urls)}"
    )
    return LocalInferenceDiscovery(
        found=False,
        source="none",
        details=f"Probed {len(probed_urls)} endpoints: {', '.join(probed_urls)}",
    )


async def register_with_microscheduler(
    endpoint_url: str,
    backend_name: Optional[str] = None,
    model: Optional[str] = None,
    max_concurrent: int = 16,
    timeout: float = 5.0,
) -> tuple[bool, Optional[str]]:
    """Register a discovered backend with MicroScheduler.

    Args:
        endpoint_url: OpenAI-compatible base URL (e.g., http://127.0.0.1:8080)
        backend_name: Name for the backend (auto-generated if None)
        model: Model name served (used in profile field)
        max_concurrent: Max concurrent requests to allow
        timeout: Request timeout in seconds

    Returns:
        (success, error_message): True on successful registration, False with
        error message on failure.
    """
    if not backend_name:
        backend_name = f"local-llm-{int(time.time())}"

    logger.info(
        f"Registering backend '{backend_name}' at {endpoint_url} "
        f"with MicroScheduler"
    )

    if AsyncClient is None:
        return False, "httpx not available"

    try:
        # Read internal key from environment (required for registration)
        internal_key = os.environ.get("AITHER_INTERNAL_SECRET", "").strip()
        if not internal_key:
            logger.warning(
                "AITHER_INTERNAL_SECRET not set; registration may be rejected"
            )

        payload = {
            "name": backend_name,
            "base_url": endpoint_url.rstrip("/"),
            "max_concurrent": max_concurrent,
            "profile": model,
            "verify": True,  # Let MicroScheduler health-check the endpoint
        }

        headers = {}
        if internal_key:
            headers["X-Internal-Key"] = internal_key

        async with AsyncClient(timeout=timeout, verify=tls_verify()) as client:
            resp = await client.post(
                "http://127.0.0.1:8150/llm/backend/register",
                json=payload,
                headers=headers,
            )

            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    f"Successfully registered backend '{backend_name}': "
                    f"{data}"
                )
                return True, None

            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.error(
                f"Failed to register backend '{backend_name}': {error_msg}"
            )
            return False, error_msg

    except asyncio.TimeoutError:
        error = "Registration request timed out"
        logger.error(f"Failed to register backend '{backend_name}': {error}")
        return False, error
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:100]}"
        logger.error(f"Failed to register backend '{backend_name}': {error}")
        return False, error


def emit_discovery_event(
    discovery: LocalInferenceDiscovery,
    room: str = "main",
    actor_id: Optional[str] = None,
) -> dict:
    """Emit an AitherEvent for local endpoint discovery.

    Args:
        discovery: The LocalInferenceDiscovery result
        room: Room ID to emit to (default: main)
        actor_id: Agent/actor ID (auto-generated if None)

    Returns:
        Event dict suitable for posting to the event spine daemon.
        Returns immediately (does not post); caller must POST to
        http://127.0.0.1:8362/events
    """
    if not actor_id:
        import uuid
        actor_id = str(uuid.uuid4())

    pillar = "orchestration"  # model_select and backend discovery are orchestration

    event = {
        "type": "local_inference_discovered",
        "actor": {
            "kind": "adk_agent",
            "id": actor_id,
            "name": actor_id[:8],
        },
        "pillar": pillar,
        "tier": "host",
        "room": room,
        "payload": {
            "found": discovery.found,
            "endpoint_url": discovery.endpoint_url,
            "model": discovery.model,
            "source": discovery.source,
            "details": discovery.details,
        },
    }

    logger.info(
        f"Emitting discovery event: found={discovery.found}, "
        f"source={discovery.source}, endpoint={discovery.endpoint_url}"
    )

    return event


async def post_discovery_event(
    event: dict,
    event_daemon_url: str = "http://127.0.0.1:8362",
    bearer_token: Optional[str] = None,
    timeout: float = 5.0,
) -> tuple[bool, Optional[str]]:
    """POST a discovery event to the event spine daemon.

    Args:
        event: Event dict from emit_discovery_event()
        event_daemon_url: Base URL of the event daemon (default: localhost:8362)
        bearer_token: Authorization bearer token (read from env if not provided)
        timeout: Request timeout in seconds

    Returns:
        (success, error_message): True on successful POST, False with error.
    """
    if AsyncClient is None:
        return False, "httpx not available"

    if not bearer_token:
        bearer_token = os.environ.get("AITHER_HARNESS_TOKEN", "").strip()

    logger.debug(f"POSTing discovery event to {event_daemon_url}/events")

    try:
        headers = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        async with AsyncClient(timeout=timeout, verify=tls_verify()) as client:
            resp = await client.post(
                f"{event_daemon_url.rstrip('/')}/events",
                json=event,
                headers=headers,
            )

            if resp.status_code in (200, 201, 202):
                logger.info(f"Event posted successfully (HTTP {resp.status_code})")
                return True, None

            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.error(f"Failed to post discovery event: {error_msg}")
            return False, error_msg

    except asyncio.TimeoutError:
        error = "Event POST timed out"
        logger.error(f"Failed to post discovery event: {error}")
        return False, error
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:100]}"
        logger.error(f"Failed to post discovery event: {error}")
        return False, error


async def auto_discover_and_register(
    enable_event_emit: bool = True,
    room: str = "main",
    actor_id: Optional[str] = None,
) -> tuple[LocalInferenceDiscovery, Optional[str]]:
    """Full discovery and registration pipeline.

    Discovers a local endpoint, registers it with MicroScheduler if found,
    and optionally emits an AitherEvent to the room.

    Args:
        enable_event_emit: Whether to emit an AitherEvent to the spine
        room: Room ID for event emission
        actor_id: Actor ID for event (auto-generated if None)

    Returns:
        (discovery_result, error_or_none): The discovery result, and an error
        message if registration failed (discovery succeeding with no backends
        found is not an error).
    """
    logger.info("Starting auto-discovery and registration pipeline")

    # Phase 1: Discover
    discovery = await discover_local_endpoint()
    logger.info(f"Discovery result: {discovery}")

    # Phase 2: Register (if found)
    if discovery.found and discovery.endpoint_url:
        success, error = await register_with_microscheduler(
            discovery.endpoint_url, model=discovery.model
        )
        if not success:
            logger.error(f"Registration failed: {error}")

    # Phase 3: Emit event (if enabled)
    if enable_event_emit:
        event = emit_discovery_event(discovery, room=room, actor_id=actor_id)
        success, error = await post_discovery_event(event)
        if not success:
            logger.warning(f"Event emission failed: {error}")

    return discovery, None
