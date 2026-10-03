"""The workspace relay: holders sign in as workspace devices; the public listener is narrow."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk import kvholder_workspace as kw
from adk.kvholder_page import HOLDER_JS

pytest.importorskip("numpy")  # the Python holder's math
ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _pub_hex(priv) -> str:
    from cryptography.hazmat.primitives import serialization as ser

    return priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()


class _Identity:
    """Stands in for GET /v1/nodes/seal-keys: status + the owner's device keys."""

    def __init__(self):
        self.status, self.keys = 200, {}

    def __call__(self):
        if self.status == 0:
            raise OSError("identity down")
        return self.status, dict(self.keys)


@pytest.fixture()
def ws_relay(tmp_path):
    ep, wp, pp = _free_port(), _free_port(), _free_port()
    master = "master-" + str(time.time_ns())
    r, servers = net.start_relay(
        master, ("127.0.0.1", ep), ("127.0.0.1", wp), public_addr=("127.0.0.1", pp)
    )
    ident = _Identity()
    keys = kw.SealKeys(fetch=ident)
    grants = kw.Grants(tmp_path / "grants.json")
    gate = kw.DeviceGate({"127.0.0.1"}, keys, grants)
    r.device_gate = gate
    fold = ed.Ed25519PrivateKey.generate()
    ident.keys["kvh-fold"] = _pub_hex(fold)
    keys.refresh()
    yield {
        "relay": r,
        "master": master,
        "ep": ep,
        "wp": wp,
        "pp": pp,
        "ident": ident,
        "keys": keys,
        "grants": grants,
        "gate": gate,
        "fold": fold,
    }
    net.stop_relay(r, servers)


def _dial(env, priv, device_id="kvh-fold", relay_id="127.0.0.1", once=True, hello=None):
    h = kv.KVHolder(8 << 20, device="pytest-" + device_id)
    sign = hello or (lambda: kw.sign_hello(priv, relay_id, device_id))
    url = f"ws://127.0.0.1:{env['pp']}/holder"
    if once:
        return net.dial_holder(url, "", h, once=True, sign=sign)
    t = threading.Thread(
        target=net.dial_holder, args=(url, "", h), kwargs={"sign": sign}, daemon=True
    )
    t.start()
    return t


def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_allowed_device_attaches_over_the_public_listener(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True, max_mb=64)
    _dial(env, env["fold"], once=False)
    assert _wait(lambda: env["relay"].holder is not None)
    assert env["relay"].holders[0].session == "dev:kvh-fold"
    assert env["relay"].holders[0].max_bytes == 8 << 20  # lends 8 MB, under the 64 MB cap
    snap = kw.snapshot(env["relay"], env["gate"], "wss://kv.test/holder")
    assert snap["swarm"]["holders"][0]["device_id"] == "kvh-fold"
    assert env["master"] not in json.dumps(snap)  # never a token in what awsh/awdesk read


def test_owner_cap_bounds_what_a_device_lends(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True, max_mb=4)
    _dial(env, env["fold"], once=False)  # offers 8 MB
    assert _wait(lambda: env["relay"].holder is not None)
    assert env["relay"].holders[0].max_bytes == 4 << 20


def test_device_not_allowed_is_refused_and_listed_pending(ws_relay):
    env = ws_relay
    assert _dial(env, env["fold"]) == 1
    assert env["relay"].holder is None
    env["grants"].reload()
    assert env["grants"].devices["kvh-fold"]["lend"] is False  # the owner sees it asked


def test_unknown_key_wrong_relay_and_old_hello_are_refused(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True)
    stranger = ed.Ed25519PrivateKey.generate()
    assert _dial(env, stranger) == 1  # same id, a key the workspace does not list
    assert _dial(env, env["fold"], device_id="kvh-other") == 1  # not enrolled
    assert _dial(env, env["fold"], relay_id="evil.example") == 1  # signed for another relay
    old = kw.sign_hello(env["fold"], "127.0.0.1", "kvh-fold")
    old["ts"] -= 10 * 60
    assert _dial(env, None, hello=lambda: old) == 1
    assert env["relay"].holder is None


def test_a_captured_hello_cannot_be_replayed(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True)
    hello = kw.sign_hello(env["fold"], "127.0.0.1", "kvh-fold")
    ok, why = env["gate"].admit(dict(hello))
    assert ok == "kvh-fold", why
    assert env["gate"].admit(dict(hello)) == (None, "replayed hello")


def test_public_listener_serves_only_the_holder(ws_relay):
    env = ws_relay
    base = f"http://127.0.0.1:{env['pp']}"
    assert b"holder.js" in urllib.request.urlopen(base + "/", timeout=5).read()
    for asset in ("/holder.js", "/state.js"):  # everything the page loads
        assert urllib.request.urlopen(base + asset, timeout=5).status == 200
    for route in ("/status", "/swarm", "/join", "/swarm/join"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + route, timeout=5)
        assert e.value.code == 404, route
    # the loopback listener still answers /status for local tools
    local = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{env['wp']}/status").read())
    assert local["attached"] is False


def test_master_token_is_refused_on_the_public_listener(ws_relay):
    env = ws_relay
    h = kv.KVHolder(1 << 20)
    assert net.dial_holder(f"ws://127.0.0.1:{env['pp']}/holder", env["master"], h, once=True) == 1
    assert env["relay"].holder is None


def test_deny_and_workspace_removal_revoke_an_attached_device(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True)
    _dial(env, env["fold"], once=False)
    assert _wait(lambda: env["relay"].holder is not None)
    env["ident"].status = 0  # identity outage: nothing is revoked on a blink
    env["keys"].refresh()
    assert kw.sweep(env["relay"], env["gate"]) == []
    env["grants"].set("kvh-fold", False)  # the owner denies it
    assert kw.sweep(env["relay"], env["gate"]) == ["kvh-fold"]
    assert env["relay"].holder is None


def test_removed_from_workspace_is_revoked(ws_relay):
    env = ws_relay
    env["grants"].set("kvh-fold", True)
    _dial(env, env["fold"], once=False)
    assert _wait(lambda: env["relay"].holder is not None)
    env["ident"].keys.clear()  # adk devices rm kvh-fold
    env["keys"].refresh()
    assert kw.sweep(env["relay"], env["gate"]) == ["kvh-fold"]
    assert env["relay"].holder is None


def test_stale_keys_stop_new_sign_ins(ws_relay, monkeypatch):
    env = ws_relay
    env["grants"].set("kvh-fold", True)
    monkeypatch.setattr(kw, "KEYS_STALE_S", -1.0)
    ok, why = env["gate"].admit(kw.sign_hello(env["fold"], "127.0.0.1", "kvh-fold"))
    assert ok is None and "not a device" in why


def test_pair_link_carries_no_relay_secret():
    link = kw.pair_link(
        "wss://kv.aitherium.com/holder", "https://idp.aitherium.com", "ABC234", "kvh-fold", 2048
    )
    assert link.startswith("aitherkv://pair?") and "c=ABC234" in link and "d=kvh-fold" in link


NODE = shutil.which("node")
APP = Path(__file__).resolve().parents[1] / "android" / "kvholder" / "assets"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_app_device_key_signs_in_and_holder_js_attaches(ws_relay, tmp_path):
    """The Android app's engine: kvdevice.js (WebCrypto Ed25519) signs, holder.js dials in."""
    env = ws_relay
    env["grants"].set("kvh-node", True)
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    shutil.copy2(APP / "kvdevice.js", tmp_path / "kvdevice.js")
    script = tmp_path / "run.js"
    script.write_text(
        "const K = require('./holder.js'), D = require('./kvdevice.js');\n"
        "const m = new Map();\n"
        "const store = {get: async (k) => m.get(k), put: async (k, v) => m.set(k, v)};\n"
        "(async () => {\n"
        "  const dev = await D.open(store, globalThis.crypto.subtle);\n"
        "  console.log('PUB ' + dev.publicHex);\n"
        "  await new Promise((r) => process.stdin.once('data', r));\n"
        "  const h = new K.Holder(new K.CpuEngine(), 16 * 1048576, 'kvh-node');\n"
        f"  K.connect('ws://127.0.0.1:{env['pp']}/holder', '', h, {{\n"
        "    hello: () => dev.hello('127.0.0.1', 'kvh-node'),\n"
        "    attached: () => console.log('ATTACHED'),\n"
        "    error: (e) => { console.log('ERR ' + e); process.exit(3); }});\n"
        "  setTimeout(() => process.exit(0), 60000);\n"
        "})();\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [NODE, str(script)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("PUB "), line
        env["ident"].keys["kvh-node"] = line[4:]  # the pairing put this key in the workspace
        env["keys"].refresh()
        proc.stdin.write("go\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "ATTACHED"
        assert _wait(lambda: env["relay"].holder is not None)
        assert env["relay"].holders[0].session == "dev:kvh-node"
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_seal_keys_fall_back_to_per_device_reads_when_the_search_fails(monkeypatch):
    from adk import devices

    pub = "ab" * 32
    calls = []

    def fake(method, path="", **kw):
        calls.append(path)
        if path == "/seal-keys":
            return 503, "directory search starved", None, ""
        if path == "/kvh-fold":
            return 200, "", {"node_id": "kvh-fold", "seal_pubkey": pub}, ""
        return 404, "", None, ""

    monkeypatch.setattr(devices, "request_nodes", fake)
    assert kw.fetch_seal_keys(lambda: ["kvh-fold", "kvh-gone"]) == (200, {"kvh-fold": pub})
    assert calls == ["/seal-keys", "/kvh-fold", "/kvh-gone"]
    # an outage on any read is an outage, never a shorter authoritative list
    monkeypatch.setattr(
        devices,
        "request_nodes",
        lambda m, p="", **k: (503, "", None, "") if p != "/kvh-fold" else (200, "", {}, ""),
    )
    assert kw.fetch_seal_keys(lambda: ["kvh-fold"])[0] != 200
    assert kw.fetch_seal_keys(None)[0] == 503  # nothing allowed: no per-device reads


def test_awsh_tools_read_the_swarm_and_can_only_revoke(tmp_path, monkeypatch):
    from adk.harnesses import kvholder_tools as kt

    monkeypatch.setenv("AITHER_KVHOLDER_GRANTS", str(tmp_path / "grants.json"))
    monkeypatch.setenv("AITHER_KVHOLDER_WORKSPACE_STATUS", str(tmp_path / "ws.json"))
    tools = {t["name"]: t["fn"] for t in kt.TOOLS}
    assert "error" in tools["awsh_kvholder_workspace"]({})  # relay not running: says so
    (tmp_path / "ws.json").write_text(json.dumps({"updated": time.time(), "swarm": {}}))
    assert tools["awsh_kvholder_workspace"]({})["stale"] is False
    kw.Grants().set("kvh-fold", True)
    assert tools["awsh_kvholder_workspace_deny"]({"device_id": "kvh-fold"})["lend"] is False
    assert kw.Grants().allowed("kvh-fold") is False
    assert not any("allow" in n for n in tools)  # lending is switched on by the owner only


def _household(env, rows, status=200):
    fake = {"status": status, "rows": rows}
    hh = kw.Household(fetch=lambda: (fake["status"], dict(fake["rows"])))
    hh.refresh()
    env["gate"].household = hh
    return fake, hh


def _child(env, kv_lend, revoked=False):
    child = ed.Ed25519PrivateKey.generate()
    env["ident"].keys["fdev_kid"] = _pub_hex(child)
    env["keys"].refresh()
    row = {"device_id": "fdev_kid", "profile_kind": "child", "kv_lend": kv_lend, "revoked": revoked}
    return child, row


def test_child_device_lends_only_when_the_owner_switched_kv_lend_on(ws_relay):
    env = ws_relay
    child, row = _child(env, kv_lend=False)
    env["grants"].set("fdev_kid", True)  # a local allow does NOT override the household switch
    fake, hh = _household(env, {"fdev_kid": row})
    hello = lambda: kw.sign_hello(child, "127.0.0.1", "fdev_kid")  # noqa: E731
    ok, why = env["gate"].admit(hello())
    assert ok is None and "child" in why
    fake["rows"]["fdev_kid"] = {**row, "kv_lend": True}
    hh.refresh()
    assert env["gate"].admit(hello())[0] == "fdev_kid"
    fake["rows"]["fdev_kid"] = {**row, "kv_lend": True, "revoked": True}
    hh.refresh()
    assert env["gate"].admit(hello())[0] is None


def test_household_switch_off_revokes_an_attached_child(ws_relay):
    env = ws_relay
    child, row = _child(env, kv_lend=True)
    fake, hh = _household(env, {"fdev_kid": row})
    _dial(env, child, device_id="fdev_kid", once=False)
    assert _wait(lambda: env["relay"].holder is not None)
    fake["status"] = 503  # registry outage: nothing is revoked
    hh.refresh()
    assert kw.sweep(env["relay"], env["gate"]) == []
    fake["status"], fake["rows"] = 200, {"fdev_kid": {**row, "kv_lend": False}}
    hh.refresh()
    assert kw.sweep(env["relay"], env["gate"]) == ["fdev_kid"]
    assert env["relay"].holder is None


def test_household_device_is_refused_while_the_registry_cannot_vouch(ws_relay, monkeypatch):
    env = ws_relay
    child, row = _child(env, kv_lend=True)
    _household(env, {"fdev_kid": row})
    monkeypatch.setattr(kw, "KEYS_STALE_S", -1.0)  # registry answer too old
    ok, why = env["gate"].may_lend("fdev_kid")
    assert not ok and "unreachable" in why


def test_mesh_admit_network_refuses_a_child_unless_the_owner_enabled_it(ws_relay):
    """The --mesh-admit path applies the same owner decision as the public listener."""
    from adk import kvholder_mesh as mesh

    env = ws_relay
    child, row = _child(env, kv_lend=False)
    fake, hh = _household(env, {"fdev_kid": row})
    door = mesh.Door(env["relay"], env["wp"], ["127.0.0.0/8"], name="t")
    try:
        signed = kw.sign_hello(child, "127.0.0.1", "fdev_kid")
        assert door.request("127.0.0.1", "kid", "a" * 64, signed)[1]["status"] == "pending"
        fake["rows"]["fdev_kid"] = {**row, "kv_lend": True}
        hh.refresh()
        signed = kw.sign_hello(child, "127.0.0.1", "fdev_kid")
        assert door.request("127.0.0.1", "kid", "b" * 64, signed)[1]["status"] == "approved"
    finally:
        door.close()
