"""Node-initiated pairing — `adk pair <CODE>`.

(Not adk/pairing.py — that module links CHAT identities across Telegram/Discord;
this one pairs THIS MACHINE with the portal as an inference node.)

The other half of AitherVeil's LOCAL_NODE_PAIRING_DESIGN.md (portal + identity
sides landed 2026-08-07). Safari and Firefox block HTTPS→loopback outright, so
the browser can NEVER auto-detect a local node there; and `adk enroll` requires
being signed in on THIS machine first, which is exactly the friction pairing
removes. The flow:

    portal (signed-in browser)  →  shows a 6-character code
    this machine                →  adk pair A7F2E9
    identity                    →  registers this node under the CODE's tenant

The code IS the credential (5-minute TTL, minted by an authenticated user,
tenant-capped) — so unlike `adk enroll` this command deliberately does NOT
require `adk login` here. The response is the standard registration response:
mTLS bundle, capability token, tunnel URL — persisted exactly where enroll
persists them, so everything downstream (heartbeats, gateway routing, the
portal's node list) cannot tell the two enrollment paths apart.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

log = logging.getLogger("adk.node_pairing")

_CONFIRM_PATH = "/api/node-pairing/confirm"
#: Identity's own confirm route. The default since 2026-10-03: the portal proxy above
#: answered an anonymous confirm with 401 "Authentication required" (measured), so
#: `adk pair` with no --portal could never succeed.
IDENTITY_CONFIRM_PATH = "/v1/nodes/pairing/confirm"


async def pair_with_code(code: str, portal_url: str,
                         node_class: str = "laptop",
                         confirm_path: str = _CONFIRM_PATH) -> Dict[str, Any]:
    """Present a portal pairing code and register this machine as a node.

    Returns ``{"paired": bool, ...}`` — never raises; failures carry ``error``.
    """
    code = (code or "").strip().upper()
    if not code:
        return {"paired": False, "error": "no code given — run `adk pair <CODE>`"}

    try:
        import httpx

        from adk.enrollment import _persist_device_cert, _save_workspace, build_registration
        from adk.fleet_enroll import _generate_node_id, _load_node_auth, _save_node_auth

        node_auth = _load_node_auth()
        node_id = node_auth.get("node_id") or _generate_node_id()

        reg = build_registration(node_id, node_class=node_class)
        payload = {"code": code, **reg}

        base = portal_url.rstrip("/")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base}{confirm_path}", json=payload)

        if resp.status_code != 200:
            detail = resp.text[:200]
            # 400 is overwhelmingly "expired" in practice — say the useful thing.
            hint = " (codes live 5 minutes — mint a fresh one on the portal)" if resp.status_code == 400 else ""
            return {"paired": False, "error": f"HTTP {resp.status_code}: {detail}{hint}"}

        data = resp.json()
        tenant_id = data.get("tenant_id", "")

        # Persist EXACTLY what enroll persists, in the same places, so heartbeats
        # and every later `adk` command see a normally-enrolled node.
        _save_workspace(data.get("workspace", {}) or {})
        cert_result = _persist_device_cert(data)
        node_auth.update({
            "node_id": data.get("node_id", node_id),
            "tenant_id": tenant_id,
            "bearer_token": data.get("bearer_token", ""),
            "public_url": data.get("public_url", ""),
            "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "enrolled_via": "pairing-code",
            # Where the device beats with its own token (/v1/nodes/device/*).
            **({"enroll_base": base} if confirm_path == IDENTITY_CONFIRM_PATH else {}),
            "node_class": reg.get("node_class", node_class),
            "inference_url": reg.get("inference_url", ""),
        })
        _save_node_auth(node_auth)
        if data.get("command_key"):
            from adk import node_commands

            node_commands.save_key(node_auth["node_id"], str(data["command_key"]))

        return {
            "paired": True,
            "node_id": node_auth["node_id"],
            "tenant_id": tenant_id,
            "public_url": node_auth["public_url"],
            "cert_enrolled": cert_result.get("success", False),
        }
    except Exception as e:  # noqa: BLE001 — CLI surface; the message IS the product
        log.warning("Pairing failed: %s", e)
        return {"paired": False, "error": str(e)}


def cmd_pair(args: Any) -> int:
    """CLI entry: `adk pair <CODE> [--portal URL]`."""
    import asyncio
    import os

    portal_url = getattr(args, "portal", None) or os.environ.get("AITHER_PORTAL_URL", "")
    confirm_path = _CONFIRM_PATH
    if not portal_url:
        from adk.devices import enroll_base

        portal_url, confirm_path = enroll_base(), IDENTITY_CONFIRM_PATH
    code = getattr(args, "code", "")

    print(f"  Pairing this machine with {portal_url} …")
    result = asyncio.run(pair_with_code(
        code, portal_url, node_class=getattr(args, "node_class", None) or "laptop",
        confirm_path=confirm_path))

    if not result.get("paired"):
        print(f"  ✗ Pairing failed: {result.get('error', 'unknown error')}")
        return 1

    print(f"  ✓ Paired as node {result['node_id']} (tenant {result.get('tenant_id') or '?'})")
    if result.get("public_url"):
        print(f"    Reachable via {result['public_url']}")
    if not result.get("cert_enrolled"):
        print("    (device cert not issued — re-run `adk pair` with a fresh code to retry)")
    print("    The portal tab you minted the code in should flip to connected on its own.")
    return 0
