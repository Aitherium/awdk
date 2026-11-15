"""One generator per agent-readiness check, all driven from one SiteProfile.

Each generator returns an :class:`Artifact` (or, for DNS-AID, record dicts plus
a zone-file artifact). Nothing here touches the network or a filesystem outside
the skill sources the profile names; deployers decide where the bytes go.

Honesty rule: a generator never invents an endpoint. A field a document needs and
the profile does not carry raises :class:`ProfileError` instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .profile import ProfileError, SiteProfile, require_absolute

AP2_EXTENSION_URI = "https://github.com/google-agentic-commerce/AP2/tree/v0.1.0"
SKILLS_SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
WBA_CONTENT_TYPE = "application/http-message-signatures-directory+json"
WBA_PATH = "/.well-known/http-message-signatures-directory"

JSON = "application/json"


@dataclass
class Artifact:
    path: str
    content: bytes
    content_type: str
    origin: str
    #: "file" (deploy as a static file), "dns" (records), "fragment" (merge by hand)
    kind: str = "file"
    check: str = ""
    records: List[Dict[str, Any]] = field(default_factory=list)

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _dump(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _art(profile: SiteProfile, path: str, doc: Any, check: str, ct: str = JSON,
         origin: str = "", kind: str = "file") -> Artifact:
    return Artifact(path=path, content=_dump(doc), content_type=ct,
                    origin=origin or profile.apex, kind=kind, check=check)


# ---------------------------------------------------------------- A2A (+AP2)
def ap2_extension(profile: SiteProfile) -> Optional[Dict[str, Any]]:
    ap2 = (profile.commerce or {}).get("ap2")
    if not ap2:
        return None
    roles = list(ap2.get("roles") or [])
    if not roles:
        raise ProfileError("commerce.ap2.roles must name at least one role")
    return {
        "uri": AP2_EXTENSION_URI,
        "description": ap2.get("description") or "Agent Payments Protocol (AP2) support",
        "required": bool(ap2.get("required", False)),
        "params": {"roles": roles},
    }


def a2a_security(profile: SiteProfile) -> Optional[Dict[str, Any]]:
    """OpenAPI-style security for the agent card, taken from the profile's auth block.

    A card that names an OAuth flow has to name URLs that RESOLVE and a scope the
    issuer publishes; the profile carries both, and this generator refuses to emit
    a scope the IdP does not publish. (The old hand-maintained card named
    `idp.../oauth/authorize`, which 404s, and `https://api.aitherium.com/a2a`,
    which redirects to a login page.)

    It emits ``securitySchemes`` only -- never a top-level ``security`` array.
    The apex concierge answers anonymously (profile agent description: "Read-only;
    no account needed"), so marking a scope as REQUIRED would advertise a bearer
    the handler never checks. A scheme an agent MAY use is honest; a requirement
    the endpoint ignores is not.
    """
    sec = (profile.auth or {}).get("a2a_security")
    if not sec:
        return None
    scheme = str(sec.get("scheme") or "oauth2")
    scopes = sec.get("scopes") or {}
    if isinstance(scopes, list):
        scopes = {str(s): "read" for s in scopes}
    if not scopes:
        raise ProfileError("auth.a2a_security.scopes must name at least one scope")
    published = [str(s) for s in (profile.auth.get("scopes") or [])]
    if not published:
        # Fail CLOSED: with no published vocabulary every scope is unverifiable.
        raise ProfileError(
            "auth.a2a_security names scopes but auth.scopes publishes none; "
            "a card cannot advertise a scope the issuer does not publish")
    unpublished = [s for s in scopes if s not in published]
    if unpublished:
        raise ProfileError(
            f"auth.a2a_security scope(s) not published by auth.scopes: {unpublished}")
    flow = {
        "authorizationUrl": require_absolute(sec.get("authorization_url"),
                                             "auth.a2a_security.authorization_url"),
        "tokenUrl": require_absolute(sec.get("token_url"),
                                     "auth.a2a_security.token_url"),
        "scopes": {str(k): str(v) for k, v in scopes.items()},
    }
    return {
        "securitySchemes": {
            scheme: {
                "type": str(sec.get("type") or "oauth2"),
                "description": str(sec.get("description") or scheme),
                "flows": {"authorizationCode": flow},
            }
        },
    }


def a2a_card(profile: SiteProfile) -> Artifact:
    interfaces = []
    for a in profile.agents:
        url = a.get("a2a_url")
        if url:
            interfaces.append({"url": require_absolute(url, f"agents[{a.get('id')}].a2a_url"),
                               "transport": str(a.get("transport") or "JSONRPC")})
    if not interfaces:
        raise ProfileError("a2a_card needs at least one agent with a live a2a_url")
    skills = [{"id": str(s["id"]), "name": str(s.get("name") or s["id"]),
               "description": str(s.get("description") or ""),
               "tags": list(s.get("tags") or [])}
              for s in (profile.skills or []) if s.get("id")]
    if not skills:
        raise ProfileError("a2a_card needs at least one skill")
    caps: Dict[str, Any] = {"streaming": False, "pushNotifications": False}
    ext = ap2_extension(profile)
    if ext:
        caps["extensions"] = [ext]
    card = {
        "protocolVersion": "0.3.0",
        "name": profile.name,
        "version": profile.version,
        "description": profile.description or profile.name,
        "url": interfaces[0]["url"],
        "preferredTransport": interfaces[0]["transport"],
        "supportedInterfaces": interfaces,
        "provider": {"organization": profile.name, "url": profile.apex},
        "capabilities": caps,
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }
    sec = a2a_security(profile)
    if sec:
        card.update(sec)
    return _art(profile, "/.well-known/agent-card.json", card, "discovery.a2aAgentCard")


# ---------------------------------------------------------------- MCP card
def mcp_server_card(profile: SiteProfile) -> Artifact:
    mcp = profile.mcp or {}
    url = require_absolute(mcp.get("url"), "mcp.url")
    card = {
        "serverInfo": {"name": mcp.get("name") or profile.name, "version": profile.version},
        "description": mcp.get("description") or profile.description,
        "transport": {"type": mcp.get("transport") or "streamable-http", "url": url,
                      "endpoint": url},
        "capabilities": mcp.get("capabilities") or {"tools": {}},
    }
    if profile.auth.get("authorization_servers"):
        card["authentication"] = {"type": "oauth2",
                                  "authorization_servers": profile.auth["authorization_servers"]}
    return _art(profile, "/.well-known/mcp/server-card.json", card, "discovery.mcpServerCard")


# ---------------------------------------------------------------- skills index
def skills_index(profile: SiteProfile) -> Dict[str, Artifact]:
    """The v0.2.0 index plus each SKILL.md it lists; digests are of the served bytes."""
    out: Dict[str, Artifact] = {}
    entries = []
    for s in profile.skills:
        src = s.get("source")
        if not src:
            continue
        data = profile.resolve_source(str(src)).read_bytes()
        sid = str(s["id"])
        path = f"/.well-known/agent-skills/{sid}/SKILL.md"
        out[path] = Artifact(path=path, content=data, content_type="text/markdown; charset=utf-8",
                             origin=profile.apex, check="discovery.agentSkills")
        entries.append({"name": sid, "type": "skill-md",
                        "description": str(s.get("description") or ""),
                        "url": profile.apex + path,
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest()})
    if not entries:
        raise ProfileError("skills_index needs at least one skill with a source file")
    idx = _art(profile, "/.well-known/agent-skills/index.json",
               {"$schema": SKILLS_SCHEMA, "skills": entries}, "discovery.agentSkills")
    out[idx.path] = idx
    return out


# ---------------------------------------------------------------- OAuth
def oauth_authorization_server(profile: SiteProfile) -> Artifact:
    """RFC 8414 metadata. Endpoints must be given: an issuer we do not run is a lie."""
    a = profile.auth or {}
    issuer = require_absolute(a.get("issuer"), "auth.issuer")
    doc = {
        "issuer": issuer,
        "authorization_endpoint": require_absolute(a.get("authorization_endpoint"),
                                                   "auth.authorization_endpoint"),
        "token_endpoint": require_absolute(a.get("token_endpoint"), "auth.token_endpoint"),
        "jwks_uri": require_absolute(a.get("jwks_uri"), "auth.jwks_uri"),
        "response_types_supported": a.get("response_types_supported") or ["code"],
        "grant_types_supported": a.get("grant_types_supported")
        or ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(a.get("scopes") or []),
    }
    for k in ("registration_endpoint", "revocation_endpoint", "userinfo_endpoint"):
        if a.get(k):
            doc[k] = require_absolute(a[k], f"auth.{k}")
    return _art(profile, "/.well-known/oauth-authorization-server", doc,
                "discovery.oauthDiscovery")


def oauth_protected_resource(profile: SiteProfile) -> Artifact:
    """RFC 9728 protected-resource metadata."""
    a = profile.auth or {}
    servers = [require_absolute(s, "auth.authorization_servers[]")
               for s in (a.get("authorization_servers") or [])]
    if not servers:
        raise ProfileError("auth.authorization_servers is required")
    doc = {
        "resource": a.get("resource") or profile.apex,
        "authorization_servers": servers,
        "scopes_supported": list(a.get("scopes") or []),
        "bearer_methods_supported": ["header"],
        "resource_name": profile.name,
    }
    if a.get("jwks_uri"):
        doc["jwks_uri"] = require_absolute(a["jwks_uri"], "auth.jwks_uri")
    return _art(profile, "/.well-known/oauth-protected-resource", doc,
                "discovery.oauthProtectedResource")


# ---------------------------------------------------------------- WebMCP
def webmcp_snippet(profile: SiteProfile) -> Artifact:
    """A read-only WebMCP registration: each tool fetches one same-origin URL."""
    tools = []
    for t in profile.webmcp_tools:
        url = str(t.get("url") or "")
        if not url.startswith("/"):
            raise ProfileError(f"webmcp tool {t.get('name')!r} needs a same-origin url ('/...')")
        tools.append({"name": str(t["name"]), "description": str(t.get("description") or ""),
                      "url": url})
    if not tools:
        raise ProfileError("webmcp_tools is empty")
    js = (
        "// Generated by adk agentic_web: read-only WebMCP tools (same-origin GETs).\n"
        "(function () {\n"
        f"  var TOOLS = {json.dumps(tools)};\n"
        "  var mc = (navigator && navigator.modelContext) || document.modelContext;\n"
        "  if (!mc) { return; }\n"
        "  var defs = TOOLS.map(function (t) {\n"
        "    return { name: t.name, description: t.description,\n"
        "      inputSchema: { type: 'object', properties: {} },\n"
        "      annotations: { readOnlyHint: true },\n"
        "      execute: function () { return fetch(t.url, { credentials: 'omit' })\n"
        "        .then(function (r) { return r.text(); })\n"
        "        .then(function (text) { return { content: [{ type: 'text', text: text }] }; });\n"
        "      } };\n"
        "  });\n"
        "  if (typeof mc.provideContext === 'function') { mc.provideContext({ tools: defs }); }\n"
        "  else if (typeof mc.registerTool === 'function') { defs.forEach(function (d) {"
        " mc.registerTool(d); }); }\n"
        "})();\n"
    )
    return Artifact(path="/webmcp-agentic.js", content=js.encode("utf-8"),
                    content_type="text/javascript; charset=utf-8", origin=profile.apex,
                    check="discovery.webMcp")


# ---------------------------------------------------------------- Web Bot Auth
def _public_jwk(jwk: Dict[str, Any]) -> Dict[str, Any]:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not jwk.get("x"):
        raise ProfileError("Web Bot Auth keys must be OKP/Ed25519 JWKs with an 'x'")
    pub = {k: v for k, v in jwk.items() if k in ("kty", "crv", "x", "kid", "alg", "use",
                                                "nbf", "exp")}
    return pub


def web_bot_auth_directory(public_jwks: Any, profile: Optional[SiteProfile] = None) -> Artifact:
    """The /.well-known/http-message-signatures-directory key set. Private parts are dropped."""
    keys = public_jwks.get("keys") if isinstance(public_jwks, dict) else public_jwks
    if not keys:
        raise ProfileError("web_bot_auth_directory needs at least one public key")
    doc = {"keys": [_public_jwk(k) for k in keys]}
    origin = profile.apex if profile else ""
    return Artifact(path=WBA_PATH, content=_dump(doc), content_type=WBA_CONTENT_TYPE,
                    origin=origin, check="botAccessControl.webBotAuth")


# ---------------------------------------------------------------- DNS-AID
_SVC_KEYS = ("alpn", "no-default-alpn", "port", "ipv4hint", "ech", "ipv6hint", "dohpath")
_KEY_RE = re.compile(r"^key(\d{1,5})$")


def _custom_key(k: Any) -> str:
    if isinstance(k, int) or (isinstance(k, str) and k.isdigit()):
        n = int(k)
    else:
        m = _KEY_RE.match(str(k))
        if not m:
            raise ProfileError(f"custom SvcParam key {k!r} must be numeric (keyNNNNN)")
        n = int(m.group(1))
    if not 0 <= n <= 65535:
        raise ProfileError(f"SvcParam key {k!r} out of range")
    return f"key{n}"


def _fqdn(host: str) -> str:
    return host if host.endswith(".") else host + "."


def _quote(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dns_aid_records(profile: SiteProfile) -> List[Dict[str, Any]]:
    """SVCB (or HTTPS) records under _<proto>._agents.<domain>., one per dns_aid entry."""
    out = []
    for e in profile.dns_aid:
        proto = str(e.get("proto") or "").strip().lstrip("_").lower()
        if not re.match(r"^[a-z0-9-]+$", proto):
            raise ProfileError(f"dns_aid proto {e.get('proto')!r} is not a label")
        rtype = str(e.get("type") or "SVCB").upper()
        if rtype not in ("SVCB", "HTTPS"):
            raise ProfileError("dns_aid type must be SVCB or HTTPS")
        target = _fqdn(str(e.get("target") or f"agent.{profile.domain}"))
        alpn = e.get("alpn") or proto
        alpn = ",".join(alpn) if isinstance(alpn, (list, tuple)) else str(alpn)
        port = int(e.get("port") or 443)
        priority = int(e.get("priority") or 1)
        parts = [str(priority), target, f"alpn={_quote(alpn)}", f"port={port}"]
        for k, v in (e.get("params") or {}).items():
            if str(k) in _SVC_KEYS or str(k) == "mandatory":
                raise ProfileError(f"{k!r} is set by its own field, not params")
            parts.append(f"{_custom_key(k)}={_quote(v)}")
        parts.append("mandatory=alpn,port")
        out.append({"name": f"_{proto}._agents.{profile.domain}.", "type": rtype,
                    "data": " ".join(parts), "ttl": int(e.get("ttl") or 300),
                    "priority": priority, "target": target})
    return out


def dns_aid_artifact(profile: SiteProfile) -> Artifact:
    recs = dns_aid_records(profile)
    if not recs:
        raise ProfileError("dns_aid is empty")
    zone = "".join(f"{r['name']} {r['ttl']} IN {r['type']} {r['data']}\n" for r in recs)
    return Artifact(path=f"dns:{profile.domain}", content=zone.encode("utf-8"),
                    content_type="text/dns", origin="dns", kind="dns",
                    check="discoverability.dnsAid", records=recs)


# ---------------------------------------------------------------- commerce
def acp(profile: SiteProfile) -> Artifact:
    c = (profile.commerce or {}).get("acp") or {}
    if not c:
        raise ProfileError("commerce.acp is not configured")
    base = require_absolute(c.get("api_base_url"), "commerce.acp.api_base_url")
    transports = list(c.get("transports") or ["https"])
    services = list(c.get("services") or [])
    if not services:
        raise ProfileError("commerce.acp.services must be non-empty")
    doc = {
        "protocol": {"name": "acp", "version": str(c.get("version") or "2025-09-29")},
        "api_base_url": base,
        "transports": transports,
        "capabilities": {"services": services},
    }
    return _art(profile, "/.well-known/acp.json", doc, "commerce.acp")


def ucp(profile: SiteProfile) -> Artifact:
    c = (profile.commerce or {}).get("ucp") or {}
    if not c:
        raise ProfileError("commerce.ucp is not configured")
    endpoints = c.get("endpoints") or {}
    if not endpoints:
        raise ProfileError("commerce.ucp.endpoints must be non-empty")
    for k, v in (endpoints.items() if isinstance(endpoints, dict) else []):
        require_absolute(v if isinstance(v, str) else (v or {}).get("url"),
                         f"commerce.ucp.endpoints.{k}")
    doc = {
        "protocol_version": str(c.get("protocol_version") or "2026-01-11"),
        "services": c.get("services") or [],
        "capabilities": c.get("capabilities") or [],
        "endpoints": endpoints,
    }
    if not doc["services"] or not doc["capabilities"]:
        raise ProfileError("commerce.ucp needs services and capabilities")
    return _art(profile, "/.well-known/ucp", doc, "commerce.ucp")


class LivePaymentRefused(ProfileError):  # noqa: N818 - reads as a verdict
    """x402 outside test mode needs a wallet and a facilitator: a decision, not a generator."""


def x402_requirements(profile: SiteProfile, test_mode: bool = True) -> Artifact:
    """x402 PaymentRequirements for the 402 body. Test networks only."""
    if not test_mode:
        raise LivePaymentRefused("x402 live mode needs an owner decision (wallet + facilitator)")
    c = dict((profile.commerce or {}).get("x402") or {})
    if c.get("test_mode") is False:
        raise LivePaymentRefused("profile commerce.x402.test_mode is false")
    resource = c.get("resource") or profile.apex + "/api/x402/demo"
    doc = {
        "x402Version": 1,
        "error": "payment required",
        "accepts": [{
            "scheme": "exact",
            "network": c.get("network") or "base-sepolia",
            "maxAmountRequired": str(c.get("amount_atomic") or "1000"),
            "resource": require_absolute(resource, "commerce.x402.resource"),
            "description": c.get("description") or f"{profile.name} test payment",
            "mimeType": "application/json",
            "payTo": c.get("pay_to") or "0x0000000000000000000000000000000000000000",
            "maxTimeoutSeconds": int(c.get("max_timeout_seconds") or 60),
            "asset": c.get("asset") or "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "extra": {"name": "USDC", "version": "2", "test_mode": True},
        }],
    }
    return _art(profile, "/.well-known/x402-test-requirements.json", doc, "commerce.x402",
                kind="fragment")


def mpp_openapi_fragment(profile: SiteProfile) -> Artifact:
    """OpenAPI fragment carrying x-payment-info per payable op and x-service-info."""
    c = (profile.commerce or {}).get("mpp") or {}
    ops = c.get("operations") or []
    if not ops:
        raise ProfileError("commerce.mpp.operations is empty")
    paths: Dict[str, Dict[str, Any]] = {}
    for op in ops:
        method = str(op.get("method") or "post").lower()
        info = {"intent": op.get("intent") or "charge",
                "method": op.get("payment_method") or "stripe",
                "amount": str(op.get("amount")), "currency": op.get("currency") or "USD"}
        if op.get("amount") is None:
            raise ProfileError(f"commerce.mpp op {op.get('path')} has no amount")
        paths.setdefault(str(op["path"]), {})[method] = {
            "summary": op.get("summary") or "Payable operation", "x-payment-info": info}
    doc = {"paths": paths,
           "x-service-info": c.get("service_info") or {"categories": ["software"]}}
    return _art(profile, "/openapi.agentic-web-fragment.json", doc, "commerce.mpp",
                kind="fragment")


# ---------------------------------------------------------------- all
GENERATORS = {
    "a2a_card": a2a_card,
    "mcp_server_card": mcp_server_card,
    "skills_index": skills_index,
    "oauth_authorization_server": oauth_authorization_server,
    "oauth_protected_resource": oauth_protected_resource,
    "webmcp_snippet": webmcp_snippet,
    "web_bot_auth_directory": None,  # needs public_jwks
    "dns_aid_records": dns_aid_artifact,
    "acp": acp,
    "ucp": ucp,
    "x402_requirements": x402_requirements,
    "mpp_openapi_fragment": mpp_openapi_fragment,
}


def _wba(profile: SiteProfile) -> Optional[Artifact]:
    jwks = (profile.web_bot_auth or {}).get("public_jwks")
    return web_bot_auth_directory(jwks, profile) if jwks else None


def generate(profile: SiteProfile, check: str) -> Dict[str, Artifact]:
    if check == "web_bot_auth_directory":
        a = _wba(profile)
        if a is None:
            raise ProfileError("web_bot_auth.public_jwks is not set")
        return {a.path: a}
    fn = GENERATORS.get(check)
    if fn is None:
        raise ProfileError(f"unknown check {check!r}; one of {sorted(GENERATORS)}")
    res = fn(profile)
    return res if isinstance(res, dict) else {res.path: res}


def _configured(profile: SiteProfile) -> List[str]:
    c = profile.commerce or {}
    want = []
    if any(a.get("a2a_url") for a in profile.agents):
        want.append("a2a_card")
    if profile.mcp.get("url"):
        want.append("mcp_server_card")
    if any(s.get("source") for s in profile.skills):
        want.append("skills_index")
    if profile.auth.get("token_endpoint"):
        want.append("oauth_authorization_server")
    if profile.auth.get("authorization_servers"):
        want.append("oauth_protected_resource")
    if profile.webmcp_tools:
        want.append("webmcp_snippet")
    if (profile.web_bot_auth or {}).get("public_jwks"):
        want.append("web_bot_auth_directory")
    if profile.dns_aid:
        want.append("dns_aid_records")
    for k in ("acp", "ucp"):
        if c.get(k):
            want.append(k)
    if c.get("x402", {}).get("enabled"):
        want.append("x402_requirements")
    if (c.get("mpp") or {}).get("operations"):
        want.append("mpp_openapi_fragment")
    return want


def generate_all(profile: SiteProfile) -> Dict[str, Artifact]:
    """Every artifact the profile has the facts for, keyed by path."""
    out: Dict[str, Artifact] = {}
    for check in _configured(profile):
        out.update(generate(profile, check))
    return out
