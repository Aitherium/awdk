"""Awconnect pairing: POST /pair/start -> owner approval -> POST /pair/poll.

The extension gets a scoped registry token, never the root bearer; only the
exact pinned extension origin on a loopback Host may pair; the token cannot
drive a coding session.
"""

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

PINNED = "chrome-extension://hlmfknhcfhjjngckfpacgleffckpmphe"
OTHER = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
ROOT = "root-token-for-pairing-tests"
LOOP = {"host": "127.0.0.1:8362"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", ROOT)
    monkeypatch.delenv("AITHER_TRUSTED_EXTENSION_IDS", raising=False)
    import adk.decisions.store as store_mod
    import adk.harnesses.daemon as daemon

    if hasattr(store_mod, "_STORE"):
        monkeypatch.setattr(store_mod, "_STORE", None, raising=False)
    reg = tmp_path / "harness_tokens.json"
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", reg)
    try:
        import adk.decisions.notify as notify_mod

        monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: None)
    except ImportError:
        pass
    app = daemon.create_app(token=ROOT)
    return TestClient(app), reg


def _start(client, origin=PINNED, host=LOOP):
    return client.post("/pair/start", headers={"origin": origin, **host})


def _poll(client, pair_id, origin=PINNED):
    return client.post("/pair/poll", json={"pair_id": pair_id},
                       headers={"origin": origin, **LOOP})


def _owner(client, path, body=None):
    return client.post(path, json=body or {}, headers={"Authorization": f"Bearer {ROOT}"})


def test_happy_path_issues_a_hashed_labelled_token_once(env):
    client, reg = env
    r = _start(client)
    assert r.status_code == 200, r.text
    started = r.json()
    assert len(started["code"]) == 6 and started["code"].isdigit()
    assert started["expires_in"] == 120

    assert _poll(client, started["pair_id"]).json()["status"] == "pending"
    assert _owner(client, "/pair/approve", {"code": started["code"]}).status_code == 200

    done = _poll(client, started["pair_id"])
    assert done.status_code == 200, done.text
    token = done.json()["token"]
    assert token and token != ROOT

    stored = json.loads(reg.read_text(encoding="utf-8"))
    assert token not in reg.read_text(encoding="utf-8")        # hashed, never plaintext
    (entry,) = stored.values()
    assert entry["label"] == "awconnect"

    # the token reads decisions and sessions
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/decisions", headers=auth).status_code == 200
    assert client.get("/sessions", headers=auth).status_code == 200
    # issued exactly once
    assert _poll(client, started["pair_id"]).status_code == 410

    # revocable
    assert _owner(client, "/pair/revoke").json()["revoked"] == 1
    assert client.get("/decisions", headers=auth).status_code == 403


def test_scoped_token_cannot_drive_a_session(env):
    client, _ = env
    started = _start(client).json()
    _owner(client, "/pair/approve", {"code": started["code"]})
    token = _poll(client, started["pair_id"]).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    for path in ("/sessions/abc/input", "/sessions/abc/submit", "/sessions/abc/message"):
        r = client.post(path, json={"text": "rm -rf /"}, headers=auth)
        assert r.status_code == 403, (path, r.status_code)
    assert client.post("/sessions", json={}, headers=auth).status_code == 403
    assert client.delete("/sessions/abc", headers=auth).status_code == 403
    assert client.post("/pair/approve", json={"code": "000000"}, headers=auth).status_code == 403
    assert client.post("/pair/revoke", headers=auth).status_code == 403
    assert client.get("/fs/list", headers=auth).status_code == 403
    write = client.post("/fs/write", json={"path": "x", "content": "x"}, headers=auth)
    assert write.status_code == 403
    assert client.get("/git/status", headers=auth).status_code == 403
    assert client.get("/git/diff", params={"path": "x"}, headers=auth).status_code == 403


def test_expired_code_cannot_be_approved_or_polled(env, monkeypatch):
    client, _ = env
    started = _start(client).json()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 121)
    assert _owner(client, "/pair/approve", {"code": started["code"]}).status_code == 404
    assert _poll(client, started["pair_id"]).status_code == 410


def test_wrong_origin_missing_origin_and_non_loopback_host_are_refused(env):
    client, _ = env
    assert _start(client, origin=OTHER).status_code == 403
    assert client.post("/pair/start", headers=LOOP).status_code == 403
    assert _start(client, origin="https://aitherium.com").status_code == 403
    assert _start(client, host={"host": "evil.example:8362"}).status_code == 403
    started = _start(client).json()
    assert _poll(client, started["pair_id"], origin=OTHER).status_code == 403


def test_wildcard_env_cannot_widen_the_trusted_set(env, monkeypatch):
    client, _ = env
    monkeypatch.setenv("AITHER_TRUSTED_EXTENSION_IDS", "*,chrome-extension://*")
    assert _start(client).status_code == 403
    assert _start(client, origin=OTHER).status_code == 403


def test_pairing_card_is_owner_only_and_approves(env):
    client, _ = env
    first = _start(client).json()
    _owner(client, "/pair/approve", {"code": first["code"]})
    token = _poll(client, first["pair_id"]).json()["token"]

    second = _start(client).json()
    cards = client.get("/decisions", headers={"Authorization": f"Bearer {ROOT}"}).json()
    rows = cards.get("decisions", cards) if isinstance(cards, dict) else cards
    card = next(c for c in rows if c.get("dedupe_key") == f"awconnect-pair:{second['pair_id']}")
    assert second["code"] in card["summary"]

    # a paired extension cannot approve another pairing through the card
    r = client.post(f"/decisions/{card['id']}/answer", json={"choice": "allow"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    r = client.post(f"/decisions/{card['id']}/answer", json={"choice": "allow"},
                    headers={"Authorization": f"Bearer {ROOT}"})
    assert r.status_code == 200, r.text
    assert _poll(client, second["pair_id"]).json()["status"] == "approved"


def test_method_scoped_path_entries_fail_closed():
    from adk.harnesses.daemon import Principal

    p = Principal("awconnect:x", paths=("/decisions", "GET /sessions"))
    assert p.may_reach("/sessions/abc/stream", "GET")
    assert p.may_reach("/sessions", "HEAD")
    assert not p.may_reach("/sessions/abc/input", "POST")
    assert not p.may_reach("/sessions", None)           # no method known -> refuse
    assert p.may_reach("/decisions/d-2345/answer", "POST")


# --- one step: the signed-in extension pairs with an Identity grant -----------------

KEY_HEX = "ab" * 32


def _signed_grant(nonce, origin=PINNED, node="pc-1", scope="extension", key=KEY_HEX):
    from adk import browser_grant, node_commands

    t = int(time.time())
    g = {"kind": "browser-grant/v1", "node_id": node, "tenant_id": "t", "user_id": "u",
         "origin": origin, "nonce": nonce, "scope": scope, "issued_at": t,
         "expires_at": t + 120}
    g["sig"] = node_commands.sign(key, node_commands.canonical(g, browser_grant.GRANT_FIELDS))
    return g


@pytest.fixture
def enrolled(env, monkeypatch):
    import adk.fleet_enroll as fe
    from adk import node_commands

    monkeypatch.setattr(fe, "_load_node_auth", lambda: {"node_id": "pc-1"})
    monkeypatch.setattr(node_commands, "load_key", lambda n: KEY_HEX if n == "pc-1" else "")
    return env


def test_a_signed_in_extension_pairs_with_the_harness_in_one_step(enrolled):
    client, reg = enrolled
    ch = client.get("/pair/challenge", headers={"origin": PINNED, **LOOP})
    assert ch.status_code == 200, ch.text
    r = client.post("/pair/grant", json={"grant": _signed_grant(ch.json()["nonce"])},
                    headers={"origin": PINNED, **LOOP})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    # The scoped token reads decisions like a card-approved pairing would.
    assert client.get("/decisions", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_the_harness_refuses_a_forged_or_foreign_grant(enrolled):
    client, _ = enrolled
    for kw in ({"key": "cd" * 32}, {"node": "other"}, {"scope": "node"}):
        nonce = client.get("/pair/challenge", headers={"origin": PINNED, **LOOP}).json()["nonce"]
        r = client.post("/pair/grant", json={"grant": _signed_grant(nonce, **kw)},
                        headers={"origin": PINNED, **LOOP})
        assert r.status_code == 401, kw
    assert client.get("/pair/challenge", headers={"origin": OTHER, **LOOP}).status_code == 403
