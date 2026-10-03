"""Mesh discovery for the KV relay: a holder finds the relay, asks, the owner approves."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from adk import kvholder_mesh as mesh
from adk import kvholder_net as net


def _free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def relay(tmp_path, monkeypatch):
    token = "master-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(tmp_path / "relay.json"))
    net.write_state({"engine_port": ep, "web_port": wp, "token": token, "public": "", "lan": ""})
    yield r, token, wp
    door = getattr(r, "mesh", None)
    if door is not None:
        door.close()
    net.stop_relay(r, servers)


def _get(url: str, headers: dict | None = None) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except ValueError:
            return e.code, {}


def _open(r, wp, admit=None) -> mesh.Door:
    door = mesh.Door(r, wp, admit or [], name="engine-host")
    r.mesh = door
    return door


def test_no_door_no_routes(relay):
    _, _, wp = relay
    assert _get(f"http://127.0.0.1:{wp}/mesh")[0] == 404
    assert _get(f"http://127.0.0.1:{wp}/mesh/request")[0] == 404


def test_holder_waits_owner_approves_holder_attaches_once(relay):
    r, master, wp = relay
    _open(r, wp)
    base = f"http://127.0.0.1:{wp}"
    found = mesh.discover(overlay=False, udp=False, port=wp)
    assert [f["name"] for f in found] == ["engine-host"] and found[0]["base"] == base

    codes, got = [], {}

    def holder():
        got["token"] = mesh.request_join(
            base, "pixel", wait_s=20, poll_s=0.1, on_code=lambda c, s: codes.append((c, s))
        )

    t = threading.Thread(target=holder)
    t.start()
    for _ in range(100):
        if codes:
            break
        time.sleep(0.05)
    code, state = codes[0]
    assert state == "pending"
    rows = mesh.pending()
    assert [(x["code"], x["device"], x["status"]) for x in rows] == [(code, "pixel", "pending")]
    assert "token" not in json.dumps(rows)  # the owner's list never carries a token
    assert mesh.decide(code)["ok"] is True
    t.join(10)
    join = got["token"]
    assert join.startswith("j-") and join != master
    assert r.admit(join) == "join"  # a real join token, single-use
    assert r.admit(join) is None
    assert mesh.pending() == []  # claimed once, then gone


def test_wrong_claim_secret_is_refused(relay):
    r, _, wp = relay
    door = _open(r, wp)
    claim_hash = hashlib.sha256(b"right").hexdigest()
    _, doc = door.request("10.0.0.5", "laptop", claim_hash)
    door.decide(doc["code"], True)
    assert door.claim(doc["id"], "wrong") == (403, {"status": "forbidden"})
    status, out = door.claim(doc["id"], "right")
    assert status == 200 and out["token"].startswith("j-")
    assert door.claim(doc["id"], "right")[0] == 404


def test_denied_request_raises(relay):
    r, _, wp = relay
    _open(r, wp)
    base = f"http://127.0.0.1:{wp}"

    def deny_when_seen(code, _state):
        threading.Timer(0.2, lambda: mesh.decide(code, approve=False)).start()

    with pytest.raises(PermissionError, match="denied"):
        mesh.request_join(base, "x", wait_s=10, poll_s=0.1, on_code=deny_when_seen)


class _Gate:
    """A stand-in for adk.kvholder_workspace.DeviceGate: admits one signed device id."""

    def __init__(self, ok: str):
        self.ok = ok

    def admit(self, hello):
        did = hello.get("device_id")
        return (did, "") if did == self.ok else (None, "not allowed")


def test_admit_network_skips_the_owner_for_an_allowed_signed_device(relay):
    r, _, wp = relay
    _open(r, wp, admit=["127.0.0.1/32"])
    r.device_gate = _Gate("kvh-dgx")
    states = []
    tok = mesh.request_join(
        f"http://127.0.0.1:{wp}",
        "dgx",
        wait_s=5,
        poll_s=0.1,
        on_code=lambda c, s: states.append(s),
        sign=lambda: {"device_id": "kvh-dgx"},
    )
    assert states == ["approved"] and r.admit(tok) == "join"


def test_admit_network_does_not_cover_other_peers(relay):
    r, _, wp = relay
    door = _open(r, wp, admit=["100.64.0.0/10"])
    r.device_gate = _Gate("kvh-tail")
    _, doc = door.request("192.168.1.50", "lan", "a" * 64, {"device_id": "kvh-tail"})
    assert doc["status"] == "pending"
    _, doc = door.request("100.64.0.38", "tailnet", "b" * 64, {"device_id": "kvh-tail"})
    assert doc["status"] == "approved"


def test_admit_network_alone_never_admits(relay):
    """An unsigned or not-allowed device on an admitted network waits for the owner's code."""
    r, _, wp = relay
    door = _open(r, wp, admit=["100.64.0.0/10"])
    _, doc = door.request("100.64.0.38", "no-gate", "c" * 64, {"device_id": "kvh-x"})
    assert doc["status"] == "pending"  # no workspace gate on this relay at all
    r.device_gate = _Gate("kvh-ok")
    _, doc = door.request("100.64.0.38", "unsigned", "d" * 64)
    assert doc["status"] == "pending"
    _, doc = door.request("100.64.0.38", "child", "e" * 64, {"device_id": "fdev_child"})
    assert doc["status"] == "pending"


def test_owner_routes_need_master_token(relay):
    r, master, wp = relay
    _open(r, wp)
    base = f"http://127.0.0.1:{wp}"
    assert _get(f"{base}/mesh/pending")[0] == 403
    assert _get(f"{base}/mesh/pending", {"X-KV-Token": "nope"})[0] == 403
    join, _ = r.mint_join(60)
    assert _get(f"{base}/mesh/approve?code=ABCDEF", {"X-KV-Token": join})[0] == 403
    assert _get(f"{base}/mesh/pending", {"X-KV-Token": master})[0] == 200


def test_request_needs_a_claim_hash_and_is_capped(relay):
    r, _, wp = relay
    door = _open(r, wp)
    assert door.request("1.2.3.4", "x", "not-hex")[0] == 400
    for i in range(mesh.PENDING_MAX):
        assert door.request("1.2.3.4", f"h{i}", "c" * 64)[0] == 200
    assert door.request("1.2.3.4", "one-too-many", "c" * 64)[0] == 429


def test_udp_query_finds_the_relay(relay):
    r, _, wp = relay
    door = _open(r, wp)
    port = _free_port(socket.SOCK_DGRAM)
    assert door.listen_udp(port, host="127.0.0.1")
    found = mesh._udp_query(port, wait_s=1.0, targets=("127.0.0.1",))
    assert found == [("127.0.0.1", wp)]


def test_candidates_order_and_dedupe(monkeypatch):
    monkeypatch.setenv("AITHER_KVHOLDER_PEERS", "spark.local, 10.0.0.2:6000")
    monkeypatch.setattr(mesh, "_tailnet_peers", lambda: ["100.64.0.38", "10.0.0.2"])
    monkeypatch.setattr(mesh, "_wireguard_peers", lambda: ["100.64.0.38"])
    monkeypatch.setattr(mesh, "_udp_query", lambda: [("192.168.1.9", 50063)])
    got = mesh.candidates(["dgx"])
    assert got == [
        ("dgx", 50063, "peer"),
        ("spark.local", 50063, "peer"),
        ("10.0.0.2", 6000, "peer"),
        ("127.0.0.1", 50063, "local"),
        ("192.168.1.9", 50063, "lan"),
        ("100.64.0.38", 50063, "tailnet"),
        ("10.0.0.2", 50063, "tailnet"),
    ]


def test_tailnet_peers_reads_online_ipv4(monkeypatch):
    doc = {
        "Peer": {
            "a": {"Online": True, "TailscaleIPs": ["100.64.0.38", "fd7a::26"]},
            "b": {"Online": False, "TailscaleIPs": ["100.64.0.37"]},
        }
    }

    class R:
        stdout = json.dumps(doc)

    monkeypatch.setattr(mesh.shutil, "which", lambda _: "tailscale")
    monkeypatch.setattr(mesh.subprocess, "run", lambda *a, **k: R())
    assert mesh._tailnet_peers() == ["100.64.0.38"]


def test_status_and_elastic_dry_run_carry_no_secret(relay):
    r, master, wp = relay
    _open(r, wp)
    st = mesh.status()
    assert st["mesh"]["name"] == "engine-host" and master not in json.dumps(st)
    out = mesh.elastic(count=2, dry_run=True)
    assert len({x["holder"] for x in out["runs"]}) == 2
    assert master not in json.dumps(out) and "j-DRYRUN" not in json.dumps(out)


def test_cli_parses_mesh_flags():
    from adk import kvholder_cli

    p = argparse.ArgumentParser()
    kvholder_cli.register(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["kvholder", "serve", "--mesh", "--peer", "spark.local"])
    assert a.mesh and a.peer == ["spark.local"] and not a.connect
    a = p.parse_args(
        ["kvholder", "phone", "--via", "lan", "--mesh", "--mesh-admit", "100.64.0.0/10"]
    )
    assert a.mesh and a.mesh_admit == ["100.64.0.0/10"]
    a = p.parse_args(["kvholder", "mesh", "approve", "K7F2Q9"])
    assert a.mesh_action == "approve" and a.code == "K7F2Q9"


# ---------------------------------------------------------------- daemon routes + awsh tools


def _daemon_app(plan: str, verifier=None):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk.harnesses import kvholder_routes

    class P:
        pass

    p = P()
    p.plan = plan
    app = FastAPI()
    kvholder_routes.mount(app, lambda: p, verifier)
    return TestClient(app)


def _event(text: str, seed: bytes | None = None, to: str = "kvholder") -> dict:
    ev = {
        "id": "e-" + str(time.time_ns()),
        "actor": {"kind": "human", "id": "owner:me"},
        "to": [to],
        "payload": {"text": text},
    }
    if seed is not None:
        from adk.harnesses import owner_steer

        ev["payload"]["owner_assertion"] = owner_steer.sign_owner_assertion(
            seed, event_id=ev["id"], target=to, actor_id="owner:me", text=text
        )
    return ev


def test_daemon_agent_token_alone_cannot_admit(relay):
    r, _, wp = relay
    door = _open(r, wp)
    _, doc = door.request("100.64.0.38", "spark", "d" * 64)
    c = _daemon_app("agent")
    assert c.get("/kvholder/status").json()["mesh"]["pending"][0]["code"] == doc["code"]
    text = json.dumps({"action": "join", "code": doc["code"]})
    res = c.post("/kvholder/join", json=_event(text))
    assert res.status_code == 403 and door.pending()[0]["status"] == "pending"


def test_daemon_owner_assertion_admits_once_and_binds_the_action(relay):
    pytest.importorskip("cryptography")
    from adk.harnesses import owner_steer

    r, _, wp = relay
    door = _open(r, wp)
    _, doc = door.request("100.64.0.38", "spark", "e" * 64)
    seed = bytes(range(32))
    verifier = owner_steer.OwnerAssertionVerifier(owner_steer.public_key_from_seed(seed))
    c = _daemon_app("agent", verifier)
    text = json.dumps({"action": "join", "code": doc["code"]})
    # signed for a session, not for kvholder
    assert c.post("/kvholder/join", json=_event(text, seed, to="sess-1")).status_code == 403
    # signed "join" replayed onto the elastic route
    assert c.post("/kvholder/elastic", json=_event(text, seed)).status_code == 403
    ev = _event(text, seed)
    res = c.post("/kvholder/join", json=ev)
    assert res.status_code == 200 and res.json()["status"] == "approved"
    assert c.post("/kvholder/join", json=ev).status_code == 403  # replay
    other = _event(json.dumps({"action": "join", "code": "ZZZZZZ"}), bytes(32))
    assert c.post("/kvholder/join", json=other).status_code == 403  # a key it does not pin
    assert "token" not in res.text.lower()


def test_daemon_local_owner_needs_no_assertion(relay):
    r, _, wp = relay
    door = _open(r, wp)
    _, doc = door.request("10.0.0.9", "laptop", "f" * 64)
    c = _daemon_app("owner")
    text = json.dumps({"action": "join", "code": doc["code"], "decision": "deny"})
    assert c.post("/kvholder/join", json={"payload": {"text": text}}).json()["status"] == "denied"
    assert c.post("/kvholder/nope", json={"payload": {"text": "{}"}}).status_code == 404


def test_awsh_tools_are_listed_and_hide_tokens(relay):
    from adk.harnesses import mcp_stdio

    r, master, wp = relay
    door = _open(r, wp)
    _, doc = door.request("100.64.0.38", "spark", "a" * 64)
    names = [t["name"] for t in mcp_stdio.TOOLS]
    for n in (
        "awsh_kvholder_status",
        "awsh_kvholder_pending",
        "awsh_kvholder_join",
        "awsh_kvholder_elastic",
    ):
        assert names.count(n) == 1
    out = mcp_stdio.BY_NAME["awsh_kvholder_join"]["fn"]({"code": doc["code"]})
    assert out["status"] == "approved"
    dump = json.dumps(
        [
            mcp_stdio.BY_NAME[n]["fn"]({})
            for n in ("awsh_kvholder_status", "awsh_kvholder_pending", "awsh_kvholder_elastic")
        ]
    )
    assert master not in dump and "j-" not in dump.replace("j-DRYRUN", "")
