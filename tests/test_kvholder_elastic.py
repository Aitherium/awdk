"""Elastic KV: many holders behind one relay, each a key range, merged exactly."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk.kvholder_page import HOLDER_JS

H, LAYERS = 2, 2
ROW = H * kv.HD * 2  # f16 row bytes
PER_KEY = 2 * ROW * LAYERS  # K+V on every layer
CAP = 512  # keys per holder: budget below


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def relay():
    token = "el-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


def _add_holder(r, token, wp, name, keys=CAP, store="wire"):
    before = len(r.holders)
    h = kv.KVHolder(keys * PER_KEY, device=name, store=store)
    url = f"ws://127.0.0.1:{wp}/holder"
    threading.Thread(target=net.dial_holder, args=(url, token, h), daemon=True).start()
    end = time.time() + 10
    while len(r.holders) <= before and time.time() < end:
        time.sleep(0.05)
    assert len(r.holders) > before, f"{name} never attached"
    return h


def _ref(q, keys, vals, scale):
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    p = np.exp(s - s.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", p / p.sum(-1, keepdims=True), vals.astype(np.float64))


class Engine:
    """The host side: its own newest keys + whatever the relay holds, as Backburner merges them."""

    def __init__(self, ep, seed=0):
        self.c = kv.KVHolderClient("127.0.0.1", ep, timeout=60)
        self.c.configure(kv.Config.for_model(LAYERS, H, "f16"))
        self.rng = np.random.default_rng(seed)
        self.k = [np.zeros((0, H, kv.HD), np.float32) for _ in range(LAYERS)]
        self.v = [np.zeros((0, H, kv.HD), np.float32) for _ in range(LAYERS)]

    def push(self, n, step=300):
        for layer in range(LAYERS):
            k = self.rng.standard_normal((n, H, kv.HD)).astype(np.float16).astype(np.float32)
            v = self.rng.standard_normal((n, H, kv.HD)).astype(np.float16).astype(np.float32)
            base = len(self.k[layer])
            for s in range(0, n, step):
                assert self.c.append(
                    layer, base + s, k[s : s + step], v[s : s + step]
                ) == base + min(s + step, n)
            self.k[layer] = np.concatenate([self.k[layer], k])
            self.v[layer] = np.concatenate([self.v[layer], v])

    def truncate(self, n):
        self.c.truncate(n)
        self.k = [x[:n] for x in self.k]
        self.v = [x[:n] for x in self.v]

    def check(self, n_tok=8):
        for layer in range(LAYERS):
            q = self.rng.standard_normal((H, kv.NR, kv.HD)).astype(np.float32)
            o, lse, meta = self.c.attn(layer, q, kv.HD**-0.5, n_tok=n_tok)
            assert meta["nk"] == len(self.k[layer])
            ref = _ref(q, self.k[layer], self.v[layer], kv.HD**-0.5)
            rows = [i for i in range(kv.NR) if i % 8 < n_tok]
            np.testing.assert_allclose(o[:, rows], ref[:, rows], atol=3e-3)


def test_three_holders_split_the_context_and_merge_exactly(relay):
    r, token, ep, wp = relay
    for name in ("a", "b", "c"):
        _add_holder(r, token, wp, name)
    e = Engine(ep)
    e.push(1300)  # 512 + 512 + 276: every APPEND chunk of 300 crosses a holder boundary somewhere
    assert [h["off"] for h in r.status()["holders"]] == [0, CAP, 2 * CAP]
    e.check(n_tok=8)
    e.check(n_tok=1)
    assert "relay holders=3 held=1300" in e.c.stats()


def test_a_holder_that_joins_mid_session_adds_capacity(relay):
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "first")
    e = Engine(ep, seed=1)
    e.push(400)
    with pytest.raises(RuntimeError, match="out of memory"):
        e.c.append(
            0, 400, np.zeros((200, H, kv.HD), np.float32), np.zeros((200, H, kv.HD), np.float32)
        )
    _add_holder(r, token, wp, "second")  # an on-demand holder arrives
    e.push(400)
    assert r.status()["holders"][1]["off"] == CAP
    e.check()


def test_truncate_across_the_boundary_then_regrow(relay):
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "a")
    _add_holder(r, token, wp, "b")
    e = Engine(ep, seed=2)
    e.push(900)
    e.truncate(600)
    e.check()
    e.push(250)
    e.check()


def test_losing_a_holder_with_keys_fails_loudly_until_reconfig(relay):
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "a")
    _add_holder(r, token, wp, "b")
    e = Engine(ep, seed=3)
    e.push(700)  # b holds [512, 700)
    victim = r.holders[1]
    victim.ws.sock.close()
    q = np.zeros((H, kv.NR, kv.HD), np.float32)
    with pytest.raises(RuntimeError, match="holder lost"):
        e.c.attn(0, q, 0.1)
    with pytest.raises(RuntimeError, match="holder lost: b held keys"):
        e.c.attn(0, q, 0.1)  # still failing: never a partial answer
    end = time.time() + 15
    while len(r.holders) < 2 and time.time() < end:  # b's dial loop comes back by itself
        time.sleep(0.1)
    e2 = Engine(ep, seed=4)  # the engine recomputes: CONFIG clears the broken state
    e2.push(700)
    e2.check()


def test_full_means_out_of_memory_not_a_crash(relay):
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "only", keys=256)
    e = Engine(ep, seed=5)
    with pytest.raises(RuntimeError, match="out of memory at 256 keys"):
        e.push(300)


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_python_and_browser_holders_share_one_context(relay, tmp_path):
    """An on-demand server holder and a phone page in the same pool."""
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "server")
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    (tmp_path / "run.js").write_text(
        "const K = require('./holder.js');\n"
        f"const h = new K.Holder(new K.CpuEngine(), {CAP * 2 * ROW * LAYERS}, 'phone');\n"
        f"K.connect('ws://127.0.0.1:{wp}/holder', '{token}', h, {{}});\n"
        "setTimeout(() => process.exit(0), 120000);\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([NODE, "run.js"], cwd=tmp_path)
    try:
        end = time.time() + 15
        while len(r.holders) < 2 and time.time() < end:
            time.sleep(0.05)
        assert len(r.holders) == 2
        e = Engine(ep, seed=6)
        e.push(800)  # server [0, 512), phone [512, 800)
        assert r.status()["holders"][1]["device"].startswith("phone")
        e.check()
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------- on-demand holders


def _mint(wp, token, ttl=600):
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{wp}/join?ttl={ttl}", headers={"X-KV-Token": token}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return _json.loads(r.read())["token"]


def test_join_token_mint_needs_the_master_token(relay):
    import urllib.error
    import urllib.request

    _, token, _, wp = relay
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{wp}/join", timeout=5)
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError):
        _mint(wp, "not-the-master")
    assert _mint(wp, token).startswith("j-")


def test_join_token_works_once_and_the_session_reconnects(relay):
    r, token, ep, wp = relay
    join = _mint(wp, token)
    h = kv.KVHolder(CAP * PER_KEY, device="runner", store="wire")
    threading.Thread(
        target=net.dial_holder, args=(f"ws://127.0.0.1:{wp}/holder", join, h), daemon=True
    ).start()
    end = time.time() + 10
    while not r.holders and time.time() < end:
        time.sleep(0.05)
    assert r.holders and r.sessions  # attached, and holds a session for reconnects
    other = kv.KVHolder(1 << 20)
    assert net.dial_holder(f"ws://127.0.0.1:{wp}/holder", join, other, once=True) == 1  # reused
    e = Engine(ep, seed=7)
    e.push(300)
    first = r.holders[0].ws
    first.sock.close()  # the runner's link drops mid-session
    end = time.time() + 15
    while r.holders[0].ws is first and time.time() < end:
        time.sleep(0.1)
    assert r.holders[0].ws is not first and len(r.holders) == 1  # same entry, new link
    assert r.status()["broken"] is None
    e.push(100)  # no re-CONFIG: the keys it held survived the reconnect
    e.check()


def test_expired_join_token_is_refused(relay):
    r, token, _, wp = relay
    join, _ = r.mint_join(ttl_s=-1)
    assert (
        net.dial_holder(f"ws://127.0.0.1:{wp}/holder", join, kv.KVHolder(1 << 20), once=True) == 1
    )


def _elastic_args(**kw):
    import argparse

    ap = argparse.ArgumentParser()
    kv.register(ap.add_subparsers(dest="command"))
    argv = (
        ["kvholder", "elastic"]
        + [x for k, v in kw.items() for x in (f"--{k}", str(v)) if v is not True]
        + [f"--{k}" for k, v in kw.items() if v is True]
    )
    return ap.parse_args(argv)


def test_elastic_print_only_mints_tokens_a_machine_can_use(relay, tmp_path, monkeypatch, capsys):
    r, token, ep, wp = relay
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(tmp_path / "relay.json"))
    net.write_state(
        {
            "engine_port": ep,
            "web_port": wp,
            "token": token,
            "public": "",
            "lan": f"http://127.0.0.1:{wp}",
        }
    )
    assert net.run_elastic(_elastic_args(**{"count": 2, "print-only": True})) == 0
    lines = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("adk kvholder serve")
    ]
    assert len(lines) == 2 and lines[0] != lines[1]
    tok = lines[0].split("--token ")[1].split()[0]
    url = lines[0].split("--connect ")[1].split()[0]
    assert url == f"ws://127.0.0.1:{wp}/holder"
    threading.Thread(
        target=net.dial_holder, args=(url, tok, kv.KVHolder(CAP * PER_KEY)), daemon=True
    ).start()
    end = time.time() + 10
    while not r.holders and time.time() < end:
        time.sleep(0.05)
    assert r.holders


def test_elastic_dry_run_never_prints_a_token(relay, tmp_path, monkeypatch, capsys):
    _, token, ep, wp = relay
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(tmp_path / "relay.json"))
    net.write_state(
        {
            "engine_port": ep,
            "web_port": wp,
            "token": token,
            "public": "https://example.trycloudflare.com",
        }
    )
    assert net.run_elastic(_elastic_args(**{"count": 1, "dry-run": True, "minutes": 10})) == 0
    out = capsys.readouterr().out
    assert "relay=wss://example.trycloudflare.com/holder" in out
    assert "join=***" in out and "j-" not in out and token not in out
    assert "kvholder-runner.yml" in out


def test_elastic_without_a_public_relay_refuses_to_launch(relay, tmp_path, monkeypatch):
    _, token, ep, wp = relay
    monkeypatch.setenv("AITHER_KVHOLDER_STATE", str(tmp_path / "relay.json"))
    net.write_state({"engine_port": ep, "web_port": wp, "token": token, "public": ""})
    assert net.run_elastic(_elastic_args(count=1)) == 2


def test_a_holder_that_comes_back_empty_fails_loudly(relay):
    """A reloaded phone tab reconnects with its session but no keys: never serve it as if full."""
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "phone")
    e = Engine(ep, seed=8)
    e.push(200)
    h = r.holders[0]
    fresh = net.ws_connect(f"ws://127.0.0.1:{wp}/holder")
    import json as _json

    fresh.send(_json.dumps({"hello": "kvholder", "token": h.session, "device": "phone", "held": 0}))
    assert _json.loads(fresh.recv()[1])["ok"]
    _, msg = fresh.recv()  # the relay configures the newcomer, as a reloaded page would answer
    assert kv.HDR.unpack_from(msg)[1] == kv.CONFIG
    fresh.send(kv.HDR.pack(kv.MAGIC, kv.OK, 0))
    end = time.time() + 5
    while not r.broken and time.time() < end:
        time.sleep(0.05)
    assert "holder lost" in r.broken
    q = np.zeros((H, kv.NR, kv.HD), np.float32)
    with pytest.raises(RuntimeError, match="holder lost"):
        e.c.attn(0, q, 0.1)


def test_f32_holders_get_ranges_sized_for_f32(relay):
    """The fast store costs 2x f16 rows per key: the relay gives such a holder half the range."""
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "fast-a", store="f32")
    _add_holder(r, token, wp, "fast-b", store="f32")
    e = Engine(ep, seed=9)
    e.push(500)
    assert [h["off"] for h in r.status()["holders"]] == [0, CAP // 2]
    e.check(n_tok=1)
    with pytest.raises(RuntimeError, match="out of memory at 512 keys"):
        e.push(13)
