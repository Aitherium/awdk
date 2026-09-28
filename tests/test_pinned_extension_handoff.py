"""The pinned Awconnect origin may sign in via the local daemon; nothing else may.

Also the CSRF guard: a browser Origin on a mutating route must be one we serve,
and the chat/execute routes take JSON bodies only. No-Origin callers (CLI, curl)
are untouched.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from adk.agent import AitherAgent
from fastapi.testclient import TestClient

from adk import extension_id as ext
from adk.server import create_app

PINNED = f"chrome-extension://{ext.PINNED_EXTENSION_ID}"
OTHER = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
PROFILE = {"endpoint": "https://idp.aitherium.com/identity", "token_type": "bearer",
           "access_token": "x",
           "user": {"username": "david", "display_name": "David", "email": "d@example.com"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "allowlist_path", lambda: tmp_path / "allowed_extension_ids")
    monkeypatch.delenv("AITHER_EXTENSION_IDS", raising=False)
    monkeypatch.delenv("AITHER_TRUSTED_EXTENSION_IDS", raising=False)
    monkeypatch.delenv("AITHER_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("AITHER_BROWSER_HANDOFF", raising=False)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=("127.0.0.1", 50000))


def _signed_in():
    store = MagicMock()
    store.get_active_profile.return_value = dict(PROFILE)
    creds = MagicMock(is_expired=False)
    return (patch("adk.auth.AuthStore", return_value=store),
            patch("adk.auth.resolve_credentials", return_value=creds))


def _whoami(client, origin):
    a, b = _signed_in()
    with a, b:
        return client.get("/identity/whoami", headers={"Origin": origin})


def _handoff(client, origin):
    a, b = _signed_in()
    resp = MagicMock(status_code=200, content=b"{}")
    resp.json.return_value = {"ticket": "t-1", "expires_in": 60}
    http = MagicMock()
    http.post = AsyncMock(return_value=resp)
    ac = MagicMock()
    ac.return_value.__aenter__ = AsyncMock(return_value=http)
    ac.return_value.__aexit__ = AsyncMock(return_value=False)
    with a, b, patch("adk.server.httpx.AsyncClient", ac):
        r = client.post("/identity/handoff", headers={"Origin": origin}, json={})
    return r, http


def test_pinned_id_is_well_formed_and_default_trusted():
    assert ext._ID_RE.match(ext.PINNED_EXTENSION_ID)
    assert ext.trusted_extension_origins() == frozenset({PINNED})


def test_pinned_extension_reads_whoami(client):
    r = _whoami(client, PINNED)
    assert r.status_code == 200 and r.json()["username"] == "david"


def test_other_extension_and_web_page_are_refused(client):
    assert _whoami(client, OTHER).status_code == 403
    assert _whoami(client, "https://evil.example").status_code == 403


def test_pinned_extension_mints_a_ticket_for_its_own_origin(client):
    r, http = _handoff(client, PINNED)
    assert r.status_code == 200, r.text
    assert r.json()["ticket"] == "t-1" and r.json()["audience"] == PINNED
    assert http.post.await_args.kwargs["json"]["audience"] == PINNED


def test_other_extension_cannot_mint(client):
    r, http = _handoff(client, OTHER)
    assert r.status_code == 403
    http.post.assert_not_called()


def test_trusted_env_replaces_the_default(client, monkeypatch):
    monkeypatch.setenv("AITHER_TRUSTED_EXTENSION_IDS", OTHER.split("//")[1])
    assert _handoff(client, OTHER)[0].status_code == 200
    assert _handoff(client, PINNED)[0].status_code == 403


def test_trusted_env_ignores_wildcards(monkeypatch):
    monkeypatch.setenv("AITHER_TRUSTED_EXTENSION_IDS", "*,chrome-extension://*, ")
    assert ext.trusted_extension_ids() == frozenset()


def test_components_accepts_the_pinned_extension(client):
    with patch("adk.addon_manager.AddonManager") as mgr:
        mgr.return_value.components_inventory.return_value = []
        assert client.get("/components", headers={"Origin": PINNED}).status_code == 200
        assert client.get("/components", headers={"Origin": OTHER}).status_code == 403


def test_cross_origin_post_is_refused(client):
    r = client.post("/chat", headers={"Origin": "https://evil.example",
                                      "Content-Type": "text/plain"}, content="hi")
    assert r.status_code == 403


def test_allowed_origin_text_plain_chat_is_415(client):
    r = client.post("/chat", headers={"Origin": "https://aitherium.com",
                                      "Content-Type": "text/plain"}, content="hi")
    assert r.status_code == 415
    r = client.post("/cli/execute", headers={"Origin": PINNED,
                                             "Content-Type": "text/plain"}, content="ls")
    assert r.status_code == 415


def test_no_origin_cli_post_is_unchanged(client):
    # A route under the JSON-only pattern that has no handler: without an Origin
    # the request reaches routing (404), neither CSRF answer (403/415) fires.
    path = "/chat/no-such-route-for-csrf-test"
    plain = {"Content-Type": "text/plain"}
    assert client.post(path, content="hi", headers=plain).status_code == 404
    assert client.post(path, json={"m": 1}).status_code == 404
    evil = {"Origin": "https://evil.example"}
    assert client.post(path, json={"m": 1}, headers=evil).status_code == 403


def test_get_with_foreign_origin_is_not_csrf_blocked(client):
    assert client.get("/health", headers={"Origin": "https://evil.example"}).status_code == 200
