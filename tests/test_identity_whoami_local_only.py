"""/identity/whoami must not offer the offline root login for a browser handoff."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from adk.agent import AitherAgent
from adk.auth import ROOT_PROFILE
from adk.server import create_app

ORIGIN = {"Origin": "https://aitherium.com"}


@pytest.fixture
def client():
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    # The handoff guard is loopback-only; TestClient's default peer is "testclient".
    return TestClient(create_app(agent=agent), client=("127.0.0.1", 50000))


def _with_profile(prof):
    store = MagicMock()
    store.get_active_profile.return_value = prof
    creds = MagicMock()
    creds.is_expired = False
    return (
        patch("adk.auth.AuthStore", return_value=store),
        patch("adk.auth.resolve_credentials", return_value=creds),
    )


def _get(client, prof):
    a, b = _with_profile(prof)
    with a, b:
        return client.get("/identity/whoami", headers=ORIGIN)


def test_root_sentinel_is_local_only(client):
    r = _get(client, dict(ROOT_PROFILE))
    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is False
    assert body["handoff"] is False
    assert body["reason"] == "local-only"
    assert "adk login" in body["hint"]
    assert "username" not in body


def test_profile_without_endpoint_or_email_is_local_only(client):
    prof = {"endpoint": "", "token_type": "bearer", "access_token": "x",
            "user": {"username": "someone", "email": ""}}
    assert _get(client, prof).json()["reason"] == "local-only"


def test_linked_identity_is_offered(client):
    prof = {"endpoint": "https://idp.aitherium.com/identity", "token_type": "bearer",
            "access_token": "x",
            "user": {"username": "alice", "display_name": "Alice", "email": "a@example.com",
                     "tenant_slug": "acme"}}
    body = _get(client, prof).json()
    assert body == {"logged_in": True, "handoff": True, "username": "alice",
                    "display_name": "Alice", "tenant_slug": "acme"}


def test_no_profile_keeps_old_shape(client):
    assert _get(client, None).json() == {"logged_in": False, "handoff": True}


def test_handoff_refuses_local_only_without_calling_identity(client):
    a, b = _with_profile(dict(ROOT_PROFILE))
    with a, b, patch("adk.server.httpx.AsyncClient") as ac:
        r = client.post("/identity/handoff", headers=ORIGIN, json={})
    assert r.status_code == 401
    assert "adk login" in r.json()["detail"]
    ac.assert_not_called()
