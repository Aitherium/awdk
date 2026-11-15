"""agentic_web generators: one test per generator, asserting the spec fields."""
from __future__ import annotations

import hashlib

import pytest

from adk.toolpacks.agentic_web import generators as g
from adk.toolpacks.agentic_web import signer
from adk.toolpacks.agentic_web.profile import ProfileError, SiteProfile


def _profile(tmp_path, **over):
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(b"---\nname: demo\ndescription: d\n---\n# Demo\n")
    d = {
        "domain": "example.com", "name": "Example", "version": "2.1.0",
        "description": "An example business.",
        "agents": [{"id": "c", "name": "Concierge", "description": "d",
                    "a2a_url": "https://example.com/a2a", "transport": "JSONRPC"}],
        "skills": [{"id": "demo", "name": "Demo", "description": "A demo skill",
                    "source": str(skill)}],
        "auth": {"issuer": "https://idp.example.com", "authorization_servers":
                 ["https://idp.example.com"], "scopes": ["openid"],
                 "authorization_endpoint": "https://idp.example.com/authorize",
                 "token_endpoint": "https://idp.example.com/token",
                 "jwks_uri": "https://idp.example.com/jwks.json"},
        "mcp": {"url": "https://mcp.example.com/mcp"},
        "webmcp_tools": [{"name": "overview", "description": "site", "url": "/llms.txt"}],
        "dns_aid": [{"proto": "a2a", "port": 443, "alpn": "a2a"}],
        "commerce": {
            "ap2": {"roles": ["merchant"]},
            "acp": {"version": "2025-09-29", "api_base_url": "https://example.com/api",
                    "transports": ["https"], "services": ["checkout"]},
            "ucp": {"protocol_version": "2026-01-11", "services": ["shopping"],
                    "capabilities": ["checkout"],
                    "endpoints": {"checkout": "https://example.com/api/checkout"}},
            "x402": {"test_mode": True},
            "mpp": {"operations": [{"path": "/api/checkout", "method": "post",
                                    "amount": "10.00", "currency": "USD"}]},
        },
    }
    d.update(over)
    return SiteProfile.from_dict(d)


def test_a2a_card_spec_fields_and_ap2(tmp_path):
    card = g.a2a_card(_profile(tmp_path)).json()
    assert card["name"] == "Example" and card["version"] == "2.1.0"
    assert card["description"]
    assert card["supportedInterfaces"] == [{"url": "https://example.com/a2a",
                                            "transport": "JSONRPC"}]
    assert isinstance(card["capabilities"], dict)
    assert card["skills"] and all({"id", "name", "description"} <= set(s)
                                  for s in card["skills"])
    ext = card["capabilities"]["extensions"][0]
    assert ext["uri"] == "https://github.com/google-agentic-commerce/AP2/tree/v0.1.0"
    assert ext["params"]["roles"] == ["merchant"]
    assert ext["required"] is False


def test_a2a_card_refuses_without_live_endpoint(tmp_path):
    with pytest.raises(ProfileError):
        g.a2a_card(_profile(tmp_path, agents=[{"id": "x", "name": "x"}]))


def test_acp_fields_and_absolute_base(tmp_path):
    doc = g.acp(_profile(tmp_path)).json()
    assert doc["protocol"]["name"] == "acp" and doc["protocol"]["version"]
    assert doc["api_base_url"].startswith("https://")
    assert doc["transports"] and doc["capabilities"]["services"]
    bad = _profile(tmp_path)
    bad.commerce["acp"]["api_base_url"] = "/api"
    with pytest.raises(ProfileError):
        g.acp(bad)


def test_ucp_fields(tmp_path):
    doc = g.ucp(_profile(tmp_path)).json()
    for k in ("protocol_version", "services", "capabilities", "endpoints"):
        assert doc[k]


def test_dns_aid_record_exact(tmp_path):
    recs = g.dns_aid_records(_profile(tmp_path))
    assert recs[0]["name"] == "_a2a._agents.example.com."
    assert recs[0]["type"] == "SVCB"
    assert recs[0]["data"] == '1 agent.example.com. alpn="a2a" port=443 mandatory=alpn,port'


def test_dns_aid_custom_params(tmp_path):
    p = _profile(tmp_path, dns_aid=[{"proto": "mcp", "params": {"65400": "v1",
                                                               "key65401": "x"}}])
    data = g.dns_aid_records(p)[0]["data"]
    assert 'key65400="v1"' in data and 'key65401="x"' in data
    assert data.endswith("mandatory=alpn,port")
    with pytest.raises(ProfileError):
        g.dns_aid_records(_profile(tmp_path, dns_aid=[{"proto": "a2a",
                                                      "params": {"cap": "x"}}]))


def test_web_bot_auth_directory():
    jwk = signer.generate_ed25519_jwk()
    art = g.web_bot_auth_directory({"keys": [jwk]})
    assert art.content_type == "application/http-message-signatures-directory+json"
    assert art.path == "/.well-known/http-message-signatures-directory"
    keys = art.json()["keys"]
    assert keys and all(k["kty"] == "OKP" and k["crv"] == "Ed25519" and "d" not in k
                        for k in keys)
    assert jwk["d"] not in art.content.decode()


def test_skills_index_digests_match(tmp_path):
    arts = g.skills_index(_profile(tmp_path))
    idx = arts["/.well-known/agent-skills/index.json"].json()
    assert idx["$schema"].endswith("/0.2.0/schema.json")
    for entry in idx["skills"]:
        served = arts["/.well-known/agent-skills/" + entry["name"] + "/SKILL.md"].content
        assert entry["digest"] == "sha256:" + hashlib.sha256(served).hexdigest()


def test_oauth_as_and_prm(tmp_path):
    p = _profile(tmp_path)
    asm = g.oauth_authorization_server(p).json()
    assert asm["issuer"] == "https://idp.example.com"
    assert asm["authorization_endpoint"] and asm["token_endpoint"] and asm["jwks_uri"]
    prm = g.oauth_protected_resource(p).json()
    assert prm["resource"] == "https://example.com"
    assert prm["authorization_servers"] == ["https://idp.example.com"]
    noendpoints = _profile(tmp_path, auth={"issuer": "https://idp.example.com"})
    with pytest.raises(ProfileError):
        g.oauth_authorization_server(noendpoints)


def test_mcp_card_and_webmcp(tmp_path):
    p = _profile(tmp_path)
    card = g.mcp_server_card(p).json()
    assert card["serverInfo"]["name"] and card["transport"]["url"].startswith("https://")
    js = g.webmcp_snippet(p).content.decode()
    assert "modelContext" in js and "/llms.txt" in js


def test_x402_refuses_live_mode(tmp_path):
    p = _profile(tmp_path)
    doc = g.x402_requirements(p).json()
    assert doc["accepts"][0]["extra"]["test_mode"] is True
    with pytest.raises(ProfileError):
        g.x402_requirements(p, test_mode=False)


def test_mpp_fragment(tmp_path):
    doc = g.mpp_openapi_fragment(_profile(tmp_path)).json()
    info = doc["paths"]["/api/checkout"]["post"]["x-payment-info"]
    assert info["amount"] == "10.00" and info["currency"] == "USD"
    assert "x-service-info" in doc


def test_generate_all_keys(tmp_path):
    arts = g.generate_all(_profile(tmp_path))
    for path in ("/.well-known/agent-card.json", "/.well-known/acp.json", "/.well-known/ucp",
                 "/.well-known/agent-skills/index.json", "dns:example.com"):
        assert path in arts
    assert all(isinstance(a, g.Artifact) and a.content for a in arts.values())


def test_example_profile_loads():
    from pathlib import Path

    import adk.toolpacks.agentic_web as pkg
    p = SiteProfile.load(str(Path(pkg.__file__).parent / "examples" /
                             "site-profile.example.yaml"))
    arts = g.generate_all(p)
    card = arts["/.well-known/agent-card.json"].json()
    assert card["supportedInterfaces"][0]["url"] == "https://aitherium.com/a2a"
    assert card["capabilities"]["extensions"][0]["required"] is False


def _with_a2a_security(tmp_path, *, scopes=None):
    """The fixture profile plus a real a2a_security block (issuer publishes scopes)."""
    return _profile(tmp_path, auth={
        "issuer": "https://idp.example.com",
        "scopes": ["openid", "agent:read"] if scopes is None else scopes,
        "a2a_security": {
            "scheme": "oauth2",
            "description": "Example Identity",
            "authorization_url": "https://idp.example.com/identity/oidc/authorize",
            "token_url": "https://idp.example.com/identity/oidc/token",
            "scopes": {"agent:read": "read own registration"},
        },
    })


def test_a2a_security_emits_scheme_only(tmp_path):
    # The concierge answers anonymously, so the card must NOT mark a bearer as
    # required: serveA2A reads no Authorization header. Scheme yes, requirement no.
    card = g.a2a_card(_with_a2a_security(tmp_path)).json()
    flow = card["securitySchemes"]["oauth2"]["flows"]["authorizationCode"]
    assert flow["scopes"] == {"agent:read": "read own registration"}
    assert flow["authorizationUrl"].startswith("https://")
    assert flow["tokenUrl"].startswith("https://")
    assert "security" not in card


def test_a2a_security_refuses_scope_the_idp_does_not_publish(tmp_path):
    with pytest.raises(ProfileError):
        g.a2a_card(_with_a2a_security(tmp_path, scopes=["openid"]))


def test_a2a_security_refuses_when_auth_scopes_publish_nothing(tmp_path):
    # Fail CLOSED: with no published vocabulary, "agent:read" cannot be verified.
    with pytest.raises(ProfileError):
        g.a2a_card(_with_a2a_security(tmp_path, scopes=[]))
