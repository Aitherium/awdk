"""The browser-pair door must be reachable from the owner's page on an OFFLINE box.

Offline narrows CORS to loopback, and offline is where AITHER_LOCAL_AUTH defaults to
`required` -- the mode the grant exists for. Measured 2026-10-04 on an offline daemon:
the challenge answered 200 with no Access-Control-Allow-Origin, so the page never got
its nonce and the Lend panel could never pair. Pinned: the door and the token-scoped
paths answer first-party origins; nothing else widens; the auth gate still refuses.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from adk import browser_grant, fleet_enroll
from adk.agent import AitherAgent
from adk.server import create_app

LOOPBACK = ("127.0.0.1", 50124)
PAGE = "https://app.aitherium.com"


def _client(monkeypatch, **env):
    for k in ("AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS", "AITHER_BROWSER_HANDOFF",
              "AITHER_LOCAL_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {"node_id": "n1", "mode": "rich"})
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    return _client(monkeypatch)


def acao(r):
    return r.headers.get("access-control-allow-origin")


def test_challenge_is_readable_by_the_first_party_page(daemon):
    r = daemon.get("/local/browser-pair/challenge", headers={"Origin": PAGE})
    assert r.status_code == 200 and r.json().get("nonce")
    assert acao(r) == PAGE


def test_pair_preflight_and_private_network(daemon):
    r = daemon.options("/local/browser-pair", headers={
        "Origin": PAGE, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
        "Access-Control-Request-Private-Network": "true"})
    assert r.status_code == 204
    assert acao(r) == PAGE
    assert r.headers.get("access-control-allow-private-network") == "true"


def test_scoped_route_still_refuses_without_a_grant_but_readably(daemon):
    r = daemon.get("/kvholder/status", headers={"Origin": PAGE})
    assert r.status_code == 401
    assert acao(r) == PAGE


@pytest.mark.parametrize("origin", ["https://evil.example.com", "https://aitherium.com.evil.test",
                                    "http://app.aitherium.com"])
def test_a_foreign_origin_gets_nothing(daemon, origin):
    r = daemon.get("/local/browser-pair/challenge", headers={"Origin": origin})
    assert acao(r) is None


def test_routes_outside_the_door_stay_loopback_only(daemon):
    for path in ("/health", "/chat", "/agents"):
        r = daemon.get(path, headers={"Origin": PAGE})
        assert acao(r) is None, path


def test_an_explicit_operator_list_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    c = _client(monkeypatch, AITHER_CORS_ORIGINS="https://portal.lan.example")
    r = c.get("/local/browser-pair/challenge", headers={"Origin": PAGE})
    assert acao(r) is None


def test_offline_the_owner_pairs_for_node_and_chats_plain_end_to_end(monkeypatch, tmp_path):
    # Athena F1 (2026-10-04): the CSRF origin rule narrowed offline too, so the pairing
    # POST answered 403 although its preflight passed. GET+OPTIONS alone missed it.
    import time
    from unittest.mock import AsyncMock

    from adk import node_commands
    from adk.llm.base import LLMResponse
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_enroll, "_load_node_auth", lambda: {"node_id": "n1", "mode": "rich"})
    key = "ab" * 32
    node_commands.save_key("n1", key)
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    for k in ("AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS", "AITHER_LOCAL_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_OFFLINE", "1")
    agent = MagicMock(spec=AitherAgent)
    agent.name = "a"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    agent.llm.chat = AsyncMock(return_value=LLMResponse(content="hi", model="m"))
    agent.chat = AsyncMock(side_effect=AssertionError("page token reached the agent loop"))
    c = TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)

    nonce = c.get("/local/browser-pair/challenge", headers={"Origin": PAGE}).json()["nonce"]
    g = {"kind": "browser-grant/v1", "node_id": "n1", "tenant_id": "platform", "user_id": "owner",
         "origin": PAGE, "nonce": nonce, "scope": "node",
         "issued_at": int(time.time()), "expires_at": int(time.time()) + 120}
    g["sig"] = node_commands.sign(key, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    r = c.post("/local/browser-pair", json={"grant": g}, headers={"Origin": PAGE})
    assert r.status_code == 200, r.text
    assert acao(r) == PAGE
    h = {"Origin": PAGE, "Authorization": f"Bearer {r.json()['token']}"}
    r = c.post("/v1/chat/completions", headers=h,
               json={"messages": [{"role": "user", "content": "hello"}], "plain": False})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "chatcmpl-plain" and acao(r) == PAGE
    agent.chat.assert_not_called()
    # A foreign page still cannot POST there, token or not.
    bad = c.post("/v1/chat/completions", headers={**h, "Origin": "https://evil.example.com"},
                 json={"messages": [{"role": "user", "content": "x"}]})
    assert bad.status_code in (401, 403)


def test_browser_tokens_are_bounded():
    browser_grant._tokens.clear()
    for _ in range(200):
        browser_grant.issue_token("https://app.aitherium.com", "node")
    assert len(browser_grant._tokens) <= 64
