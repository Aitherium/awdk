"""One-command orchestrator: adk join (GitHub → hardware → serve → mesh → earn).

This module chains the real AitherOS surfaces in order to onboard a community
compute node: GitHub device flow → detect hardware → resolve recipe → apply
(serve) → verify → enroll → obtain mesh key → mesh overlay join → register
backend → earnings reporting.

All steps degrade gracefully with clear errors; --dry-run walks the plan
without side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.commands.join")

# Public edge for backend registration; gateway.aitherium.com serves Genesis.
# AITHER_GENESIS_URL overrides it for on-fleet or self-hosted use.
_DEFAULT_GENESIS_URL = "https://gateway.aitherium.com"


async def _github_device_flow_login(
    identity_url: str, client_name: str = "adk-join"
) -> dict[str, Any]:
    """Perform GitHub device flow login against AitherIdentity.

    Returns: {access_token, token_type, username, user_id, tenant_id}
    """
    import httpx

    base = identity_url.rstrip("/")

    # Step 1: Start device flow
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/auth/github/device/start",
                json={"client_name": client_name}
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub device start failed: HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
    except httpx.RequestError as e:
        raise RuntimeError(f"GitHub device start unreachable: {e}") from e

    data = resp.json()
    handle = data.get("handle", "")
    user_code = data.get("user_code", "")
    verification_uri = data.get("verification_uri", "")
    expires_in = int(data.get("expires_in", 900))
    interval = int(data.get("interval", 5))

    if not handle or not user_code:
        raise RuntimeError(
            f"GitHub device start returned incomplete response: {data}"
        )

    print()
    print("    GitHub device flow authentication:")
    print(f"      1. Open:  {verification_uri}")
    print(f"      2. Enter: {user_code}")
    print(f"      3. Waiting (expires in {expires_in}s)...")
    print()

    # Step 2: Poll for authorization
    deadline = time.time() + expires_in
    while time.time() < deadline:
        await asyncio.sleep(interval)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base}/auth/github/device/poll",
                    json={"handle": handle}
                )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"GitHub device poll failed: HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
        except httpx.RequestError as e:
            raise RuntimeError(f"GitHub device poll unreachable: {e}") from e

        poll_data = resp.json()
        status = poll_data.get("status", "")

        if status == "complete":
            print("    [ok] Authenticated!")
            return {
                "access_token": poll_data.get("access_token", ""),
                "token_type": poll_data.get("token_type", "bearer"),
                "username": poll_data.get("username", ""),
                "user_id": poll_data.get("user_id", ""),
                "tenant_id": poll_data.get("tenant_id", ""),
            }
        elif status == "pending":
            continue
        elif status == "error":
            error = poll_data.get("error", "unknown")
            raise RuntimeError(f"GitHub authorization failed: {error}")
        else:
            raise RuntimeError(
                f"GitHub device poll returned unknown status: {status}"
            )

    raise RuntimeError("GitHub device flow expired (user did not authorize)")


async def _obtain_mesh_key(identity_url: str, auth_token: str) -> str:
    """Obtain a mesh overlay key for the AUTHENTICATED tenant.

    Calls AitherIdentity POST /v1/mesh-keys/issue — the endpoint derives the
    tenant from the caller's session identity (never caller-supplied) and
    fail-closed denies an anon caller. Returns the key string or raises.
    """
    import httpx

    base = identity_url.rstrip("/")

    try:
        from adk._tls import tls_verify
        verify = tls_verify()
    except ImportError:
        verify = True

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=verify) as client:
            # No body: the endpoint keys on the authenticated identity, not
            # on any caller-supplied tenant_id (fail-closed authz pattern #2).
            resp = await client.post(
                f"{base}/v1/mesh-keys/issue",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Mesh key issuance unavailable (endpoint not yet deployed)"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Mesh key issuance failed: HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
    except httpx.RequestError as e:
        raise RuntimeError(f"Mesh key issuance unreachable: {e}") from e

    data = resp.json()
    key = data.get("mesh_key", "")
    if not key:
        raise RuntimeError(
            f"Mesh key issuance returned no key: {data}"
        )
    return key


def _print_success_summary(
    node_id: str, mesh_ip: str, models: list[str], tenant_id: str
) -> None:
    """Print the success summary with the node's enrollment details."""
    print()
    print("    [ok] Community node onboarded!")
    print()
    print(f"      Node ID:         {node_id}")
    print(f"      Mesh IP:         {mesh_ip}")
    print(f"      Models:          {', '.join(models) if models else 'none'}")
    print(f"      Earning to:      {tenant_id}")
    print()
    print("    Next: models are live on AitherNet. Community can route inference")
    print("    to your node. Earnings accrue in your tenant account.")
    print()


def _serving_spec(recipe: dict, model_override: str | None = None) -> dict[str, Any]:
    """What a resolved recipe serves: id, port, backend type and model names.

    Pure. ``model_override`` (``adk join --model``) replaces the recipe's model
    list. A recipe with no local port (the cloud-api fallback) yields port 0.
    """
    if not isinstance(recipe, dict):
        recipe = {}
    inference = recipe.get("inference_config") or {}
    deployment = recipe.get("deployment") or {}
    backend_cfg = recipe.get("backend_config") or {}
    try:
        port = int(deployment.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if model_override:
        models = [model_override]
    else:
        models = [
            str(m.get("served_name") or m.get("name") or "")
            for m in inference.get("models") or []
            if isinstance(m, dict)
        ]
        models = [m for m in models if m]
    return {
        "recipe_id": str(recipe.get("id") or ""),
        "port": port,
        "backend_type": str(
            backend_cfg.get("backend_type") or inference.get("engine") or ""
        ),
        "models": models,
    }


def _backend_registration(
    serving: dict[str, Any], mesh_ip: str, access_token: str
) -> dict[str, Any]:
    """Keyword arguments for node_register_backend after a mesh join.

    The backend is reachable on the node's overlay IP at the recipe's port; the
    caller's own bearer authenticates the register (Genesis authorizes it).
    """
    return {
        "genesis_url": os.getenv("AITHER_GENESIS_URL", _DEFAULT_GENESIS_URL),
        "base_url": f"http://{mesh_ip}:{serving['port']}",
        "backend_type": serving["backend_type"],
        "models": list(serving["models"]),
        "token": access_token,
    }


async def join_mesh(
    github: bool = True,
    cloud_provider: str | None = None,
    model: str | None = None,
    no_browser: bool = False,
    dry_run: bool = False,
) -> int:
    """One-command community node onboarding orchestrator.

    Chain order:
      1. GitHub device flow (print URL + code, poll to completion)
      2. node_detect_hardware
      3. node_resolve_recipe
      4. node_apply (serve)
      5. node_verify
      6. rich_enroll
      7. Obtain mesh key (call NEW endpoint)
      8. mesh.join
      9. Register with AitherMesh
      10. node_register_backend
      11. Print success summary

    Args:
        github: Use GitHub device flow (default: True)
        cloud_provider: Cloud provider for remote deployment (deferred to P2)
        model: Override resolved model
        no_browser: Do not attempt browser open for GitHub auth
        dry_run: Walk the plan without side effects

    Returns:
        0 on success, non-zero on failure
    """
    from adk._tls import tls_verify

    # Resolve service URLs.
    #
    # These defaults MUST be the public edge. `adk join` is the one command a
    # stranger runs on their own GPU box, and identity defaulted to
    # https://localhost:8115 — a fleet-INTERNAL address. On any machine that is not
    # running AitherOS locally, step 1 (the GitHub device flow) hit a refused
    # connection, and so did node registration and the mesh-key issue. The command
    # could only ever have worked for us. The conductor default was already public,
    # which is exactly why this went unnoticed.
    #
    # https://idp.aitherium.com is the public IdP and serves the device-flow routes
    # (POST /auth/github/device/start|poll and /auth/device/code|token) — verified
    # live 2026-07-24. Both remain overridable for on-fleet or self-hosted use.
    identity_url = os.getenv(
        "AITHER_IDENTITY_URL", "https://idp.aitherium.com"
    )
    # conductor.aitherium.com — gateway.aitherium.com is genesis + the MCP gateway
    # and answers 404 for /v1/mesh/onboard (measured 2026-09-02).
    conductor_url = os.getenv(
        "AITHER_CONDUCTOR_URL", "https://conductor.aitherium.com"
    )

    try:
        if dry_run:
            print("    [DRY RUN] Planned steps:")
            print("      1. GitHub device flow auth")
            print("      2. Detect hardware (CPU, RAM, GPU)")
            print("      3. Resolve inference recipe")
            print("      4. Apply recipe (serve)")
            print("      5. Verify deployment")
            print("      6. Register node with platform")
            print("      7. Obtain mesh overlay key")
            print("      8. Join mesh overlay")
            print("      9. Register backend for routing")
            print("      10. Print success summary + earnings")
            print()
            return 0

        # ─ GitHub Authentication
        print("    [1/10] GitHub authentication...")
        auth = await _github_device_flow_login(identity_url)
        access_token = auth.get("access_token", "")
        tenant_id = auth.get("tenant_id", "")

        if not access_token or not tenant_id:
            print("    [x] GitHub auth returned no token or tenant_id")
            return 1

        # ─ Hardware Detection
        print("    [2/10] Detecting hardware...")
        try:
            from adk.toolpacks.node_bootstrap.tools import node_detect_hardware
            hw_result = node_detect_hardware(verbose=False)
            if hw_result.get("error"):
                print(f"    [x] Hardware detection failed: {hw_result.get('error')}")
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Hardware detection failed: {e}")
            return 1

        # ─ Resolve Recipe
        # The node_bootstrap tools take keywords that differ from what this
        # chain once passed: node_resolve_recipe(prefer_backend, recipe_id),
        # node_apply(recipe_id, ...), node_verify(base_url, ...). The old calls
        # (a hardware dict as prefer_backend, model_override=, use_docker=, an
        # apply dict as base_url) raised TypeError or posted garbage, and the
        # broad excepts below printed it as an ordinary step failure, so
        # `adk join` never got past step 3. Every call here is by keyword and
        # tests/test_join_call_shapes.py binds each one to the real signature.
        print("    [3/10] Resolving inference recipe...")
        try:
            from adk.toolpacks.node_bootstrap.tools import node_resolve_recipe
            recipe_result = node_resolve_recipe(prefer_backend="auto")
            if recipe_result.get("error"):
                print(
                    f"    [x] Recipe resolution failed: "
                    f"{recipe_result.get('error')}"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Recipe resolution failed: {e}")
            return 1

        serving = _serving_spec(recipe_result.get("recipe") or {}, model)
        if not serving["recipe_id"] or not serving["port"]:
            print(
                "    [x] Resolved recipe serves nothing locally "
                f"({serving['recipe_id'] or 'no recipe'}): nothing to share"
            )
            return 1

        # ─ Apply Recipe (Serve)
        print("    [4/10] Applying recipe (serving)...")
        try:
            from adk.toolpacks.node_bootstrap.tools import node_apply
            apply_result = node_apply(recipe_id=serving["recipe_id"], dry_run=False)
            if apply_result.get("error"):
                print(
                    f"    [x] Recipe application failed: "
                    f"{apply_result.get('error')}"
                )
                return 1
            if not apply_result.get("applied"):
                # A delegate recipe is reported, not run: no engine started.
                print(
                    "    [x] Recipe was not applied: "
                    f"{apply_result.get('fix') or apply_result.get('mode', 'unknown')}"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Recipe application failed: {e}")
            return 1

        # ─ Verify Deployment
        print("    [5/10] Verifying deployment...")
        try:
            from adk.toolpacks.node_bootstrap.tools import node_verify
            verify_result = node_verify(
                base_url=f"http://localhost:{serving['port']}",
                backend_type=serving["backend_type"],
                model=serving["models"][0] if serving["models"] else "",
                recipe_id=serving["recipe_id"],
            )
            if verify_result.get("error"):
                print(
                    f"    [x] Verification failed: "
                    f"{verify_result.get('error')}"
                )
                return 1
            if verify_result.get("status") != "healthy":
                print(
                    "    [x] Verification failed: backend is "
                    f"{verify_result.get('status', 'unknown')} (health="
                    f"{verify_result.get('health')}, coherent="
                    f"{verify_result.get('coherent')})"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Verification failed: {e}")
            return 1

        # ─ Register Node with Platform (rich_enroll)
        print("    [6/10] Registering node with platform...")
        try:
            from adk.enrollment import rich_enroll
            from adk.fleet_enroll import _generate_node_id
            # rich_enroll(base_url, token, node_id) — this call passed NOTHING
            # until 2026-09-01, so step 6 raised TypeError on every run, was
            # swallowed by the broad except below, and printed as "Node
            # registration failed". `adk join` could never complete.
            local_node_id = os.getenv("AITHER_NODE_NAME", "") or _generate_node_id()
            enroll_result = await rich_enroll(identity_url, access_token, local_node_id)
            if enroll_result.get("error"):
                print(
                    f"    [x] Node registration failed: "
                    f"{enroll_result.get('error')}"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Node registration failed: {e}")
            return 1

        node_id = enroll_result.get("node_id", "") or local_node_id
        if not node_id:
            print("    [x] No node_id returned from enrollment")
            return 1

        # ─ Obtain Mesh Key
        print("    [7/10] Obtaining mesh overlay key...")
        try:
            mesh_key = await _obtain_mesh_key(identity_url, access_token)
        except RuntimeError as e:
            print(f"    [x] Mesh key issuance failed: {e}")
            return 1

        # ─ Mesh Overlay Join
        print("    [8/10] Joining mesh overlay...")
        try:
            from adk.mesh import join
            mesh_result = await join(
                conductor_url=conductor_url,
                node_id=node_id,
                role="worker",
                headscale=True,
                headscale_auth_key=mesh_key,
                # The conductor wants the node's OWN capability token (endpoint:mesh,
                # minted at register) and the tenant it belongs to (2026-09-02).
                psk=enroll_result.get("bearer_token", "") or None,
                tenant_id=tenant_id,
            )
            if mesh_result.get("error"):
                print(
                    f"    [x] Mesh join failed: {mesh_result.get('error')}"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Mesh join failed: {e}")
            return 1

        mesh_ip = mesh_result.get("overlay_ip", "")
        if not mesh_ip:
            print(f"    [x] No overlay_ip returned from mesh join")
            return 1

        # ─ Register Backend for Routing
        print("    [9/10] Registering backend for routing...")
        try:
            from adk.toolpacks.node_bootstrap.tools import node_register_backend
            # node_register_backend(genesis_url, base_url, backend_type,
            # models, preferred, token, name). This passed (node_id, mesh_ip,
            # recipe_result) positionally until 2026-10-19: the node id became
            # the Genesis URL, the overlay IP the backend URL, and the recipe
            # dict the backend type, with no token, so step 9 could never
            # register anything.
            backend_result = node_register_backend(
                **_backend_registration(serving, mesh_ip, access_token)
            )
            if backend_result.get("error"):
                print(
                    f"    [x] Backend registration failed: "
                    f"{backend_result.get('error')}"
                )
                return 1
        except Exception as e:  # noqa: BLE001
            print(f"    [x] Backend registration failed: {e}")
            return 1

        # ─ Success Summary
        print("    [10/10] Finalizing...")
        _print_success_summary(node_id, mesh_ip, serving["models"], tenant_id)
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"    [x] Onboarding failed: {e}")
        logger.exception("join_mesh failed with exception")
        return 1


def cmd_join(args: Any) -> int:
    """CLI handler for `adk join`."""
    github = not getattr(args, "no_github", False)
    cloud_provider = getattr(args, "cloud_provider", None)
    model = getattr(args, "model", None)
    no_browser = getattr(args, "no_browser", False)
    dry_run = getattr(args, "dry_run", False)

    try:
        result = asyncio.run(
            join_mesh(
                github=github,
                cloud_provider=cloud_provider,
                model=model,
                no_browser=no_browser,
                dry_run=dry_run,
            )
        )
        return result
    except KeyboardInterrupt:
        print("\n    Interrupted by user")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"    [x] Unexpected error: {e}")
        logger.exception("cmd_join failed")
        return 1
