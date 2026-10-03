"""KV holder: the merged result must equal attention over every key, over the real wire."""

from __future__ import annotations

import threading

import numpy as np
import pytest
from adk import kvholder as kv


def _full_attention(q, keys, vals, scale):
    scores = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    probs = np.exp(scores - scores.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", probs / probs.sum(-1, keepdims=True), vals.astype(np.float64))


@pytest.fixture()
def holder():
    srv = kv.HolderServer(("127.0.0.1", 0), kv.KVHolder(64 << 20))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _client(srv, n_layer=2, n_head_kv=2, kv_type="f16"):
    c = kv.KVHolderClient("127.0.0.1", srv.server_address[1], timeout=10)
    c.configure(kv.Config.for_model(n_layer, n_head_kv, kv_type))
    return c


def _rand(rng, *shape):
    return rng.standard_normal(shape).astype(np.float32)


def test_partial_merge_is_exact_without_the_wire():
    rng = np.random.default_rng(0)
    q, keys, vals = (
        _rand(rng, 2, kv.NR, kv.HD),
        _rand(rng, 900, 2, kv.HD),
        _rand(rng, 900, 2, kv.HD),
    )
    scale = kv.HD**-0.5
    parts = [
        kv.partial_attention(q, keys[a:b], vals[a:b], scale)
        for a, b in ((0, 300), (300, 650), (650, 900))
    ]
    out, _ = kv.merge_partials(parts)
    np.testing.assert_allclose(out, _full_attention(q, keys, vals, scale), atol=1e-5)


def test_empty_partial_is_neutral():
    rng = np.random.default_rng(1)
    q, keys, vals = _rand(rng, 2, kv.NR, kv.HD), _rand(rng, 64, 2, kv.HD), _rand(rng, 64, 2, kv.HD)
    host = kv.partial_attention(q, keys, vals, 0.1)
    empty = kv.partial_attention(q, keys[:0], vals[:0], 0.1)
    assert np.isneginf(empty[1]).all()
    out, lse = kv.merge_partials([host, empty])
    np.testing.assert_allclose(out, host[0], atol=1e-6)
    np.testing.assert_allclose(lse, host[1], atol=1e-6)


@pytest.mark.parametrize("kv_type,tol", [("f16", 3e-3), ("q8_0", 3e-3), ("q4_0", 3e-3)])
def test_host_plus_holder_equals_full_attention_over_the_wire(holder, kv_type, tol):
    """Backburner's split: holder keeps the oldest keys, host the newest; merge == all."""
    rng = np.random.default_rng(2)
    n_old, n_new, h = 5000, 700, 2
    keys, vals = _rand(rng, n_old + n_new, h, kv.HD), _rand(rng, n_old + n_new, h, kv.HD)
    c = _client(holder, n_layer=2, n_head_kv=h, kv_type=kv_type)
    assert c.hello()["version"] == kv.VERSION
    # append in two chunks so pos0 bookkeeping is exercised; layer 1 holds the real keys
    assert c.append(1, 0, keys[:3000], vals[:3000]) == 3000
    assert c.append(1, 3000, keys[3000:n_old], vals[3000:n_old]) == n_old
    # what the holder actually has is the quantized keys; the reference must use the same
    t = kv.KV_TYPES[kv_type]
    cfg = kv.Config.for_model(2, h, kv_type)
    keys_q = kv.dequant_rows(np.frombuffer(kv.quant_rows(keys[:n_old], t), np.uint8), n_old, cfg)
    vals_q = kv.dequant_rows(np.frombuffer(kv.quant_rows(vals[:n_old], t), np.uint8), n_old, cfg)
    q, scale = _rand(rng, h, kv.NR, kv.HD), kv.HD**-0.5
    out_far, lse_far, meta = c.attn(1, q, scale)
    assert meta["nk"] == n_old and meta["pages"] == 2
    part_near = kv.partial_attention(q, keys[n_old:], vals[n_old:], scale)
    out, _ = kv.merge_partials([part_near, (out_far, lse_far)])
    ref = _full_attention(
        q, np.concatenate([keys_q, keys[n_old:]]), np.concatenate([vals_q, vals[n_old:]]), scale
    )
    np.testing.assert_allclose(out, ref, atol=tol)
    c.close()


def test_two_holders_split_the_old_context(holder):
    """TWO-PHONES design A: each holder takes half the old pages; the host merges N partials."""
    srv2 = kv.HolderServer(("127.0.0.1", 0), kv.KVHolder(64 << 20))
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        rng = np.random.default_rng(3)
        keys, vals = _rand(rng, 3000, 1, kv.HD), _rand(rng, 3000, 1, kv.HD)
        a, b = _client(holder, 1, 1), _client(srv2, 1, 1)
        a.append(0, 0, keys[:1200], vals[:1200])
        b.append(0, 0, keys[1200:2400], vals[1200:2400])
        q, scale = _rand(rng, 1, kv.NR, kv.HD), 0.0625
        pa, pb = a.attn(0, q, scale)[:2], b.attn(0, q, scale)[:2]
        host = kv.partial_attention(q, keys[2400:], vals[2400:], scale)
        out, _ = kv.merge_partials([pa, pb, host])
        keys_h = keys.astype(np.float16).astype(np.float32)
        vals_h = vals.astype(np.float16).astype(np.float32)
        ref = _full_attention(
            q,
            np.concatenate([keys_h[:2400], keys[2400:]]),
            np.concatenate([vals_h[:2400], vals[2400:]]),
            scale,
        )
        np.testing.assert_allclose(out, ref, atol=3e-3)
    finally:
        srv2.shutdown()
        srv2.server_close()


def test_attn_big_matches_per_group_calls(holder):
    rng = np.random.default_rng(4)
    keys, vals = _rand(rng, 500, 2, kv.HD), _rand(rng, 500, 2, kv.HD)
    c = _client(holder, 1, 2)
    c.append(0, 0, keys, vals)
    q_big = _rand(rng, 3, 2, kv.NR, kv.HD)
    out_big, lse_big, _ = c.attn(0, q_big, 0.05)
    for g in range(3):
        out1, lse1, _ = c.attn(0, q_big[g], 0.05)
        np.testing.assert_allclose(out_big[g], out1, atol=1e-3)
        np.testing.assert_allclose(lse_big[g], lse1, atol=1e-4)


def test_protocol_refusals(holder):
    rng = np.random.default_rng(5)
    c = _client(holder, 1, 1)
    keys = _rand(rng, 10, 1, kv.HD)
    with pytest.raises(RuntimeError, match="pos0 5 != held 0"):
        c.append(0, 5, keys, keys)
    c.append(0, 0, keys, keys)
    c.truncate(4)
    assert "held=4" in c.stats()
    assert c.append(0, 4, keys[:2], keys[:2]) == 6
    assert c.ping(1000) >= 0


def test_out_of_memory_is_a_refusal_not_a_crash():
    srv = kv.HolderServer(("127.0.0.1", 0), kv.KVHolder(1 << 20))  # 1 MB loan
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = _client(srv, 1, 1)
        rng = np.random.default_rng(6)
        keys = _rand(rng, 1024, 1, kv.HD)  # 1024 x 512 B x 2 = 1 MB -> exceeds with any prior row
        c.append(0, 0, keys[:512], keys[:512])
        with pytest.raises(RuntimeError, match="out of memory at 512 keys"):
            c.append(0, 512, keys, keys)
        assert "held=512" in c.stats()
    finally:
        srv.shutdown()
        srv.server_close()


def test_plan_matches_backburners_measured_footprint():
    # phone-attn.h: 196,608 keys x 16 layers of q8_0 = 6.8 GB (Qwen3.8-27B, 4 KV heads)
    prof = kv.PROFILES["qwen38-27b"]
    r = kv.plan_capacity(int(7.3e9), prof["n_layer"], prof["n_head_kv"], "q8_0")
    assert r["bytes_per_token"] == 34816
    assert r["tokens"] % kv.PAGE == 0
    assert abs(196608 * r["bytes_per_token"] - 6.845e9) < 0.01e9
    assert 180_000 < r["tokens"] < 200_000


def test_cli_plan_runs(capsys):
    assert kv.main(["plan", "--free-gb", "8"]) == 0
    assert "old tokens" in capsys.readouterr().out
