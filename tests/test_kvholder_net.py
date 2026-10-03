"""KV holder transports: relay <-> a dialed-in holder (Python or browser JS), exact on the wire."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
import pytest
from adk import kvholder as kv
from adk import kvholder_net as net
from adk.kvholder_page import HOLDER_JS, PAGE_HTML


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def relay():
    token = "t0k-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


def _wait_attached(r, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if r.holder:
            return
        time.sleep(0.05)
    raise AssertionError("holder never attached")


def _reference(q, keys, vals, scale):
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    p = np.exp(s - s.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", p / p.sum(-1, keepdims=True), vals.astype(np.float64))


def _exactness_over_relay(ep, kv_type, n_tok=8, n_old=1500, n_new=300, h=2, seed=0):
    rng = np.random.default_rng(seed)
    keys = rng.standard_normal((n_old + n_new, h, kv.HD)).astype(np.float32)
    vals = rng.standard_normal((n_old + n_new, h, kv.HD)).astype(np.float32)
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=60)
    c.configure(kv.Config.for_model(1, h, kv_type))
    assert c.append(0, 0, keys[:n_old], vals[:n_old]) == n_old
    q = rng.standard_normal((h, kv.NR, kv.HD)).astype(np.float32)
    scale = kv.HD**-0.5
    far_o, far_lse, meta = c.attn(0, q, scale, n_tok=n_tok)
    assert meta["nk"] == n_old
    near = kv.partial_attention(q, keys[n_old:], vals[n_old:], scale)
    out, _ = kv.merge_partials([near, (far_o, far_lse)])
    t = kv.KV_TYPES[kv_type]
    cfg = kv.Config.for_model(1, h, kv_type)
    kq = kv.dequant_rows(np.frombuffer(kv.quant_rows(keys[:n_old], t), np.uint8), n_old, cfg)
    vq = kv.dequant_rows(np.frombuffer(kv.quant_rows(vals[:n_old], t), np.uint8), n_old, cfg)
    ref = _reference(
        q, np.concatenate([kq, keys[n_old:]]), np.concatenate([vq, vals[n_old:]]), scale
    )
    rows = [r for r in range(kv.NR) if r % 8 < n_tok]  # rows t >= n_tok are padding
    np.testing.assert_allclose(out[:, rows], ref[:, rows], atol=3e-3)
    stats = c.stats()
    c.close()
    return stats


def test_python_holder_dials_in_and_is_exact(relay):
    r, token, ep, wp = relay
    h = kv.KVHolder(64 << 20, device="pytest-holder")
    threading.Thread(
        target=net.dial_holder, args=(f"ws://127.0.0.1:{wp}/holder", token, h), daemon=True
    ).start()
    _wait_attached(r)
    assert "held=1500" in _exactness_over_relay(ep, "q8_0")
    assert r.status()["attached"] and r.status()["device"] == "pytest-holder"


def test_wrong_token_is_refused(relay):
    r, _, _, wp = relay
    h = kv.KVHolder(1 << 20)
    assert net.dial_holder(f"ws://127.0.0.1:{wp}/holder", "wrong", h, once=True) == 1
    assert r.holder is None


def test_engine_gets_an_error_not_a_hang_without_holder(relay):
    _, _, ep, _ = relay
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=10)
    with pytest.raises(RuntimeError, match="no holder attached"):
        c.hello()


def test_page_status_and_script_are_served(relay):
    r, _, _, wp = relay
    base = f"http://127.0.0.1:{wp}"
    page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
    assert "KV Holder" in page and "holder.js" in page
    js = urllib.request.urlopen(base + "/holder.js", timeout=5).read().decode()
    assert "GpuEngine" in js
    st = json.loads(urllib.request.urlopen(base + "/status", timeout=5).read())
    assert st["attached"] is False


def test_ws_mask_roundtrip():
    data = bytes(range(256)) * 3 + b"xy"
    m = b"\x01\x02\x03\x04"
    assert net._xor_mask(net._xor_mask(data, m), m) == data
    assert net.holder_url("http://localhost:50063/", "abc") == "http://localhost:50063/#t=abc"


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("kv_type,n_tok", [("q8_0", 8), ("f16", 1), ("q4_0", 3)])
def test_browser_holder_js_dials_in_and_is_exact(relay, tmp_path, kv_type, n_tok):
    """The page's holder (CPU engine) under Node: the code a phone runs when WebGPU is absent."""
    r, token, ep, wp = relay
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    script = tmp_path / "run.js"
    script.write_text(
        "const K = require('./holder.js');\n"
        "const h = new K.Holder(new K.CpuEngine(), 64 * 1048576, 'node-test');\n"
        "const fail = (e) => { console.error(e); process.exit(3); };\n"
        f"K.connect('ws://127.0.0.1:{wp}/holder', '{token}', h, {{error: fail}});\n"
        "setTimeout(() => process.exit(0), 120000);\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [NODE, str(script)], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        _wait_attached(r)
        assert "engine=cpu" in _exactness_over_relay(ep, kv_type, n_tok=n_tok, n_old=600, n_new=200)
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_relay_reattach_after_holder_drops(relay):
    r, token, ep, wp = relay
    h1 = kv.KVHolder(8 << 20, device="first")
    t = threading.Thread(
        target=net.dial_holder, args=(f"ws://127.0.0.1:{wp}/holder", token, h1), daemon=True
    )
    t.start()
    _wait_attached(r)
    r.holder.ws.sock.close()  # the cable is pulled
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=10)
    with pytest.raises(RuntimeError):
        c.hello()
    _wait_attached(r, timeout=15)  # dial_holder reconnects on its own
    assert c.hello()["version"] == kv.VERSION
    c.close()


def test_cli_works_without_numpy():
    code = (
        "import sys; sys.modules['numpy'] = None\n"
        "import argparse, adk.kvholder as k\n"
        "ap = argparse.ArgumentParser(); k.register(ap.add_subparsers(dest='command'))\n"
        "assert k.run(ap.parse_args(['kvholder', 'plan', '--free-gb', '8'])) == 0\n"
        "assert k.run(ap.parse_args(['kvholder', 'serve', '--max-mb', '8'])) == 2\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    assert r.returncode == 0, r.stderr


def test_page_has_no_external_assets():
    assert "http://" not in PAGE_HTML and "https://" not in PAGE_HTML


def test_unauthenticated_peer_cannot_send_a_big_first_message(relay):
    """Pre-auth the relay reads a few KB at most: a stranger on a tunnel URL announcing a 1 GB
    frame is cut off at once, instead of the relay waiting to buffer it."""
    import struct as _st

    r, _, _, wp = relay
    ws = net.ws_connect(f"ws://127.0.0.1:{wp}/holder")
    ws.sock.sendall(bytes([0x82, 0x80 | 127]) + _st.pack(">Q", 1 << 30) + bytes(4) + b"x" * 64)
    ws.sock.settimeout(5)
    t0 = time.time()
    with pytest.raises(ConnectionError):  # a timeout (the relay still waiting) is NOT a pass
        ws.recv()
    assert time.time() - t0 < 3
    assert r.holder is None
