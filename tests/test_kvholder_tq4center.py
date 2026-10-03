"""tq4 key centering: the holder stores k - mu and folds q . mu back in, exactly."""

from __future__ import annotations

import threading
import time

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only

H = 2
SCALE = kv.HD**-0.5


def _f16(x):
    return x.astype(np.float16).astype(np.float32)


def _keys(rng, n, offset):
    """Keys like a real layer 0: a large shared per-head offset plus small detail."""
    return _f16(rng.standard_normal((n, H, kv.HD)) + offset[None])


def _holder(center=True, store="tq4"):
    h = kv.KVHolder(1 << 30, store=store, tq4_center=center)
    assert h.handle(kv.CONFIG, kv.Config.for_model(1, H, "f16").pack())[0] == kv.OK
    return h


def _append(h, pos0, k, v):
    p = kv.APPEND_REQ.pack(0, pos0, k.shape[0]) + kv.quant_rows(k, 0) + kv.quant_rows(v, 0)
    assert h.handle(kv.APPEND, p)[0] == kv.OK


def _attn(h, q, nk=0):
    rep = h.handle(
        kv.ATTN, kv.ATTN_REQ.pack(0, 8, nk, SCALE) + q[None].astype(np.float16).tobytes()
    )
    assert rep[0] == kv.ATTN_OK
    n = H * kv.NR * kv.HD
    o = np.frombuffer(rep[1], np.float16, n, kv.ATTN_REP.size).astype(np.float32)
    lse = np.frombuffer(rep[1], np.float32, H * kv.NR, kv.ATTN_REP.size + n * 2)
    return o.reshape(H, kv.NR, kv.HD), lse.reshape(H, kv.NR)


def _ref(q, k, v):
    """(O, lse) of exact attention, float64."""
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * SCALE, k.astype(np.float64))
    mx = s.max(-1, keepdims=True)
    p = np.exp(s - mx)
    den = p.sum(-1, keepdims=True)
    return np.einsum("hrn,nhd->hrd", p / den, v.astype(np.float64)), (mx + np.log(den))[..., 0]


def _decoded(k, mu):
    """The keys a centered tq4 holder effectively holds: decode(encode(k - mu)) + mu."""
    kt = k.transpose(1, 0, 2) - mu[:, None, :]
    return (kv.tq4_decode(*kv.tq4_encode(kt)) + mu[:, None, :]).transpose(1, 0, 2)


def test_centering_is_exact_over_the_keys_it_holds():
    """Output AND lse equal exact attention over the holder's effective keys."""
    rng = np.random.default_rng(1)
    offset = 6.0 * rng.standard_normal((H, kv.HD))
    k, v = _keys(rng, 900, offset), _f16(rng.standard_normal((900, H, kv.HD)))
    h = _holder()
    _append(h, 0, k, v)
    mu = h.st.mu[0][0]
    np.testing.assert_allclose(mu, k[:900].mean(axis=0), atol=1e-4)
    q = _f16(rng.standard_normal((H, kv.NR, kv.HD)))
    o, lse = _attn(h, q)
    v_eff = kv.tq4_decode(*kv.tq4_encode(v.transpose(1, 0, 2))).transpose(1, 0, 2)
    ro, rl = _ref(q, _decoded(k, mu), v_eff)
    np.testing.assert_allclose(o, ro, atol=3e-3)
    np.testing.assert_allclose(lse, rl, atol=2e-3)  # q . mu folded back in


def test_centering_cuts_the_error_on_offset_keys():
    rng = np.random.default_rng(2)
    offset = 8.0 * rng.standard_normal((H, kv.HD))
    k, v = _keys(rng, 1500, offset), _f16(rng.standard_normal((1500, H, kv.HD)))
    q = _f16(rng.standard_normal((H, kv.NR, kv.HD)) * 0.3)
    ro, rl = _ref(q, k, v)
    errs = {}
    for center in (True, False):
        h = _holder(center)
        _append(h, 0, k, v)
        o, lse = _attn(h, q)
        errs[center] = (np.abs(o - ro).mean(), np.abs(lse - rl).mean())
    assert errs[True][0] < errs[False][0] / 2, errs  # the rest is the 4-bit values
    assert errs[True][1] < errs[False][1] / 20, errs


def test_lse_merge_with_the_host_tail_stays_exact():
    """The relay/host merge needs the TRUE lse: a missing q . mu would skew every weight."""
    rng = np.random.default_rng(3)
    offset = 5.0 * rng.standard_normal((H, kv.HD))
    k = _keys(rng, 1200, offset)
    v = _f16(rng.standard_normal((1200, H, kv.HD)))
    h = _holder()
    _append(h, 0, k[:800], v[:800])
    q = _f16(rng.standard_normal((H, kv.NR, kv.HD)))
    o_h, l_h = _attn(h, q)
    o_t, l_t = kv.partial_attention(q, k[800:], v[800:], SCALE)  # the host's own newest keys
    o, _ = kv.merge_partials([(o_h, l_h), (o_t, l_t)])
    v_eff = kv.tq4_decode(*kv.tq4_encode(v[:800].transpose(1, 0, 2))).transpose(1, 0, 2)
    k_all = np.concatenate([_decoded(k[:800], h.st.mu[0][0]), k[800:]])
    ro, _ = _ref(q, k_all, np.concatenate([v_eff, v[800:]]))
    np.testing.assert_allclose(o, ro, atol=3e-3)


def test_the_mean_is_fixed_by_the_first_append():
    rng = np.random.default_rng(4)
    k1 = _keys(rng, 300, 4.0 * rng.standard_normal((H, kv.HD)))
    k2 = _keys(rng, 300, 4.0 * rng.standard_normal((H, kv.HD)))  # drifts: another offset
    v = _f16(rng.standard_normal((600, H, kv.HD)))
    h = _holder()
    _append(h, 0, k1, v[:300])
    mu = h.st.mu[0][0].copy()
    _append(h, 300, k2, v[300:])
    assert np.array_equal(h.st.mu[0][0], mu)  # already-encoded keys never shift
    q = _f16(rng.standard_normal((H, kv.NR, kv.HD)))
    o, lse = _attn(h, q)
    k_eff = np.concatenate([_decoded(k1, mu), _decoded(k2, mu)])
    v_eff = kv.tq4_decode(*kv.tq4_encode(v.transpose(1, 0, 2))).transpose(1, 0, 2)
    ro, rl = _ref(q, k_eff, v_eff)
    np.testing.assert_allclose(o, ro, atol=3e-3)
    np.testing.assert_allclose(lse, rl, atol=2e-3)
    # TRUNCATE + re-append reuse the same mean; CONFIG starts over
    assert h.handle(kv.TRUNCATE, b"\x00\x00\x00\x00")[0] == kv.OK
    _append(h, 0, k2, v[:300])
    assert np.array_equal(h.st.mu[0][0], mu)
    h.handle(kv.CONFIG, kv.Config.for_model(1, H, "f16").pack())
    assert h.st.mu == {}


def test_store_bytes_are_unchanged():
    rng = np.random.default_rng(5)
    h = _holder()
    _append(h, 0, _keys(rng, 256, np.zeros((H, kv.HD))), _f16(rng.standard_normal((256, H, kv.HD))))
    assert h.st.held_bytes == 256 * 2 * H * (kv.HD // 2 + 4)


# ---------------------------------------------------------------- the relay sees who centers


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.parametrize("center", [True, False])
def test_relay_flags_an_uncentered_tq4_holder(center):
    token = "c-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    try:
        h = kv.KVHolder(64 << 20, device=f"tq4-{center}", store="tq4", tq4_center=center)
        url = f"ws://127.0.0.1:{wp}/holder"
        threading.Thread(target=net.dial_holder, args=(url, token, h), daemon=True).start()
        end = time.time() + 10
        while not r.holders and time.time() < end:
            time.sleep(0.05)
        assert r.holders
        warn = r.status()["warnings"]
        if center:
            assert warn == []
        else:
            assert warn == [f"tq4-False: {net.TQ4_UNCENTERED}"]
    finally:
        net.stop_relay(r, servers)


def test_pooled_sessions_each_keep_the_mean_they_encoded_with():
    """B attaches to A's centered blocks (A's mean) and centers its own tail on its own."""
    from adk import kvpool as pool

    rng = np.random.default_rng(6)
    off_a, off_b = 5.0 * rng.standard_normal((H, kv.HD)), 5.0 * rng.standard_normal((H, kv.HD))
    k_pre, k_tail = _keys(rng, 512, off_a), _keys(rng, 300, off_b)
    v = _f16(rng.standard_normal((812, H, kv.HD)))
    fp = pool.model_fingerprint("m")
    hashes = pool.chain_hashes(range(512), fp)
    req = pool.pack_pool_req(fp, pool.BLOCK_TOKENS, hashes)
    h = kv.KVHolder(1 << 30, store="tq4")
    cfg = kv.Config.for_model(1, H, "f16").pack()
    a, b = kv._Conn(), kv._Conn()
    for c, sid in ((a, 1), (b, 2)):
        assert h.handle(pool.SESSION, pool.SESSION_REQ.pack(sid), c)[0] == kv.OK
        assert h.handle(kv.CONFIG, cfg, c)[0] == kv.OK
    p = kv.APPEND_REQ.pack(0, 0, 512) + kv.quant_rows(k_pre, 0) + kv.quant_rows(v[:512], 0)
    assert h.handle(kv.APPEND, p, a)[0] == kv.OK
    assert h.handle(pool.POOL_PUBLISH, req, a)[0] == kv.OK
    assert h.handle(pool.POOL_ATTACH, req, b)[1] == (512).to_bytes(4, "little")
    p = kv.APPEND_REQ.pack(0, 512, 300) + kv.quant_rows(k_tail, 0) + kv.quant_rows(v[512:], 0)
    assert h.handle(kv.APPEND, p, b)[0] == kv.OK
    mu_a, mu_b = h.sessions[1].mu[0][0], h.sessions[2].mu[0][0]
    assert not np.allclose(mu_a, mu_b)
    q = _f16(rng.standard_normal((H, kv.NR, kv.HD)))
    rep = h.handle(
        kv.ATTN, kv.ATTN_REQ.pack(0, 8, 0, SCALE) + q[None].astype(np.float16).tobytes(), b
    )
    n = H * kv.NR * kv.HD
    o = np.frombuffer(rep[1], np.float16, n, kv.ATTN_REP.size).astype(np.float32)
    lse = np.frombuffer(rep[1], np.float32, H * kv.NR, kv.ATTN_REP.size + n * 2)
    k_eff = np.concatenate([_decoded(k_pre, mu_a), _decoded(k_tail, mu_b)])
    v_eff = np.concatenate(
        [
            kv.tq4_decode(*kv.tq4_encode(x.transpose(1, 0, 2))).transpose(1, 0, 2)
            for x in (v[:512], v[512:])
        ]
    )
    ro, rl = _ref(q, k_eff, v_eff)
    np.testing.assert_allclose(o.reshape(H, kv.NR, kv.HD), ro, atol=3e-3)
    np.testing.assert_allclose(lse.reshape(H, kv.NR), rl, atol=2e-3)
