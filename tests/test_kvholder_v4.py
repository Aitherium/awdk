"""PATN v4 shapes: a DeepSeek-MLA-like layout (1 latent KV head, keys 576, values 512)."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk.kvholder_page import HOLDER_JS

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only

MLA = {"k_dim": 576, "v_dim": 512, "rows": 128}  # 16 query heads x 8 tokens per group
H, LAYERS = 1, 2


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _ref(q, keys, vals, scale):
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    p = np.exp(s - s.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", p / p.sum(-1, keepdims=True), vals.astype(np.float64))


def test_v4_config_round_trips_and_v3_stays_v3():
    v3 = kv.Config.for_model(16, 4, "q8_0")
    assert len(v3.pack()) == kv.CONFIG_REQ.size and v3.is_v3
    v4 = kv.Config.for_model(LAYERS, H, "q8_0", **MLA)
    raw = v4.pack()
    assert len(raw) == kv.CONFIG_REQ.size + kv.CONFIG_V4.size and not v4.is_v3
    back = kv.Config.unpack(raw)
    assert (back.k_dim, back.v_dim, back.rows) == (576, 512, 128)
    assert back.v_rs == 512 // 32 * 34 and back.rs == 576 // 32 * 34


@pytest.fixture()
def relay():
    token = "v4-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


@pytest.mark.parametrize("store,kv_type", [("f32", "q8_0"), ("wire", "f16")])
def test_mla_shape_through_two_holders_is_exact(relay, store, kv_type):
    r, token, ep, wp = relay
    cfg = kv.Config.for_model(LAYERS, H, kv_type, **MLA)
    per_key = (cfg.rs + cfg.v_rs) if store == "wire" else H * (576 + 512) * 4
    for name in ("a", "b"):
        h = kv.KVHolder(700 * per_key * LAYERS, device=name, store=store)
        url = f"ws://127.0.0.1:{wp}/holder"
        threading.Thread(target=net.dial_holder, args=(url, token, h), daemon=True).start()
        end = time.time() + 10
        while len(r.holders) < (1 if name == "a" else 2) and time.time() < end:
            time.sleep(0.05)
    assert len(r.holders) == 2
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=60)
    c.configure(cfg)
    rng = np.random.default_rng(20)
    n = 1000  # 640 on the first holder, the rest on the second
    t = kv.KV_TYPES[kv_type]
    for layer in range(LAYERS):
        keys = rng.standard_normal((n, H, 576)).astype(np.float32)
        vals = rng.standard_normal((n, H, 512)).astype(np.float32)
        for s in range(0, n, 300):
            c.append(layer, s, keys[s : s + 300], vals[s : s + 300])
    assert r.status()["holders"][1]["off"] > 0
    kq = kv.dequant_rows(np.frombuffer(kv.quant_rows(keys, t), np.uint8), n, cfg)
    vq = kv.dequant_rows(np.frombuffer(kv.quant_rows(vals, t), np.uint8), n, cfg, "v")
    q = rng.standard_normal((H, 128, 576)).astype(np.float32)
    o, lse, meta = c.attn(LAYERS - 1, q, 576**-0.5, n_tok=1)
    assert o.shape == (H, 128, 512) and meta["nk"] == n
    rows = [i for i in range(128) if i % 8 < 1]
    ref = _ref(q, kq, vq, 576**-0.5)
    np.testing.assert_allclose(o[:, rows], ref[:, rows], atol=3e-3)
    pad = [i for i in range(128) if i % 8 >= 1]
    assert np.isneginf(lse[:, pad]).all()
    c.close()


def test_tq4_refuses_non_power_of_two_heads():
    h = kv.KVHolder(1 << 26, store="tq4")
    rep = h.handle(kv.CONFIG, kv.Config.for_model(LAYERS, H, "q8_0", **MLA).pack())
    assert rep[0] == kv.ERR and b"power-of-two" in rep[1]


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_browser_holder_refuses_v4_loudly(tmp_path):
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    cfg = kv.Config.for_model(LAYERS, H, "q8_0", **MLA).pack().hex()
    (tmp_path / "run.js").write_text(
        "const K = require('./holder.js');\n"
        "const h = new K.Holder(new K.CpuEngine(), 1 << 26, 'n');\n"
        f"h.handle(3, Buffer.from('{cfg}', 'hex')).then(([t, p]) => {{\n"
        "  console.log(t, Buffer.from(p).toString()); });\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [NODE, "run.js"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert out.stdout.startswith("10 ") and "v3 shapes only" in out.stdout, out.stdout + out.stderr
