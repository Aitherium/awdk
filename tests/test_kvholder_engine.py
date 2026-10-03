"""kvholder chat engine: a real HF model, far keys on holders, greedy output equal to local."""

from __future__ import annotations

import argparse
import threading
import time

import pytest

np = pytest.importorskip("numpy")
torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")

from adk import kvholder as kv  # noqa: E402
from adk import kvholder_engine as eng  # noqa: E402
from adk import kvholder_net as net  # noqa: E402


def _tiny(seed: int = 0):
    torch.manual_seed(seed)
    cfg = transformers.Qwen3Config(
        vocab_size=97,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=32,
        max_position_embeddings=512,
        attn_implementation="sdpa",
    )
    return transformers.Qwen3ForCausalLM(cfg).float().eval()


def _holder():
    srv = kv.HolderServer(("127.0.0.1", 0), kv.KVHolder(64 << 20, device="test", store="f32"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, kv.KVHolderClient("127.0.0.1", srv.server_address[1])


def _link(model, client, wire="v3"):
    c = model.config
    return eng.HolderLink(
        client,
        c.num_hidden_layers,
        c.num_key_value_heads,
        c.num_attention_heads,
        c.head_dim,
        wire=wire,
    )


def _prompt(n: int, seed: int = 1) -> list[int]:
    return np.random.default_rng(seed).integers(0, 97, n).tolist()


@pytest.mark.parametrize(
    "wire, chunk, qblock",
    [("v3", 24, 512), ("v4", 24, 512), ("v4", 4096, 16)],  # last: one pass, blocked queries
)
def test_far_keys_on_a_holder_match_local_greedy(wire, chunk, qblock, monkeypatch):
    monkeypatch.setattr(eng, "_QBLOCK", qblock)
    monkeypatch.setattr(eng, "_APPEND_MAX", 16)
    model = _tiny()
    srv, client = _holder()
    try:
        e = eng.Engine(model, _link(model, client, wire), window=16, block=8, chunk=chunk)
        ids = _prompt(70)
        out, st = e.generate(ids, 24)
        ref = eng.reference_greedy(model, ids, 24)
        assert out == ref
        assert st["far_keys"] >= 70 - 16 - 8  # most of the prompt lives on the holder
        assert st["near_keys"] < 16 + 8
        assert st["holder_calls"] > 0
        assert srv.holder.st.n[0] == st["far_keys"]
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def test_logits_match_every_step_and_the_engine_carries_turns():
    model = _tiny(3)
    srv, client = _holder()
    try:
        e = eng.Engine(model, _link(model, client), window=8, block=4, chunk=16)
        ids = _prompt(40, 5)
        e.generate(ids, 6)
        more = _prompt(12, 6)
        out2, _ = e.generate(more, 6)
        # the second turn equals a local run over turn 1 + its reply + turn 2
        local = eng.Engine(model, None, window=10**9)
        local.generate(ids, 6)
        second, _ = local.generate(more, 6)
        assert out2 == second
        assert e.far > 0 and local.far == 0
        # one step's logits, holder path vs the model's own attention: fp16 on the wire only
        e2 = eng.Engine(
            model,
            _link(model, kv.KVHolderClient("127.0.0.1", srv.server_address[1])),
            window=8,
            block=4,
        )
        got = e2.feed(ids)
        with torch.no_grad():
            want = model(input_ids=torch.tensor([ids])).logits[0, -1]
        assert torch.allclose(got, want, atol=2e-3, rtol=1e-3)
        e2.link.client.close()
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def test_through_a_relay_split_over_two_dialed_in_holders():
    token = "t-" + str(time.time_ns())
    relay, servers = net.start_relay(token, ("127.0.0.1", 0), ("127.0.0.1", 0))
    ep = servers[0].server_address[1]
    wp = servers[1].server_address[1]
    url = f"ws://127.0.0.1:{wp}/holder"
    # 1 MB each = 128 keys of this model on the v3 wire: the relay must split the prompt
    holders = [kv.KVHolder(1 << 20, device=f"h{i}", store="f32") for i in range(2)]
    for h in holders:
        threading.Thread(target=net.dial_holder, args=(url, token, h), daemon=True).start()
    end = time.time() + 10
    while len(relay.holders) < 2 and time.time() < end:
        time.sleep(0.05)
    assert len(relay.holders) == 2
    model = _tiny(7)
    client = kv.KVHolderClient("127.0.0.1", ep, timeout=30)
    try:
        e = eng.Engine(model, _link(model, client, "v3"), window=8, block=8, chunk=64)
        ids = _prompt(220, 9)
        out, st = e.generate(ids, 16)
        assert out == eng.reference_greedy(model, ids, 16)
        assert all(h.st.n[0] > 0 for h in holders), [h.st.n[0] for h in holders]
        assert sum(h.st.n[0] for h in holders) == st["far_keys"]
    finally:
        client.close()
        net.stop_relay(relay, servers)


def test_cli_registers_chat_without_importing_torch_heavy_paths():
    from adk import kvholder_cli

    ap = argparse.ArgumentParser()
    kvholder_cli.register(ap.add_subparsers(dest="command"))
    a = ap.parse_args(["kvholder", "chat", "--window", "64", "--verify", "8", "--wire", "v4"])
    assert (a.kvholder_action, a.window, a.verify, a.wire) == ("chat", 64, 8, "v4")


def test_v3_refuses_a_shape_it_cannot_pad():
    class _C:
        def configure(self, cfg):
            raise AssertionError("must refuse before CONFIG")

    with pytest.raises(ValueError, match="v4"):
        eng.HolderLink(_C(), 2, 1, 8, 128, wire="v3")  # 8 query heads per KV head > 6


def _offset_keys(n=600, h=2, d=64, seed=0):
    """Keys with a large shared per-head offset, the shape real models' keys have."""
    rng = np.random.default_rng(seed)
    off = 6.0 * rng.standard_normal((1, h, d)).astype(np.float32)
    keys = rng.standard_normal((n, h, d)).astype(np.float32) + off
    vals = rng.standard_normal((n, h, d)).astype(np.float32)
    q = rng.standard_normal((5, 2 * h, d)).astype(np.float32)  # 5 tokens, 2 query heads per KV
    return q, keys, vals


def _exact(q, keys, vals, scale):
    """Reference over every key: O [T, H, D], lse [T, H] (query head h reads KV head h // 2)."""
    s = np.einsum("thd,nhd->thn", q.astype(np.float64), np.repeat(keys, 2, 1)) * scale
    m = s.max(-1, keepdims=True)
    p = np.exp(s - m)
    o = np.einsum("thn,nhd->thd", p / p.sum(-1, keepdims=True), np.repeat(vals, 2, 1))
    return o, (m[..., 0] + np.log(p.sum(-1)))


# q4_0 tol: the 4-bit VALUES alone cost ~0.1 on random data; the lse check is the exactness one
@pytest.mark.parametrize("kv_type, center, tol", [("f16", True, 3e-3), ("q4_0", True, 0.15)])
def test_centered_far_keys_are_exact_up_to_the_row_format(kv_type, center, tol):
    q, keys, vals = _offset_keys()
    srv, client = _holder()
    try:
        link = eng.HolderLink(client, 1, 2, 4, 64, kv_type=kv_type, center=center)
        link.append(0, keys[:256], vals[:256])
        link.append(0, keys[256:], vals[256:])  # one mean, fixed at the first append
        o, lse = link.attn(0, q, 0.125)
        want_o, want_lse = _exact(q, keys, vals, 0.125)
        # the lse carries scale * q . mu back: drop that and this fails by whole nats
        assert np.abs(lse - want_lse).max() < 0.05
        assert np.linalg.norm(o - want_o) / np.linalg.norm(want_o) < tol
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def test_centering_is_what_makes_q4_0_usable():
    q, keys, vals = _offset_keys(seed=3)
    want_o, _ = _exact(q, keys, vals, 0.125)
    err = {}
    for center in (False, True):
        srv, client = _holder()
        try:
            link = eng.HolderLink(client, 1, 2, 4, 64, kv_type="q4_0", center=center)
            link.append(0, keys, vals)
            o, _ = link.attn(0, q, 0.125)
            err[center] = np.linalg.norm(o - want_o) / np.linalg.norm(want_o)
        finally:
            client.close()
            srv.shutdown()
            srv.server_close()
    assert err[True] < err[False] / 2, err


def test_perplexity_through_a_holder_matches_local():
    model = _tiny(11)
    ids = _prompt(96, 12)
    local = eng.perplexity(eng.Engine(model, None, window=10**9), ids, 32, chunk=8)
    srv, client = _holder()
    try:
        e = eng.Engine(model, _link(model, client), window=16, block=8)
        far = eng.perplexity(e, ids, 32, chunk=8)
        assert e.far > 0
        assert abs(far - local) / local < 1e-3
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def test_choose_kv_takes_the_most_compact_format_within_tol():
    ppl = {
        ("f16", False): 12.8,
        ("q8_0", False): 12.84,
        ("q8_0", True): 12.86,
        ("q4_0", False): 13.7,
        ("q4_0", True): 13.1,
    }
    assert eng.choose_kv(ppl, 0.03) == ("q4_0", True)  # centered is the q4_0 that fits
    assert eng.choose_kv(ppl, 0.01) == ("q8_0", False)
    assert eng.choose_kv(ppl, 0.001) == ("f16", False)


def test_measure_kv_runs_every_format_through_a_holder():
    ppl = eng.measure_kv(_tiny(13), _prompt(96, 14), n_eval=32, window=16)
    assert set(ppl) == {
        ("f16", False),
        ("q8_0", False),
        ("q8_0", True),
        ("q4_0", False),
        ("q4_0", True),
    }
    assert abs(ppl[("q8_0", True)] - ppl[("f16", False)]) / ppl[("f16", False)] < 0.05
