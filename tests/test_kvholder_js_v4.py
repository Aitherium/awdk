"""The browser holder (holder.js) speaks PATN v4 shapes: Qwen3 / Llama / Gemma / MLA layouts.

Node drives the page's CPU engine (what a phone runs without WebGPU) through the Python relay;
the WebGPU engine runs the same CONFIG/APPEND/ATTN state machine with shape-specialized shaders.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk.kvholder_page import HOLDER_JS

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# (k_dim, v_dim, rows per KV head, KV heads): Qwen3-8B (GQA 2 -> 16 rows), a 64/128 split,
# the v3 Qwen3.8 shape (sends no v4 tail), and DeepSeek MLA (one latent head, 576/512).
SHAPES = {
    "qwen3": (128, 128, 16, 2),
    "k64v128": (64, 128, 8, 2),
    "v3": (256, 256, 48, 2),
    "mla": (576, 512, 128, 1),
}


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def relay():
    token = "js4-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


@pytest.fixture()
def js_holder(relay, tmp_path):
    """A Node process running holder.js's CPU engine, dialed into the relay."""
    r, token, ep, wp = relay
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    (tmp_path / "run.js").write_text(
        "const K = require('./holder.js');\n"
        "const h = new K.Holder(new K.CpuEngine(), 256 * 1048576, 'node-v4');\n"
        "const fail = (e) => { console.error(e); process.exit(3); };\n"
        f"K.connect('ws://127.0.0.1:{wp}/holder', '{token}', h, {{error: fail}});\n"
        "setTimeout(() => process.exit(0), 120000);\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [NODE, "run.js"], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        end = time.time() + 10
        while not r.holder and time.time() < end:
            time.sleep(0.05)
        assert r.holder, "the JS holder never attached"
        yield r, ep
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def _ref(q, keys, vals, scale):
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    p = np.exp(s - s.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", p / p.sum(-1, keepdims=True), vals.astype(np.float64))


def _wire(x, cfg, side):
    t = cfg.is_q8
    n = x.shape[0]
    return kv.dequant_rows(np.frombuffer(kv.quant_rows(x, t), np.uint8), n, cfg, side)


@pytest.mark.parametrize("kv_type", ["f16", "q8_0", "q4_0"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_js_holder_is_exact_for_v4_shapes(js_holder, shape, kv_type):
    _, ep = js_holder
    kd, vd, rows, h = SHAPES[shape]
    cfg = kv.Config.for_model(2, h, kv_type, k_dim=kd, v_dim=vd, rows=rows)
    assert cfg.is_v3 == (shape == "v3")
    rng = np.random.default_rng(kd + vd + rows)
    n_old, n_new, n_tok = 300, 60, 3
    keys = rng.standard_normal((n_old + n_new, h, kd)).astype(np.float32)
    vals = rng.standard_normal((n_old + n_new, h, vd)).astype(np.float32)
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=60)
    c.configure(cfg)
    for layer in (0, 1):
        assert c.append(layer, 0, keys[:200], vals[:200]) == 200
        assert c.append(layer, 200, keys[200:n_old], vals[200:n_old]) == n_old
    q = rng.standard_normal((h, rows, kd)).astype(np.float32)
    scale = kd**-0.5
    far_o, far_lse, meta = c.attn(1, q, scale, n_tok=n_tok)
    assert meta["nk"] == n_old and far_o.shape == (h, rows, vd)
    near = kv.partial_attention(q, keys[n_old:], vals[n_old:], scale)
    out, _ = kv.merge_partials([near, (far_o, far_lse)])
    ref = _ref(
        q,
        np.concatenate([_wire(keys[:n_old], cfg, "k"), keys[n_old:]]),
        np.concatenate([_wire(vals[:n_old], cfg, "v"), vals[n_old:]]),
        scale,
    )
    live = [i for i in range(rows) if i % 8 < n_tok]
    pad = [i for i in range(rows) if i % 8 >= n_tok]
    np.testing.assert_allclose(out[:, live], ref[:, live], atol=3e-3)
    assert np.isneginf(far_lse[:, pad]).all() and not np.isinf(far_lse[:, live]).any()
    assert "engine=cpu" in c.stats()
    c.close()


def test_js_holder_attn_big_and_truncate_on_a_v4_shape(js_holder):
    _, ep = js_holder
    kd, vd, rows, h = SHAPES["qwen3"]
    cfg = kv.Config.for_model(1, h, "q8_0", k_dim=kd, v_dim=vd, rows=rows)
    rng = np.random.default_rng(5)
    keys = rng.standard_normal((400, h, kd)).astype(np.float32)
    vals = rng.standard_normal((400, h, vd)).astype(np.float32)
    c = kv.KVHolderClient("127.0.0.1", ep, timeout=60)
    c.configure(cfg)
    c.append(0, 0, keys, vals)
    c.truncate(250)
    q = rng.standard_normal((3, h, rows, kd)).astype(np.float32)  # ATTN_BIG: 3 groups
    o, lse, meta = c.attn(0, q, kd**-0.5, n_tok=8)
    assert meta["nk"] == 250 and o.shape == (3, h, rows, vd)
    kq, vq = _wire(keys[:250], cfg, "k"), _wire(vals[:250], cfg, "v")
    for g in range(3):
        np.testing.assert_allclose(o[g], _ref(q[g], kq, vq, kd**-0.5), atol=3e-3)
    c.close()


def _node(tmp_path, body: str) -> dict:
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    (tmp_path / "run.js").write_text("const K = require('./holder.js');\n" + body, encoding="utf-8")
    out = subprocess.run(
        [NODE, "run.js"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _cfg_hex(**kw) -> str:
    base = {"n_layer": 2, "n_head_kv": 2, "kv": "q8_0"}
    base.update(kw)
    return kv.Config.for_model(**base).pack().hex()


def test_js_config_validation_matches_the_python_holder(tmp_path):
    """Every CONFIG the Python KVHolder refuses, holder.js refuses with the same text, and the
    reverse; a fake engine with tq4 set stands in for the WebGPU tq4 store."""
    cases = {
        "qwen3": _cfg_hex(k_dim=128, v_dim=128, rows=16),
        "mla": _cfg_hex(n_head_kv=1, k_dim=576, v_dim=512, rows=128),
        "v3": _cfg_hex(),
        "rows_not_8": _cfg_hex(k_dim=128, rows=12),
        "dim_not_32": _cfg_hex(k_dim=80, v_dim=128),
        "dim_too_big": _cfg_hex(k_dim=8192, v_dim=128),
    }
    # a v4 tail whose V bytes per head disagree with the row type
    raw = bytearray.fromhex(cases["qwen3"])
    raw[64:68] = (999).to_bytes(4, "little")
    cases["bad_hbv"] = raw.hex()
    want = {}
    for store in ("f32", "tq4"):
        for name, hx in cases.items():
            t, p = kv.KVHolder(1 << 26, store=store).handle(kv.CONFIG, bytes.fromhex(hx))
            want[f"{store}:{name}"] = [t, p.decode()]
    got = _node(
        tmp_path,
        f"const cases = {json.dumps(cases)}; const out = {{}};\n"
        "(async () => {\n"
        "  for (const store of ['f32', 'tq4']) for (const [name, hx] of Object.entries(cases)) {\n"
        "    const eng = store === 'tq4' ? {tq4: true, kind: 'fake', configure() {}, held() "
        "{ return 0; }} : new K.CpuEngine();\n"
        "    const [t, p] = await new K.Holder(eng, 1 << 26).handle(3, Buffer.from(hx, 'hex'));\n"
        "    out[store + ':' + name] = [t, Buffer.from(p).toString()];\n"
        "  }\n"
        "  console.log(JSON.stringify(out));\n"
        "})();\n",
    )
    assert got == want
    assert want["f32:mla"][0] == kv.OK and want["tq4:mla"][0] == kv.ERR


def test_js_store_sizing_matches_the_relay(tmp_path):
    """The relay sizes a holder's key range from the store it announces in its hello; the bytes
    holder.js actually charges per key must be the same number or keys land past its budget."""
    shapes = {k: list(v) for k, v in SHAPES.items()}
    got = _node(
        tmp_path,
        f"const shapes = {json.dumps(shapes)}; const out = {{}};\n"
        "for (const [name, [kd, vd, rows, h]] of Object.entries(shapes)) {\n"
        "  const cfg = K.mkCfg(1, h, 1, kd, vd, rows); const e = new K.CpuEngine();\n"
        "  e.configure(cfg); out[name + ':wire'] = e.bytesPerKey();\n"
        "  for (const [store, f16, tq4] of [['f16', true, false], ['f32', false, false], "
        "['tq4', false, true]]) {\n"
        "    const g = Object.assign(Object.create(K.GpuEngine.prototype), {cfg, f16, tq4});\n"
        "    out[name + ':' + store] = g.bytesPerKey();\n"
        "  }\n"
        "}\n"
        "console.log(JSON.stringify(out));\n",
    )
    for name, (kd, vd, rows, h) in SHAPES.items():
        cfg = kv.Config.for_model(1, h, "q8_0", k_dim=kd, v_dim=vd, rows=rows)
        for store in ("wire", "f16", "f32", "tq4"):
            relay = net.Relay("sizing")
            relay.cfg = cfg
            att = net._Attached(None, "x", 0.0, max_bytes=1 << 30, store=store)
            relay._place(att)
            assert att.cap == ((1 << 30) // got[f"{name}:{store}"]) // 64 * 64, (name, store)
    assert got["v3:wire"] == 2 * 2 * 272  # v3 unchanged: K + V q8_0 rows of 2 heads


def test_js_holder_charges_v4_appends_against_its_budget(tmp_path):
    kd, vd, rows, h = SHAPES["k64v128"]
    cfg = kv.Config.for_model(1, h, "q4_0", k_dim=kd, v_dim=vd, rows=rows)
    per_key = cfg.rs + cfg.v_rs
    rng = np.random.default_rng(1)
    k = kv.quant_rows(rng.standard_normal((10, h, kd)).astype(np.float32), cfg.is_q8)
    v = kv.quant_rows(rng.standard_normal((10, h, vd)).astype(np.float32), cfg.is_q8)
    ok = (kv.APPEND_REQ.pack(0, 0, 10) + k + v).hex()
    more = (kv.APPEND_REQ.pack(0, 10, 1) + k[: cfg.rs] + v[: cfg.v_rs]).hex()
    short = (kv.APPEND_REQ.pack(0, 10, 1) + k[: cfg.rs] + v[: cfg.rs]).hex()  # V sized like K
    got = _node(
        tmp_path,
        f"const h = new K.Holder(new K.CpuEngine(), {10 * per_key});\n"
        "(async () => { const out = [];\n"
        f"  for (const [t, hx] of [[3, '{cfg.pack().hex()}'], [4, '{ok}'], [4, '{short}'],"
        f" [4, '{more}']]) {{\n"
        "    const [rt, p] = await h.handle(t, Buffer.from(hx, 'hex'));\n"
        "    out.push([rt, Buffer.from(p).toString('hex')]); }\n"
        "  console.log(JSON.stringify(out)); })();\n",
    )
    assert [g[0] for g in got] == [kv.OK, kv.OK, kv.ERR, kv.ERR]
    assert bytes.fromhex(got[2][1]) == b"bad APPEND"
    assert bytes.fromhex(got[3][1]).startswith(b"out of memory at 10")


@pytest.mark.parametrize("dim", [64, 128, 256, 512])
def test_js_tq4_round_trips_within_the_4_bit_bound_at_any_pow2_width(tmp_path, dim):
    """tq4 for every power-of-two head width: the rotation inverts exactly and reconstruction
    error sits at the 16-level Lloyd-Max bound (~0.0095 relative MSE for a Gaussian)."""
    res = _node(
        tmp_path,
        "const C = [-2.7326,-2.0690,-1.6181,-1.2562,-0.9424,-0.6568,-0.3881,-0.1284,"
        "0.1284,0.3881,0.6568,0.9424,1.2562,1.6181,2.0690,2.7326];\n"
        "let seed = 11; const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648;"
        " return seed / 2147483648; };\n"
        "const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12))"
        " * Math.cos(6.283185 * rnd());\n"
        f"const N = 300, D = {dim}; const x = new Float32Array(N * D).map(gauss);\n"
        "const orig = x.slice();\n"
        "const r = orig.slice(0, D); K.tq4Rotate(r, 0, D); K.tq4Unrotate(r, 0, D);\n"
        "let rot = 0; for (let i = 0; i < D; i++) rot = Math.max(rot, Math.abs(r[i] - orig[i]));\n"
        "const {codes, norms} = K.tq4Encode(x, N, D);\n"
        "let num = 0, den = 0;\n"
        "for (let v = 0; v < N; v++) { const y = new Float32Array(D);\n"
        "  for (let j = 0; j < D; j += 2) { const b = codes[(v * D + j) >> 1];\n"
        "    y[j] = C[b & 15] * norms[v] / Math.sqrt(D);"
        " y[j + 1] = C[b >> 4] * norms[v] / Math.sqrt(D); }\n"
        "  K.tq4Unrotate(y, 0, D);\n"
        "  for (let j = 0; j < D; j++) { const e = y[j] - orig[v * D + j]; num += e * e;"
        " den += orig[v * D + j] ** 2; } }\n"
        "console.log(JSON.stringify({rot, mse: num / den, bytes: codes.length}));\n",
    )
    assert res["rot"] < 1e-5 and 0.005 < res["mse"] < 0.013, res
    assert res["bytes"] == 300 * dim // 2


def test_js_shaders_are_specialized_per_shape(tmp_path):
    """The WGSL a WebGPU holder compiles carries the CONFIG's widths and rows. Each workgroup
    takes a block of 16 (or 8) query rows, so a key is read once per block instead of once per
    row; the block shrinks to fit the GPU's workgroup memory, and very wide queries are read
    from storage instead."""
    got = _node(
        tmp_path,
        "const w = K.wgsl, out = {};\n"
        "out.v3 = w.partial('f32', 256, 256, 48, 32768);"
        " out.v3small = w.partial('f32', 256, 256, 48, 16384);\n"
        "out.qwen = w.partial('f16', 128, 128, 16, 16384);"
        " out.mla = w.partial('f32', 576, 512, 128, 32768);\n"
        "out.wide = w.partial('f32', 4096, 4096, 8, 32768); out.tq = w.partial('tq4', 128, 64, 16);\n"
        "out.merge = w.merge(512, 48); out.plan = w.plan(256, 256, 32768);\n"
        "console.log(JSON.stringify(out));\n",
    )
    assert "(h * 48u + qrow(li))" in got["v3"] and "qs: array<vec4<f32>, 1024>;" in got["v3"]
    assert "qs: array<vec4<f32>, 512>;" in got["v3small"]  # 16 KB of workgroup memory: 8 rows
    assert "enable f16;" in got["qwen"] and "array<vec4<f16>>" in got["qwen"]
    assert "h * 16u" in got["qwen"]
    assert (
        "var o: array<vec4<f32>, 4>;" in got["mla"] and "qs: array<vec4<f32>, 1152>" in got["mla"]
    )
    assert "var<workgroup> qs" not in got["wide"] and "Q[qb + d]" in got["wide"]
    assert "INVK: f32 = 0.0883883476" in got["tq"] and "INVV: f32 = 0.125" in got["tq"]
    assert "array<vec4<u32>>" in got["tq"] and "(k * p.nkv + h) * 4u" in got["tq"]
    assert "d < 512u" in got["merge"] and "(row % 48u) % 8u >= mp.ntok" in got["merge"]
    assert got["plan"]["RB"] == 16 and got["plan"]["shared"] is True


# ---------------------------------------------------------------- tq4 key centering


def _tq4_session(kd, vd, rows, h, kvt, shift, seed=0):
    """CONFIG + two APPENDs (the first fixes mu) + one ATTN, as raw PATN messages."""
    cfg = kv.Config.for_model(1, h, kvt, k_dim=kd, v_dim=vd, rows=rows)
    rng = np.random.default_rng(seed)
    n = 400
    off = (shift * rng.standard_normal((1, h, kd))).astype(np.float32)  # a shared per-head offset
    keys = (rng.standard_normal((n, h, kd)) + off).astype(np.float32)
    vals = rng.standard_normal((n, h, vd)).astype(np.float32)
    q = rng.standard_normal((h, rows, kd)).astype(np.float16)
    t = cfg.is_q8
    msgs = [
        (kv.CONFIG, cfg.pack()),
        (
            kv.APPEND,
            kv.APPEND_REQ.pack(0, 0, 250)
            + kv.quant_rows(keys[:250], t)
            + kv.quant_rows(vals[:250], t),
        ),
        (
            kv.APPEND,
            kv.APPEND_REQ.pack(0, 250, 150)
            + kv.quant_rows(keys[250:], t)
            + kv.quant_rows(vals[250:], t),
        ),
        (kv.ATTN, kv.ATTN_REQ.pack(0, 5, 0, kd**-0.5) + q.tobytes()),
    ]
    return cfg, keys, vals, q.astype(np.float32), msgs


def _js_replies(tmp_path, msgs, engine="new K.CpuTq4Engine()"):
    (tmp_path / "m.json").write_text(json.dumps([[t, p.hex()] for t, p in msgs]))
    got = _node(
        tmp_path,
        "const m = require('./m.json');\n"
        f"(async () => {{ const h = new K.Holder({engine}, 1 << 30); const out = [];\n"
        "  for (const [t, hx] of m) { const [rt, p] = await h.handle(t, Buffer.from(hx, 'hex'));\n"
        "    out.push([rt, Buffer.from(p).toString('hex')]); }\n"
        "  console.log(JSON.stringify({out})); })();\n",
    )
    return [(t, bytes.fromhex(p)) for t, p in got["out"]]


def _attn_rep(rep, cfg):
    on = cfg.n_head_kv * cfg.rows * cfg.v_dim
    o = np.frombuffer(rep, np.float16, on, kv.ATTN_REP.size).astype(np.float32)
    lse = np.frombuffer(rep, np.float32, cfg.n_head_kv * cfg.rows, kv.ATTN_REP.size + 2 * on)
    return o.reshape(cfg.n_head_kv, cfg.rows, cfg.v_dim), lse.reshape(cfg.n_head_kv, cfg.rows)


@pytest.mark.parametrize("center", [True, False])
@pytest.mark.parametrize(
    "kd,vd,rows,h,kvt",
    [(128, 128, 16, 2, "q8_0"), (64, 128, 8, 2, "f16"), (256, 256, 48, 1, "q4_0")],
)
def test_js_tq4_matches_the_python_tq4_holder(tmp_path, kd, vd, rows, h, kvt, center):
    """Same rotation, codes, mean and lse correction as adk.kvholder --store tq4: the partials
    agree to f16 rounding, centered (the default) and uncentered."""
    cfg, _, _, _, msgs = _tq4_session(kd, vd, rows, h, kvt, shift=5.0)
    py = kv.KVHolder(1 << 30, store="tq4", tq4_center=center)
    want = [py.handle(t, p) for t, p in msgs]
    got = _js_replies(tmp_path, msgs, f"new K.CpuTq4Engine({{center: {str(center).lower()}}})")
    assert [g[0] for g in got] == [w[0] for w in want] == [kv.OK, kv.OK, kv.OK, kv.ATTN_OK]
    (o_js, l_js), (o_py, l_py) = _attn_rep(got[-1][1], cfg), _attn_rep(want[-1][1], cfg)
    np.testing.assert_allclose(o_js, o_py, atol=4e-3)
    assert (np.isneginf(l_js) == np.isneginf(l_py)).all()
    live = np.isfinite(l_py)
    np.testing.assert_allclose(l_js[live], l_py[live], rtol=1e-5, atol=1e-4)


def test_js_tq4_centering_keeps_the_merge_exact(tmp_path):
    """The holder's (O, lse) merged with a host tail over other keys equals full attention over
    [decode(k - mu) + mu, tail]: the lse carries scale * q.mu back. Keys share a large per-head
    offset (5 sigma), so a holder that dropped the correction would be far off."""
    cfg, keys, vals, q, msgs = _tq4_session(128, 128, 16, 2, "q8_0", shift=5.0, seed=3)
    o, lse = _attn_rep(_js_replies(tmp_path, msgs)[-1][1], cfg)
    n, scale = keys.shape[0], 128**-0.5
    kq = kv.dequant_rows(np.frombuffer(kv.quant_rows(keys, 1), np.uint8), n, cfg)
    vq = kv.dequant_rows(np.frombuffer(kv.quant_rows(vals, 1), np.uint8), n, cfg, "v")
    mu = kq[:250].mean(axis=0)  # [H, D]: the FIRST append's mean, frozen
    k_held = kv.tq4_decode(*kv.tq4_encode(kq - mu)) + mu
    v_held = kv.tq4_decode(*kv.tq4_encode(vq))
    rng = np.random.default_rng(9)
    tail_k = (kq[:1] + rng.standard_normal((60, 2, 128))).astype(np.float32)
    tail_v = rng.standard_normal((60, 2, 128)).astype(np.float32)
    out, _ = kv.merge_partials([kv.partial_attention(q, tail_k, tail_v, scale), (o, lse)])
    ref = _ref(q, np.concatenate([k_held, tail_k]), np.concatenate([v_held, tail_v]), scale)
    live = [r for r in range(16) if r % 8 < 5]
    np.testing.assert_allclose(out[:, live], ref[:, live], atol=5e-3)


def test_js_tq4_holder_announces_centering_to_the_relay(relay, tmp_path):
    r, token, _, wp = relay
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    procs = []
    for i, center in enumerate(("true", "false")):
        (tmp_path / f"run{i}.js").write_text(
            "const K = require('./holder.js');\n"
            f"const h = new K.Holder(new K.CpuTq4Engine({{center: {center}}}), 1 << 26, 'js{i}');\n"
            f"K.connect('ws://127.0.0.1:{wp}/holder', '{token}', h, {{}});\n"
            "setTimeout(() => process.exit(0), 60000);\n",
            encoding="utf-8",
        )
        procs.append(subprocess.Popen([NODE, f"run{i}.js"], cwd=tmp_path))
        end = time.time() + 10
        while len(r.holders) <= i and time.time() < end:
            time.sleep(0.05)
    try:
        assert [h.store for h in r.holders] == ["tq4", "tq4"]
        assert [h.tq4 for h in r.holders] == ["centered", ""]
        assert net.tq4_warnings(r.holders) == [f"js1 (cpu-tq4): {net.TQ4_UNCENTERED}"]
    finally:
        for p in procs:
            p.kill()
            p.communicate(timeout=10)


# ---------------------------------------------------------------- a phone tab in the background

_PAGE_GLUE = """
const K = require('./holder.js');
// the page's own wiring (index.html): pause = close the link, resume = dial with the session
const doc = new EventTarget(), win = new EventTarget();
doc.visibilityState = 'visible';
const h = new K.Holder(new K.CpuEngine(), 64 * 1048576, 'phone-tab');
let token = process.argv[2], ws = null, did = '';
const dial = () => { ws = K.connect(process.argv[3], token, h, {session: (s) => { token = s; }}); };
K.pauseWhenHidden(doc, win, {
  pause: () => { ws.onclose = null; ws.close(); ws = null; did = 'paused'; },
  resume: () => { dial(); did = 'resumed'; },
});
dial();
const flip = (state, ev) => { doc.visibilityState = state; (ev === 'vis' ? doc : win)
  .dispatchEvent(new Event(ev === 'vis' ? 'visibilitychange' : ev)); };
require('readline').createInterface({input: process.stdin}).on('line', (l) => {
  const [state, ev] = l.trim().split(' '); did = 'nothing'; flip(state, ev); console.log(did);
});
setTimeout(() => process.exit(0), 120000);
"""


def _tab(tmp_path, token, wp):
    (tmp_path / "holder.js").write_text(HOLDER_JS, encoding="utf-8")
    (tmp_path / "tab.js").write_text(_PAGE_GLUE, encoding="utf-8")
    return subprocess.Popen(
        [NODE, "tab.js", token, f"ws://127.0.0.1:{wp}/holder"],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def _say(proc, line: str, expect: str) -> None:
    proc.stdin.write(line + "\n")
    proc.stdin.flush()
    assert proc.stdout.readline().strip() == expect


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition never held")


def test_hidden_tab_detaches_so_the_engine_is_not_left_waiting(relay, tmp_path):
    """A frozen background tab used to stay attached and silent: the engine's next CONFIG sat
    out the relay's 60 s call timeout. Now the tab closes its link when hidden, the relay finds
    it gone at once, and when the tab is visible again it rejoins and is configured."""
    r, token, ep, wp = relay
    tab = _tab(tmp_path, token, wp)
    try:
        _wait(lambda: len(r.holders) == 1)
        c = kv.KVHolderClient("127.0.0.1", ep, timeout=120)
        cfg = kv.Config.for_model(1, 2, "q8_0", k_dim=128, v_dim=128, rows=16)
        c.configure(cfg)
        _say(tab, "hidden vis", "paused")
        t0 = time.time()
        try:
            c.configure(cfg)  # the relay meets the closed link: no 60 s wait
        except RuntimeError as e:
            assert "lost" in str(e) or "no holder" in str(e), e
        assert time.time() - t0 < 5
        _wait(lambda: not r.holders)
        _say(tab, "visible vis", "resumed")
        _wait(lambda: len(r.holders) == 1)
        c.configure(cfg)
        rng = np.random.default_rng(2)
        keys = rng.standard_normal((300, 2, 128)).astype(np.float32)
        vals = rng.standard_normal((300, 2, 128)).astype(np.float32)
        assert c.append(0, 0, keys, vals) == 300
        q = rng.standard_normal((2, 16, 128)).astype(np.float32)
        o, _, meta = c.attn(0, q, 128**-0.5, n_tok=8)
        ref = _ref(q, _wire(keys, cfg, "k"), _wire(vals, cfg, "v"), 128**-0.5)
        np.testing.assert_allclose(o, ref, atol=3e-3)
        c.close()
    finally:
        tab.kill()
        tab.communicate(timeout=10)


def test_a_quick_trip_to_the_background_keeps_every_key(relay, tmp_path):
    """Hidden and back before the engine calls: the tab rejoins with its session token and the
    same keys, the relay swaps the link, and attention over the held keys is still exact."""
    r, token, ep, wp = relay
    tab = _tab(tmp_path, token, wp)
    try:
        _wait(lambda: len(r.holders) == 1)
        c = kv.KVHolderClient("127.0.0.1", ep, timeout=120)
        cfg = kv.Config.for_model(1, 2, "f16", k_dim=64, v_dim=128, rows=8)
        c.configure(cfg)
        rng = np.random.default_rng(4)
        keys = rng.standard_normal((500, 2, 64)).astype(np.float32)
        vals = rng.standard_normal((500, 2, 128)).astype(np.float32)
        c.append(0, 0, keys, vals)
        first, old_link = r.holders[0], r.holders[0].ws
        _say(tab, "hidden pagehide", "paused")
        _say(tab, "visible pageshow", "resumed")
        _wait(lambda: r.holders and r.holders[0].ws is not old_link)
        assert r.holders == [first] and not r.broken  # same entry, new link, nothing lost
        q = rng.standard_normal((2, 8, 64)).astype(np.float32)
        o, _, meta = c.attn(0, q, 0.125, n_tok=8)
        assert meta["nk"] == 500
        np.testing.assert_allclose(
            o, _ref(q, _wire(keys, cfg, "k"), _wire(vals, cfg, "v"), 0.125), atol=3e-3
        )
        c.close()
    finally:
        tab.kill()
        tab.communicate(timeout=10)


# ---------------------------------------------------------------- which GPU a holder lends


def test_js_holder_asks_for_the_high_performance_gpu_and_names_it(tmp_path):
    """A two-GPU machine lent its integrated GPU (33 ms/call next to an idle RTX 5090). The
    holder asks for 'high-performance' unless the lend setting says otherwise, falls back to any
    adapter rather than none, and its hello names the adapter it got."""
    got = _node(
        tmp_path,
        "const asked = [];\n"
        "const fake = (ret) => ({requestAdapter: async (o) => { asked.push(o || null);"
        " return ret(o); }});\n"
        "const dgpu = {info: {vendor: 'nvidia', architecture: 'blackwell', device: ''}};\n"
        "const igpu = {info: {vendor: 'amd', architecture: '', description: 'AMD Radeon'}};\n"
        "(async () => {\n"
        "  const out = {};\n"
        "  out.hp = K.adapterLabel(await K.pickAdapter(fake(() => dgpu), undefined));\n"
        "  out.fallback = K.adapterLabel(await K.pickAdapter(fake((o) => o ? null : igpu),"
        " 'high-performance'));\n"
        "  out.asked = asked;\n"
        "  out.power = [K.resolvePower(undefined, null), K.resolvePower('low-power', null),\n"
        "    K.resolvePower('battery', {charging: false}), K.resolvePower('battery',"
        " {charging: true}),\n"
        "    K.resolvePower('battery', null)];\n"
        "  let sent = null;\n"
        "  globalThis.WebSocket = class { constructor() { setTimeout(() => this.onopen(), 0); }\n"
        "    send(m) { sent = JSON.parse(m); } close() {} };\n"
        "  const eng = new K.CpuEngine(); eng.adapterName = 'nvidia blackwell';"
        " eng.power = 'high-performance';\n"
        "  K.connect('ws://x/holder', 't', new K.Holder(eng, 1 << 20, 'pc'), {});\n"
        "  const cpu = new K.CpuEngine(); let sentCpu = null;\n"
        "  await new Promise((r) => setTimeout(r, 20)); out.hello = sent;\n"
        "  K.connect('ws://x/holder', 't', new K.Holder(cpu, 1 << 20, 'pc'), {});\n"
        "  await new Promise((r) => setTimeout(r, 20)); out.helloCpu = sent;\n"
        "  console.log(JSON.stringify(out));\n"
        "})();\n",
    )
    assert got["hp"] == "nvidia blackwell"
    assert got["fallback"] == "amd AMD Radeon"
    assert got["asked"] == [
        {"powerPreference": "high-performance"},
        {"powerPreference": "high-performance"},
        None,
    ]
    assert got["power"] == [
        "high-performance",
        "low-power",
        "low-power",
        "high-performance",
        "high-performance",
    ]
    assert got["hello"]["adapter"] == "nvidia blackwell"
    assert got["hello"]["power"] == "high-performance"
    assert "adapter" not in got["helloCpu"]  # a CPU holder's hello is unchanged
