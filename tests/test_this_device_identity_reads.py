"""The "This device" page can say who is signed in, and whether the node is beating, on a
daemon that runs AITHER_LOCAL_AUTH=required (the default offline).

Measured 2026-11-16 on the owner's PC: the daemon was up, signed in as the owner, and
``GET /identity/whoami`` from the page answered 401, so the page printed "awdk did not say
who is signed in" and "Local: not detected here yet". The fix is a pairing the page already
knows (adk.browser_grant), not a looser gate: the three read-only identity routes ride the
``node`` scope, and nothing that spends the account does.

Also pinned: /node/status reports the heartbeat the device's LEASE HOLDER sends (the daemon
stands by while adk.node_beat beats), instead of "never" on a node that is online.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from adk import browser_grant, device_identity, fleet_enroll, node_commands
from adk.agent import AitherAgent
from adk.server import create_app

LOOPBACK = ("127.0.0.1", 50321)
PAGE = "https://aitherium.com"
NODE = "adk-test-desktop"
KEY = "ab" * 32
PROFILE = {"endpoint": "https://idp.aitherium.com", "token_type": "bearer", "access_token": "x",
           "user": {"username": "david", "display_name": "David", "email": "d@example.com",
                    "tenant_slug": "david"}}


def _app(monkeypatch, mode="required"):
    for k in ("AITHER_OFFLINE", "AITHER_SERVER_API_KEY", "AITHER_CORS_ORIGINS",
              "AITHER_BROWSER_HANDOFF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AITHER_LOCAL_AUTH", mode)
    agent = MagicMock(spec=AitherAgent)
    agent.name = "test-agent"
    agent.llm = MagicMock()
    agent.llm.provider_name = "mock"
    return TestClient(create_app(agent=agent), client=LOOPBACK, raise_server_exceptions=False)


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv(device_identity.DEVICE_FILE_ENV, str(tmp_path / "device.json"))
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    (tmp_path / "node_auth.json").write_text(json.dumps(
        {"node_id": NODE, "mode": "rich", "tenant_id": "t1",
         "enrolled_at": "2026-11-01T00:00:00Z"}))
    node_commands.save_key(NODE, KEY)
    browser_grant._nonces.clear()
    browser_grant._tokens.clear()
    return _app(monkeypatch)


def _page_token(c, scope="node"):
    nonce = c.get("/local/browser-pair/challenge", headers={"Origin": PAGE}).json()["nonce"]
    g = {"kind": "browser-grant/v1", "node_id": NODE, "tenant_id": "platform", "user_id": "owner",
         "origin": PAGE, "nonce": nonce, "scope": scope,
         "issued_at": int(time.time()), "expires_at": int(time.time()) + 120}
    g["sig"] = node_commands.sign(KEY, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    r = c.post("/local/browser-pair", json={"grant": g}, headers={"Origin": PAGE})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(c, scope="node"):
    return {"Origin": PAGE, "Authorization": f"Bearer {_page_token(c, scope)}"}


def _profile():
    store = MagicMock()
    store.get_active_profile.return_value = dict(PROFILE)
    creds = MagicMock()
    creds.is_expired = False
    return (patch("adk.auth.AuthStore", return_value=store),
            patch("adk.auth.resolve_credentials", return_value=creds))


def test_an_unpaired_page_is_refused_on_a_required_daemon(daemon):
    for path in ("/identity/whoami", "/node/status", "/mesh/overlay"):
        assert daemon.get(path, headers={"Origin": PAGE}).status_code == 401, path


def test_a_paired_page_learns_who_is_signed_in_and_what_the_node_is(daemon):
    h = _headers(daemon)
    a, b = _profile()
    with a, b:
        who = daemon.get("/identity/whoami", headers=h)
    assert who.status_code == 200, who.text
    assert who.json()["display_name"] == "David" and who.json()["logged_in"] is True
    st = daemon.get("/node/status", headers=h)
    assert st.status_code == 200 and st.json()["node_id"] == NODE


def test_the_pairing_never_opens_what_spends_the_account(daemon):
    h = _headers(daemon)
    assert daemon.post("/identity/handoff", json={}, headers=h).status_code == 401
    assert daemon.post("/mesh/join", json={}, headers=h).status_code == 401
    assert daemon.get("/agents", headers=h).status_code == 401


def test_a_token_is_bound_to_the_origin_that_paired(daemon):
    tok = _page_token(daemon)
    other = {"Origin": "https://other.aitherium.com", "Authorization": f"Bearer {tok}"}
    assert daemon.get("/identity/whoami", headers=other).status_code == 401
    assert daemon.get("/identity/whoami",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_the_kvholder_scope_does_not_read_identity(daemon):
    assert daemon.get("/identity/whoami", headers=_headers(daemon, "kvholder")).status_code == 401


# --- the heartbeat the lease holder sends is the node's heartbeat -------------------------

def test_claim_lease_keeps_the_last_accepted_beat(tmp_path):
    f = tmp_path / "device.json"
    assert device_identity.claim_lease("node_beat", 60, pid=7, now=1000, path=f)
    device_identity.note_beat_ok("node_beat", pid=7, now=1001, path=f)
    assert device_identity.claim_lease("node_beat", 60, pid=7, now=1060, path=f)
    assert device_identity.load_device(f)["leader"]["ok_at"] == 1001


def test_only_the_holder_can_record_an_accepted_beat(tmp_path):
    f = tmp_path / "device.json"
    device_identity.claim_lease("node_beat", 60, pid=7, now=1000, path=f)
    device_identity.note_beat_ok("daemon", pid=8, now=1005, path=f)
    assert "ok_at" not in device_identity.load_device(f)["leader"]


def test_leader_beat_is_none_when_the_holder_has_gone_quiet(tmp_path):
    f = tmp_path / "device.json"
    device_identity.claim_lease("node_beat", 60, pid=7, now=1000, path=f)
    assert device_identity.leader_beat(now=1100, path=f)["facet"] == "node_beat"
    assert device_identity.leader_beat(now=1000 + 60 * 3 + 1, path=f) is None


def _lease(tmp_path, ok_at, node_id=NODE, lease_node=None):
    """device.json with a fresh lease; ``node_id`` is its TOP-LEVEL id (the last facet that
    recorded), ``lease_node`` the node the holder's accepted beat was for."""
    now = time.time()
    leader = {"facet": "node_beat", "pid": 99, "ts": now, "interval": 60}
    if ok_at is not None:
        leader["ok_at"] = ok_at
    if lease_node:
        leader["node_id"] = lease_node
    (tmp_path / "device.json").write_text(json.dumps({"node_id": node_id, "leader": leader}))


def _heartbeat(c):
    return c.get("/node/status", headers=_headers(c)).json()["heartbeat"]


def test_node_status_reports_the_lease_holders_accepted_beat(daemon, tmp_path):
    _lease(tmp_path, ok_at=time.time() - 20)
    hb = _heartbeat(daemon)
    assert hb["running"] is True and hb["online"] is True
    assert hb["held_by"] == "node_beat" and hb["last_result"] == "ok"
    assert 15 <= hb["age_seconds"] <= 40


def test_a_holder_that_never_recorded_an_accepted_beat_is_not_called_online(daemon, tmp_path):
    _lease(tmp_path, ok_at=None)
    hb = _heartbeat(daemon)
    assert hb["running"] is True and hb["online"] is False and hb["last_result"] == "unconfirmed"


def test_a_stale_accepted_beat_is_not_online(daemon, tmp_path):
    _lease(tmp_path, ok_at=time.time() - 3600)
    hb = _heartbeat(daemon)
    assert hb["online"] is False and hb["last_result"] == "stale"


def test_another_nodes_lease_is_ignored(daemon, tmp_path):
    _lease(tmp_path, ok_at=time.time(), node_id="some-other-node", lease_node="some-other-node")
    hb = _heartbeat(daemon)
    assert hb["running"] is False and hb["last_result"] == "never"


def test_a_device_json_top_level_id_that_differs_from_node_auth_still_counts(daemon, tmp_path):
    """The owner's PC 2026-11-16: device.json said ...-b5efa661 (the last facet to record),
    node_auth.json said ...-e4b01ad2, and a fresh node_beat lease held the beat. The lease is the
    machine's, so /node/status must report it, not 'never'."""
    _lease(tmp_path, ok_at=time.time() - 5, node_id="adk-other-facet")
    hb = _heartbeat(daemon)
    assert hb["running"] is True and hb["online"] is True and hb["held_by"] == "node_beat"


def test_a_lease_that_names_the_node_it_beat_for_matches_by_that_name(daemon, tmp_path):
    _lease(tmp_path, ok_at=time.time() - 5, node_id="adk-other-facet", lease_node=NODE)
    assert _heartbeat(daemon)["online"] is True


def test_note_beat_ok_records_which_node_the_beat_was_for(tmp_path):
    f = tmp_path / "device.json"
    device_identity.claim_lease("node_beat", 60, pid=7, now=1000, path=f)
    device_identity.note_beat_ok("node_beat", pid=7, now=1001, node_id="n-1", path=f)
    device_identity.claim_lease("node_beat", 60, pid=7, now=1060, path=f)  # a renewal keeps it
    lb = device_identity.leader_beat(now=1061, path=f)
    assert lb["node_id"] == "n-1" and "n-1" in lb["known_ids"]
    assert device_identity.beat_is_for(lb, "n-1") and not device_identity.beat_is_for(lb, "n-2")
    assert not device_identity.beat_is_for(None, "n-1")
