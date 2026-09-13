"""Fleet enrollment for local AitherOS nodes — auto-register on boot.

Provides both Node and Agent fleet registration (portal + local Genesis).

Two-fleet model:
  - PORTAL fleet: external discovery via portal.aitherium.com (FederationLiteClient)
  - LOCAL fleet: Genesis/Node for in-network routing

On boot (after services are live), fleet_enroll.enroll_on_boot() will:
  1. Register the local node (via federation or direct Genesis API)
  2. Persist node_id + api_key to ~/.aither/node_auth.json
  3. Start a background heartbeat loop (every 60s)
  4. Scan ~/.aither/agents.json and upsert agents to the portal fleet

All operations are graceful: failures log but do not crash boot. Gated by
AITHER_FLEET_ENROLL env var (or config flag).

Usage:

    from adk.fleet_enroll import enroll_on_boot
    # After services are up:
    await enroll_on_boot()

"""

from __future__ import annotations

__all__ = [
    "enroll_on_boot",
]

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("adk.fleet_enroll")

_AITHER_DIR = Path.home() / ".aither"
_NODE_AUTH_FILE = _AITHER_DIR / "node_auth.json"
_AGENTS_FILE = _AITHER_DIR / "agents.json"
_AUTH_FILE = _AITHER_DIR / "auth.json"
_CONFIG_FILE = _AITHER_DIR / "config.json"


def _should_enroll() -> bool:
    """Check if fleet enrollment is enabled.

    Checks (in order):
      1. AITHER_FLEET_ENROLL env var (true/1)
      2. Config entry in AITHER_CONFIG_FILE (fleet.enroll: true)
      3. Default: False (offline/sovereign by default)
    """
    if os.environ.get("AITHER_FLEET_ENROLL", "").lower() in ("true", "1"):
        return True

    # Check config file if it exists
    config_file = _AITHER_DIR / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
            if config.get("fleet", {}).get("enroll"):
                return True
        except Exception:
            pass

    return False


def _load_auth_config() -> Dict[str, Any]:
    """Load auth.json to get tenant info."""
    if not _AUTH_FILE.exists():
        return {}
    try:
        return json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read auth.json: %s", e)
        return {}


def _load_node_auth() -> Dict[str, Any]:
    """Load node_auth.json (persistent node identity)."""
    if not _NODE_AUTH_FILE.exists():
        return {}
    try:
        return json.loads(_NODE_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read node_auth.json: %s", e)
        return {}


def _save_node_auth(data: Dict[str, Any]) -> None:
    """Persist node_auth.json."""
    _AITHER_DIR.mkdir(parents=True, exist_ok=True)
    data["enrolled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _NODE_AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_agents_registry() -> Dict[str, Any]:
    """Load agents.json (local agent registry)."""
    if not _AGENTS_FILE.exists():
        return {"agents": {}}
    try:
        data = json.loads(_AGENTS_FILE.read_text(encoding="utf-8"))
        if "agents" not in data:
            data = {"agents": data}
        return data
    except Exception as e:
        log.warning("Failed to read agents.json: %s", e)
        return {"agents": {}}


async def _register_node_with_federation(
    hub_url: str,
    api_key: str,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Register node with federation hub (portal or local Genesis).

    Args:
        hub_url: Portal or Genesis URL (e.g., https://api.aitherium.com or http://localhost:8001)
        api_key: API key or token for authentication
        node_id: Optional pre-assigned node ID

    Returns:
        {"node_id": "...", "api_key": "...", ...} on success
        {"error": True, ...} on failure
    """
    try:
        from adk.federation_lite import FederationLiteClient

        tenant_slug = _extract_tenant_slug()
        client = FederationLiteClient(
            hub_url=hub_url,
            api_key=api_key,
            node_id=node_id,
        )
        result = await client.register(tenant_slug)
        if not result.get("error"):
            log.info("Successfully registered node with %s", hub_url)
            return {
                "node_id": result.get("node_id", client.node_id),
                "api_key": result.get("api_key", api_key),
                "hub_url": hub_url,
            }
        else:
            log.warning("Federation registration failed: %s", result.get("detail", "unknown"))
            return {"error": True, "detail": result.get("detail")}
    except Exception as e:
        log.warning("Federation registration error: %s", e)
        return {"error": True, "detail": str(e)}


async def _register_node_with_genesis(genesis_url: str, api_key: str) -> Dict[str, Any]:
    """Fallback: Register node directly with Genesis POST /federation/register.

    Args:
        genesis_url: Genesis service URL (e.g., http://localhost:8001)
        api_key: Bearer token

    Returns:
        {"node_id": "...", "api_key": "...", ...} on success
    """
    try:
        import httpx

        node_id = _generate_node_id()
        payload = {
            "tenant_slug": _extract_tenant_slug(),
            "node_id": node_id,
            "timestamp": int(time.time()),
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{genesis_url.rstrip('/')}/federation/register",
                json=payload,
                headers=headers,
            )
            if resp.status_code >= 200 and resp.status_code < 300:
                data = resp.json()
                log.info("Successfully registered node with Genesis")
                return {
                    "node_id": data.get("node_id", node_id),
                    "api_key": data.get("api_key", api_key),
                    "hub_url": genesis_url,
                }
            else:
                log.warning("Genesis registration failed: %s", resp.text[:200])
                return {"error": True, "detail": resp.text[:200]}
    except Exception as e:
        log.warning("Genesis registration error: %s", e)
        return {"error": True, "detail": str(e)}


def _generate_node_id() -> str:
    """Generate a unique node ID (e.g., adk-a3f8b2c1-7x9k)."""
    import hashlib
    import secrets
    import socket
    import uuid

    # Fingerprint: hostname + MAC + home dir
    fingerprint = f"{socket.gethostname()}-{uuid.getnode()}-{Path.home()}"
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:8]
    rand = secrets.token_hex(4)
    return f"adk-{digest}-{rand}"


def _extract_tenant_slug() -> str:
    """Extract tenant_slug from auth.json or fall back to default.

    Returns a tenant slug suitable for federation (e.g., "personal", "acme-corp", etc.)
    """
    auth = _load_auth_config()
    slug = auth.get("tenant_slug") or auth.get("user", {}).get("tenant_slug", "")
    if slug:
        return slug

    # Fallback: use username or "personal"
    username = auth.get("user", {}).get("username", "")
    if username:
        return username.lower().replace(" ", "-")

    return "personal"


def _enable_session_sync_default() -> None:
    """
    Enable session sync by default in config.json after enrollment.

    Persists AITHER_SESSION_SYNC=true in the config file, but respects
    an explicit user opt-out if one is already set.

    Idempotent: safe to call multiple times.
    """
    try:
        _AITHER_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing config if present
        config = {}
        if _CONFIG_FILE.exists():
            try:
                config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Failed to read existing config: %s", e)

        # Check for explicit opt-out (user has disabled it)
        # If session_sync.enabled is explicitly False, respect it
        if config.get("session_sync", {}).get("enabled") is False:
            log.info("Session sync opt-out detected — not overriding")
            return

        # Set default to enabled
        config.setdefault("session_sync", {})["enabled"] = True

        # Write config (atomic via temp file + rename for Windows)
        temp_file = _CONFIG_FILE.with_suffix(".json.tmp")
        temp_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
        temp_file.replace(_CONFIG_FILE)

        log.info("Enabled session sync by default in config")
    except Exception as e:
        # Best-effort; enrollment should not fail due to config write
        log.warning("Failed to enable session sync config default: %s", e)


async def _upsert_agents_to_portal(hub_url: str, api_key: str, node_id: str) -> bool:
    """Upsert all local agents to the portal fleet.

    Reads ~/.aither/agents.json and sends to /v1/agents/upsert-batch on the hub.

    Returns True if all agents were sent (or no agents to send), False if error.
    """
    try:
        from adk.federation_lite import FederationLiteClient

        registry = _load_agents_registry()
        agents_dict = registry.get("agents", {})
        if not agents_dict:
            log.info("No local agents to upsert")
            return True

        # Convert registry entries to agent cards
        agents = []
        for name, entry in agents_dict.items():
            agents.append({
                "name": name,
                "url": entry.get("url", f"http://localhost:{entry.get('port', 8000)}"),
                "capabilities": entry.get("capabilities", []),
                "status": entry.get("status", "active"),
                "metadata": entry.get("metadata", {}),
            })

        client = FederationLiteClient(
            hub_url=hub_url,
            api_key=api_key,
            node_id=node_id,
        )
        result = await client.upsert_agents(agents)
        if result.get("error"):
            log.warning("Agent upsert failed: %s", result.get("detail"))
            return False
        log.info("Upserted %d agents to portal", len(agents))
        return True
    except Exception as e:
        log.warning("Agent upsert error: %s", e)
        return False


async def _sync_entitled_packs_best_effort(
    api_key: str,
    portal_url: str,
) -> tuple:
    """Best-effort entitled-pack sync for enrollment auto-install.

    Tries to sync packs from the portal; never blocks or raises — failures
    just log a warning.

    Returns:
        (packs_installed, packs_failed) counts
    """
    try:
        from adk.shell.plugins.builtins.packs import sync_entitled_packs

        installed, failed = await sync_entitled_packs(
            auth_token=api_key,
            base_url=portal_url,
        )
        if installed > 0:
            log.info("Enrollment auto-sync: installed %d packs", installed)
        if failed > 0:
            log.warning("Enrollment auto-sync: %d pack installs failed", failed)
        return installed, failed
    except ImportError:
        log.debug("Pack sync unavailable (packs plugin not found)")
        return 0, 0
    except Exception as e:  # noqa: BLE001 — never block enrollment
        log.warning("Enrollment auto-sync failed (continuing): %s", e)
        return 0, 0


async def _heartbeat_loop(
    hub_url: str,
    api_key: str,
    node_id: str,
    interval: int = 60,
) -> None:
    """Background heartbeat loop — sends periodic status to the hub.

    Runs indefinitely (in background). Sends every `interval` seconds.
    Failures are logged but do not break the loop.
    """
    try:
        from adk.federation_lite import FederationLiteClient

        client = FederationLiteClient(
            hub_url=hub_url,
            api_key=api_key,
            node_id=node_id,
        )

        log.info("Starting heartbeat loop (interval=%ds)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                registry = _load_agents_registry()
                agents = list(registry.get("agents", {}).keys())
                metrics = {
                    "agents_active": len(agents),
                    "timestamp": int(time.time()),
                }
                result = await client.heartbeat(
                    status="online",
                    metrics=metrics,
                    agents=[{"name": a} for a in agents] if agents else None,
                )
                if not result.get("error"):
                    log.debug("Heartbeat sent (agents=%d)", len(agents))
                else:
                    log.debug("Heartbeat failed: %s", result.get("detail"))
            except asyncio.CancelledError:
                log.info("Heartbeat loop cancelled")
                break
            except Exception as e:
                log.debug("Heartbeat error: %s", e)
    except Exception as e:
        log.warning("Heartbeat loop setup failed: %s", e)


async def _self_mint_gateway_key(bearer_token: str, node_id: str) -> str:
    """Trade a tenant-scoped capability bearer_token for a real avk_... gateway key.

    For LOCAL co-located deploys, calls AitherSecrets' self-service POST /api-keys.
    For REMOTE nodes (no local secrets vault), exchanges via Genesis' endpoint
    POST /v1/workspace/api-keys/enrollment-token/exchange.

    Identity_nodes.py's register_node mints the bearer_token this consumes.
    Best-effort: returns "" on any failure, never raises.
    """
    try:
        import httpx
        from urllib.parse import urlparse

        from adk._tls import tls_verify

        # Determine if this is a LOCAL or REMOTE scenario
        secrets_url = os.environ.get("AITHER_SECRETS_URL", "https://localhost:8111")
        parsed = urlparse(secrets_url)
        is_localhost = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        # Internal AitherOS services (AitherSecrets :8111 on a full mesh node)
        # serve HTTPS with the internal CA — a plain http:// URL hits a TLS port
        # and dies with "Server disconnected without sending a response", the
        # exact failure that silently killed local self-mint on an edge node
        # running the secrets service. Coerce http->https for localhost;
        # tls_verify() below trusts the internal CA.
        if is_localhost and parsed.scheme == "http":
            secrets_url = "https://" + secrets_url[len("http://"):]

        # If we have explicit remote gateway URLs, treat as remote
        gateway_url = os.environ.get("AITHER_GATEWAY_URL", "").strip()
        api_url = os.environ.get("AITHER_API_URL", "").strip()
        has_explicit_remote = (gateway_url and "localhost" not in gateway_url.lower()) or \
                              (api_url and "localhost" not in api_url.lower())

        if is_localhost and not has_explicit_remote:
            # LOCAL path: call AitherSecrets directly (fast, co-located)
            async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
                resp = await client.post(
                    f"{secrets_url.rstrip('/')}/api-keys",
                    json={"name": f"node-{node_id}", "scopes": ["endpoint:secrets"]},
                    headers={"Authorization": f"Bearer {bearer_token}"},
                )
            if resp.status_code == 200:
                minted = resp.json().get("api_key", "")
                if minted:
                    log.info("Node self-minted its own gateway key (local): %s", node_id)
                    return minted
            else:
                log.debug(
                    "Self-service key mint failed (HTTP %s) — falling back to user token: %s",
                    resp.status_code, resp.text[:200],
                )
        else:
            # REMOTE path: exchange the enrollment token via the PUBLIC endpoint
            # that made this node remote (gateway/api), NOT localhost genesis — a
            # remote node has no local :8001, so defaulting to genesis localhost
            # would POST to a dead port and silently fall back to the user token
            # (the whole remote-mint would be inert). Prefer the explicit public
            # URL; only fall back to AITHER_GENESIS_URL if it is itself remote.
            genesis_env = os.environ.get("AITHER_GENESIS_URL", "").strip()
            exchange_base = (
                api_url or gateway_url
                or (genesis_env if "localhost" not in genesis_env.lower() and genesis_env else "")
            ).rstrip("/")
            if not exchange_base:
                log.warning(
                    "Remote node but no public gateway/api URL set "
                    "(AITHER_GATEWAY_URL / AITHER_API_URL) — cannot exchange enrollment "
                    "token; falling back to user token."
                )
                return ""
            log.debug(
                "Node is remote (secrets_url=%s) — exchanging bearer_token via %s",
                secrets_url, exchange_base,
            )
            async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
                resp = await client.post(
                    f"{exchange_base}/v1/workspace/api-keys/enrollment-token/exchange",
                    json={"enrollment_token": bearer_token},
                )
            if resp.status_code == 200:
                data = resp.json()
                minted = data.get("token", "")
                if minted:
                    log.info(
                        "Node self-minted its own gateway key (remote via Genesis): %s",
                        node_id,
                    )
                    return minted
            else:
                log.warning(
                    "Remote enrollment-token exchange failed (HTTP %s) — "
                    "falling back to user token: %s",
                    resp.status_code, resp.text[:200],
                )
    except Exception as e:  # noqa: BLE001 — self-service is additive, must not block enrollment
        log.warning("Self-service key mint failed — falling back to user token: %s", e)
    return ""


async def enroll_on_boot(
    genesis_url: Optional[str] = None,
    portal_url: Optional[str] = None,
    enable_heartbeat: bool = True,
) -> Dict[str, Any]:
    """Enroll this node into both local and portal fleets.

    Called by process_supervisor after services are up. Orchestrates:
      1. Node registration (federation or Genesis fallback)
      2. Persist node_id + api_key
      3. Agent upsert to portal
      4. Start heartbeat loop (optional, background)

    Args:
        genesis_url: Local Genesis URL (default http://localhost:8001)
        portal_url: Portal hub URL (default https://api.aitherium.com)
        enable_heartbeat: Start background heartbeat loop (default True)

    Returns:
        {
            "enrolled": bool,
            "node_id": str,
            "agents_upserted": int,
            "error": optional error detail
        }
    """
    if not _should_enroll():
        log.debug("Fleet enrollment disabled (AITHER_FLEET_ENROLL not set)")
        return {
            "enrolled": False,
            "reason": "enrollment disabled",
        }

    # Defaults
    genesis_url = genesis_url or os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
    portal_url = portal_url or os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")

    # Resolve Identity service URL for rich enrollment (node registration)
    enroll_base = (
        os.environ.get("AITHER_ENROLL_BASE")
        or os.environ.get("AITHERIDENTITY_URL")
        or os.environ.get("AITHER_IDP_PUBLIC_URL")
        or "https://idp.aitherium.com"
    )

    log.info(
        "Enrolling node in fleet (identity=%s, genesis=%s, portal=%s)",
        enroll_base, genesis_url, portal_url
    )

    # Load or fetch API key
    auth = _load_auth_config()
    api_key = auth.get("access_token") or auth.get("user", {}).get("api_key") or "aither_root_local"

    # Check if already registered
    node_auth = _load_node_auth()
    if node_auth.get("node_id"):
        log.info("Node already enrolled: %s", node_auth["node_id"])
        # Still upsert agents and start heartbeat if enabled
        if enable_heartbeat and not node_auth.get("_heartbeat_started"):
            try:
                asyncio.create_task(
                    _heartbeat_loop(
                        node_auth.get("hub_url", genesis_url),
                        node_auth.get("api_key", api_key),
                        node_auth["node_id"],
                    )
                )
                node_auth["_heartbeat_started"] = True
                _save_node_auth(node_auth)
            except RuntimeError:
                # No running event loop (sync context) — never crash boot.
                log.debug("No event loop for heartbeat; skipping background task")
        return {
            "enrolled": True,
            "node_id": node_auth["node_id"],
            "already_registered": True,
        }

    node_id = _generate_node_id()

    # PRIMARY: rich endpoint enrollment (hardware + inference readiness → Identity
    # node registry → AitherDirectory). Falls through to the legacy federation path
    # if it doesn't take (older control plane, no token, offline, etc.).
    try:
        from adk.enrollment import rich_enroll

        rich = await rich_enroll(
            enroll_base, api_key, node_id, enable_heartbeat=enable_heartbeat
        )
    except Exception as e:  # never let enrollment block boot
        log.debug("Rich enrollment unavailable: %s", e)
        rich = {"enrolled": False, "error": str(e)}

    if rich.get("enrolled"):
        # Self-service a node-scoped gateway key using the capability token
        # /v1/nodes/register just minted, instead of persisting the enrolling
        # USER's own access token as this node's long-lived credential (the
        # prior behavior below, api_key=api_key) — a real, separate
        # over-broad-credential issue this fixes at the same time.
        node_api_key = api_key
        bearer_token = rich.get("bearer_token", "")
        if bearer_token:
            minted = await _self_mint_gateway_key(bearer_token, node_id)
            if minted:
                node_api_key = minted

        _save_node_auth({
            "node_id": node_id,
            "api_key": node_api_key,
            "hub_url": portal_url,
            "tenant_slug": _extract_tenant_slug(),
            "mode": "rich",
            "_heartbeat_started": enable_heartbeat,
        })

        # Enable session sync by default (post-enrollment)
        _enable_session_sync_default()

        # Best-effort: auto-sync entitled packs for onboarding
        packs_installed, packs_failed = await _sync_entitled_packs_best_effort(
            api_key, portal_url,
        )

        log.info("Node enrolled (rich): %s", node_id)
        agents_upserted = 0
        if await _upsert_agents_to_portal(portal_url, api_key, node_id):
            registry = _load_agents_registry()
            agents_upserted = len(registry.get("agents", {}))
        return {
            "enrolled": True,
            "node_id": node_id,
            "agents_upserted": agents_upserted,
            "packs_installed": packs_installed,
            "packs_failed": packs_failed,
            "mode": "rich",
            "workspace": rich.get("workspace", {}),
        }

    # FALLBACK: legacy federation registration (agents only, no hardware).
    log.info("Rich enrollment did not take (%s); using federation path",
             rich.get("error", "unknown"))
    result = await _register_node_with_federation(portal_url, api_key, node_id)
    if result.get("error"):
        # Fallback to Genesis
        log.info("Portal registration failed, falling back to Genesis")
        result = await _register_node_with_genesis(genesis_url, api_key)

    if result.get("error"):
        log.error("Node registration failed: %s", result.get("detail"))
        return {
            "enrolled": False,
            "error": result.get("detail", "unknown"),
        }

    # Persist
    node_id = result["node_id"]
    api_key = result.get("api_key", api_key)
    hub_url = result.get("hub_url", genesis_url)
    _save_node_auth({
        "node_id": node_id,
        "api_key": api_key,
        "hub_url": hub_url,
        "tenant_slug": _extract_tenant_slug(),
    })

    # Enable session sync by default (post-enrollment)
    _enable_session_sync_default()

    # Best-effort: auto-sync entitled packs for onboarding
    packs_installed, packs_failed = await _sync_entitled_packs_best_effort(
        api_key, portal_url,
    )

    log.info("Node enrolled: %s", node_id)

    # Upsert agents
    agents_upserted = 0
    if await _upsert_agents_to_portal(hub_url, api_key, node_id):
        registry = _load_agents_registry()
        agents_upserted = len(registry.get("agents", {}))

    # Start heartbeat
    if enable_heartbeat:
        try:
            asyncio.create_task(_heartbeat_loop(hub_url, api_key, node_id))
        except RuntimeError:
            # No running event loop (sync context) — never crash boot.
            log.debug("No event loop for heartbeat; skipping background task")

    return {
        "enrolled": True,
        "node_id": node_id,
        "agents_upserted": agents_upserted,
        "packs_installed": packs_installed,
        "packs_failed": packs_failed,
    }
