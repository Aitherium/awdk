"""V4.1 compressed sparse attention split across KV holders must equal the single-device math.

The single-device side below is a direct numpy port of DeepSeek-V4.1 ``inference/model.py``
(Compressor pooling, Indexer.forward scoring + masking + top-k, select_candidate_blocks,
Attention.forward's [window | compressed] concat, sparse_attn's online softmax with attn_sink)
on a tiny random model with the real model's cross-layer sharing pattern. The distributed side
is ``adk.kvholder_v41``: holders own the OLD compressed rows + index keys, the host owns the
newest slice and the window. Mutants prove the parity check fails when the protocol is wrong.
"""

from __future__ import annotations

import dataclasses

import pytest

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only

from adk import kvholder as kv  # noqa: E402
from adk import kvholder_v41 as k41  # noqa: E402

# The real V4.1-Flash-1017 config.json (the fields the attention layout reads).
REAL = {
    "n_layers": 40,
    "kv_source_layers": [2, 8, 14, 20],
    "index_source_layers": [2, 8, 14, 20, 24, 28, 32, 36],
    "index_topk": 512,
    "candidate_source_layer": 20,
    "candidate_topk_blocks": 2048,
    "candidate_block_size": 8,
    "compress_ratios": [0, 0] + [2] * 18 + [1] * 20 + [0, 0, 0],
}

# A tiny model with the same shape of sharing: window-only layer, a ratio-2 group whose second
# index source reuses the first's index keys, then a ratio-1 group led by the candidate source
# with a level-two consumer. top-k is small so the selection really bites.
TINY = {
    "n_layers": 8,
    "compress_ratios": [0, 2, 2, 2, 1, 1, 1, 1],
    "kv_source_layers": [1, 4],
    "index_source_layers": [1, 3, 4, 6],
    "candidate_source_layer": 4,
    "candidate_topk_blocks": 3,
    "candidate_block_size": 4,
    "index_topk": 5,
}
DIM, D, RD, H, HI, DI, QL, WIN = 24, 16, 4, 4, 4, 8, 12, 4


# ---------------------------------------------------------------- tiny model (model.py math)


def _rms(x, w):
    return x / np.sqrt((x * x).mean(-1, keepdims=True) + 1e-6) * w


def _rope(x, pos, theta):
    """apply_rotary_emb on the last RD dims: interleaved pairs as complex numbers."""
    x, pos = x.copy(), np.asarray(pos, np.float64)
    ang = np.multiply.outer(pos, theta ** (-np.arange(0, RD, 2) / RD))
    ang = ang.reshape(pos.shape + (1,) * (x.ndim - pos.ndim - 1) + ang.shape[-1:])
    tail = x[..., -RD:]
    c = (tail[..., 0::2] + 1j * tail[..., 1::2]) * np.exp(1j * ang)
    x[..., -RD:][..., 0::2], x[..., -RD:][..., 1::2] = c.real, c.imag
    return x


class Tiny:
    def __init__(self, cfg: dict, n_tok: int, seed: int, f32: bool = False):
        rng = np.random.default_rng(seed)
        self.lay = k41.V41Layout.from_config(cfg)
        nl = self.lay.n_layers
        g = lambda *s: rng.standard_normal(s) / np.sqrt(s[0])  # noqa: E731
        self.x = rng.standard_normal((nl, n_tok, DIM))
        self.w = []
        for il in range(nl):
            w = {
                "wq_a": g(DIM, QL),
                "q_norm": 1 + 0.1 * rng.standard_normal(QL),
                "wq_b": g(QL, H * D) * 3,
                "wkv": g(DIM, D),
                "kv_norm": 1 + 0.1 * rng.standard_normal(D),
                "sink": rng.standard_normal(H),
            }
            if self.lay.is_index_source(il):
                w |= {"iq": g(QL, HI * DI) * 3, "iw": g(DIM, HI) * 4}
            if il in self.lay.kv_source_layers:
                w |= {
                    "cwkv": g(DIM, D),
                    "cwgate": g(DIM, D),
                    "cnorm": 1 + 0.1 * rng.standard_normal(D),
                    "ik": g(D, DI) * 2,
                    "iknorm": 1 + 0.1 * rng.standard_normal(DI),
                }
            self.w.append(w)
        # compressed caches for every kv source, built once over the whole history
        self.ckv, self.cik = {}, {}
        for s in self.lay.kv_source_layers:
            r, w = self.lay.ratio(s), self.w[s]
            n = n_tok // r
            xs = self.x[s, : n * r]
            kvp, sc = xs @ w["cwkv"], xs @ w["cwgate"]
            if r == 1:
                lat = _rms(kvp, w["cnorm"])
            else:  # softmax-gated pooling per channel over each group of r tokens
                kvp, sc = kvp.reshape(n, r, D), sc.reshape(n, r, D)
                p = np.exp(sc - sc.max(1, keepdims=True))
                lat = _rms((kvp * p / p.sum(1, keepdims=True)).sum(1), w["cnorm"])
            pos = np.arange(n) * r  # a latent stands for the first token of its group
            self.cik[s] = _rope(_rms(lat @ w["ik"], w["iknorm"]), pos, 160000.0)
            self.ckv[s] = _rope(lat, pos, 160000.0)
        if f32:  # the wire carries f32: make every input exactly f32-representable
            self.ckv = {s: a.astype(np.float32).astype(np.float64) for s, a in self.ckv.items()}
            self.cik = {s: a.astype(np.float32).astype(np.float64) for s, a in self.cik.items()}
        self.f32 = f32

    def _c(self, a):
        return a.astype(np.float32).astype(np.float64) if self.f32 else a

    def q(self, il, pos):
        w = self.w[il]
        qr = _rms(self.x[il, pos] @ w["wq_a"], w["q_norm"])
        theta = 160000.0 if self.lay.ratio(il) else 10000.0
        return self._c(_rope((qr @ w["wq_b"]).reshape(len(pos), H, D), pos, theta)), qr

    def index_q(self, il, pos):
        w = self.w[il]
        _, qr = self.q(il, pos)
        qi = _rope((qr @ w["iq"]).reshape(len(pos), HI, DI), pos, 160000.0)
        wt = (self.x[il, pos] @ w["iw"]) * (DI**-0.5 * HI**-0.5)
        return self._c(qi), self._c(wt)

    def window(self, il, p):
        w = self.w[il]
        t = np.arange(max(0, p - WIN + 1), p + 1)
        theta = 160000.0 if self.lay.ratio(il) else 10000.0
        return self._c(_rope(_rms(self.x[il, t] @ w["wkv"], w["kv_norm"]), t, theta))


# ---------------------------------------------------------------- single-device reference


def ref_select_candidate_blocks(logits, clens, topk_blocks, block_size):
    """model.py select_candidate_blocks, numpy."""
    s, width = logits.shape
    pad = -width % block_size
    sc = np.concatenate([logits, np.full((s, pad), -np.inf)], 1).reshape(s, -1, block_size).max(-1)
    nb = sc.shape[1]
    last = (clens - 1) // block_size
    sc[np.arange(nb)[None, :] == last[:, None]] = np.inf
    k = min(topk_blocks, nb)
    top = np.argsort(-sc, axis=1, kind="stable")[:, :k]
    keep = np.zeros_like(sc, bool)
    np.put_along_axis(keep, top, np.take_along_axis(sc, top, 1) > -np.inf, 1)
    return np.repeat(keep, block_size, axis=1)[:, :width]


def ref_indexer(m: Tiny, il, pos, end_pos, shared):
    """model.py Indexer.forward for queries at ``pos`` (chunk ending at end_pos)."""
    lay = m.lay
    r = lay.ratio(il)
    width = end_pos // r
    qi, wt = m.index_q(il, pos)
    ik = m.cik[lay.kv_src(il)][:width]
    score = np.einsum("sht,sh->st", np.maximum(np.einsum("shd,td->sht", qi, ik), 0), wt)
    clens = (np.asarray(pos) + 1) // r
    score[np.arange(width)[None, :] >= clens[:, None]] = -np.inf
    if lay.is_cand_source(il):
        shared["cand"] = ref_select_candidate_blocks(
            score, clens, lay.candidate_topk_blocks, lay.candidate_block_size
        )
    elif lay.uses_candidates(il):
        score = np.where(shared["cand"], score, -np.inf)
    k = min(lay.index_topk, width)
    idxs = np.sort(np.argsort(-score, axis=1, kind="stable")[:, :k], axis=1)
    return [row[row < c] for row, c in zip(idxs, clens)]


def ref_step(m: Tiny, start: int, n_q: int):
    """Every layer's attention output for queries start..start+n_q-1, single device."""
    lay, pos, end_pos = m.lay, np.arange(start, start + n_q), start + n_q
    shared, outs, sels = {}, [], []
    for il in range(lay.n_layers):
        q, _ = m.q(il, pos)
        scale = D**-0.5
        if lay.ratio(il) and lay.is_index_source(il):
            shared["topk"] = ref_indexer(m, il, pos, end_pos, shared)
        o = np.zeros((n_q, H, D))
        for s, p in enumerate(pos):
            rows = m.window(il, p)
            if lay.ratio(il):
                rows = np.concatenate([rows, m.ckv[lay.kv_src(il)][shared["topk"][s]]])
            lg = (q[s] @ rows.T) * scale  # [H, n]  -- sparse_attn
            mx = np.maximum(lg.max(1), m.w[il]["sink"])
            e = np.exp(lg - mx[:, None])
            o[s] = (e @ rows) / (e.sum(1) + np.exp(m.w[il]["sink"] - mx))[:, None]
        outs.append(o)
        sels.append([set(x.tolist()) for x in shared["topk"]] if lay.ratio(il) else None)
    return outs, sels


# ---------------------------------------------------------------- distributed


def build_parts(m: Tiny, cuts, end_pos, wire=False, lay=None):
    """Holders own raw [cuts[i], cuts[i+1]); the host owns [cuts[-1], end_pos)."""
    lay, parts = lay or m.lay, []
    bounds = list(cuts) + [None]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        h = k41.V41Holder(lay, lo, hi)
        part = k41.V41HolderClient(h.handle) if wire and hi is not None else h
        for s in lay.kv_source_layers:
            r = lay.ratio(s)
            c0, c1 = lo // r, (end_pos // r if hi is None else hi // r)
            part.load(s, c0, m.ckv[s][c0:c1], m.cik[s][c0:c1])
        parts.append(part)
    return parts


def dist_step(m: Tiny, start, n_q, cuts, coord_cls=k41.V41Coordinator, wire=False, lay=None):
    lay = lay or m.lay
    pos, end_pos = np.arange(start, start + n_q), start + n_q
    assert start >= cuts[-1], "holders keep only pages older than every query"
    coord = coord_cls(lay, build_parts(m, cuts, end_pos, wire, lay))
    outs, sels = [], []
    for il in range(lay.n_layers):
        q, _ = m.q(il, pos)
        scale = D**-0.5
        partials = []
        if lay.ratio(il):
            if lay.is_index_source(il):
                qi, wt = m.index_q(il, pos)
                coord.index_layer(il, qi, wt, (pos + 1) // lay.ratio(il), end_pos)
            partials += coord.compressed_partials(il, q, scale)
            if not wire:
                per_part = [p.selected(il) for p in coord.parts]
                sels.append(
                    [set(np.concatenate([pp[s] for pp in per_part]).tolist()) for s in range(n_q)]
                )
        else:
            sels.append(None)
        win = [k41.partial_softmax(q[s], m.window(il, p), scale) for s, p in enumerate(pos)]
        partials.append((np.stack([o for o, _ in win]), np.stack([lse for _, lse in win])))
        o, _ = k41.merge_lse(partials, sink=m.w[il]["sink"])
        outs.append(o)
    coord.end_step()
    return outs, sels


CASES = [  # (seed, n_tok history, start, n_q, holder cuts)
    (1, 64, 40, 1, (0, 16, 32)),  # decode (model.py start_pos > 0 path), two holders
    (2, 64, 48, 1, (0, 8, 40)),
    (3, 72, 36, 6, (0, 12, 24, 36)),  # chunk of 6 queries, three holders
    (4, 96, 64, 4, (0, 32)),
    (5, 80, 20, 8, (0, 20)),  # few rows: queries see fewer than top-k
]


def check_parity(case, coord_cls=k41.V41Coordinator, wire=False, tol=1e-9, lay_cls=None):
    seed, n_tok, start, n_q, cuts = case
    m = Tiny(TINY, n_tok, seed, f32=wire)
    ref_o, ref_s = ref_step(m, start, n_q)
    lay = lay_cls(**dataclasses.asdict(m.lay)) if lay_cls else None
    got_o, got_s = dist_step(m, start, n_q, cuts, coord_cls, wire, lay)
    for il in range(m.lay.n_layers):
        if not wire:
            assert got_s[il] == ref_s[il], f"layer {il}: selection differs"
        err = np.abs(got_o[il] - ref_o[il]).max()
        assert err < tol, f"layer {il}: |o - ref| = {err:.3g}"


# ---------------------------------------------------------------- tests


@pytest.mark.parametrize("case", CASES, ids=[f"seed{c[0]}" for c in CASES])
def test_distributed_equals_single_device(case):
    check_parity(case)


@pytest.mark.parametrize("case", CASES[:3], ids=[f"seed{c[0]}" for c in CASES[:3]])
def test_parity_over_the_wire(case):
    check_parity(case, wire=True, tol=2e-5)  # f32 on the wire


def test_holders_carry_real_weight():
    """Guard against a vacuous pass: most selected rows in these cases live on holders."""
    seed, n_tok, start, n_q, cuts = CASES[0]
    m = Tiny(TINY, n_tok, seed)
    _, sels = ref_step(m, start, n_q)
    held = total = 0
    for il, sel in enumerate(sels):
        if sel:
            r = m.lay.ratio(il)
            for s in sel:
                total += len(s)
                held += sum(1 for c in s if c < cuts[-1] // r)
    assert total and held / total > 0.5


def test_merge_matches_patn_merge_and_sink_is_a_zero_key():
    rng = np.random.default_rng(0)
    q, rows = rng.standard_normal((H, D)), rng.standard_normal((20, D))
    a, b = k41.partial_softmax(q, rows[:7], 0.3), k41.partial_softmax(q, rows[7:], 0.3)
    full = k41.partial_softmax(q, rows, 0.3)
    o, lse = k41.merge_lse([a, b])
    assert np.allclose(o, full[0]) and np.allclose(lse, full[1])
    o32, lse32 = kv.merge_partials([a, b])  # the PATN v3 merge agrees
    assert np.allclose(o32, o, atol=1e-5) and np.allclose(lse32, lse, atol=1e-5)
    sink = rng.standard_normal(H)
    o_s, _ = k41.merge_lse([a, b], sink=sink)
    den = np.exp(full[1]) + np.exp(sink)
    assert np.allclose(o_s, full[0] * (np.exp(full[1]) / den)[:, None])


def test_numpy_reference_matches_torch_model_py():
    """The numpy port above is model.py: same candidate blocks and top-k as the torch code."""
    torch = pytest.importorskip("torch")
    f = torch.nn.functional

    def select_candidate_blocks(
        logits, compress_lens, topk_blocks, block_size
    ):  # model.py verbatim
        width = logits.size(-1)
        scores = f.pad(logits, (0, -width % block_size), value=-torch.inf)
        scores = scores.unflatten(-1, (-1, block_size)).amax(dim=-1)
        num_blocks = scores.size(-1)
        last = (compress_lens - 1) // block_size
        scores = scores.masked_fill(
            torch.arange(num_blocks, device=logits.device) == last, torch.inf
        )
        top = scores.topk(min(topk_blocks, num_blocks), dim=-1)
        keep = torch.zeros_like(scores, dtype=torch.bool).scatter_(
            -1, top.indices, top.values > -torch.inf
        )
        return keep.repeat_interleave(block_size, dim=-1)[..., :width]

    rng = np.random.default_rng(7)
    for width, s in ((37, 5), (64, 3), (9, 4)):
        score = rng.standard_normal((s, width))
        clens = rng.integers(0, width + 1, s)
        score[np.arange(width)[None, :] >= clens[:, None]] = -np.inf
        t_score = torch.from_numpy(score)
        got = select_candidate_blocks(t_score, torch.from_numpy(clens).unsqueeze(-1), 3, 4).numpy()
        assert (got == ref_select_candidate_blocks(score.copy(), clens, 3, 4)).all()
        k = min(5, width)  # Indexer.forward's top-k, re-sorted, unreachable -> -1
        idxs = t_score.topk(k, dim=-1, sorted=False).indices.sort(dim=-1).values
        t_sel = torch.where(idxs < torch.from_numpy(clens).unsqueeze(-1), idxs, -1).numpy()
        n_sel = np.sort(np.argsort(-score, axis=1, kind="stable")[:, :k], axis=1)
        for row_t, row_n, c in zip(t_sel, n_sel, clens):
            assert set(row_t[row_t >= 0].tolist()) == set(row_n[row_n < c].tolist())


def test_real_config_layout():
    lay = k41.V41Layout.from_config(REAL)
    assert lay.kv_src(0) == -1 and lay.topk_src(1) == -1
    assert (lay.kv_src(5), lay.topk_src(5)) == (2, 2)
    assert (lay.kv_src(13), lay.topk_src(13)) == (8, 8)
    assert (lay.kv_src(23), lay.topk_src(23)) == (20, 20)
    assert (lay.kv_src(37), lay.topk_src(37)) == (20, 36)  # index keys of 20, selection of 36
    assert lay.is_cand_source(20) and not lay.uses_candidates(20)
    assert all(lay.uses_candidates(i) for i in (24, 28, 32, 36)) and not lay.uses_candidates(14)
    assert lay.align() == 8 and lay.rows_per_token() == 2.5
    # f16 rows of [512 KV | 128 index K]: 3.2 KB per raw token, 128k tokens ~ 0.42 GB
    assert k41.holder_bytes(lay, 131072, 512, 128, 2) == int(2.5 * 131072 * 640 * 2)


def test_layout_rejects_inconsistent_configs():
    bad = dict(REAL, compress_ratios=[0, 0] + [2] * 18 + [1] * 3 + [2] * 17 + [0, 0, 0])
    with pytest.raises(ValueError):
        k41.V41Layout.from_config(bad)
    with pytest.raises(ValueError):
        k41.V41Layout.from_config(dict(REAL, candidate_topk_blocks=10))  # 80 < 512
    lay = k41.V41Layout.from_config(TINY)
    with pytest.raises(ValueError):
        k41.V41Holder(lay, 0, 6)  # not a multiple of align() == 4


def test_wire_errors_are_reported_not_raised():
    lay = k41.V41Layout.from_config(TINY)
    h = k41.V41Holder(lay, 0, 8)
    rt, msg = h.handle(
        k41.V41_SCORE, k41.pack_score(2, np.zeros((1, HI, DI)), np.zeros((1, HI)), [1], None)
    )
    assert rt == k41.V41_ERR and b"not an index source" in msg
    assert h.handle(99, b"")[0] == k41.V41_ERR


# ---------------------------------------------------------------- mutants: the check must bite


class _LocalTopk(k41.V41Coordinator):
    """MUTANT: every part keeps its own top-k (no global merge)."""

    def index_layer(self, layer, q_idx, w, clens, end_pos):
        lay = self.layout
        cl = np.asarray(clens)
        pins = np.where(cl > 0, (cl - 1) // lay.candidate_block_size, k41.NO_PIN)
        for p in self.parts:
            rep = p.score(layer, q_idx, w, clens, pins if lay.is_cand_source(layer) else None)
            thr = k41.global_threshold([rep["pos"]], lay.index_topk)
            blk = (
                k41.global_threshold([rep["blk"]], lay.candidate_topk_blocks)
                if "blk" in rep
                else None
            )
            p.select(layer, thr, blk)


class _NoPin(k41.V41Coordinator):
    """MUTANT: forgets select_candidate_blocks' pin on the newest (partial) block."""

    def index_layer(self, layer, q_idx, w, clens, end_pos):
        lay = self.layout
        width = end_pos // lay.ratio(layer)
        reps = [p.score(layer, q_idx, w, clens, None) for p in self.parts]
        thr = k41.global_threshold([x["pos"] for x in reps], min(lay.index_topk, width))
        blk = None
        if lay.is_cand_source(layer):
            n_blk = -(-width // lay.candidate_block_size)
            blk = k41.global_threshold(
                [x["blk"] for x in reps], min(lay.candidate_topk_blocks, n_blk)
            )
        for p in self.parts:
            p.select(layer, thr, blk)


def _fails_somewhere(**kw) -> bool:
    failed = 0
    for case in CASES:
        try:
            check_parity(case, **kw)
        except AssertionError:
            failed += 1
    return failed > 0


def test_mutant_local_topk_fails():
    assert _fails_somewhere(coord_cls=_LocalTopk)


def test_mutant_missing_pin_fails():
    assert _fails_somewhere(coord_cls=_NoPin)


def test_mutant_strict_threshold_fails(monkeypatch):
    """MUTANT: selecting strictly above the k-th key drops the k-th row."""

    def strict(scores, ids, thr_s, thr_i):
        return (scores > -np.inf) & (scores > thr_s[:, None])

    monkeypatch.setattr(k41, "at_or_above", strict)
    assert _fails_somewhere()


class _NoCandidates(k41.V41Layout):
    """MUTANT: level-two layers score over every row instead of the candidate blocks."""

    def uses_candidates(self, layer):
        return False


def test_mutant_ignoring_candidates_fails():
    assert _fails_somewhere(lay_cls=_NoCandidates)


_real_merge = k41.merge_lse


def _average_merge(parts, sink=None):
    return np.mean([p[0] for p in parts], 0), None


def _no_sink_merge(parts, sink=None):
    return _real_merge(parts)


@pytest.mark.parametrize("bad_merge", [_average_merge, _no_sink_merge], ids=["average", "no-sink"])
def test_mutant_merge_fails(monkeypatch, bad_merge):
    monkeypatch.setattr(k41, "merge_lse", bad_merge)
    assert _fails_somewhere()


def test_correct_protocol_passes_every_case():
    """The control for the mutants: the same harness, unmutated, fails nowhere."""
    assert not _fails_somewhere()


def test_mutant_take_all_when_union_has_exactly_k_fails(monkeypatch):
    """MUTANT (a real bug found here): with exactly k finite keys in the union, "select every
    finite key" over-selects on a part whose local list was cut at k."""
    real = k41.global_threshold

    def take_all_at_k(parts, k):
        thr_s, thr_i = real(parts, k)
        fin = (np.concatenate([p[0] for p in parts], axis=1) > -np.inf).sum(1)
        return np.where(fin == k, -np.inf, thr_s), np.where(fin == k, k41._ALL, thr_i)

    monkeypatch.setattr(k41, "global_threshold", take_all_at_k)
    assert _fails_somewhere()
