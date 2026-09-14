"""Cloudflare pack — cf_* agent tools.

Design rules (same doctrine as other tool packs):
  * Pure HTTP (httpx). No wrangler, no cloudflare SDK, no browser OAuth.
  * Fail soft with actionable guidance — a missing token or an under-scoped
    token is a STATUS with a fix, never an exception into the agent loop.
  * The API token is resolved via config.api_token() and is never logged,
    echoed or returned; all outbound text goes through config.redact().

Two transports:

  1. DOCS — https://docs.mcp.cloudflare.com/mcp is a PUBLIC (no-auth) MCP
     server. It speaks JSON-RPC over HTTP with SSE-framed responses. adk has no
     working remote-MCP client, so cf_docs_search implements the handshake
     directly here: initialize -> tools/call search_cloudflare_documentation,
     then parse the ``data:`` frame out of the SSE body.

  2. ACCOUNT — the account MCP server (mcp.cloudflare.com) requires interactive
     OAuth, so every account operation uses the REST API v4 with a token:
       GET  /user/tokens/verify                        identity
       GET  /zones                                     zones
       GET  /zones/{z}/dns_records                     DNS
       GET  /accounts/{a}/workers/scripts              Workers
       GET  /zones/{z}/workers/routes                  routes
       PUT  /accounts/{a}/workers/scripts/{name}       deploy (ES-module multipart)
       GET  /accounts/{a}/cfd_tunnel                   tunnels
       POST /graphql                                   analytics
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from . import config

logger = logging.getLogger("cloudflare_pack")

_TIMEOUT = 20.0
_DOCS_TIMEOUT = 45.0
_MAX_DOC_CHARS = 12000

_DNS_TYPES = {"A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA", "PTR"}
_PROXYABLE = {"A", "AAAA", "CNAME"}

_SCOPE_HINTS = {
    "workers_scripts": "Account -> Workers Scripts -> Edit",
    "workers_routes": "Zone -> Workers Routes -> Edit",
    "dns": "Zone -> DNS -> Edit",
    "zone_read": "Zone -> Zone -> Read",
    "tunnels": "Account -> Cloudflare Tunnel -> Read (Edit to modify)",
    "analytics": "Zone -> Analytics -> Read",
}

_NOT_CONFIGURED = {
    "error": "cloudflare not configured — no API token",
    "fix": ("set CLOUDFLARE_API_TOKEN (or CF_API_TOKEN) in the environment, "
            "add it to ~/.aither/provider_keys.json, or store it in the "
            "AitherSecrets vault as CLOUDFLARE_API_TOKEN"),
    "note": ("create the token at "
             "https://dash.cloudflare.com/profile/api-tokens — the docs tool "
             "cf_docs_search needs no token and still works"),
}


# ── REST plumbing ───────────────────────────────────────────────────────


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _api(method: str, path: str, *, capability: str = "", token: str = "",
         **kwargs) -> dict:
    """One guarded Cloudflare v4 call.

    Returns {"ok": True, "result": ..., "result_info": ...} or a fail-soft
    {"ok": False, "error": ..., "fix": ...}. Never raises.
    """
    token = token or config.api_token()
    if not token:
        return {"ok": False, **_NOT_CONFIGURED}
    url = f"{config.api_base()}{path}"
    try:
        r = httpx.request(method, url, headers=_headers(token),
                          timeout=kwargs.pop("timeout", _TIMEOUT), **kwargs)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"cloudflare API unreachable: {config.redact(exc)}"}

    try:
        body = r.json()
    except ValueError:
        body = {}

    if r.status_code == 200 and isinstance(body, dict) and body.get("success"):
        return {"ok": True, "result": body.get("result"),
                "result_info": body.get("result_info") or {}}

    errors = body.get("errors") if isinstance(body, dict) else None
    detail = "; ".join(
        f"{e.get('code')}: {e.get('message')}"
        for e in (errors or []) if isinstance(e, dict)
    ) or config.redact(r.text[:300])

    out: dict = {"ok": False, "status": r.status_code,
                 "error": f"cloudflare API HTTP {r.status_code}",
                 "detail": config.redact(detail)}
    if _is_auth_error(r.status_code, detail):
        out["error"] = "permission denied — the API token lacks the required scope"
        out["missing_scope"] = _SCOPE_HINTS.get(capability, "see the endpoint's docs")
        out["fix"] = ("add the scope above at "
                      "https://dash.cloudflare.com/profile/api-tokens, then retry. "
                      "Call cf_whoami() to see exactly what this token CAN do.")
    return out


def _is_auth_error(status: int, detail: str) -> bool:
    d = (detail or "").lower()
    return (status in (401, 403)
            or "authentication error" in d
            or "not authorized" in d
            or "insufficient" in d)


def _zone(zone: str) -> tuple[str, dict | None]:
    """Resolve a zone: explicit id/name -> vaulted CLOUDFLARE_ZONE_ID."""
    z = (zone or "").strip()
    if not z:
        z = config.zone_id()
        if not z:
            return "", {"error": "no zone — pass zone=<id or name> or set CLOUDFLARE_ZONE_ID",
                        "hint": "cf_zones() lists the zones this token can see"}
        return z, None
    if _looks_like_id(z):
        return z, None
    # a name like "aitherium.com" — resolve it to an id
    res = _api("GET", "/zones", capability="zone_read", params={"name": z, "per_page": 1})
    if not res.get("ok"):
        return "", res
    hits = res.get("result") or []
    if not hits:
        return "", {"error": f"zone not found: {z}",
                    "hint": "cf_zones() lists the zones this token can see"}
    return str(hits[0].get("id") or ""), None


def _looks_like_id(s: str) -> bool:
    return len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower())


def _account() -> tuple[str, dict | None]:
    a = config.account_id()
    if not a:
        return "", {"error": "no account id — set CLOUDFLARE_ACCOUNT_ID "
                             "(or CF_ACCOUNT_ID), or store it in the vault"}
    return a, None


# ── 1. DOCS (keyless, public MCP server) ────────────────────────────────


def _parse_sse(text: str) -> dict:
    """Pull the JSON-RPC payload out of an SSE-framed body (``data: {...}``).
    Plain JSON bodies are accepted too."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                return json.loads(chunk)
            except ValueError:
                continue
    try:
        return json.loads(text)
    except ValueError:
        return {}


def cf_docs_search(query: str) -> dict:
    """Search the OFFICIAL Cloudflare documentation (live, no credentials).

    ALWAYS use this before writing wrangler commands, wrangler.toml/jsonc,
    Workers/Pages code, bindings, or DNS/Zero-Trust config. Cloudflare changes
    its flags and APIs often — your memory of them is stale, these docs are not.

    query: a natural-language question, e.g. 'wrangler routes deploy',
           'Durable Object bindings in wrangler.jsonc', 'Workers KV limits'

    Returns the documentation excerpts with their source URLs. Cite the URLs.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is required, e.g. 'how do I configure wrangler routes'"}

    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "aither-cloudflare-pack",
                                      "version": "1.0.0"}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "search_cloudflare_documentation",
                       "arguments": {"query": q}}}
    try:
        with httpx.Client(timeout=_DOCS_TIMEOUT, follow_redirects=True) as client:
            r = client.post(config.DOCS_MCP_URL, headers=headers, json=init)
            if r.status_code >= 400:
                return {"error": f"cloudflare docs MCP HTTP {r.status_code} on initialize",
                        "detail": config.redact(r.text[:200]),
                        "hint": "the docs server is public; this is a transport/network problem"}
            # the server MAY hand back a session id — echo it if so
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
            if sid:
                headers["Mcp-Session-Id"] = sid
                client.post(config.DOCS_MCP_URL, headers=headers,
                            json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            r = client.post(config.DOCS_MCP_URL, headers=headers, json=call)
            if r.status_code >= 400:
                return {"error": f"cloudflare docs MCP HTTP {r.status_code} on tools/call",
                        "detail": config.redact(r.text[:200])}
            payload = _parse_sse(r.text)
    except httpx.HTTPError as exc:
        return {"error": f"cloudflare docs MCP unreachable: {config.redact(exc)}",
                "hint": "no credential is needed for docs — this is a network problem"}

    if not payload:
        return {"error": "cloudflare docs MCP returned an unparseable body"}
    if "error" in payload:
        err = payload["error"]
        return {"error": "cloudflare docs MCP error",
                "detail": config.redact(err.get("message") if isinstance(err, dict) else err)}

    result = payload.get("result") or {}
    chunks = [c.get("text", "") for c in (result.get("content") or [])
              if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n\n".join(t for t in chunks if t).strip()
    # Cloudflare's own docs MCP emits the origin TWICE in every <url> it returns
    # ("https://developers.cloudflare.com/https://developers.cloudflare.com/workers/...").
    # Their bug, but the agent is told to cite these, so a doubled URL becomes a dead
    # link in our output. Collapse the duplicate on the way through.
    text = text.replace("https://developers.cloudflare.com/https://developers.cloudflare.com/",
                        "https://developers.cloudflare.com/")
    if not text:
        return {"query": q, "results": "",
                "note": "no documentation matched — try different wording"}
    truncated = len(text) > _MAX_DOC_CHARS
    return {"query": q,
            "source": "docs.mcp.cloudflare.com (official Cloudflare docs MCP)",
            "truncated": truncated,
            "results": text[:_MAX_DOC_CHARS],
            "hint": "cite the developers.cloudflare.com URLs above in your answer"}


# ── 2. IDENTITY + CAPABILITY ────────────────────────────────────────────


def cf_config() -> dict:
    """Show the pack's effective Cloudflare configuration (token masked)."""
    tok = config.api_token()
    return {
        "api_base": config.api_base(),
        "api_token": config.mask_key(tok) or "(unset)",
        "account_id": config.mask_id(config.account_id()) or "(unset)",
        "zone_id": config.mask_id(config.zone_id()) or "(unset)",
        "docs_mcp": config.DOCS_MCP_URL + " (public, no auth)",
        "how_to_set": ("env CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / "
                       "CLOUDFLARE_ZONE_ID, ~/.aither/provider_keys.json, or the "
                       "AitherSecrets vault (same names)"),
    }


def _probe(label: str, method: str, path: str, capability: str,
           **kwargs) -> dict:
    res = _api(method, path, capability=capability, **kwargs)
    if res.get("ok"):
        return {"capability": label, "allowed": True}
    out = {"capability": label, "allowed": False,
           "reason": res.get("error", "failed")}
    if res.get("missing_scope"):
        out["add_scope"] = res["missing_scope"]
    return out


def cf_whoami() -> dict:
    """Verify the Cloudflare API token and PROBE what it can actually do.

    Call this the moment any cf_* write fails. Cloudflare's verify endpoint only
    says the token is *valid*, not what it is scoped for — so this also makes one
    real read against zones, DNS, Workers scripts, Worker routes and tunnels and
    reports, per capability, allowed=true/false plus the exact scope to add.

    Read-only tokens are common and deliberate: if this says a write scope is
    missing, tell the operator which scope to add. Do NOT retry the write.
    """
    tok = config.api_token()
    if not tok:
        return dict(_NOT_CONFIGURED)

    verify = _api("GET", "/user/tokens/verify", capability="zone_read", token=tok)
    if not verify.get("ok"):
        return {"token": config.mask_key(tok), "valid": False,
                "error": verify.get("error"), "detail": verify.get("detail"),
                "fix": "the token is invalid or revoked — mint a new one at "
                       "https://dash.cloudflare.com/profile/api-tokens"}

    info = verify.get("result") or {}
    zone = config.zone_id()
    acct = config.account_id()

    caps = [_probe("zones:read", "GET", "/zones", "zone_read",
                   params={"per_page": 1})]
    if zone:
        caps.append(_probe("dns:read", "GET", f"/zones/{zone}/dns_records", "dns",
                           params={"per_page": 1}))
        caps.append(_probe("workers_routes:read", "GET",
                           f"/zones/{zone}/workers/routes", "workers_routes"))
        caps.append(_probe("analytics:read", "GET",
                           f"/zones/{zone}/analytics/dashboard", "analytics",
                           params={"since": "-60"}))
    if acct:
        caps.append(_probe("workers_scripts:read", "GET",
                           f"/accounts/{acct}/workers/scripts", "workers_scripts"))
        caps.append(_probe("tunnels:read", "GET", f"/accounts/{acct}/cfd_tunnel",
                           "tunnels", params={"per_page": 1}))

    allowed = [c["capability"] for c in caps if c["allowed"]]
    denied = [c for c in caps if not c["allowed"]]
    read_only = not any(c.startswith(("workers_scripts", "dns")) for c in allowed)

    out = {
        "token": config.mask_key(tok),
        "valid": True,
        "token_status": info.get("status", "unknown"),
        "expires_on": info.get("expires_on"),
        "account_id": config.mask_id(acct) or "(unset)",
        "zone_id": config.mask_id(zone) or "(unset)",
        "allowed": allowed,
        "denied": [{"capability": c["capability"],
                    "add_scope": c.get("add_scope", "?")} for c in denied],
    }
    if denied:
        out["verdict"] = (
            "This token is UNDER-SCOPED for: "
            + ", ".join(c["capability"] for c in denied)
            + ". Any write there WILL fail — do not retry it. Tell the operator "
              "to add these scopes at https://dash.cloudflare.com/profile/api-tokens: "
            + "; ".join(sorted({c.get("add_scope", "?") for c in denied}))
        )
    elif read_only:
        out["verdict"] = ("token reads fine; write scopes were not exercised — "
                          "a write may still fail with 'Authentication error'")
    else:
        out["verdict"] = "token can read every probed capability"
    if not zone:
        out.setdefault("notes", []).append("no zone id — zone probes skipped")
    if not acct:
        out.setdefault("notes", []).append("no account id — account probes skipped")
    return out


# ── 3. ZONES + DNS ──────────────────────────────────────────────────────


def cf_zones() -> dict:
    """List the Cloudflare zones (domains) this API token can see."""
    res = _api("GET", "/zones", capability="zone_read", params={"per_page": 50})
    if not res.get("ok"):
        return res
    zones = [
        {"id": config.mask_id(str(z.get("id") or "")),
         "name": z.get("name"),
         "status": z.get("status"),
         "plan": (z.get("plan") or {}).get("name"),
         "name_servers": z.get("name_servers") or []}
        for z in (res.get("result") or []) if isinstance(z, dict)
    ]
    return {"count": len(zones), "zones": zones,
            "note": "pass a zone NAME (e.g. example.com) to the other tools — "
                    "ids are masked here on purpose"}


def cf_dns_list(zone: str = "") -> dict:
    """List DNS records for a zone.

    zone: zone id or name (default: the configured CLOUDFLARE_ZONE_ID)
    """
    zid, err = _zone(zone)
    if err:
        return err
    res = _api("GET", f"/zones/{zid}/dns_records", capability="dns",
               params={"per_page": 100})
    if not res.get("ok"):
        return res
    recs = [
        {"id": r.get("id"), "type": r.get("type"), "name": r.get("name"),
         "content": r.get("content"), "proxied": r.get("proxied"),
         "ttl": r.get("ttl")}
        for r in (res.get("result") or []) if isinstance(r, dict)
    ]
    return {"zone": config.mask_id(zid), "count": len(recs), "records": recs}


def _as_bool(v, default: bool = True) -> bool:
    """Parse a boolean that may arrive as a real bool, a string, or a number.

    Tool arguments cross a JSON boundary where a schema may declare a string, and
    `bool("false")` is True. Anything not recognised falls back to `default` rather
    than being silently coerced, so a typo cannot flip a routing decision.
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off", ""):
        return False
    return default

def cf_dns_upsert(name: str, type: str, content: str, proxied: bool = True,
                  zone: str = "") -> dict:  # noqa: A002 — 'type' is the CF field name
    """Create a DNS record, or update it in place if name+type already exists.

    name: full record name, e.g. 'api.example.com'
    type: A | AAAA | CNAME | TXT | MX | NS | SRV | CAA | PTR
    content: the record value (IP for A, target host for CNAME, text for TXT)
    proxied: route through Cloudflare's proxy (only A/AAAA/CNAME; forced off otherwise)
    zone: zone id or name (default: the configured CLOUDFLARE_ZONE_ID)

    Destructive-ish: this OVERWRITES an existing record's content. Run
    cf_dns_list first and tell the operator what you are about to change.
    """
    rec_name = (name or "").strip().rstrip(".")
    rec_type = (type or "").strip().upper()
    value = (content or "").strip()
    if not rec_name:
        return {"error": "name is required, e.g. 'api.example.com'"}
    if rec_type not in _DNS_TYPES:
        return {"error": f"unsupported type {rec_type or '(empty)'}",
                "supported": sorted(_DNS_TYPES)}
    if not value:
        return {"error": "content is required (the record value)"}
    zid, err = _zone(zone)
    if err:
        return err

    # 🚨 `proxied` ARRIVES AS A STRING over MCP, and `bool("false")` is True.
    # The signature says `proxied: bool = True`, but the tool schema declares it a
    # string, so every value a caller would reach for does the OPPOSITE of what it
    # says: measured 2026-08-20 against the live zone, "false" -> proxied=True and
    # "0" -> proxied=True, while only "" produced False. This decides whether a
    # hostname is fronted by Cloudflare or answers with the ORIGIN's own address, so
    # the empty-string case silently exposes an origin, and the caller is told
    # nothing -- the API returns 200 and the record shows the value it chose, not
    # the one that was asked for.
    proxied = _as_bool(proxied)

    if rec_type not in _PROXYABLE:
        proxied = False

    existing = _api("GET", f"/zones/{zid}/dns_records", capability="dns",
                    params={"name": rec_name, "type": rec_type, "per_page": 1})
    if not existing.get("ok"):
        return existing
    hits = existing.get("result") or []
    payload = {"type": rec_type, "name": rec_name, "content": value,
               "proxied": bool(proxied), "ttl": 1}

    if hits:
        rid = str(hits[0].get("id") or "")
        res = _api("PUT", f"/zones/{zid}/dns_records/{rid}", capability="dns",
                   json=payload)
        action = "updated"
    else:
        res = _api("POST", f"/zones/{zid}/dns_records", capability="dns",
                   json=payload)
        action = "created"
    if not res.get("ok"):
        return res
    r = res.get("result") or {}
    return {"action": action, "zone": config.mask_id(zid), "id": r.get("id"),
            "name": r.get("name"), "type": r.get("type"),
            "content": r.get("content"), "proxied": r.get("proxied")}


def cf_dns_delete(record: str, zone: str = "") -> dict:
    """DELETE a DNS record. Destructive — confirm with cf_dns_list first.

    record: the record id, or its full name (e.g. 'old.example.com'). A name is
            only accepted when it matches EXACTLY ONE record.
    zone: zone id or name (default: the configured CLOUDFLARE_ZONE_ID)
    """
    ref = (record or "").strip().rstrip(".")
    if not ref:
        return {"error": "record is required (record id or full record name)"}
    zid, err = _zone(zone)
    if err:
        return err

    rid = ref
    if not _looks_like_id(ref):
        found = _api("GET", f"/zones/{zid}/dns_records", capability="dns",
                     params={"name": ref, "per_page": 5})
        if not found.get("ok"):
            return found
        hits = found.get("result") or []
        if not hits:
            return {"error": f"no DNS record named {ref} in this zone"}
        if len(hits) > 1:
            return {"error": f"{len(hits)} records named {ref} — pass the record id",
                    "candidates": [{"id": h.get("id"), "type": h.get("type"),
                                    "content": h.get("content")} for h in hits]}
        rid = str(hits[0].get("id") or "")

    res = _api("DELETE", f"/zones/{zid}/dns_records/{rid}", capability="dns")
    if not res.get("ok"):
        return res
    return {"action": "deleted", "zone": config.mask_id(zid), "record": rid}


# ── 4. WORKERS + ROUTES ─────────────────────────────────────────────────


def cf_workers_list() -> dict:
    """List the Worker scripts deployed on the Cloudflare account."""
    acct, err = _account()
    if err:
        return err
    res = _api("GET", f"/accounts/{acct}/workers/scripts",
               capability="workers_scripts")
    if not res.get("ok"):
        return res
    scripts = [
        {"name": s.get("id"), "modified_on": s.get("modified_on"),
         "created_on": s.get("created_on"),
         "usage_model": s.get("usage_model"),
         "has_modules": bool(s.get("etag"))}
        for s in (res.get("result") or []) if isinstance(s, dict)
    ]
    return {"account": config.mask_id(acct), "count": len(scripts),
            "workers": scripts}


def cf_worker_routes(zone: str = "") -> dict:
    """List Worker routes for a zone — which Worker owns which hostname pattern.

    Check this BEFORE cf_worker_deploy: a route pattern can belong to only ONE
    Worker. Assigning a pattern another Worker already owns hard-fails the
    deploy, so report the current owner instead of blindly deploying.

    zone: zone id or name (default: the configured CLOUDFLARE_ZONE_ID)
    """
    zid, err = _zone(zone)
    if err:
        return err
    res = _api("GET", f"/zones/{zid}/workers/routes", capability="workers_routes")
    if not res.get("ok"):
        return res
    routes = [
        {"id": r.get("id"), "pattern": r.get("pattern"),
         "worker": r.get("script") or "(none — route disabled)"}
        for r in (res.get("result") or []) if isinstance(r, dict)
    ]
    return {"zone": config.mask_id(zid), "count": len(routes), "routes": routes,
            "note": "a pattern maps to exactly one worker; re-pointing it "
                    "requires updating the existing route, not adding a second"}


def cf_worker_deploy(name: str, script_path: str, routes: str = "") -> dict:
    """Upload a Worker script (ES module) and optionally assign it routes.

    name: the Worker script name, e.g. 'edge-router'
    script_path: path to a local .js/.mjs/.ts ES-module file (must `export default`)
    routes: optional comma-separated route patterns, e.g.
            'api.example.com/*,www.example.com/hook*'

    Guarded: validates the script locally, then checks every route's current
    owner (a pattern can belong to only ONE Worker) and REFUSES rather than
    stealing another Worker's route. On a permission failure it names the exact
    scope to add. Call cf_workers_list / cf_worker_routes first.
    """
    script_name = (name or "").strip()
    if not script_name or not all(c.isalnum() or c in "-_" for c in script_name):
        return {"error": "name is required and must be alphanumeric with - or _",
                "got": script_name or "(empty)"}
    p = Path((script_path or "").strip())
    if not p.is_file():
        return {"error": f"script not found: {script_path or '(empty)'}",
                "hint": "script_path must be a local ES-module file"}
    if p.suffix.lower() not in (".js", ".mjs", ".ts"):
        return {"error": f"unsupported script type {p.suffix or '(none)'} — "
                         "expected .js, .mjs or .ts"}
    try:
        source = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"script unreadable: {exc}"}
    if len(source.encode()) > 10_000_000:
        return {"error": "script too large (>10MB) — Workers reject it"}
    if "export default" not in source and "export {" not in source:
        return {"error": "script has no ES-module export — a module Worker must "
                         "`export default { fetch(request, env, ctx) { ... } }`",
                "hint": "cf_docs_search('workers module syntax export default fetch')"}

    acct, err = _account()
    if err:
        return err

    wanted = [r.strip() for r in (routes or "").split(",") if r.strip()]
    zid = ""
    if wanted:
        zid, zerr = _zone("")
        if zerr:
            return {"error": "routes were requested but no zone is configured",
                    "detail": zerr}
        current = cf_worker_routes(zid)
        if "routes" not in current:
            # FAIL CLOSED. This guard is the only thing standing between a deploy and
            # silently stealing another Worker's hostname — a route belongs to exactly
            # ONE Worker, so re-pointing it black-holes whatever was serving that traffic.
            # Workers-Routes:Read (zone) and Workers-Scripts:Edit (account) are SEPARATE
            # scopes, so a token can be perfectly able to upload while unable to see who
            # owns the routes. Skipping the check in that state is precisely when it
            # matters most. Refuse, and say why.
            return {"error": "cannot verify route ownership — refusing to assign routes",
                    "detail": current.get("error") or current,
                    "why": "a route belongs to exactly ONE Worker; assigning one blindly "
                           "would hijack another Worker's traffic",
                    "fix": "grant the token Zone -> Workers Routes -> Read (or Edit), then "
                           "retry. To upload the script WITHOUT touching routes, call "
                           "cf_worker_deploy(...) with routes=\"\"."}
        owned = {r["pattern"]: r["worker"] for r in current["routes"]}
        conflicts = [{"pattern": pat, "owned_by": owned[pat]}
                     for pat in wanted
                     if pat in owned and owned[pat] not in (script_name, "(none — route disabled)")]
        if conflicts:
            return {"error": "route conflict — a route belongs to only ONE Worker; "
                             "deploying would hijack another Worker's traffic",
                    "conflicts": conflicts,
                    "fix": "pick a different pattern, or ask the operator to "
                           "confirm re-pointing the route off "
                           + ", ".join(sorted({c["owned_by"] for c in conflicts}))}

    metadata = {"main_module": p.name,
                "compatibility_date": config.compatibility_date()}
    files = {
        "metadata": ("metadata.json", json.dumps(metadata), "application/json"),
        p.name: (p.name, source, "application/javascript+module"),
    }
    token = config.api_token()
    if not token:
        return dict(_NOT_CONFIGURED)
    url = f"{config.api_base()}/accounts/{acct}/workers/scripts/{script_name}"
    try:
        r = httpx.put(url, headers={"Authorization": f"Bearer {token}"},
                      files=files, timeout=90)
        try:
            body = r.json()
        except ValueError:
            body = {}
    except httpx.HTTPError as exc:
        return {"error": f"worker upload failed: {config.redact(exc)}"}

    if not (r.status_code == 200 and isinstance(body, dict) and body.get("success")):
        errs = "; ".join(f"{e.get('code')}: {e.get('message')}"
                         for e in (body.get("errors") or []) if isinstance(e, dict))
        detail = config.redact(errs or r.text[:300])
        out = {"error": f"worker upload HTTP {r.status_code}", "detail": detail,
               "worker": script_name}
        if _is_auth_error(r.status_code, detail):
            out["error"] = "permission denied — cannot upload Workers with this token"
            out["missing_scope"] = _SCOPE_HINTS["workers_scripts"]
            out["fix"] = ("add 'Account -> Workers Scripts -> Edit' at "
                          "https://dash.cloudflare.com/profile/api-tokens; "
                          "cf_whoami() shows what this token CAN do")
        return out

    deployed: dict = {"action": "deployed", "worker": script_name,
                      "main_module": p.name,
                      "compatibility_date": metadata["compatibility_date"],
                      "account": config.mask_id(acct)}
    if wanted and zid:
        assigned, failed = [], []
        for pat in wanted:
            res = _api("POST", f"/zones/{zid}/workers/routes",
                       capability="workers_routes",
                       json={"pattern": pat, "script": script_name})
            if res.get("ok"):
                assigned.append(pat)
            else:
                failed.append({"pattern": pat, "error": res.get("error"),
                               "detail": res.get("detail"),
                               "missing_scope": res.get("missing_scope")})
        deployed["routes_assigned"] = assigned
        if failed:
            deployed["routes_failed"] = failed
            deployed["note"] = ("the script uploaded but some routes did not "
                                "attach — the Worker is live only on its "
                                "*.workers.dev URL for those hostnames")
    return deployed


# ── 5. TUNNELS + ANALYTICS ──────────────────────────────────────────────


def cf_tunnel_list() -> dict:
    """List the account's cloudflared tunnels and their configured ingress.

    Ingress is the hostname -> local-service map a tunnel serves. Use it to see
    which tunnel already publishes a hostname before adding another.
    """
    acct, err = _account()
    if err:
        return err
    res = _api("GET", f"/accounts/{acct}/cfd_tunnel", capability="tunnels",
               params={"is_deleted": "false", "per_page": 50})
    if not res.get("ok"):
        return res
    tunnels = []
    for t in (res.get("result") or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        entry = {"id": config.mask_id(tid), "name": t.get("name"),
                 "status": t.get("status"),
                 "connections": len(t.get("connections") or [])}
        cfg = _api("GET", f"/accounts/{acct}/cfd_tunnel/{tid}/configurations",
                   capability="tunnels")
        if cfg.get("ok"):
            ing = ((cfg.get("result") or {}).get("config") or {}).get("ingress") or []
            entry["ingress"] = [
                {"hostname": i.get("hostname") or "(catch-all)",
                 "service": i.get("service"), "path": i.get("path")}
                for i in ing if isinstance(i, dict)
            ]
        else:
            entry["ingress"] = f"unavailable: {cfg.get('error')}"
        tunnels.append(entry)
    return {"account": config.mask_id(acct), "count": len(tunnels),
            "tunnels": tunnels,
            "note": "remotely-managed tunnels ignore local config files — the "
                    "ingress above is the source of truth"}


def cf_analytics(zone: str = "", hours: int = 24) -> dict:
    """Zone request/bandwidth/threat summary for the last N hours.

    zone: zone id or name (default: the configured CLOUDFLARE_ZONE_ID)
    hours: lookback window, 1-168 (default 24)

    Needs Zone -> Analytics -> Read on the token; fails soft if not granted.
    """
    zid, err = _zone(zone)
    if err:
        return err
    try:
        window = max(1, min(168, int(hours)))
    except (TypeError, ValueError):
        window = 24

    res = _api("GET", f"/zones/{zid}/analytics/dashboard", capability="analytics",
               params={"since": f"-{window * 60}", "until": "0"})
    if res.get("ok"):
        totals = (res.get("result") or {}).get("totals") or {}
        req = totals.get("requests") or {}
        bw = totals.get("bandwidth") or {}
        threats = totals.get("threats") or {}
        return {"zone": config.mask_id(zid), "window_hours": window,
                "requests": {"all": req.get("all"), "cached": req.get("cached"),
                             "uncached": req.get("uncached")},
                "bandwidth_bytes": {"all": bw.get("all"), "cached": bw.get("cached")},
                "threats": threats.get("all"),
                "source": "zone analytics dashboard"}

    # Free plans lose the dashboard endpoint — fall back to GraphQL.
    token = config.api_token()
    if not token:
        return dict(_NOT_CONFIGURED)
    gql = {
        "query": """query($zone: String!, $since: Time!) {
  viewer { zones(filter: {zoneTag: $zone}) {
    httpRequests1hGroups(limit: 168, filter: {datetime_geq: $since},
                         orderBy: [datetime_ASC]) {
      sum { requests bytes cachedRequests threats }
    } } } }""",
        "variables": {"zone": zid,
                      "since": _iso_hours_ago(window)},
    }
    try:
        r = httpx.post(config.GRAPHQL_URL, headers=_headers(token), json=gql,
                       timeout=_TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": f"analytics unavailable: {config.redact(exc)}",
                "dashboard_error": res.get("error"),
                "missing_scope": res.get("missing_scope") or _SCOPE_HINTS["analytics"]}

    if body.get("errors"):
        return {"error": "analytics denied on both dashboard and GraphQL",
                "detail": config.redact(str(body["errors"])[:200]),
                "missing_scope": _SCOPE_HINTS["analytics"],
                "fix": "add Zone -> Analytics -> Read to the token"}
    groups = (((body.get("data") or {}).get("viewer") or {}).get("zones") or [{}])
    rows = (groups[0] or {}).get("httpRequests1hGroups") or [] if groups else []
    agg = {"requests": 0, "bytes": 0, "cachedRequests": 0, "threats": 0}
    for row in rows:
        s = row.get("sum") or {}
        for k in agg:
            agg[k] += int(s.get(k) or 0)
    return {"zone": config.mask_id(zid), "window_hours": window,
            "requests": {"all": agg["requests"], "cached": agg["cachedRequests"]},
            "bandwidth_bytes": {"all": agg["bytes"]},
            "threats": agg["threats"],
            "source": "graphql httpRequests1hGroups (dashboard endpoint unavailable)"}


def _iso_hours_ago(hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:00:00Z")
