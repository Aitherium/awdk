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
    "enroll_base_url",
    "active_node_link",
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


#: The bearer the local-root profile carries. It is NOT an identity: Identity 401s it.
LOCAL_ROOT_TOKEN = "aither_root_local"


def _is_local_root(profile: Dict[str, Any]) -> bool:
    """The offline placeholder profile, in any of the shapes it has been written in.

    Older writers set no ``is_local_root`` flag, only ``token_type: local`` and the
    ``aither_root_local`` bearer. Measured 2026-09-22: such a file made ``adk rc``
    believe it was signed in, and enrolment died on an opaque Identity 401.
    """
    return bool(
        profile.get("is_local_root")
        or profile.get("token_type") == "local"
        or profile.get("access_token") == LOCAL_ROOT_TOKEN
    )


def _load_auth_config() -> Dict[str, Any]:
    """Load auth.json and return the ACTIVE identity as a flat dict.

    ``adk login`` writes the multi-profile layout of ``adk.shell.auth.AuthStore``
    (``{"version": 1, "active_profile": "cloud", "profiles": {"cloud": {...}}}``),
    but every reader here looked for ``access_token`` / ``tenant_slug`` at the TOP
    level — so after a real login ``adk enroll`` said "Not signed in" and the boot
    path enrolled with the bearer ``aither_root_local``. Measured 2026-09-13 while
    wiring the phone door. Flatten to the active profile; a legacy flat file passes
    through unchanged. The local root placeholder is NOT an identity.
    """
    if not _AUTH_FILE.exists():
        return {}
    try:
        data = json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read auth.json: %s", e)
        return {}
    if not isinstance(data, dict):
        return {}
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        active = data.get("active_profile") or ""
        profile = profiles.get(active) if active else None
        if not isinstance(profile, dict) and profiles:
            # No active marker: the only profile, or the first one, is the identity.
            profile = next((p for p in profiles.values() if isinstance(p, dict)), None)
        if not isinstance(profile, dict) or _is_local_root(profile):
            return {}
        flat = dict(profile)
        user = flat.get("user") if isinstance(flat.get("user"), dict) else {}
        if user.get("tenant_slug") and not flat.get("tenant_slug"):
            flat["tenant_slug"] = user["tenant_slug"]
        return flat
    return {} if _is_local_root(data) else data


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


def _extract_tenant_id() -> str:
    """The signed-in identity's tenant_id from auth.json, or "".

    ``adk login`` stores it under ``user.tenant_id``; a legacy flat file may
    carry it at the top level. Never invented: no identity means no tenant.
    """
    auth = _load_auth_config()
    user = auth.get("user") if isinstance(auth.get("user"), dict) else {}
    return str(auth.get("tenant_id") or user.get("tenant_id") or "").strip()


def node_tenant_id() -> str:
    """The tenant this node syncs as: node_auth.json ``tenant_id``, else the
    signed-in identity's tenant from auth.json. Empty when neither has one.

    Enrollment used to persist only ``tenant_slug``, so every reader that
    needed ``tenant_id`` (``adk ingest --brain``, session ingest) saw "" and
    silently turned brain sync off on every enrolled node.
    """
    tid = str(_load_node_auth().get("tenant_id") or "").strip()
    return tid or _extract_tenant_id()


def _backfill_node_tenant_id(node_auth: Dict[str, Any]) -> None:
    """Add a missing ``tenant_id`` to an existing node_auth.json in place,
    keeping every other field (including ``enrolled_at``) unchanged."""
    if str(node_auth.get("tenant_id") or "").strip():
        return
    tid = _extract_tenant_id()
    if not tid:
        return
    data = dict(node_auth)
    data["tenant_id"] = tid
    try:
        _NODE_AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        node_auth["tenant_id"] = tid
    except OSError as exc:
        log.warning("Could not backfill tenant_id into node_auth.json: %s", exc)


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


def enroll_base_url() -> str:
    """The Identity service that owns ``/v1/nodes/*`` — one resolution for
    enrollment, the heartbeat and ``adk devices``."""
    return (
        os.environ.get("AITHER_ENROLL_BASE")
        or os.environ.get("AITHERIDENTITY_URL")
        or os.environ.get("AITHER_IDP_PUBLIC_URL")
        or "https://idp.aitherium.com"
    ).rstrip("/")


#: Identity ANSWERED and said no. These are never papered over by the federation
#: path: a 402 (subscription_required) or 403 (device_quota_exceeded) that turned
#: into a "successful" federation registration is the fail-open shape this code
#: keeps paying for — the device looks enrolled and is in no list the owner sees.
_REFUSAL_STATUSES = (401, 402, 403)

# ── The reverse link (FRONT C4b) ───────────────────────────────────────────
#
# A device is enrolled when identity knows about it. It is REACHABLE when
# something can actually get to it. For a laptop with `wg` those are the same
# thing; for a phone they were never the same thing, because a phone has no wg
# module and the `public_url` identity advertises resolves through a proxy that
# forwards over WireGuard. The link closes that gap: an outbound WebSocket this
# process holds open, over which the tunnel sends allowlisted requests.
#
# ONE per process, same reasoning as the heartbeat task below.
_node_link: Optional[Any] = None
_node_link_task: Optional["asyncio.Task[None]"] = None


def active_node_link() -> Optional[Any]:
    """The reverse link this process holds, if any. ``adk devices status`` reads it."""
    return _node_link


def _start_node_link(
    node_id: str,
    token: str,
    *,
    inference_url: str = "",
    harness_url: str = "",
    harness_token: str = "",
) -> Optional[Any]:
    """Create and schedule the reverse link. Returns it, or None.

    Never raises: a device that cannot hold a link is still enrolled, still
    heart-beating, and still reachable over WireGuard if it has it. The failure is
    LOGGED and visible on the link object (`last_error`) rather than swallowed --
    "enrolled but unreachable" is exactly the state that needs to be sayable.
    """
    global _node_link, _node_link_task
    if _node_link_task is not None and not _node_link_task.done():
        return _node_link
    try:
        from adk.node_link import NodeLink, default_tunnel_url

        link = NodeLink(
            tunnel_url=default_tunnel_url(),
            node_id=node_id,
            token=token,
            inference_url=inference_url,
            harness_url=harness_url,
            harness_token=harness_token,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Reverse link unavailable: %s", e)
        return None
    _node_link = link
    try:
        _node_link_task = asyncio.get_running_loop().create_task(link.run())
    except RuntimeError:
        # Synchronous caller: the object exists (so reach reads `none` honestly)
        # but nothing is driving it.
        log.debug("No event loop for the reverse link; not started")
        _node_link_task = None
    return link


def _link_reach_provider(link: Optional[Any]) -> Optional[Any]:
    """`reach_kind` callable for the heartbeat, or None when there is no link."""
    return link.reach_kind if link is not None else None


# One heartbeat task per process. This used to be a `_heartbeat_started` flag
# PERSISTED in node_auth.json, which is wrong across processes: `adk enroll`
# exits, the task dies with its loop, and every later boot read the flag and
# never started a heartbeat again.
_heartbeat_task: Optional["asyncio.Task[None]"] = None


def _start_heartbeat_task(coro) -> bool:
    """Schedule ``coro`` as this process's heartbeat unless one is already running.

    Returns True if scheduled. Never raises: no running loop means the caller is
    synchronous and the task is simply not started.
    """
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        coro.close()
        return False
    try:
        _heartbeat_task = asyncio.get_running_loop().create_task(coro)
        return True
    except RuntimeError:
        coro.close()
        log.debug("No event loop for heartbeat; skipping background task")
        return False


async def enroll_on_boot(
    genesis_url: Optional[str] = None,
    portal_url: Optional[str] = None,
    enable_heartbeat: bool = True,
    *,
    inference_url: Optional[str] = None,
    node_class: str = "laptop",
    start_link: bool = True,
    harness_url: str = "",
    harness_token: str = "",
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
        inference_url: Explicit local inference base URL; ``None``/``"auto"``
            walks :func:`adk.enrollment.probe_inference`'s ladder. The url the
            probe settles on is persisted so the heartbeat re-probes the SAME one.
        node_class: ``phone`` | ``laptop`` | ``sovereign``.

    Returns:
        {
            "enrolled": bool,
            "node_id": str,
            "agents_upserted": int,
            "error": optional error detail,
            "http_status" / "body": present when identity REFUSED (verbatim)
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
    enroll_base = enroll_base_url()

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
        _backfill_node_tenant_id(node_auth)
        # Still upsert agents and start heartbeat if enabled
        if enable_heartbeat:
            if node_auth.get("mode") == "rich":
                # The identity spine's heartbeat, re-probing the url that was
                # persisted at registration — not a fresh ladder walk.
                from adk.enrollment import heartbeat_loop as _rich_heartbeat

                _link = _start_node_link(
                    node_auth["node_id"], api_key,
                    inference_url=node_auth.get("inference_url") or "",
                    harness_url=harness_url,
                    harness_token=harness_token,
                ) if start_link else None
                _start_heartbeat_task(_rich_heartbeat(
                    node_auth.get("enroll_base") or enroll_base,
                    api_key,
                    node_auth["node_id"],
                    inference_url=node_auth.get("inference_url") or None,
                    node_class=node_auth.get("node_class") or "laptop",
                    reach_provider=_link_reach_provider(_link),
                    harness_provider=(
                        (lambda: (harness_url, bool(harness_url))) if harness_url else None
                    ),
                ))
            else:
                _start_heartbeat_task(_heartbeat_loop(
                    node_auth.get("hub_url", genesis_url),
                    node_auth.get("api_key", api_key),
                    node_auth["node_id"],
                ))
        return {
            "enrolled": True,
            "node_id": node_auth["node_id"],
            "already_registered": True,
            "inference_url": node_auth.get("inference_url", ""),
            "inference_kind": node_auth.get("inference_kind", "none"),
            "node_class": node_auth.get("node_class", "laptop"),
        }

    node_id = _generate_node_id()

    # PRIMARY: rich endpoint enrollment (hardware + inference readiness → Identity
    # node registry → AitherDirectory). Falls through to the legacy federation path
    # if it doesn't take (older control plane, no token, offline, etc.) — but NOT
    # when identity answered with a refusal; that is surfaced verbatim.
    # The link object is created BEFORE registration so the heartbeat rich_enroll
    # starts can read its live reach. It is not DIALLED until registration
    # succeeds: the tunnel refuses to attach a link for a node its table does not
    # carry, and identity is what puts it there.
    link = _start_node_link(
        node_id, api_key,
        inference_url="" if (inference_url or "auto") == "auto" else (inference_url or ""),
        harness_url=harness_url,
        harness_token=harness_token,
    ) if start_link else None

    try:
        from adk.enrollment import rich_enroll

        rich = await rich_enroll(
            enroll_base, api_key, node_id,
            enable_heartbeat=enable_heartbeat,
            inference_url=inference_url,
            node_class=node_class,
            reach_provider=_link_reach_provider(link),
            harness_provider=(
                (lambda: (harness_url, bool(harness_url))) if harness_url else None
            ),
        )
    except Exception as e:  # never let enrollment block boot
        log.debug("Rich enrollment unavailable: %s", e)
        rich = {"enrolled": False, "error": str(e)}

    if not rich.get("enrolled") and rich.get("http_status") in _REFUSAL_STATUSES:
        log.error("Identity refused enrollment (HTTP %s): %s",
                  rich.get("http_status"), (rich.get("body") or "")[:200])
        return {
            "enrolled": False,
            "error": rich.get("error", "identity refused enrollment"),
            "http_status": rich.get("http_status"),
            "body": rich.get("body", ""),
            "url": rich.get("url", ""),
        }

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

        reg = rich.get("registration", {}) or {}
        # The link now knows where the local server actually answered, so the
        # requests the tunnel sends have somewhere to go.
        if link is not None:
            link.inference_url = (reg.get("inference_url") or "").rstrip("/")
        _save_node_auth({
            "node_id": node_id,
            "api_key": node_api_key,
            "hub_url": portal_url,
            "enroll_base": enroll_base,
            "tenant_slug": _extract_tenant_slug(),
            # Identity's registration answer is authoritative; the signed-in
            # identity's tenant is the fallback for an older control plane.
            "tenant_id": str(rich.get("tenant_id") or "").strip() or _extract_tenant_id(),
            "mode": "rich",
            # The url the probe SETTLED on (explicit or the ladder's hit), so the
            # heartbeat re-probes this exact server instead of walking again.
            "inference_url": reg.get("inference_url", ""),
            "inference_kind": reg.get("inference_kind", "none"),
            "node_class": reg.get("node_class", node_class),
            "public_url": rich.get("public_url", ""),
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
            "workspace_id": rich.get("workspace_id", ""),
            "registration": reg,
            "public_url": rich.get("public_url", ""),
            "inference_url": reg.get("inference_url", ""),
            "inference_kind": reg.get("inference_kind", "none"),
            "inference_ready": bool(reg.get("inference_ready")),
            "node_class": reg.get("node_class", node_class),
            "reach_kind": link.reach_kind() if link is not None else "none",
            "link_error": getattr(link, "last_error", "") if link is not None else "",
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
        "tenant_id": str(result.get("tenant_id") or "").strip() or _extract_tenant_id(),
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
        _start_heartbeat_task(_heartbeat_loop(hub_url, api_key, node_id))

    return {
        "enrolled": True,
        "node_id": node_id,
        "agents_upserted": agents_upserted,
        "packs_installed": packs_installed,
        "packs_failed": packs_failed,
    }
