"""adk.mesh_provider — one-command setup for AitherNet community inference provider.

A community node self-services into the AitherNet compute market via:
  1. Advertise an inference endpoint to the peer record
  2. Grant participation consent (GDPR-style, revocable)
  3. Poll for operator trust tier (fail-closed gate)
  4. Print the operator command for final registration

This wraps the manual runbook into one idempotent command: adk mesh provide
--inference-url http://10.77.x.x:8000/v1 --model NAME
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("adk.mesh_provider")

# Public federation entry point. Internal fleet callers override these via
# AITHER_STRATA_URL / AITHER_CONDUCTOR_URL (set in placement_policy.yaml + compose)
# with the in-cluster service addresses; a standalone/remote node reaches the same
# services through the authenticated public gateway.
DEFAULT_STRATA_URL = os.getenv("AITHER_STRATA_URL", "https://gateway.aitherium.com")
DEFAULT_CONDUCTOR_URL = os.getenv("AITHER_CONDUCTOR_URL", "https://gateway.aitherium.com")


def _tls_verify() -> Any:
    """Return TLS verify policy (CA bundle path or True). Never False."""
    from adk._tls import tls_verify
    return tls_verify()


def _get_auth_token() -> str | None:
    """Resolve an auth token from adk config or environment.

    Returns the token or None if not set. This token is used to authenticate
    API calls to Strata and Conductor endpoints.
    """
    # Try environment first
    token = os.getenv("AITHER_AUTH_TOKEN", "").strip()
    if token:
        return token

    # Try adk config file
    try:
        from pathlib import Path
        config_file = Path.home() / ".aither" / "config.json"
        if config_file.exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            return config.get("auth_token", "").strip() or None
    except Exception:
        pass

    return None


async def _resolve_peer_id() -> str:
    """Get the peer_id from the node's onboarded state.

    After a node runs `adk mesh onboard`, its peer_id is stored in the
    Strata peer record (the node's mesh identity). Real peer IDs have the format
    'peer-<12 hex characters>'.

    Resolution order:
      1. AITHER_PEER_ID environment variable (if set)
      2. ~/.aither/node_auth.json peer_id (set by onboard flow)
      3. Raise a clear error instructing the user to onboard or set AITHER_PEER_ID

    Never fabricates a peer ID from overlay IP or other sources.
    """
    # Try environment first
    peer_id = os.getenv("AITHER_PEER_ID", "").strip()
    if peer_id:
        return peer_id

    # Try adk node_auth.json (set by onboard flow)
    try:
        from pathlib import Path
        node_auth_file = Path.home() / ".aither" / "node_auth.json"
        if node_auth_file.exists():
            auth_data = json.loads(node_auth_file.read_text(encoding="utf-8"))
            peer_id = auth_data.get("peer_id", "").strip()
            if peer_id:
                return peer_id
    except Exception:
        pass

    # Fail-closed: do not fabricate. Real peer IDs look like 'peer-<12 hex>'.
    raise RuntimeError(
        "Cannot resolve peer_id. Run 'adk mesh onboard' first to register this node, "
        "or set AITHER_PEER_ID environment variable to the node's authenticated peer ID "
        "(format: peer-<12 hex characters>)."
    )


async def advertise(
    peer_id: str,
    inference_url: str,
    inference_model: str,
    strata_url: str = DEFAULT_STRATA_URL,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Register the inference endpoint on the peer record.

    POST /strata/mesh/peers/inference with:
      - peer_id: the node's authenticated peer identity
      - inference_url: the OpenAI-compatible server URL
      - inference_model: model name (e.g., "gemma4-12b")

    Fail-closed: if the peer doesn't exist, the request 404s and we return error.
    Never trusts caller input for scope — the peer_id is authenticated at
    onboard time.
    """
    import httpx

    if not peer_id:
        return {
            "ok": False,
            "error": "peer_id required",
            "step": "advertise",
        }
    if not inference_url or not inference_model:
        return {
            "ok": False,
            "error": "inference_url and inference_model required",
            "step": "advertise",
        }

    url = strata_url.rstrip("/") + "/strata/mesh/peers/inference"
    payload = {
        "peer_id": peer_id,
        "inference_url": inference_url,
        "inference_model": inference_model,
    }
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=_tls_verify()) as c:
            r = await c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return {
                "ok": True,
                "step": "advertise",
                "peer_id": peer_id,
                "inference_url": inference_url,
                "inference_model": inference_model,
            }
    except httpx.HTTPStatusError as e:
        logger.error("advertise failed: %s %s", e.response.status_code, e.response.text)
        return {
            "ok": False,
            "error": f"advertise failed: {e.response.status_code}",
            "step": "advertise",
        }
    except Exception as e:
        logger.error("advertise error: %s", e)
        return {
            "ok": False,
            "error": f"advertise error: {e}",
            "step": "advertise",
        }


async def grant_consent(
    peer_id: str,
    tenant_id: str | None = None,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
    granted: bool = True,
) -> dict[str, Any]:
    """Grant participation consent for the node to serve community inference.

    POST /v1/mesh/peers/{peer}/consent with:
      - tenant_id: the owner's authenticated tenant (required for fail-closed gate)
      - granted: true

    Default-deny: without an explicit grant, the routing gate refuses.
    Tenant must be derived from the authenticated caller, never from the request.

    In the CLI context, the authenticated caller is the user providing a valid
    auth_token. The tenant_id is the user's authenticated owner identity, and
    Conductor validates the binding server-side. No token = deny.
    """
    import httpx

    if not peer_id:
        return {
            "ok": False,
            "error": "peer_id required",
            "step": "consent",
        }

    # Fail-closed: tenant_id is required and must match the caller's identity.
    # If not provided, we cannot proceed.
    if not tenant_id:
        return {
            "ok": False,
            "error": "tenant_id required (your authenticated owner identity)",
            "step": "consent",
        }

    # Fail-closed: auth_token is required for authentication.
    # Unauthenticated consent grants are denied.
    if not auth_token:
        return {
            "ok": False,
            "error": "auth_token required (authentication mandatory for consent grant)",
            "step": "consent",
        }

    url = conductor_url.rstrip("/") + f"/v1/mesh/peers/{peer_id}/consent"
    # ``granted`` defaults True so every existing caller is unchanged. The
    # False direction is REVOCATION through the same door: one endpoint, one
    # audit trail, and no second consent writer to drift from this one.
    payload = {
        "tenant_id": tenant_id,
        "granted": bool(granted),
    }
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=_tls_verify()) as c:
            r = await c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return {
                "ok": True,
                "step": "consent",
                "peer_id": peer_id,
                "tenant_id": tenant_id,
                "granted": bool(granted),
            }
    except httpx.HTTPStatusError as e:
        logger.error("consent failed: %s %s", e.response.status_code, e.response.text)
        return {
            "ok": False,
            "error": f"consent failed: {e.response.status_code}",
            "step": "consent",
        }
    except Exception as e:
        logger.error("consent error: %s", e)
        return {
            "ok": False,
            "error": f"consent error: {e}",
            "step": "consent",
        }


async def request_trust(
    peer_id: str,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Submit a self-service trust-tier request to the platform.

    POST /v1/mesh/peers/{peer_id}/trust-request with authentication.
    This records a pending request for the operator to review, and may trigger
    an auto-grant to tier-1 if the platform has AITHER_MESH_AUTO_TRUST_TIER1
    enabled and the peer has community_inference_consent=true (never auto-grant
    above tier 1; operator-only for higher tiers).

    Fail-closed: an authenticated caller is required; unauthenticated requests
    are refused (401). Cross-tenant requests are also refused (403) at Strata.
    """
    import httpx

    if not peer_id:
        return {
            "ok": False,
            "error": "peer_id required",
            "step": "request_trust",
        }

    if not auth_token:
        return {
            "ok": False,
            "error": "auth_token required (authentication mandatory for trust requests)",
            "step": "request_trust",
        }

    url = conductor_url.rstrip("/") + f"/v1/mesh/peers/{peer_id}/trust-request"
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=_tls_verify()) as c:
            r = await c.post(url, json={}, headers=headers)
            r.raise_for_status()
            result = r.json()
            return {
                "ok": True,
                "step": "request_trust",
                "peer_id": peer_id,
                "status": result.get("status", "pending"),
                "auto_tier_eligible": result.get("auto_tier_eligible", False),
            }
    except httpx.HTTPStatusError as e:
        logger.error("request_trust failed: %s %s", e.response.status_code, e.response.text)
        status_code = e.response.status_code
        if status_code == 401:
            error = "Authentication required for trust requests"
        elif status_code == 403:
            error = "You are not the owner of this peer"
        elif status_code == 404:
            error = f"Unknown peer: {peer_id}"
        else:
            error = f"request_trust failed: {status_code}"
        return {
            "ok": False,
            "error": error,
            "step": "request_trust",
        }
    except Exception as e:
        logger.error("request_trust error: %s", e)
        return {
            "ok": False,
            "error": f"request_trust error: {e}",
            "step": "request_trust",
        }


async def poll_trust_tier(
    peer_id: str,
    strata_url: str = DEFAULT_STRATA_URL,
    auth_token: str | None = None,
    max_wait_seconds: int = 30,
) -> dict[str, Any]:
    """Poll for operator trust grant on the peer record.

    GET {strata}/strata/mesh/peers/{peer} and read the trust_level field
    (the authoritative shared peer record; a tenant-scoped mesh token may
    read only its OWN peer — Conductor has no peer GET route).

    Trust level:
      - 0 = guest (never routed, default)
      - 1 = trusted (eligible for routing)
      - 2 = verified

    Fail-closed: if trust is 0 or missing, the node is not yet eligible.
    This polls with a bounded timeout; the operator must manually promote
    the node (the node cannot self-promote).
    """
    import httpx

    if not peer_id:
        return {
            "ok": False,
            "error": "peer_id required",
            "step": "poll_trust",
        }

    url = strata_url.rstrip("/") + f"/strata/mesh/peers/{peer_id}"
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    start_time = time.time()
    poll_interval = 2.0  # seconds between polls

    while True:
        elapsed = time.time() - start_time
        if elapsed > max_wait_seconds:
            # Timeout: still guest (trust tier 0)
            return {
                "ok": False,
                "error": "operator trust grant timed out",
                "step": "poll_trust",
                "peer_id": peer_id,
                "waited_seconds": int(elapsed),
                "message": (
                    "Node is still in guest tier (0). An operator must promote it. "
                    "See the next step below for the operator command."
                ),
            }

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=_tls_verify()) as c:
                r = await c.get(url, headers=headers)
                r.raise_for_status()
                peer_data = r.json()
                # Real wire shape: Strata returns trust_level (NOT trust_tier).
                try:
                    trust_level = int(peer_data.get("trust_level", 0))
                except (TypeError, ValueError):
                    trust_level = 0

                if trust_level >= 1:
                    # Promoted! Success.
                    return {
                        "ok": True,
                        "step": "poll_trust",
                        "peer_id": peer_id,
                        "trust_level": trust_level,
                    }
        except httpx.HTTPStatusError as e:
            logger.warning("poll_trust GET failed: %s", e.response.status_code)
        except Exception as e:
            logger.warning("poll_trust error: %s", e)

        # Poll interval: wait before retrying
        await asyncio.sleep(poll_interval)


async def join_pool(
    peer_id: str,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """SELF-SERVICE pool entry: put this owner's gated peer into the routable pool.

    POST /v1/mesh/peers/{peer_id}/join-pool — the platform re-runs the fail-closed
    community gate server-side (trust>=1 AND consent AND advertised endpoint) and
    derives the backend from the AUTHENTICATED peer record. Nothing routable is
    caller-supplied. 403 = not eligible yet (with the reason); 404 = not your peer.
    """
    import httpx

    if not peer_id:
        return {"ok": False, "error": "peer_id required", "step": "join_pool"}
    if not auth_token:
        return {
            "ok": False,
            "error": "auth_token required (authentication mandatory for pool entry)",
            "step": "join_pool",
        }

    url = conductor_url.rstrip("/") + f"/v1/mesh/peers/{peer_id}/join-pool"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {auth_token}"}
    try:
        async with httpx.AsyncClient(timeout=25.0, verify=_tls_verify()) as c:
            r = await c.post(url, json={}, headers=headers)
            r.raise_for_status()
            result = r.json()
            return {
                "ok": True,
                "step": "join_pool",
                "peer_id": peer_id,
                "backend_name": result.get("backend_name", ""),
                "joined": bool(result.get("joined")),
            }
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 403:
            error = ("Not eligible for the pool yet: needs trust tier >= 1, owner "
                     "consent, and an advertised inference endpoint")
        elif code == 404:
            error = f"Unknown peer (or not your peer): {peer_id}"
        elif code == 401:
            error = "Authentication required for pool entry"
        else:
            error = f"join_pool failed: {code}"
        logger.error("join_pool failed: %s %s", code, e.response.text[:200])
        return {"ok": False, "error": error, "step": "join_pool"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"join_pool transport error: {e}", "step": "join_pool"}


async def leave_pool(
    peer_id: str,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """SELF-SERVICE pool exit (drain): remove this owner's backend from routing."""
    import httpx

    if not peer_id:
        return {"ok": False, "error": "peer_id required", "step": "leave_pool"}
    if not auth_token:
        return {
            "ok": False,
            "error": "auth_token required (authentication mandatory for pool exit)",
            "step": "leave_pool",
        }

    url = conductor_url.rstrip("/") + f"/v1/mesh/peers/{peer_id}/leave-pool"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {auth_token}"}
    try:
        async with httpx.AsyncClient(timeout=25.0, verify=_tls_verify()) as c:
            r = await c.post(url, json={}, headers=headers)
            r.raise_for_status()
            result = r.json()
            return {"ok": True, "step": "leave_pool", "peer_id": peer_id,
                    "backend_name": result.get("backend_name", "")}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        error = (f"Unknown peer (or not your peer): {peer_id}" if code == 404
                 else f"leave_pool failed: {code}")
        logger.error("leave_pool failed: %s %s", code, e.response.text[:200])
        return {"ok": False, "error": error, "step": "leave_pool"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"leave_pool transport error: {e}", "step": "leave_pool"}


async def federation_token(
    peer_id: str,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """SELF-SERVICE relay-federation credential: mint the AITHER_NODE_TOKEN
    this node's sovereign relay presents on the community hub's /ws/chat.

    POST /v1/mesh/peers/{peer_id}/federation-token — ownership enforced server-side
    (404 = not your peer). The key is bound to USER ID == peer_id: the hub's
    federation gate only trusts a bridge whose join nick equals the token's
    authenticated user id, so run the relay with AITHERNET_NODE_SLUG=<peer_id>.
    The token is returned ONCE — store it in the relay's gitignored env.
    """
    import httpx

    if not peer_id:
        return {"ok": False, "error": "peer_id required", "step": "federation_token"}
    if not auth_token:
        return {
            "ok": False,
            "error": "auth_token required (authentication mandatory for token mint)",
            "step": "federation_token",
        }

    url = conductor_url.rstrip("/") + f"/v1/mesh/peers/{peer_id}/federation-token"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {auth_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=_tls_verify()) as c:
            r = await c.post(url, json={}, headers=headers)
            r.raise_for_status()
            result = r.json()
            return {
                "ok": True,
                "step": "federation_token",
                "peer_id": peer_id,
                "node_token": result.get("node_token", ""),
                "node_slug": result.get("node_slug", peer_id),
                "expires_in_days": result.get("expires_in_days"),
            }
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 404:
            error = f"Unknown peer (or not your peer): {peer_id}"
        elif code == 401:
            error = "Authentication required for federation-token mint"
        else:
            error = f"federation_token failed: {code}"
        logger.error("federation_token failed: %s %s", code, e.response.text[:200])
        return {"ok": False, "error": error, "step": "federation_token"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"federation_token transport error: {e}",
                "step": "federation_token"}


async def provide(
    inference_url: str,
    inference_model: str,
    peer_id: str | None = None,
    tenant_id: str | None = None,
    wait_seconds: int = 30,
    strata_url: str | None = None,
    conductor_url: str | None = None,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """One-command provider setup: advertise → consent → poll trust.

    Returns a comprehensive report with the status of each step and next
    actions (operator command) if applicable.

    Args:
        inference_url: OpenAI-compatible server URL (e.g., http://10.77.x.x:8000/v1)
        inference_model: Model name (e.g., "gemma4-12b")
        peer_id: Node's mesh peer ID (auto-resolved if not given)
        tenant_id: Owner's authenticated tenant (required for fail-closed gate)
        wait_seconds: Timeout for polling operator trust (default 30s)
        strata_url: Strata endpoint override
        conductor_url: Conductor endpoint override
        auth_token: Bearer token for API calls (auto-resolved if not given)

    Returns:
        dict with keys:
          - ok: True if all steps succeeded
          - steps: list of step results
          - next_action: operator command (if awaiting trust)
    """
    strata_url = strata_url or os.getenv("AITHER_STRATA_URL", DEFAULT_STRATA_URL)
    conductor_url = conductor_url or os.getenv(
        "AITHER_CONDUCTOR_URL", DEFAULT_CONDUCTOR_URL
    )
    auth_token = auth_token or _get_auth_token()

    # Fail-closed: auth_token is required for consent binding.
    # Without authentication, the provider cannot grant or revoke consent.
    if not auth_token:
        return {
            "ok": False,
            "error": (
                "auth_token required for consent binding. "
                "Set AITHER_AUTH_TOKEN environment variable or configure ~/.aither/config.json"
            ),
            "step": "init",
        }

    if not peer_id:
        try:
            peer_id = await _resolve_peer_id()
        except RuntimeError as e:
            return {
                "ok": False,
                "error": str(e),
                "step": "init",
            }

    # Fail-closed: tenant_id is required
    if not tenant_id:
        tenant_id = os.getenv("AITHER_TENANT_ID", "").strip()
    if not tenant_id:
        return {
            "ok": False,
            "error": (
                "tenant_id required (your authenticated owner identity). "
                "Set AITHER_TENANT_ID or pass --tenant-id."
            ),
            "step": "init",
        }

    report = {
        "ok": True,
        "steps": [],
        "peer_id": peer_id,
        "inference_url": inference_url,
        "inference_model": inference_model,
    }

    # Step 1: Advertise
    logger.info("advertise: peer=%s url=%s model=%s", peer_id, inference_url, inference_model)
    ad_result = await advertise(
        peer_id=peer_id,
        inference_url=inference_url,
        inference_model=inference_model,
        strata_url=strata_url,
        auth_token=auth_token,
    )
    report["steps"].append(ad_result)
    if not ad_result.get("ok"):
        report["ok"] = False
        return report

    # Step 2: Grant Consent
    logger.info("consent: peer=%s tenant=%s", peer_id, tenant_id)
    consent_result = await grant_consent(
        peer_id=peer_id,
        tenant_id=tenant_id,
        conductor_url=conductor_url,
        auth_token=auth_token,
    )
    report["steps"].append(consent_result)
    if not consent_result.get("ok"):
        report["ok"] = False
        return report

    # Step 3: Request Trust (self-service trust-tier request)
    logger.info("request_trust: peer=%s", peer_id)
    trust_req_result = await request_trust(
        peer_id=peer_id,
        conductor_url=conductor_url,
        auth_token=auth_token,
    )
    report["steps"].append(trust_req_result)
    if not trust_req_result.get("ok"):
        report["ok"] = False
        # Trust request failed — but this might be a transient error, so continue
        # to polling (which will timeout if the request wasn't recorded)
        logger.warning("trust request failed: %s", trust_req_result.get("error"))

    # Step 4: Poll Trust (bounded timeout) — reads the authoritative Strata peer
    # record (Conductor has no peer GET route; the field is trust_level).
    # If auto-tier-1 was triggered by request_trust, this should complete quickly.
    logger.info("poll_trust: peer=%s wait=%ds", peer_id, wait_seconds)
    trust_result = await poll_trust_tier(
        peer_id=peer_id,
        strata_url=strata_url,
        auth_token=auth_token,
        max_wait_seconds=wait_seconds,
    )
    report["steps"].append(trust_result)

    if trust_result.get("ok"):
        # Trust granted — complete the loop SELF-SERVICE: join the routable pool.
        # The platform re-runs the fail-closed gate server-side; nothing routable
        # comes from this client. (Previously this printed an operator command —
        # owner directive 2026-07-23: no manual operator step.)
        report["trust_granted"] = True
        logger.info("join_pool: peer=%s", peer_id)
        join_result = await join_pool(
            peer_id=peer_id,
            conductor_url=conductor_url,
            auth_token=auth_token,
        )
        report["steps"].append(join_result)
        if join_result.get("ok"):
            report["joined_pool"] = True
            report["backend_name"] = join_result.get("backend_name", "")
            report["message"] = (
                "You are now serving the AitherNet community pool and earning "
                "settlement. Leave anytime with: adk mesh leave"
            )
        else:
            report["ok"] = False
            report["joined_pool"] = False
            report["message"] = join_result.get("error", "pool entry failed")
    else:
        # Still guest (trust 0) — awaiting operator promotion
        report["ok"] = False
        report["trust_granted"] = False
        report["awaiting_operator"] = True
        report["message"] = trust_result.get("message", "")
        report["next_action"] = (
            f"Operator (platform only): promote the node with:\n"
            f"  curl -X POST {conductor_url}/v1/mesh/peers/{peer_id}/trust-tier \\\n"
            f"    -H 'Content-Type: application/json' \\\n"
            f"    -d '{{\"trust_level\": 1}}'"
        )

    return report


async def flux_node(
    flux_image: str | None = None,
    flux_port: int | None = None,
    mesh_src: str | None = None,
    node_id: str | None = None,
    aither_internal_secret: str | None = None,
) -> dict[str, Any]:
    """Start a Flux event-plane listener on this node.

    The Flux listener participates in the AitherMesh event-plane and serves
    on the given port. This is a node-local operation (runs via subprocess).

    Args:
        flux_image: Docker image to run (default: ghcr.io/aitherium/mesh-agent:latest)
        flux_port: Port to bind the listener (default: 8117)
        mesh_src: Host path to mount as /app (default: /opt/aitheros/mesh-src)
        node_id: Mesh node identifier (required; e.g., spark-dgx, computed-node-1)
        aither_internal_secret: Service-internal secret from vault (required; never
                                echoed). If not provided, attempts to resolve from
                                AITHER_INTERNAL_SECRET environment variable.

    Returns:
        dict with keys:
          - ok: True if the container started and passed health checks
          - message: Human-readable status
          - error: (if ok=False) error description
          - container: container name (aither-flux)
          - port: bound port
    """
    from pathlib import Path

    # Explicit param > environment > built-in default (library callers get the
    # same env resolution the CLI layer performs).
    if flux_image is None:
        flux_image = os.getenv("FLUX_IMAGE", "ghcr.io/aitherium/mesh-agent:latest")
    if flux_port is None:
        flux_port = int(os.getenv("FLUX_PORT", "8117"))
    if mesh_src is None:
        mesh_src = os.getenv("MESH_SRC", "/opt/aitheros/mesh-src")

    # Fail-closed: node_id is required and identifies the node on the mesh.
    if not node_id:
        return {
            "ok": False,
            "error": "node_id required (e.g., spark-dgx, computed-node-1)",
            "step": "flux_node_init",
        }

    # Fail-closed: aither_internal_secret is required and must not be echoed.
    if not aither_internal_secret:
        aither_internal_secret = os.getenv("AITHER_INTERNAL_SECRET", "").strip()
    if not aither_internal_secret:
        return {
            "ok": False,
            "error": (
                "aither_internal_secret required. "
                "Set AITHER_INTERNAL_SECRET environment variable or pass "
                "--aither-internal-secret (never echoed)"
            ),
            "step": "flux_node_init",
        }

    # Find the flux-node-up.sh script. Try common paths:
    # 1. In the adk package (relative to this file)
    # 2. In the system PATH via 'which aither-flux-node-up.sh'
    # 3. In awskills/scripts/ from the repo root
    script_paths = [
        Path(__file__).parent.parent.parent / "awskills" / "scripts" / "flux-node-up.sh",
        Path("/usr/local/bin/flux-node-up.sh"),
        Path("/opt/aitheros/scripts/flux-node-up.sh"),
    ]

    script_path = None
    for p in script_paths:
        if p.exists() and p.is_file():
            script_path = p
            break

    if not script_path:
        return {
            "ok": False,
            "error": (
                "flux-node-up.sh not found in expected locations. "
                "Ensure awskills is installed or the script is in PATH."
            ),
            "step": "flux_node_init",
        }

    # Prepare environment for subprocess (secret is NOT echoed in logs).
    env = os.environ.copy()
    env["FLUX_IMAGE"] = flux_image
    env["FLUX_PORT"] = str(flux_port)
    env["MESH_SRC"] = mesh_src
    env["NODE_ID"] = node_id
    env["AITHER_INTERNAL_SECRET"] = aither_internal_secret

    logger.info("starting flux listener: node=%s port=%d", node_id, flux_port)

    try:
        # Run the script as subprocess (no-shell to avoid injection).
        # Redirect stdout/stderr to capture output.
        result = subprocess.run(
            [str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60.0,  # 60s timeout for the script
        )

        if result.returncode == 0:
            # Success: extract the final message from stdout
            output_lines = result.stdout.strip().split("\n")
            return {
                "ok": True,
                "message": "Flux listener started and healthy",
                "container": "aither-flux",
                "port": flux_port,
                "node_id": node_id,
                "output": "\n".join(output_lines[-5:]),  # Last few lines
            }
        else:
            # Failure: extract error from stderr or stdout
            error_output = result.stderr.strip() or result.stdout.strip()
            logger.error("flux_node script failed: %s", error_output)
            return {
                "ok": False,
                "error": f"flux-node-up.sh exited with code {result.returncode}",
                "details": error_output[:500],  # Truncate long errors
                "step": "flux_node_start",
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "flux-node-up.sh timed out (60s)",
            "step": "flux_node_start",
        }
    except Exception as e:
        logger.error("flux_node error: %s", e)
        return {
            "ok": False,
            "error": f"flux_node error: {e}",
            "step": "flux_node_start",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Idle-only lending — the NODE-SIDE half of "Lend Compute" (W11 / L7)
# ─────────────────────────────────────────────────────────────────────────────
#
# ELIGIBILITY is platform-side and default-deny (the Conductor consent record
# under the ``community_inference`` purpose). SERVING is this node's own
# inference process. Idle-only belongs HERE, on the node, and nowhere else: the
# only alternative is the node self-reporting "I am idle" to the platform and the
# platform trusting it, which turns a comfort feature into a security-relevant
# claim a provider controls.
#
# So the supervisor never asks the platform for anything and never asserts a
# state. It does exactly one thing in each direction:
#
#   idle past the threshold, past the cooldown, and enabled  ->  grant consent
#   no longer idle                                           ->  WITHDRAW
#
# Withdrawing is revoking consent (and, if asked, withdrawing the advertised
# endpoint). The platform's existing PER-REQUEST re-gate
# (``AitherLLMQueue._regate_community_backend`` -> ``evaluate_community_peer``)
# evicts the peer on the next routed request. That is why this file must NOT
# touch either routing gate: a mistake in the cross-mesh scope filter or the
# per-peer re-gate is a cross-tenant defect, not a feature bug.
#
# The eligibility decision is a PURE FUNCTION (``idle_lending_eligible``) taking
# the config, the measured idle minutes and a clock, so both directions can be
# asserted without a GPU, a mesh, or a network. A flag whose decision cannot be
# tested in both directions is decorative.
#
# Configuration shape: ``enabled`` defaults FALSE (an explicit affirmative is
# required), a minimum-idle threshold, and a cooldown (default 360 minutes) so a
# twitchy idle signal cannot flap the endpoint. Flapping an advertised endpoint is
# worse than never advertising it — every flap is a routed request that fails on a
# peer that was there a second ago.

#: Unset is OFF. Every other toggle in this family defaults ON via ``!== false``
#: and copying that idiom here would make lending somebody's GPU the shipped
#: default, which is a safety risk and a poor user experience.
IDLE_LENDING_ENABLED_DEFAULT = False

#: Minutes the box must have been idle before lending may start.
IDLE_LENDING_MIN_IDLE_MINUTES = 30.0

#: Minutes between two starts. Prevents endpoint flap on a twitchy idle signal.
IDLE_LENDING_COOLDOWN_MINUTES = 360.0

#: A serve command this supervisor may PRINT for the operator. It binds the
#: inference server to LOOPBACK and reaches the mesh over the overlay, never
#: 0.0.0.0 — an unauthenticated inference port on every interface is how a
#: provider's box becomes reachable from their whole LAN (and, through a tunnel,
#: further). This is a POSITIVE bind, not merely the absence of 0.0.0.0: a child
#: spawned with no --host inherits its runtime's default, and both awdk apps
#: default to 0.0.0.0.
IDLE_LENDING_SERVE_BIND = "127.0.0.1"


def idle_lending_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the idle-lending config: defaults, then env, then explicit overrides.

    ``enabled`` is truthy ONLY for an explicit affirmative. An unparseable or
    absent value is OFF — the fail-closed direction, and the one that matters,
    because the thing being switched on is somebody else's work running on the
    owner's hardware.
    """
    raw_enabled = os.getenv("AITHER_IDLE_LENDING", "").strip().lower()
    cfg: dict[str, Any] = {
        "enabled": raw_enabled in ("1", "true", "yes", "on")
        if raw_enabled
        else IDLE_LENDING_ENABLED_DEFAULT,
        "min_idle_minutes": _float_env(
            "AITHER_IDLE_LENDING_MIN_IDLE_MINUTES", IDLE_LENDING_MIN_IDLE_MINUTES
        ),
        "cooldown_minutes": _float_env(
            "AITHER_IDLE_LENDING_COOLDOWN_MINUTES", IDLE_LENDING_COOLDOWN_MINUTES
        ),
        "bind_host": IDLE_LENDING_SERVE_BIND,
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def _float_env(name: str, default: float) -> float:
    """Read a float from the environment, falling back on anything unparseable.

    A malformed threshold must not become 0.0 (i.e. "always idle enough"); it
    falls back to the conservative default instead.
    """
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def idle_lending_eligible(
    cfg: dict[str, Any],
    idle_minutes: float,
    last_started_at: float = 0.0,
    now: float | None = None,
) -> bool:
    """May this node START lending right now? Pure; no I/O, no clock of its own.

    Three conditions, all required, in the order that makes a false cheapest:

      1. ``enabled`` — the owner turned it on. Unset is False.
      2. the box has been idle at least ``min_idle_minutes``.
      3. at least ``cooldown_minutes`` have passed since the last start.

    Returns False for a negative or non-numeric idle reading: "I could not
    measure idleness" is not "the box is idle". That distinction is the whole
    reason this is a function rather than an inline check — it is the direction
    a reasonable-looking implementation gets wrong.
    """
    if not cfg.get("enabled", False):
        return False
    try:
        idle = float(idle_minutes)
    except (TypeError, ValueError):
        return False
    if idle < 0:
        return False
    if idle < float(cfg.get("min_idle_minutes", IDLE_LENDING_MIN_IDLE_MINUTES)):
        return False
    cooldown_s = float(cfg.get("cooldown_minutes", IDLE_LENDING_COOLDOWN_MINUTES)) * 60.0
    clock = time.time() if now is None else float(now)
    return (clock - float(last_started_at or 0.0)) >= cooldown_s


def idle_lending_should_withdraw(cfg: dict[str, Any], idle_minutes: float) -> bool:
    """Must this node WITHDRAW right now?

    Withdrawal is NOT the negation of ``idle_lending_eligible``: the cooldown
    governs starting, never stopping. Making them symmetric would hold a busy
    box in the pool for the rest of its cooldown, which is exactly the promise
    "idle-only" makes and the one a naive ``not eligible()`` breaks.

    Withdrawal is also the answer when the owner switches the flag OFF while
    lending, and when idleness cannot be measured at all — if we do not know the
    box is idle, we do not keep serving on the assumption that it is.
    """
    if not cfg.get("enabled", False):
        return True
    try:
        idle = float(idle_minutes)
    except (TypeError, ValueError):
        return True
    if idle < 0:
        return True
    return idle < float(cfg.get("min_idle_minutes", IDLE_LENDING_MIN_IDLE_MINUTES))


def idle_lending_serve_hint(model: str, port: int = 8000) -> str:
    """The serve command this supervisor prints for the operator.

    Emitted with an EXPLICIT loopback bind. The platform reaches the endpoint
    over the WireGuard overlay address advertised in the peer record, so binding
    the inference server to every interface buys nothing and costs an
    unauthenticated port on the operator's LAN.
    """
    safe_model = (model or "").strip() or "<model>"
    return (
        f"llama-server --model {safe_model} "
        f"--host {IDLE_LENDING_SERVE_BIND} --port {int(port)}"
    )


async def idle_lending_tick(
    peer_id: str,
    idle_minutes: float,
    *,
    tenant_id: str | None = None,
    conductor_url: str = DEFAULT_CONDUCTOR_URL,
    auth_token: str | None = None,
    last_started_at: float = 0.0,
    cfg: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One supervisor step. Decides, then acts through the EXISTING consent door.

    Returns a report; never raises. It calls ``grant_consent`` in both directions
    (granted True to lend, False to withdraw) because that is the one door the
    routing gate reads — this function adds no rival gate and writes no state of
    its own. Auth is the adk token; the fleet internal secret is never used, and
    TLS verification is never disabled.
    """
    resolved = cfg or idle_lending_config()
    decision = "hold"
    if idle_lending_should_withdraw(resolved, idle_minutes):
        decision = "withdraw"
    elif idle_lending_eligible(resolved, idle_minutes, last_started_at, now):
        decision = "lend"

    report: dict[str, Any] = {
        "ok": True,
        "decision": decision,
        "idle_minutes": idle_minutes,
        "enabled": bool(resolved.get("enabled", False)),
        "min_idle_minutes": resolved.get("min_idle_minutes"),
        "cooldown_minutes": resolved.get("cooldown_minutes"),
        "bind_host": resolved.get("bind_host", IDLE_LENDING_SERVE_BIND),
        "step": "idle_lending_tick",
    }
    if decision == "hold":
        report["message"] = (
            "Idle long enough to keep lending, but inside the start cooldown; "
            "not re-advertising yet."
        )
        return report

    granted = decision == "lend"
    result = await grant_consent(
        peer_id,
        tenant_id=tenant_id,
        conductor_url=conductor_url,
        auth_token=auth_token,
        granted=granted,
    )
    report["consent"] = result
    report["ok"] = bool(result.get("ok", False))
    report["message"] = (
        "Lending: consent granted; the platform may route to this node."
        if granted
        else "Withdrawn: consent revoked. The platform's per-request re-gate "
        "evicts this node on the next routed request."
    )
    return report
