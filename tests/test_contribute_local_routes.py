"""The owner's settings page flips data-contribution consent through the daemon.

On the real app with AITHER_LOCAL_AUTH=required:
- no token / a foreign origin: 401 or 403 on /local/contribute*;
- a ``contribute``-scope browser token opens /local/contribute* for its origin,
  and nothing else (not /kvholder/, not /v1/models);
- a ``kvholder`` token does not open /local/contribute;
- toggling writes the same consent record ``adk contribute`` reads, and the
  preview carries no planted value.
"""

import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from adk import browser_grant, contribute, fleet_enroll, node_commands
from adk.agent import AitherAgent
from adk.server import create_app

LOOPBACK = ("127.0.0.1", 50124)
OWNER = "https://desktop.aitherium.com"
NODE = "adk-test-desktop"
KEY = "ab" * 32
PLANT = "gh" + "p_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Jh1Gf0EdCbAz"


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS",
              "AITHER_BROWSER_HANDOFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "required")
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_CONTRIBUTE_HOME", str(tmp_path / "contribute"))
    monkeypatch.setenv("AWMINE_OUT", str(tmp_path / "awmine"))
    monkeypatch.setattr(fleet_enroll, "_load_node_auth",
                        lambda: {"node_id": NODE, "mode": "rich"})
    node_commands.save_key(NODE, KEY)
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)


def _token(c, scope, origin=OWNER):
    r = c.get("/local/browser-pair/challenge", headers={"Origin": origin})
    assert r.status_code == 200, r.text
    g = {"kind": "browser-grant/v1", "node_id": NODE, "tenant_id": "platform",
         "user_id": "owner", "origin": origin, "nonce": r.json()["nonce"], "scope": scope,
         "issued_at": int(time.time()), "expires_at": int(time.time()) + 120}
    g["sig"] = node_commands.sign(KEY, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    r = c.post("/local/browser-pair", json={"grant": g}, headers={"Origin": origin})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok, origin=OWNER):
    return {"Origin": origin, "Authorization": f"Bearer {tok}"}


def test_unpaired_page_is_refused(daemon):
    assert daemon.get("/local/contribute", headers={"Origin": OWNER}).status_code == 401
    r = daemon.post("/local/contribute", json={"on": True}, headers={"Origin": OWNER})
    assert r.status_code == 401


def test_kvholder_token_does_not_open_contribute(daemon):
    tok = _token(daemon, "kvholder")
    assert daemon.get("/local/contribute", headers=_h(tok)).status_code == 401


def test_contribute_token_opens_only_its_routes(daemon):
    tok = _token(daemon, "contribute")
    r = daemon.get("/local/contribute", headers=_h(tok))
    assert r.status_code == 200
    assert r.json()["categories"] == {c: False for c in contribute.CATEGORIES}
    assert r.json()["uploads_open"] is (contribute.TERMS_VERSION is not None)
    assert daemon.get("/kvholder/status", headers=_h(tok)).status_code == 401
    assert daemon.get("/v1/models", headers=_h(tok)).status_code == 401
    assert daemon.get("/local/contribute",
                      headers=_h(tok, "https://evil.example")).status_code == 401


def test_toggle_writes_the_cli_consent_record(daemon):
    tok = _token(daemon, "contribute")
    r = daemon.post("/local/contribute", json={"on": True, "categories": ["skills"]},
                    headers=_h(tok))
    assert r.status_code == 200 and r.json()["categories"]["skills"] is True
    assert contribute.load_consent()["categories"]["skills"] is True
    r = daemon.post("/local/contribute", json={"on": True, "categories": ["browser_history"]},
                    headers=_h(tok))
    assert r.status_code == 400
    r = daemon.post("/local/contribute", json={"on": "yes"}, headers=_h(tok))
    assert r.status_code == 400
    r = daemon.post("/local/contribute/delete", headers=_h(tok))
    assert r.status_code == 200
    assert not any(contribute.load_consent()["categories"].values())


def test_preview_is_redacted(daemon, tmp_path):
    skill = tmp_path / "contribute" / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("use token " + PLANT + " carefully", encoding="utf-8")
    tok = _token(daemon, "contribute")
    r = daemon.get("/local/contribute/preview?category=skills", headers=_h(tok))
    assert r.status_code == 200, r.text
    assert PLANT not in json.dumps(r.json())
    assert r.json()["categories"] == ["skills"]


def test_page_can_choose_a_licence_tier(daemon):
    tok = _token(daemon, "contribute")
    r = daemon.post("/local/contribute",
                    json={"on": True, "categories": ["skills"], "licence": "cc-by-4.0",
                          "credit_name": "Ada"}, headers=_h(tok))
    assert r.status_code == 200, r.text
    assert r.json()["licences"]["skills"] == "cc-by-4.0"
    s = daemon.get("/local/contribute", headers=_h(tok)).json()
    assert s["licences"]["skills"] == "cc-by-4.0" and s["credit_name"] == "Ada"
    assert set(s["licence_options"]) == set(contribute.LICENCES)
    r = daemon.post("/local/contribute", json={"on": True, "licence": "mit"}, headers=_h(tok))
    assert r.status_code == 400
