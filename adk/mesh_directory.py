"""mesh_directory -- how ``adk mesh ls`` asks for the node directory.

Measured 2026-11-12, twice:

1. ``adk mesh ls`` answered ``401 Unauthorized``. Its default directory is
   ``https://gateway.aitherium.com`` and it asked for ``/nodes`` with only the mesh
   PSK header: no login session was sent, so the edge said "Missing bearer token".
2. With the login session sent the same host answers 404 for ``/nodes`` and for
   ``/v1/mesh/nodes``. ``gateway.aitherium.com`` is the MCP gateway
   (``config/cloudflare/tunnel-ingress.yaml`` -> ``aitheros-mcpgateway:8182``); it
   has no peer-directory route at all. The peer directory (mesh-core ``/nodes``) is
   reachable only over the overlay.

So there are two directories, told apart by host:

* the public default -> the ACCOUNT's node registry, Identity ``GET /v1/nodes`` (what
  ``adk devices list`` reads), with the login session the rest of the kit uses
  (:func:`adk.mcp_setup.auth_candidates`, the live ``adk login`` session first).
  Stored credentials are tried in turn only because the host is Aitherium's own
  (:func:`adk.mcp_setup.may_try_every_credential`);
* anything else (mesh-core on the overlay, ``--mesh-url https://<ip>:8125``) ->
  ``/nodes`` with the mesh PSK, as before. A person's bearer is never sent there.

Every answer says which registry it read (``registry``). Pure planning plus one
fetch helper; the CLI prints.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

#: Hosts that are the public MCP gateway: no peer directory behind them.
PUBLIC_GATEWAY_HOSTS = ("gateway.aitherium.com",)
ACCOUNT_PATH = "/v1/nodes"
OVERLAY_PATH = "/nodes"
ACCOUNT_REGISTRY = "account devices (Identity /v1/nodes; same list as `adk devices list`)"
OVERLAY_REGISTRY = "mesh peer directory (mesh-core /nodes over the overlay)"


def is_public_gateway(mesh_url: str) -> bool:
    try:
        host = (urlparse(mesh_url).hostname or "").lower()
    except ValueError:
        return False
    return host in PUBLIC_GATEWAY_HOSTS


def _identity_base() -> str:
    from adk.fleet_enroll import enroll_base_url

    return enroll_base_url()


def plan(mesh_url: str, psk_headers: Dict[str, str],
         candidates: Optional[List[Tuple[str, str]]] = None,
         identity_base: Optional[str] = None) -> Dict[str, Any]:
    """What to send: ``{url, kind, registry, attempts: [(headers, source), ...]}``.

    ``attempts`` is empty for the account registry when this machine holds no login
    credential -- the caller says "run adk login" instead of sending nothing and
    printing the server's 401."""
    base = mesh_url.rstrip("/")
    if not is_public_gateway(base):
        return {"url": base + OVERLAY_PATH, "kind": "overlay", "registry": OVERLAY_REGISTRY,
                "attempts": [(dict(psk_headers), "mesh PSK")]}
    idp = (identity_base or _identity_base()).rstrip("/")
    if candidates is None:
        from adk.mcp_setup import auth_candidates, may_try_every_credential

        candidates = auth_candidates()
        if not may_try_every_credential(idp):
            candidates = candidates[:1]
    attempts = [({"Accept": "application/json", "Authorization": "Bearer %s" % tok}, source)
                for tok, source in candidates]
    return {"url": idp + ACCOUNT_PATH, "kind": "account", "registry": ACCOUNT_REGISTRY,
            "attempts": attempts}


def explain(status: int, kind: str, tried: List[str]) -> str:
    """One line for a refused directory request: what was sent and what to do."""
    sent = ", ".join(tried) or "no credential"
    if status in (401, 403) and kind == "account":
        return ("the node registry refused the request (HTTP %d) with: %s. Sign in "
                "again with `adk login`, then re-run `adk mesh ls`." % (status, sent))
    if status in (401, 403):
        return ("the mesh directory refused the request (HTTP %d) with: %s. It expects "
                "the mesh PSK (AITHER_MESH_PSK); a machine that is not on the overlay "
                "should use the default directory instead (drop --mesh-url)."
                % (status, sent))
    return "the node directory answered HTTP %d" % status


def _rows(body: Any, kind: str) -> List[Dict[str, Any]]:
    """Directory rows in the shape ``adk mesh ls`` prints (node_id, overlay_ip, role,
    services). Account rows carry other field names; they are mapped, not dropped."""
    if isinstance(body, dict):
        body = body.get("endpoints") if kind == "account" else body.get("nodes")
    if not isinstance(body, list):
        return []
    rows = [r for r in body if isinstance(r, dict)]
    if kind != "account":
        return rows
    out = []
    for r in rows:
        row = dict(r)
        row.setdefault("overlay_ip", r.get("overlay_ip") or r.get("mesh_ip") or "?")
        row.setdefault("role", r.get("node_class") or r.get("status") or "?")
        if not isinstance(row.get("services"), list):
            kind_ = r.get("inference_kind")
            row["services"] = [str(kind_)] if kind_ and kind_ != "none" else []
        out.append(row)
    return out


async def fetch(mesh_url: str, psk_headers: Dict[str, str], get: Callable[..., Any],
                candidates: Optional[List[Tuple[str, str]]] = None,
                identity_base: Optional[str] = None) -> Dict[str, Any]:
    """Ask the directory. ``get(url, headers)`` is an awaitable returning an object
    with ``status_code``, ``headers`` and ``json()`` (httpx's response).

    Returns ``{ok, nodes?, error?, url, registry, auth?}``. A 401/403 moves to the
    next stored credential; any other status ends the attempt."""
    p = plan(mesh_url, psk_headers, candidates, identity_base)
    base = {"url": p["url"], "registry": p["registry"]}
    if not p["attempts"]:
        return {"ok": False, **base,
                "error": "not signed in: the node registry at %s needs your login "
                         "session. Run `adk login`, then `adk mesh ls`." % p["url"]}
    tried: List[str] = []
    status = 0
    for headers, source in p["attempts"]:
        resp = await get(p["url"], headers)
        status = int(resp.status_code)
        tried.append(source)
        if status in (401, 403):
            continue
        if status != 200:
            return {"ok": False, **base, "error": explain(status, p["kind"], tried)}
        ctype = str(resp.headers.get("content-type", ""))
        body = resp.json() if ctype.startswith("application/json") else []
        return {"ok": True, **base, "auth": source, "nodes": _rows(body, p["kind"])}
    return {"ok": False, **base, "error": explain(status, p["kind"], tried)}
