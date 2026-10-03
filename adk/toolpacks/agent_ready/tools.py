"""agent_ready_* tools: probe, scan and fix a website's agent readiness.

Standard library only (urllib), so the pack works in any adk install. Each
check returns ``{"status": "pass"|"fail"|"neutral", "evidence": ..., "fix": ...}``
and the probe never raises: a network error is a ``fail`` carrying the error.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_UA = "adk-agent-ready/1.0 (+https://github.com/Aitherium/awdk)"
_TIMEOUT = 15
_SCANNER = "https://isitagentready.com/api/scan"
_HERE = Path(__file__).resolve().parent


def _fetch(url: str, *, accept: Optional[str] = None, method: str = "GET",
           body: Optional[bytes] = None, timeout: int = _TIMEOUT) -> Tuple[int, Dict[str, str], bytes, str]:
    """Return (status, lower-cased headers, body, error). Never raises."""
    headers = {"User-Agent": _UA}
    if accept:
        headers["Accept"] = accept
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — caller-chosen public URL
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b"", ""
    except Exception as e:  # noqa: BLE001 — a dead site is a verdict, not a crash
        return 0, {}, b"", f"{type(e).__name__}: {e}"


def _base(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url.rstrip("/")


def _json(body: bytes) -> Optional[Any]:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _verdict(ok: bool, evidence: str, fix: str) -> Dict[str, str]:
    return {"status": "pass" if ok else "fail", "evidence": evidence, "fix": "" if ok else fix}


def _ev(status: int, headers: Dict[str, str], err: str) -> str:
    if err:
        return f"unreachable ({err})"
    return f"HTTP {status}, content-type {headers.get('content-type', '-')}"


def _check_robots(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/robots.txt")
    text = b.decode("utf-8", "replace") if s == 200 else ""
    m = re.search(r"(?im)^\s*Content-Signal\s*:\s*(.+)$", text)
    return _verdict(bool(m), f"Content-Signal: {m.group(1).strip()}" if m else _ev(s, h, e) + ", no Content-Signal line",
                    "Add under `User-agent: *` in robots.txt: `Content-Signal: search=yes, ai-input=yes, ai-train=no` "
                    "(set each to your own preference; https://contentsignals.org).")


def _check_link_headers(base: str) -> Dict[str, str]:
    s, h, _, e = _fetch(base + "/")
    link = h.get("link", "")
    rels = re.findall(r'rel="?([^";,]+)', link)
    wanted = {"api-catalog", "service-desc", "service-doc", "describedby"}
    ok = bool(set(rels) & wanted)
    return _verdict(ok, f"Link rels: {sorted(set(rels))}" if link else _ev(s, h, e) + ", no Link header",
                    'Return e.g. `Link: </.well-known/api-catalog>; rel="api-catalog"` on the homepage. Static hosts '
                    "cannot set headers: put an edge layer in front (agent_ready_worker_template).")


def _check_markdown(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/", accept="text/markdown")
    ct = h.get("content-type", "")
    ok = s == 200 and ct.startswith("text/markdown")
    return _verdict(ok, _ev(s, h, e) + (f", {len(b)} bytes" if ok else ""),
                    "Answer `Accept: text/markdown` with a markdown body (`Content-Type: text/markdown`, `Vary: Accept`); "
                    "HTML stays the default for browsers. On Cloudflare, enable Markdown for Agents or use the Worker template.")


def _check_api_catalog(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/.well-known/api-catalog", accept="application/linkset+json, application/json")
    doc = _json(b) if s == 200 else None
    linkset = doc.get("linkset") if isinstance(doc, dict) else None
    typed = h.get("content-type", "").startswith("application/linkset+json")
    ok = bool(linkset) and isinstance(linkset, list) and typed
    note = _ev(s, h, e) + (f", {len(linkset)} entries" if isinstance(linkset, list) else "")
    return _verdict(ok, note, "Serve /.well-known/api-catalog (RFC 9727) as `application/linkset+json` with a `linkset` "
                    "array; each entry an `anchor` plus `service-desc`, `service-doc` and optionally `status` links.")


def _check_oauth(base: str) -> Dict[str, Any]:
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        s, h, b, e = _fetch(base + path)
        doc = _json(b) if s == 200 else None
        if isinstance(doc, dict) and all(k in doc for k in ("issuer", "authorization_endpoint", "token_endpoint")):
            return _verdict(True, f"{path}: issuer {doc.get('issuer')}", "")
    return _verdict(False, "neither oauth-authorization-server nor openid-configuration answered with metadata",
                    "Publish RFC 8414 / OIDC discovery metadata (issuer, authorization_endpoint, token_endpoint, jwks_uri, "
                    "grant_types_supported, response_types_supported). If your issuer lives on another host, answer the "
                    "apex path with that issuer's document; never invent an issuer you do not run.")


def _check_prm(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/.well-known/oauth-protected-resource")
    doc = _json(b) if s == 200 else None
    ok = isinstance(doc, dict) and bool(doc.get("resource")) and bool(doc.get("authorization_servers"))
    return _verdict(ok, f"authorization_servers {doc.get('authorization_servers')}" if ok else _ev(s, h, e),
                    "Serve /.well-known/oauth-protected-resource (RFC 9728) with `resource`, `authorization_servers`, "
                    "`scopes_supported` and `bearer_methods_supported: [\"header\"]`.")


def _check_auth_md(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/auth.md", accept="text/markdown, text/plain, */*")
    first = next((ln for ln in b.decode("utf-8", "replace").splitlines() if ln.startswith("# ")), "") if s == 200 else ""
    ok = "auth.md" in first.lower()
    return _verdict(ok, f"H1: {first}" if first else _ev(s, h, e),
                    "Serve /auth.md as markdown with an H1 containing `auth.md`: who the agent audience is, the "
                    "registration endpoints, the supported methods and how to present the credential.")


def _check_mcp_card(base: str) -> Dict[str, str]:
    for path in ("/.well-known/mcp/server-card.json", "/.well-known/mcp/server-cards.json", "/.well-known/mcp.json"):
        s, h, b, e = _fetch(base + path)
        doc = _json(b) if s == 200 else None
        if isinstance(doc, dict) and (doc.get("serverInfo") or doc.get("servers")):
            return _verdict(True, f"{path}: {json.dumps(doc.get('serverInfo') or len(doc.get('servers') or []))}", "")
    return _verdict(False, "no MCP server card at the well-known paths",
                    "Serve /.well-known/mcp/server-card.json (SEP-1649): `serverInfo` {name, version}, a `transport` "
                    "endpoint and `capabilities`. Only if you actually run an MCP server.")


def _check_a2a(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/.well-known/agent-card.json")
    doc = _json(b) if s == 200 else None
    ok = isinstance(doc, dict) and bool(doc.get("name")) and bool(doc.get("supportedInterfaces") or doc.get("url"))
    return _verdict(ok, f"agent {doc.get('name')}" if ok else _ev(s, h, e),
                    "Serve /.well-known/agent-card.json (A2A) with name, version, description, supportedInterfaces "
                    "(a LIVE A2A endpoint + protocolBinding), capabilities and skills. No live A2A transport = leave it absent.")


def _check_skills(base: str) -> Dict[str, str]:
    s, h, b, e = _fetch(base + "/.well-known/agent-skills/index.json")
    doc = _json(b) if s == 200 else None
    skills = doc.get("skills") if isinstance(doc, dict) else None
    ok = isinstance(skills, list) and len(skills) > 0 and all(
        isinstance(x, dict) and {"name", "type", "description", "url", "digest"} <= set(x) for x in skills)
    return _verdict(ok, f"{len(skills)} skills" if isinstance(skills, list) else _ev(s, h, e),
                    "Serve /.well-known/agent-skills/index.json with `$schema` "
                    "https://schemas.agentskills.io/discovery/0.2.0/schema.json and `skills[]` entries "
                    "{name, type: skill-md, description, url, digest: sha256:<hex of the served bytes>}. Compute the "
                    "digest at build time from the exact bytes you publish.")


_CHECKS = {
    "contentSignals": _check_robots,
    "linkHeaders": _check_link_headers,
    "markdownNegotiation": _check_markdown,
    "apiCatalog": _check_api_catalog,
    "oauthDiscovery": _check_oauth,
    "oauthProtectedResource": _check_prm,
    "authMd": _check_auth_md,
    "mcpServerCard": _check_mcp_card,
    "a2aAgentCard": _check_a2a,
    "agentSkills": _check_skills,
}


def agent_ready_probe(url: str) -> dict:
    """Probe a website's agent readiness yourself, with no third-party scanner.

    Checks Content Signals (robots.txt), homepage Link headers, Accept text/markdown
    negotiation, the RFC 9727 API catalog, OAuth/OIDC discovery, RFC 9728
    protected-resource metadata, auth.md, the MCP server card, the A2A agent card
    and the agent-skills index. Returns per-check {status, evidence, fix} plus a
    pass count. Run it before and after a change and quote the verdicts.
    """
    base = _base(url)
    checks = {}
    for name, fn in _CHECKS.items():
        try:
            checks[name] = fn(base)
        except Exception as exc:  # noqa: BLE001 — one broken check must not sink the probe
            checks[name] = {"status": "fail", "evidence": f"check crashed: {exc}", "fix": ""}
    passed = sum(1 for c in checks.values() if c["status"] == "pass")
    return {"site": base, "passed": passed, "total": len(checks), "checks": checks}


def agent_ready_scan(url: str) -> dict:
    """Run the public isitagentready.com scanner on a site and summarise it.

    This sends the URL to a third-party service (isitagentready.com). Returns the
    level, and per check the status and message, grouped by category. Use
    agent_ready_probe when the site must not be sent to a third party.
    """
    base = _base(url)
    s, _, b, e = _fetch(_SCANNER, method="POST", body=json.dumps({"url": base}).encode(), timeout=120)
    doc = _json(b)
    if s != 200 or not isinstance(doc, dict):
        return {"site": base, "error": e or f"scanner answered HTTP {s}", "checks": {}}
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    for cat, checks in (doc.get("checks") or {}).items():
        out[cat] = {k: {"status": v.get("status", ""), "message": v.get("message", "")} for k, v in checks.items()}
    return {"site": base, "level": doc.get("level"), "levelName": doc.get("levelName"), "checks": out,
            "next": (doc.get("nextLevel") or {}).get("name")}


def agent_ready_worker_template() -> dict:
    """Return the Cloudflare Worker that makes a static site agent-ready.

    It is the Worker aitherium.com runs in front of GitHub Pages: Link headers on
    the homepage, correct Content-Types for extensionless well-known files,
    Accept text/markdown negotiation (homepage -> llms.txt, other pages converted
    from HTML), and the issuer's OAuth metadata answered at the apex. Every other
    request is a plain passthrough and any error falls back to passthrough.
    Edit the constants at the top (IDP_ISSUER, HOMEPAGE_LINKS) for your site.
    """
    path = _HERE / "edge_worker.js"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"error": f"template missing: {exc}"}
    return {
        "path": str(path),
        "source": source,
        "wrangler_toml": 'name = "agent-edge"\nmain = "index.js"\ncompatibility_date = "2026-01-01"\n'
                         'workers_dev = false\n\n[[routes]]\npattern = "example.com/*"\nzone_name = "example.com"\n',
        "notes": [
            "The route only fires on a PROXIED (orange-cloud) DNS record; verify with `curl -sI https://<site>/ | grep -i ^link`.",
            "Keep the discovery documents as static files in your site; the Worker only fixes how they are served.",
            "Re-run agent_ready_probe after deploying.",
        ],
    }
