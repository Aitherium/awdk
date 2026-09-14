"""``adk approvals`` — decide the permission cards blocking federated agents.

When a peer agent is refused on the A2A drive plane it does not just 403: the
gateway raises an :class:`AccessRequest` permission card and hands back its id.
The card sits pending until a human decides, and approval mints a one-time
bearer (``a2ag_…``) that the requester replays as ``X-A2A-Grant``.

This is the terminal surface for that decision, alongside the portal tray and
the Awconnect popup. All three resolve the SAME cards from the same store,
so a card approved here disappears from the others.

Target resolution (``--url`` > ``$AITHER_A2A_URL`` > ``https://127.0.0.1:8766``).
The gateway gates these routes on the fleet internal key, so this command needs
``$AITHER_INTERNAL_SECRET`` — it is an operator tool, run where the gateway
runs. TLS goes through :mod:`adk._tls`, never ``verify=False``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("adk.commands.approvals")

DEFAULT_URL = "https://127.0.0.1:8766"
GRANT_HEADER = "X-A2A-Grant"


def _base_url(args: Any) -> str:
    url = (
        getattr(args, "url", "")
        or os.getenv("AITHER_A2A_URL", "")
        or DEFAULT_URL
    )
    return url.rstrip("/")


def _internal_key() -> str:
    return (os.getenv("AITHER_INTERNAL_SECRET", "") or "").strip()


def _client(timeout: float = 20.0):
    import httpx
    from adk._tls import tls_verify

    key = _internal_key()
    if not key:
        raise RuntimeError(
            "AITHER_INTERNAL_SECRET is not set — the gateway's access-request "
            "routes are owner/internal-gated. Run this where the gateway runs, "
            "or export the key for this shell."
        )
    return httpx.Client(
        timeout=timeout,
        verify=tls_verify(),
        headers={"X-Internal-Key": key, "Content-Type": "application/json"},
    )


def _fail(msg: str) -> int:
    print(f"  {msg}", file=sys.stderr)
    return 1


def _explain(resp: Any) -> str:
    """Turn an error response into something a human can act on."""
    try:
        detail = resp.json().get("detail")
    except Exception:  # noqa: BLE001 — body may not be JSON
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("error") or json.dumps(detail)
    return f"HTTP {resp.status_code}: {detail or resp.text[:200]}"


def _cmd_list(args: Any) -> int:
    import httpx

    url = f"{_base_url(args)}/access-requests"
    if getattr(args, "tenant", ""):
        url += f"?tenant_id={args.tenant}"
    try:
        with _client() as c:
            resp = c.get(url)
    except RuntimeError as e:
        return _fail(str(e))
    except httpx.HTTPError as e:
        return _fail(f"cannot reach the A2A gateway at {_base_url(args)}: {e}")

    if resp.status_code != 200:
        return _fail(_explain(resp))

    data = resp.json()
    requests = data.get("requests", [])
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    if not requests:
        print("  No pending access requests.")
        return 0

    print(f"  {len(requests)} pending access request(s):\n")
    for r in requests:
        print(f"  {r.get('request_id')}")
        print(f"    agent    {r.get('agent_id')}  (tenant {r.get('tenant_id')})")
        print(f"    wants    {r.get('requested_resource')} [{r.get('requested_action')}]")
        print(f"    because  {r.get('denial_reason') or '-'}")
        print()
    print("  adk approvals approve <id>   /   adk approvals deny <id>")
    return 0


def _decide(args: Any, decision: str) -> int:
    import httpx

    request_id = args.request_id
    url = f"{_base_url(args)}/access-requests/{request_id}/{decision}"
    body: dict[str, Any] = {
        "approver": getattr(args, "approver", "") or os.getenv("USER") or "owner",
    }
    if decision == "approve":
        body["approver_tenant_id"] = getattr(args, "tenant", "") or "platform"
        body["ttl_minutes"] = int(getattr(args, "ttl", 60) or 60)
        if getattr(args, "reason", ""):
            body["reason"] = args.reason
    elif getattr(args, "reason", ""):
        body["message"] = args.reason

    try:
        with _client() as c:
            resp = c.post(url, json=body)
    except RuntimeError as e:
        return _fail(str(e))
    except httpx.HTTPError as e:
        return _fail(f"cannot reach the A2A gateway at {_base_url(args)}: {e}")

    # 409 is a DECISION, not a fault: the card was already resolved, or this
    # approver lacks authority over that tenant. Say which — a bare "failed"
    # sends the operator looking for an outage that is not there.
    if resp.status_code != 200:
        return _fail(_explain(resp))

    data = resp.json()
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0

    if decision == "deny":
        print(f"  Denied {request_id}. No grant was minted.")
        return 0

    token = data.get("grant_token", "")
    print(f"  Approved {request_id}.")
    print(f"  TTL: {data.get('ttl_minutes')} minutes")
    print(f"  Scope: {', '.join(data.get('grant_capabilities') or [])}")
    print()
    print("  This token is shown ONCE. The requester must send it as:")
    print(f"    {data.get('present_as_header') or GRANT_HEADER}: {token}")
    return 0


def cmd_approvals(args: Any) -> int:
    action = getattr(args, "approvals_command", None) or "list"
    if action == "list":
        return _cmd_list(args)
    if action in ("approve", "deny"):
        return _decide(args, action)
    print("Usage: adk approvals [list|approve <id>|deny <id>]", file=sys.stderr)
    return 2
