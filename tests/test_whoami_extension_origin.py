"""/identity/whoami accepts ONLY the allowlisted Awconnect extension origin."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from adk import extension_id as ext
from adk.agent import AitherAgent
from adk.server import create_app

OURS = "hhnjemffdkpakbnimmkkcjagddakagcd"
OTHER = "abcdefghijklmnopabcdefghijklmnop"
PROFILE = {"endpoint": "https://idp.aitherium.com/identity", "token_type": "bearer",
           "access_token": "x",
           "user": {"username": "alice", "display_name": "Alice", "email": "a@example.com"}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "allowlist_path", lambda: tmp_path / "allowed_extension_ids")
    monkeypatch.delenv("AITHER_EXTENSION_IDS", raising=False)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=("127.0.0.1", 50000))


def _whoami(client, origin, path="/identity/whoami"):
    store = MagicMock()
    store.get_active_profile.return_value = dict(PROFILE)
    creds = MagicMock(is_expired=False)
    with patch("adk.auth.AuthStore", return_value=store), \
            patch("adk.auth.resolve_credentials", return_value=creds):
        return client.get(path, headers={"Origin": origin})


def test_unpacked_id_matches_chrome_on_windows():
    # Measured: Chrome showed this id for the folder the owner loaded on 2026-09-27.
    assert ext.unpacked_extension_id("E:\\aw-build\\awconnect-ext", windows=True) == OURS


def test_unknown_extension_is_refused(client):
    assert _whoami(client, f"chrome-extension://{OURS}").status_code == 403


def test_allowlisted_extension_reads_the_name(client):
    ext.allow_extension_id(OURS)
    r = _whoami(client, f"chrome-extension://{OURS}")
    assert r.status_code == 200 and r.json()["username"] == "alice"
    assert _whoami(client, f"chrome-extension://{OTHER}").status_code == 403


def test_env_allowlist_for_a_store_build(client, monkeypatch):
    monkeypatch.setenv("AITHER_EXTENSION_IDS", OTHER)
    assert _whoami(client, f"chrome-extension://{OTHER}").status_code == 200


def test_extension_cannot_mint_a_handoff_ticket(client):
    ext.allow_extension_id(OURS)
    with patch("adk.server.httpx.AsyncClient") as ac:
        r = client.post("/identity/handoff", headers={"Origin": f"chrome-extension://{OURS}"},
                        json={})
    assert r.status_code == 403
    ac.assert_not_called()


def test_malformed_id_is_rejected():
    with pytest.raises(ValueError):
        ext.allow_extension_id("../../etc")
