"""``adk operator`` -- ask your workspace's Aither Operator from a terminal.

The same operator, policy, approval cards and audit as the Operator tab in Aither
Control: every call goes to the portal's ``/api/operator/*`` under YOUR ``adk login``
session, and the server decides what your role may do. This module holds no policy of
its own and never acts on anything but the words you typed.

    adk operator ask "pause lending on the deck"
    adk operator approvals                  # cards waiting for you (owner / co-guardian)
    adk operator approve <card id> [--deny]
    adk operator audit [--limit 20]         # what the operator did and why
    adk operator policy                     # tiers per action, your role, added packs
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

__all__ = ["cmd_operator", "request_operator", "OperatorError"]

_TIMEOUT = 30.0


class OperatorError(RuntimeError):
    """The call did not complete (no session, no network)."""


def portal_base() -> str:
    """Where ``/api/operator`` lives: ``$AITHER_OPERATOR_URL``, else the control plane."""
    explicit = (os.environ.get("AITHER_OPERATOR_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    from adk.control_plane import control_plane_url

    return control_plane_url().rstrip("/")


def _send(method: str, url: str, headers: Dict[str, str], body: Optional[str],
          timeout: float) -> Tuple[int, str]:
    """One HTTP exchange -> ``(status, text)``. Tests monkeypatch this."""
    import httpx

    from adk._tls import tls_verify

    with httpx.Client(timeout=timeout, verify=tls_verify()) as client:
        resp = client.request(method, url, headers=headers, content=body)
    return resp.status_code, resp.text


def request_operator(method: str, path: str, payload: Optional[Dict[str, Any]] = None, *,
                     token: Optional[str] = None) -> Tuple[int, Any]:
    from adk.devices import resolve_bearer

    bearer = token if token is not None else resolve_bearer()
    if not bearer:
        raise OperatorError("Not signed in. Run `adk login` first.")
    url = f"{portal_base()}/api/operator/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json",
               "Content-Type": "application/json"}
    body = json.dumps(payload) if payload is not None else None
    try:
        status, text = _send(method, url, headers, body, _TIMEOUT)
    except Exception as e:  # transport errors have many types; name the url
        raise OperatorError(f"{method} {url} failed: {e}") from e
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    return status, parsed


def _refused(status: int, body: Any) -> int:
    detail = body.get("detail") or body.get("error") if isinstance(body, dict) else body
    print(f"x HTTP {status}: {detail or 'refused'}")
    return 1


def cmd_operator(args: Any) -> int:
    sub = getattr(args, "operator_command", None) or "policy"
    try:
        if sub == "ask":
            text = " ".join(getattr(args, "text", None) or []).strip()
            if not text:
                print("Say what to do, e.g. adk operator ask \"pause lending on the deck\"")
                return 2
            status, body = request_operator("POST", "request", {"text": text})
            if status != 200:
                return _refused(status, body)
            for d in (body or {}).get("decisions") or []:
                card = f"  (card {d['approval_id']})" if d.get("approval_id") else ""
                print(f"{d.get('status')}: {d.get('message')}{card}")
            return 0
        if sub == "approvals":
            status, body = request_operator("GET", "approvals")
            if status != 200:
                return _refused(status, body)
            cards = (body or {}).get("approvals") or []
            for c in cards:
                print(f"{c['id']}  {c['action']} {c['target']}  -- {c.get('reasoning', '')}")
            if not cards:
                print("Nothing is waiting for you.")
            return 0
        if sub == "approve":
            allow = not getattr(args, "deny", False)
            status, body = request_operator("POST", f"approvals/{args.card_id}",
                                            {"allow": allow})
            if status != 200:
                return _refused(status, body)
            d = (body or {}).get("decision") or {}
            print(f"{d.get('status')}: {d.get('message')}")
            return 0
        if sub == "audit":
            status, body = request_operator("GET", "audit")
            if status != 200:
                return _refused(status, body)
            rows = (body or {}).get("audit") or []
            for r in rows[: max(1, int(getattr(args, "limit", 20) or 20))]:
                print(f"{r['id']}  {r['decision']:<16} {r['action']} {r['target']}  "
                      f"-- {r['reasoning']}")
            return 0
        status, body = request_operator("GET", "policy")
        if status != 200:
            return _refused(status, body)
        if getattr(args, "json", False):
            print(json.dumps(body, indent=2))
            return 0
        print(f"role: {body.get('role')}  kind: {body.get('kind')}  "
              f"packs: {', '.join(body.get('packs') or []) or 'none'}")
        for action, tier in sorted(((body.get("policy") or {}).get("tiers") or {}).items()):
            print(f"  {tier:<8} {action}")
        return 0
    except OperatorError as e:
        print(f"x {e}")
        return 1


def add_parser(sub: Any) -> None:
    """Register ``adk operator`` on the top-level subparsers."""
    op = sub.add_parser("operator", help="Ask your workspace's Aither Operator "
                                         "(same policy and audit as Aither Control)")
    op_sub = op.add_subparsers(dest="operator_command")
    ask = op_sub.add_parser("ask", help="Ask the operator to do something")
    ask.add_argument("text", nargs="+", help="What to do, in your own words")
    op_sub.add_parser("approvals", help="Cards waiting for your answer")
    ap = op_sub.add_parser("approve", help="Answer a card")
    ap.add_argument("card_id")
    ap.add_argument("--deny", action="store_true", help="Deny instead of allow")
    au = op_sub.add_parser("audit", help="What the operator did and why")
    au.add_argument("--limit", type=int, default=20)
    po = op_sub.add_parser("policy", help="Tiers per action, your role, added packs")
    po.add_argument("--json", action="store_true")
