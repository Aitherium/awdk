"""The signed-in owner's page drives the lend routes with AITHER_LOCAL_AUTH=required;
everyone else still gets 401.

The page earns a scoped token by handing back an Identity-signed grant over a nonce
this daemon issued (adk/browser_grant.py). Pinned here, on the real app in required
mode:

- no token, a foreign site, a bad signature, another device's grant, an expired
  grant, a replayed nonce, a grant for another origin: 401 on /kvholder/*;
- a good grant from the owner's first-party origin opens /kvholder/* for that
  origin only -- not other routes, not another origin;
- the challenge and pair doors themselves never open anything else.
"""

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from adk import browser_grant, fleet_enroll, node_commands
from adk.agent import AitherAgent
from adk.server import create_app

LOOPBACK = ("127.0.0.1", 50123)
OWNER = "https://desktop.aitherium.com"
NODE = "adk-test-desktop"
KEY = "cd" * 32


def _app(monkeypatch):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS",
              "AITHER_BROWSER_HANDOFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", "required")
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setattr(fleet_enroll, "_load_node_auth",
                        lambda: {"node_id": NODE, "mode": "rich"})
    node_commands.save_key(NODE, KEY)
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    return _app(monkeypatch)


def _grant(nonce, **over):
    g = {"kind": "browser-grant/v1", "node_id": NODE, "tenant_id": "platform",
         "user_id": "owner", "origin": OWNER, "nonce": nonce, "scope": "kvholder",
         "issued_at": int(time.time()), "expires_at": int(time.time()) + 120}
    g.update(over)
    sig_key = over.pop("_key", KEY) if "_key" in over else KEY
    g.pop("_key", None)
    g["sig"] = node_commands.sign(sig_key, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    return g


def _pair(c, grant, origin=OWNER):
    return c.post("/local/browser-pair", json={"grant": grant}, headers={"Origin": origin})


def _nonce(c, origin=OWNER):
    r = c.get("/local/browser-pair/challenge", headers={"Origin": origin})
    assert r.status_code == 200, r.text
    assert r.json()["node_id"] == NODE and r.json()["local_auth"] == "required"
    return r.json()["nonce"]


def test_without_a_grant_the_page_gets_401(daemon):
    r = daemon.get("/kvholder/status", headers={"Origin": OWNER})
    assert r.status_code == 401


def test_the_owners_grant_opens_the_lend_routes_for_that_origin_only(daemon):
    r = _pair(daemon, _grant(_nonce(daemon)))
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    h = {"Origin": OWNER, "Authorization": f"Bearer {tok}"}
    assert daemon.get("/kvholder/status", headers=h).status_code != 401
    # Not another origin, and not any other route.
    # Another first-party page (it would pass the route's own origin guard) does
    # not inherit the token: it is bound to the origin that paired.
    other = {"Origin": "https://other.aitherium.com", "Authorization": f"Bearer {tok}"}
    assert daemon.get("/kvholder/status", headers=other).status_code == 401
    assert daemon.get("/kvholder/status",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert daemon.post("/cli/execute", json={"command": "status"}, headers=h).status_code == 401
    assert daemon.get("/agents", headers=h).status_code == 401


@pytest.mark.parametrize("bad", [
    lambda n: _grant(n, _key="ef" * 32),                       # not this device's key
    lambda n: _grant(n, node_id="someone-elses-box"),          # another device
    lambda n: _grant(n, expires_at=int(time.time()) - 1),      # expired
    lambda n: _grant(n, origin="https://other.aitherium.com"),  # another origin
    lambda n: _grant(n, scope="everything"),                   # a scope that does not exist
    lambda n: _grant("not-a-nonce-this-daemon-issued"),        # a nonce it never issued
    lambda n: {**_grant(n), "user_id": "attacker"},            # changed after signing
])
def test_anything_but_a_good_grant_is_refused_and_opens_nothing(daemon, bad):
    r = _pair(daemon, bad(_nonce(daemon)))
    assert r.status_code == 401
    assert daemon.get("/kvholder/status", headers={"Origin": OWNER}).status_code == 401


def test_a_nonce_works_once(daemon):
    n = _nonce(daemon)
    assert _pair(daemon, _grant(n)).status_code == 200
    assert _pair(daemon, _grant(n)).status_code == 401


def test_the_doors_refuse_a_foreign_site(daemon):
    assert daemon.get("/local/browser-pair/challenge",
                      headers={"Origin": "https://evil.example"}).status_code == 403
    assert _pair(daemon, _grant(_nonce(daemon)), origin="https://evil.example").status_code == 403


def test_a_device_with_no_key_and_no_session_refuses(daemon, monkeypatch, tmp_path):
    (tmp_path / "node_command_key.json").unlink()
    monkeypatch.setattr("adk.server._active_access_token", lambda: "")
    assert _pair(daemon, _grant(_nonce(daemon))).status_code == 401


def test_the_paired_owner_reads_and_toggles_the_workspace_swarm(daemon, monkeypatch, tmp_path):
    import json

    status = tmp_path / "workspace.json"
    grants = tmp_path / "grants.json"
    monkeypatch.setenv("AITHER_KVHOLDER_WORKSPACE_STATUS", str(status))
    monkeypatch.setenv("AITHER_KVHOLDER_GRANTS", str(grants))
    status.write_text(json.dumps({"relay": "wss://kv.aitherium.com", "updated": time.time(),
                                  "grants": {"kvh-fold": {"lend": True}},
                                  "swarm": {"holders": [{"device_id": "kvh-fold"}]}}))
    # Not paired: the swarm is the owner's, 401 like every other gated route.
    assert daemon.get("/kvholder/workspace", headers={"Origin": OWNER}).status_code == 401
    tok = _pair(daemon, _grant(_nonce(daemon))).json()["token"]
    h = {"Origin": OWNER, "Authorization": f"Bearer {tok}"}
    r = daemon.get("/kvholder/workspace", headers=h)
    assert r.status_code == 200 and r.json()["running"] is True
    assert r.json()["swarm"]["holders"][0]["device_id"] == "kvh-fold"
    off = daemon.post("/kvholder/workspace/grant", json={"device_id": "kvh-fold", "lend": False},
                      headers=h)
    assert off.status_code == 200 and off.json()["lend"] is False
    assert json.loads(grants.read_text())["devices"]["kvh-fold"]["lend"] is False
    assert daemon.post("/kvholder/workspace/grant", json={"device_id": "../x", "lend": True},
                       headers=h).status_code == 400
