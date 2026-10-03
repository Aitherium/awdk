"""The agent daemon does not trust "loopback" as a person, and offline means offline.

Security review of the awnix desktop (AitherOS PR #10908), three findings in adk.server:
  1. any local process -- any uid -- could drive the daemon (no credential on
     loopback), including /identity/handoff, which mints a ticket for the owner's
     account to anyone who sets an allowed Origin header;
  2. AITHER_OFFLINE did not narrow CORS/CSRF: a hosted page could POST /chat to the
     local agent and read the reply; a third party's github.io origin was trusted;
  3. offline, the daemon dialled 127.0.0.1:8182 by default and presented the account
     key or ~/.aither/session-bearer to whatever listened there.
Each test below fails on the code before the fix.
"""
from __future__ import annotations

import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from adk import local_auth
from adk.agent import AitherAgent
from adk import server as srv
from adk.server import create_app


def _gateway_attach_policy(raw):
    return srv._gateway_attach_policy(raw)


def _local_gateway_api_key(cfg, **kw):
    return srv._local_gateway_api_key(cfg, **kw)

LOOPBACK = ("127.0.0.1", 50123)
HOSTED = "https://desktop.aitherium.com"


def _app(monkeypatch, **env):
    for k in ("AITHER_OFFLINE", "AITHER_LOCAL_AUTH", "AITHER_SERVER_API_KEY",
              "AITHER_CORS_ORIGINS", "AITHER_BROWSER_HANDOFF"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    # A route past auth may still 500 on the mock agent; only the auth verdict matters.
    return TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)


def _tok():
    return {local_auth.HEADER: local_auth.read_token() or ""}


# ── the credential itself ──────────────────────────────────────────────────────

def test_token_is_minted_owner_only_and_stable(tmp_path, monkeypatch):
    path = tmp_path / "a" / "daemon-token"
    first = local_auth.ensure_token(path)
    assert first and local_auth.ensure_token(path) == first
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_mode_defaults_to_required_offline_only():
    assert local_auth.mode({"AITHER_OFFLINE": "1"}) == "required"
    assert local_auth.mode({}) == "off"
    assert local_auth.mode({"AITHER_LOCAL_AUTH": "required"}) == "required"
    assert local_auth.mode({"AITHER_OFFLINE": "1", "AITHER_LOCAL_AUTH": "off"}) == "off"


def test_the_token_is_only_ever_sent_to_loopback(tmp_path):
    path = tmp_path / "t"
    local_auth.ensure_token(path)
    assert local_auth.headers_for("http://127.0.0.1:9001/chat", path)
    assert local_auth.headers_for("http://[::1]:9001", path)
    for url in ("https://api.aitherium.com", "http://10.0.0.5:9001",
                "http://127.0.0.1.evil.test:9001"):
        assert local_auth.headers_for(url, path) == {}, url


_HDR = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
V4_LO, V6_LO = "0100007F", "00000000000000000000000001000000"
V6_ANY, V6_MAPPED_LO = "0" * 32, "0000000000000000FFFF00000100007F"
ME, OTHER = 1000, 1001


def _table(tmp_path, tcp=(), tcp6=()):
    """A /proc/net with LISTEN rows (address hex, port, uid)."""
    net = tmp_path / "net"
    net.mkdir(parents=True, exist_ok=True)
    for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
        body = "".join(f"   {i}: {a}:{p:04X} 00000000:0000 0A 00000000:00000000 00:00000000 "
                       f"00000000 {u}\n" for i, (a, p, u) in enumerate(rows))
        (net / name).write_text(_HDR + body, encoding="ascii")
    return net


def _send(tmp_path, url, net):
    path = tmp_path / "t"
    local_auth.ensure_token(path)
    return bool(local_auth.headers_for(url, path, net, uid=ME))


def test_the_token_goes_to_the_owners_own_listener(tmp_path):
    net = _table(tmp_path, tcp=[(V4_LO, 9001, ME)], tcp6=[(V6_LO, 9001, ME)])
    assert _send(tmp_path, "http://127.0.0.1:9001", net)
    assert _send(tmp_path, "http://localhost:9001", net)
    assert _send(tmp_path, "http://[::1]:9001", net)


def test_a_squatter_on_the_other_loopback_gets_nothing(tmp_path):
    """Review repro: the owner on 127.0.0.1:9001, another uid on [::1]:9001. `localhost`
    resolves to both, so the client may reach the squatter: withhold."""
    net = _table(tmp_path, tcp=[(V4_LO, 9001, ME)], tcp6=[(V6_LO, 9001, OTHER)])
    assert not _send(tmp_path, "http://localhost:9001", net)
    assert not _send(tmp_path, "http://[::1]:9001", net)
    assert _send(tmp_path, "http://127.0.0.1:9001", net)  # only the owner's socket there


def test_a_squatter_on_a_wildcard_or_mapped_address_gets_nothing(tmp_path):
    net = _table(tmp_path, tcp=[(V4_LO, 9001, ME)], tcp6=[(V6_ANY, 9001, OTHER)])
    assert not _send(tmp_path, "http://127.0.0.1:9001", net)  # :: takes v4 too (dual-stack)
    net = _table(tmp_path / "m", tcp=[(V4_LO, 9001, ME)], tcp6=[(V6_MAPPED_LO, 9001, OTHER)])
    assert not _send(tmp_path / "m", "http://127.0.0.1:9001", net)
    assert not local_auth.headers_for("http://[::ffff:127.0.0.1]:9001",
                                      tmp_path / "t", net, uid=ME)


def test_no_listener_at_all_gets_nothing(tmp_path):
    """The owner's daemon is down: whoever binds next is unknown. Fail closed."""
    net = _table(tmp_path, tcp=[(V4_LO, 9002, ME)])
    assert not _send(tmp_path, "http://127.0.0.1:9001", net)


def test_an_unreadable_table_falls_back_to_the_loopback_rule(tmp_path):
    """Not Linux: the kernel cannot say who listens, so ownership is not judged."""
    assert _send(tmp_path, "http://127.0.0.1:9001", tmp_path / "no-proc")


# ── finding 1: loopback is a machine, not a person ─────────────────────────────

def test_offline_daemon_refuses_an_anonymous_local_caller(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    assert c.post("/chat", json={"message": "hi"}).status_code == 401
    assert c.post("/cli/execute", json={"args": ["version"]}).status_code == 401
    assert c.get("/agents").status_code == 401
    assert c.get("/health").status_code == 200  # liveness stays open


def test_offline_daemon_accepts_the_owner(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    assert c.get("/agents", headers=_tok()).status_code != 401
    wrong = {local_auth.HEADER: "not-the-token"}
    assert c.get("/agents", headers=wrong).status_code == 401


def test_the_remote_api_key_still_works_when_required(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1", AITHER_SERVER_API_KEY="k-remote")
    assert c.get("/agents", headers={"Authorization": "Bearer k-remote"}).status_code != 401


def test_online_default_keeps_the_previous_loopback_behaviour(monkeypatch):
    c = _app(monkeypatch)
    assert c.get("/agents").status_code != 401


def _signed_in():
    store = MagicMock()
    store.get_active_profile.return_value = {
        "endpoint": "https://idp.aitherium.com/identity", "token_type": "bearer",
        "access_token": "x", "user": {"username": "david"}}
    return (patch("adk.auth.AuthStore", return_value=store),
            patch("adk.auth.resolve_credentials", return_value=MagicMock(is_expired=False)))


@pytest.mark.parametrize("env", [{"AITHER_OFFLINE": "1"}, {"AITHER_LOCAL_AUTH": "required"}])
def test_handoff_needs_the_owner_credential_offline_or_required(monkeypatch, env):
    """Any non-browser process sets any Origin it likes: offline, or in required mode,
    the ticket route must not mint for a caller that cannot read the owner's token."""
    c = _app(monkeypatch, **env)
    a, b = _signed_in()
    with a, b, patch("adk.server.httpx.AsyncClient") as ac:
        r = c.post("/identity/handoff", headers={"Origin": "https://aitherium.com"}, json={})
        assert r.status_code == 401 and "local credential" in r.text
        ac.assert_not_called()


def _minting(c, headers):
    a, b = _signed_in()
    resp = MagicMock(status_code=200, content=b"{}")
    resp.json.return_value = {"ticket": "t-1", "expires_in": 60}
    with a, b, patch("adk.server.httpx.AsyncClient") as ac:
        ac.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        return c.post("/identity/handoff", headers=headers, json={})


def test_online_handoff_keeps_the_browser_flow(monkeypatch):
    """ONLINE, default auth mode: the hosted "sign in with this device" page cannot read
    the token, so the ticket keeps its previous gate (loopback peer, allowlisted Origin,
    CSRF) -- Veil local-identity-handoff.ts and awkit local-device.tsx work as before."""
    c = _app(monkeypatch)
    r = _minting(c, {"Origin": "https://aitherium.com"})
    assert r.status_code == 200 and r.json()["ticket"] == "t-1"
    assert _minting(c, {"Origin": "https://evil.example"}).status_code == 403


def test_required_mode_handoff_mints_for_the_owner(monkeypatch):
    """The token gate is a gate, not a wall: the owner's software still gets a ticket.
    (Offline, a hosted Origin is already refused by the loopback-only CSRF rule.)"""
    c = _app(monkeypatch, AITHER_LOCAL_AUTH="required")
    r = _minting(c, {"Origin": "https://aitherium.com", **_tok()})
    assert r.status_code == 200
    off = _app(monkeypatch, AITHER_OFFLINE="1")
    assert _minting(off, {"Origin": "https://aitherium.com", **_tok()}).status_code == 403


@pytest.mark.parametrize("env", [{}, {"AITHER_OFFLINE": "1"}])
def test_mesh_join_needs_the_owner_credential_in_every_mode(monkeypatch, env):
    c = _app(monkeypatch, **env)
    a, b = _signed_in()
    with a, b, patch("adk.server.httpx.AsyncClient"):
        r = c.post("/mesh/join", headers={"Origin": "https://aitherium.com"}, json={})
    assert r.status_code == 401


# ── websockets: the HTTP middleware never sees them ────────────────────────────

def _ws_code(c, path, headers):
    from starlette.websockets import WebSocketDisconnect
    try:
        with c.websocket_connect(path, headers=headers) as ws:
            ws.receive_json()
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def _ws_routes(c):
    from starlette.routing import WebSocketRoute
    return [r.path for r in c.app.routes if isinstance(r, WebSocketRoute)]


def test_every_websocket_route_refuses_an_anonymous_local_caller(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    routes = _ws_routes(c)
    assert "/ws/chat" in routes
    for path in routes:
        assert _ws_code(c, path, {}) == 1008, path


def test_a_websocket_from_a_foreign_origin_is_refused_even_with_the_token(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    for path in _ws_routes(c):
        assert _ws_code(c, path, {"Origin": "https://evil.example", **_tok()}) == 1008, path
    online = _app(monkeypatch)
    assert _ws_code(online, "/ws/chat", {"Origin": "https://evil.example"}) == 1008


def test_the_owner_reaches_the_websocket_handler(monkeypatch):
    """Past the guard: the handler itself answers (4000 = no chat relay in this app)."""
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    assert _ws_code(c, "/ws/chat", _tok()) == 4000
    assert _ws_code(c, "/ws/chat", {"Origin": "http://127.0.0.1:3000", **_tok()}) == 4000


# ── finding 2: offline trusts no hosted page ───────────────────────────────────

def _preflight(c, origin):
    return c.options("/chat", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})


def test_offline_cors_admits_loopback_only(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    assert "access-control-allow-origin" not in _preflight(c, HOSTED).headers
    assert "access-control-allow-origin" not in _preflight(c, "https://acme.aitherium.com").headers
    assert _preflight(c, "http://127.0.0.1:3000").headers.get(
        "access-control-allow-origin") == "http://127.0.0.1:3000"


def test_offline_csrf_refuses_a_hosted_origin_even_with_the_token(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    r = c.post("/chat", headers={"Origin": HOSTED, **_tok()}, json={"message": "hi"})
    assert r.status_code == 403


def test_online_still_serves_our_hosted_surfaces(monkeypatch):
    c = _app(monkeypatch)
    assert _preflight(c, HOSTED).headers.get("access-control-allow-origin") == HOSTED


def test_the_third_party_pages_origin_is_gone(monkeypatch):
    c = _app(monkeypatch)
    assert "access-control-allow-origin" not in _preflight(
        c, "https://elodineofficial.github.io").headers
    r = c.post("/chat", headers={"Origin": "https://elodineofficial.github.io"},
               json={"message": "hi"})
    assert r.status_code == 403


# ── finding 3: offline dials no gateway it was not told to, and never leaks ────

def test_no_gateway_is_dialled_by_default(monkeypatch):
    monkeypatch.delenv("AITHER_MCP_GATEWAY", raising=False)
    assert _gateway_attach_policy("")["refuse"]


def test_plaintext_loopback_needs_explicit_trust_and_never_gets_the_account(monkeypatch):
    monkeypatch.delenv("AITHER_MCP_GATEWAY_TRUSTED", raising=False)
    assert _gateway_attach_policy("127.0.0.1:8182")["refuse"]
    monkeypatch.setenv("AITHER_MCP_GATEWAY_TRUSTED", "1")
    p = _gateway_attach_policy("127.0.0.1:8182")
    assert not p["refuse"] and p["account_ok"] is False
    assert _gateway_attach_policy("https://127.0.0.1:8182")["account_ok"] is True
    assert _gateway_attach_policy("https://gateway.example.com")["refuse"]


def test_the_account_credential_never_goes_to_a_plaintext_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".aither").mkdir()
    (tmp_path / ".aither" / "session-bearer").write_text("ACCOUNT-BEARER", encoding="utf-8")
    monkeypatch.delenv("AITHER_MCP_KEY", raising=False)
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    cfg = MagicMock(aither_api_key="ACCOUNT-KEY")
    assert _local_gateway_api_key(cfg, account_ok=False) == ""
    monkeypatch.setenv("AITHER_MCP_KEY", "gateway-own-key")
    assert _local_gateway_api_key(cfg, account_ok=False) == "gateway-own-key"


def test_x_session_import_refuses_offline(monkeypatch):
    c = _app(monkeypatch, AITHER_OFFLINE="1")
    r = c.post("/x-session/import", headers=_tok(),
               json={"cookies": [{"name": "auth_token"}, {"name": "ct0"}]})
    assert r.status_code == 503
