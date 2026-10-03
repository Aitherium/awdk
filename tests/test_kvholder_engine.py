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
