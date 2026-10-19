"""adk kvholder_v41 — exact distributed attention for DeepSeek-V4.1's compressed sparse attention.

V4.1 attention (reference ``inference/model.py``, Attention / Indexer / select_candidate_blocks)
is one softmax over two KV sources: a sliding window of raw KV (host-only, ``window_size`` rows)
plus ``index_topk`` compressed rows that an indexer picks out of the whole history. The
compressed rows and the indexer keys are what grow with context, so they are what a KV holder
(phone, laptop, mesh node) keeps for its OLD page range. The window, the sink and everything
with weights stay on the host; a holder needs no weights at all.

Per decode step and per layer the host and the holders run this protocol (``KVHOLDER_V41.md``
has the proof and the C++ plan):

1. SCORE (index-source layers only). The host sends the indexer query ``q_idx [S, Hi, Di]``
   (already RoPE'd and quantized), the head weights ``w [S, Hi]`` (``weights_proj(x)`` times
   its scale), the per-query visible length and the pinned block. Each part computes
   ``score[t] = sum_h relu(q_idx_h . k_t) * w_h`` over ITS rows only -- a score depends on one
   index key and the query, nothing else -- and returns its local top-k (and, on the
   candidate-source layer, its local top-k_b blocks by block max).
2. MERGE. The host takes the global top-k of the union. Any member of the global top-k is in
   its own part's local top-k (fewer than k items outrank it anywhere), so the union holds the
   whole answer. The host returns one threshold per query: the k-th key under the total order
   (score desc, position asc).
3. ATTEND. Each part selects its rows whose key is at or above the threshold (exactly the
   global top-k restricted to that part), then computes the partial softmax over them for the
   layer's real query ``q [S, H, D]``: ``(O, lse)`` per head. Layers between index sources
   reuse the selection, exactly as the reference reuses ``shared_attn.topk_idxs``.
4. LSE MERGE. The host merges every part's ``(O, lse)``, its window partial and the attention
   sink (a key with logit ``sink[h]`` and value 0) with the log-sum-exp merge. The result is
   the single-device softmax, bit-for-bit up to float reassociation.

Selection is exact with respect to the total order (score desc, position asc). The reference's
``torch.topk`` breaks exact ties arbitrarily, so on a genuine tie (two finite scores that are
equal to the last bit) the reference itself is not defined; this module pins it down.

Wire messages (``handle``) extend PATN with types 40-44; payloads are little-endian, scores
travel as f64 so a holder compares its cached score against a threshold that is bit-identical.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

try:  # numpy is optional for awdk; this module is all math and needs it
    import numpy as np
except ImportError:  # pragma: no cover - the CLI lanes import adk without numpy
    np = None  # type: ignore[assignment]

# ---------------------------------------------------------------- wire (PATN extension)

V41_LOAD, V41_SCORE, V41_SCORE_OK, V41_ATTEND, V41_ATTEND_OK = 40, 41, 42, 43, 44
V41_OK, V41_ERR = 9, 10  # PATN OK / ERR
V41_TYPES = (V41_LOAD, V41_SCORE, V41_ATTEND)

LOAD_REQ = struct.Struct("<IIIII")  # src layer, first compressed position, n rows, D, Di
SCORE_REQ = struct.Struct("<IIII")  # layer, S, Hi, Di
SCORE_REP = struct.Struct("<III")  # S, m positions, m blocks
ATTEND_REQ = struct.Struct("<IIIIIf")  # layer, S, H, D, flags (1 pos thr, 2 blk thr), scale
ATTEND_REP = struct.Struct("<III")  # S, H, D

NO_PIN = -1
_ALL = np.iinfo(np.int64).max if np is not None else 0  # threshold id meaning "every finite key"


def _need_numpy() -> None:
    if np is None:
        raise RuntimeError("adk.kvholder_v41 needs numpy: pip install numpy")


# ---------------------------------------------------------------- layout


@dataclass(frozen=True)
class V41Layout:
    """The cross-layer sharing of V4.1's compressed attention, from the model config."""

    n_layers: int
    compress_ratios: tuple
    kv_source_layers: tuple
    index_source_layers: tuple
    candidate_source_layer: int
    candidate_topk_blocks: int
    candidate_block_size: int
    index_topk: int

    @classmethod
    def from_config(cls, cfg: dict) -> V41Layout:
        lay = cls(
            n_layers=int(cfg["n_layers"]),
            compress_ratios=tuple(int(r) for r in cfg["compress_ratios"][: int(cfg["n_layers"])]),
            kv_source_layers=tuple(int(x) for x in cfg["kv_source_layers"]),
            index_source_layers=tuple(int(x) for x in cfg["index_source_layers"]),
            candidate_source_layer=int(cfg.get("candidate_source_layer", -1)),
            candidate_topk_blocks=int(cfg.get("candidate_topk_blocks", 0)),
            candidate_block_size=int(cfg.get("candidate_block_size", 1)),
            index_topk=int(cfg["index_topk"]),
        )
        lay.validate()
        return lay

    def ratio(self, layer: int) -> int:
        return self.compress_ratios[layer]

    def kv_src(self, layer: int) -> int:
        """The kv source whose compressed rows (and index keys) this layer reads; -1 if none."""
        if not self.ratio(layer):
            return -1
        return max((s for s in self.kv_source_layers if s <= layer), default=-1)

    def topk_src(self, layer: int) -> int:
        """The index source whose top-k selection this layer attends with; -1 if none."""
        if not self.ratio(layer):
            return -1
        return max((s for s in self.index_source_layers if s <= layer), default=-1)

    def is_index_source(self, layer: int) -> bool:
        return bool(self.ratio(layer)) and layer in self.index_source_layers

    def is_cand_source(self, layer: int) -> bool:
        return layer == self.candidate_source_layer

    def uses_candidates(self, layer: int) -> bool:
        return self.is_index_source(layer) and 0 <= self.candidate_source_layer < layer

    def validate(self) -> None:
        for il in range(self.n_layers):
            r = self.ratio(il)
            if not r:
                continue
            kv, tk = self.kv_src(il), self.topk_src(il)
            if kv < 0 or tk < 0:
                raise ValueError(f"layer {il}: compressed but no kv/index source at or before it")
            if self.ratio(kv) != r or self.ratio(tk) != r:
                raise ValueError(f"layer {il}: ratio {r} differs from its sources' ratio")
        cs = self.candidate_source_layer
        if cs >= 0:
            if not self.is_index_source(cs):
                raise ValueError("candidate source layer must be an index source")
            if self.candidate_topk_blocks * self.candidate_block_size < self.index_topk:
                # otherwise the reference's topk can pick a reachable -inf (non-candidate) row
                raise ValueError("candidate_topk_blocks * block_size must cover index_topk")

    def align(self) -> int:
        """Raw-token multiple every holder page boundary must sit on (whole groups, whole blocks)."""
        a = 1
        for s in self.kv_source_layers:
            r = self.ratio(s)
            if not r:
                continue
            cs = self.candidate_source_layer
            blk = self.candidate_block_size if cs >= 0 and self.kv_src(cs) == s else 1
            a = a * (r * blk) // math.gcd(a, r * blk)
        return a

    def rows_per_token(self) -> float:
        """Compressed rows a holder stores per raw token (index keys ride along, one per row)."""
        return sum(1.0 / self.ratio(s) for s in self.kv_source_layers if self.ratio(s))


def holder_bytes(lay: V41Layout, n_tokens: int, head_dim: int, index_dim: int, elem: float) -> int:
    """Bytes a holder needs for ``n_tokens`` raw tokens: rows x (KV + index key) x element size."""
    return int(lay.rows_per_token() * n_tokens * (head_dim + index_dim) * elem)


# ---------------------------------------------------------------- the math


def index_scores(q_idx, w, keys):
    """score[s, t] = sum_h relu(q_idx[s, h] . keys[t]) * w[s, h]  (model.py Indexer.forward)."""
    dots = np.einsum("shd,td->sht", q_idx.astype(np.float64), keys.astype(np.float64))
    return np.einsum("sht,sh->st", np.maximum(dots, 0.0), w.astype(np.float64))


def block_max(scores, ids, block_size: int):
    """Best score per whole block of positions; ``ids`` must start on a block boundary."""
    s, n = scores.shape
    pad = -n % block_size
    if pad:
        scores = np.concatenate([scores, np.full((s, pad), -np.inf)], axis=1)
    bmax = scores.reshape(s, -1, block_size).max(axis=2)
    first = int(ids[0]) if len(ids) else 0
    return bmax, first // block_size + np.arange(bmax.shape[1], dtype=np.int64)


def local_topk(scores, ids, k: int):
    """Per query, the ``k`` best finite keys under (score desc, id asc), padded with -inf.

    Returns (scores [S, m], ids [S, m]) with m = min(k, n)."""
    s, n = scores.shape
    m = min(k, n)
    out_s = np.full((s, m), -np.inf)
    out_i = np.full((s, m), -1, np.int64)
    for q in range(s):
        order = np.lexsort((ids, -scores[q]))[:m]
        out_s[q], out_i[q] = scores[q, order], ids[order]
    out_i[~np.isfinite(out_s) & (out_s < 0)] = -1
    return out_s, out_i


def global_threshold(parts: list, k: int):
    """Host merge: the k-th key per query over every part's local top-k.

    ``parts`` is a list of (scores [S, m_i], ids [S, m_i]). Returns (thr_score [S], thr_id [S]);
    a query with fewer than k finite keys gets (-inf, ALL): select every finite key."""
    s = parts[0][0].shape[0]
    allsc = np.concatenate([p[0] for p in parts], axis=1)
    allid = np.concatenate([p[1] for p in parts], axis=1)
    thr_s = np.full(s, -np.inf)
    thr_i = np.full(s, _ALL, np.int64)
    for q in range(s):
        fin = allsc[q] > -np.inf
        sc, ix = allsc[q][fin], allid[q][fin]
        if k <= 0:
            thr_s[q], thr_i[q] = np.inf, -1  # nothing reachable: select nothing
        elif sc.size >= k:
            # == k matters: a part whose local list was cut at k holds MORE finite keys than
            # it sent, so "every finite key" would over-select; the k-th key is the bound
            kth = np.lexsort((ix, -sc))[k - 1]
            thr_s[q], thr_i[q] = sc[kth], ix[kth]
        # else fewer than k finite in the union: no part truncated a finite key, take them all
    return thr_s, thr_i


def at_or_above(scores, ids, thr_s, thr_i):
    """Keys at or above the threshold under (score desc, id asc); -inf never qualifies."""
    ts, ti = thr_s[:, None], thr_i[:, None]
    return (scores > -np.inf) & ((scores > ts) | ((scores == ts) & (ids[None, :] <= ti)))


def partial_softmax(q, rows, scale: float):
    """One part's share: q [H, D] against rows [m, D] (K = V, one latent head) -> (O [H, D], lse [H])."""
    h = q.shape[0]
    if rows.shape[0] == 0:
        return np.zeros((h, q.shape[1])), np.full(h, -np.inf)
    lg = (q.astype(np.float64) @ rows.astype(np.float64).T) * scale  # [H, m]
    mx = lg.max(axis=1)
    p = np.exp(lg - mx[:, None])
    den = p.sum(axis=1)
    return (p @ rows.astype(np.float64)) / den[:, None], mx + np.log(den)


def merge_lse(parts: list, sink=None):
    """Exact log-sum-exp merge of (O [..., D], lse [...]) over disjoint key sets.

    ``sink`` [H] is V4.1's attn_sink: one extra logit per head whose value is zero, so it only
    adds exp(sink) to the denominator (``sparse_attn``: ``sum_exp += exp(attn_sink - max)``)."""
    if sink is not None:
        o0, l0 = parts[0]
        sink_lse = np.broadcast_to(np.asarray(sink, np.float64), np.shape(l0))
        parts = list(parts) + [(np.zeros(np.shape(o0)), sink_lse)]
    lses = np.stack([np.asarray(p[1], np.float64) for p in parts])
    mx = lses.max(axis=0)
    safe = np.where(np.isfinite(mx), mx, 0.0)
    wts = np.where(np.isfinite(lses), np.exp(lses - safe), 0.0)
    tot = wts.sum(axis=0)
    out = sum(wts[i][..., None] * np.asarray(parts[i][0], np.float64) for i in range(len(parts)))
    ok = tot > 0
    out = np.where(ok[..., None], out / np.where(ok, tot, 1.0)[..., None], 0.0)
    return out, np.where(ok, safe + np.log(np.where(ok, tot, 1.0)), -np.inf)


# ---------------------------------------------------------------- a part (holder or host slice)


@dataclass
class _Table:
    c0: int  # first compressed position
    kv: object  # [n, D]
    ik: object  # [n, Di]


@dataclass
class V41Holder:
    """Holds compressed rows + index keys of raw tokens [raw_lo, raw_hi) for every kv source.

    ``raw_hi=None`` is an open tail (the host's own newest slice); a holder's ``raw_hi`` is fixed
    and aligned. Per step it caches its scores and selections; nothing else is state."""

    layout: V41Layout
    raw_lo: int
    raw_hi: int | None = None
    tables: dict = field(default_factory=dict)
    _scores: dict = field(default_factory=dict)  # layer -> (scores [S, n], ids [n])
    _sel: dict = field(default_factory=dict)  # index layer -> bool [S, n]
    _cand: object = None  # bool [S, n] over the candidate source's table

    def __post_init__(self) -> None:
        _need_numpy()
        a = self.layout.align()
        if self.raw_lo % a or (self.raw_hi is not None and self.raw_hi % a):
            raise ValueError(f"holder page boundaries must be multiples of {a} raw tokens")

    def comp_range(self, src: int) -> tuple[int, int | None]:
        r = self.layout.ratio(src)
        return self.raw_lo // r, (None if self.raw_hi is None else self.raw_hi // r)

    def load(self, src: int, c0: int, kv, ik) -> None:
        """Store (or extend) rows starting at compressed position ``c0`` of kv source ``src``."""
        lo, hi = self.comp_range(src)
        t = self.tables.get(src)
        if t is None:
            if c0 != lo:
                raise ValueError(f"src {src}: first row must be {lo}, got {c0}")
            t = self.tables[src] = _Table(
                c0, np.zeros((0, kv.shape[1])), np.zeros((0, ik.shape[1]))
            )
        if c0 != t.c0 + t.kv.shape[0]:
            raise ValueError(f"src {src}: rows must be contiguous (next is {t.c0 + t.kv.shape[0]})")
        if hi is not None and c0 + kv.shape[0] > hi:
            raise ValueError(f"src {src}: rows past this holder's range end {hi}")
        t.kv = np.concatenate([t.kv, np.asarray(kv, np.float64)])
        t.ik = np.concatenate([t.ik, np.asarray(ik, np.float64)])

    def _table(self, src: int) -> _Table:
        t = self.tables.get(src)
        if t is None:
            raise KeyError(f"no rows for kv source {src}")
        return t

    # -- round 1
    def score(self, layer: int, q_idx, w, clens, pins=None):
        """Local candidates for index-source ``layer``.

        Returns {"pos": (scores, ids)} and, on the candidate source, {"blk": (scores, ids)}."""
        lay = self.layout
        if not lay.is_index_source(layer):
            raise ValueError(f"layer {layer} is not an index source")
        t = self._table(lay.kv_src(layer))
        ids = t.c0 + np.arange(t.kv.shape[0], dtype=np.int64)
        sc = index_scores(q_idx, w, t.ik)
        sc[ids[None, :] >= np.asarray(clens)[:, None]] = -np.inf  # not visible to that query yet
        out = {}
        if lay.is_cand_source(layer):
            bmax, bids = block_max(sc, ids, lay.candidate_block_size)
            if pins is not None:
                bmax[np.asarray(pins)[:, None] == bids[None, :]] = np.inf
            out["blk"] = local_topk(bmax, bids, lay.candidate_topk_blocks)
            self._scores[("blk", layer)] = (bmax, bids)
        elif lay.uses_candidates(layer):
            if self._cand is None:
                raise RuntimeError(f"layer {layer}: candidate blocks not selected yet this step")
            sc = np.where(self._cand, sc, -np.inf)
        self._scores[layer] = (sc, ids)
        out["pos"] = local_topk(sc, ids, lay.index_topk)
        return out

    # -- round 2
    def select(self, layer: int, pos_thr, blk_thr=None) -> None:
        sc, ids = self._scores.pop(layer)
        self._sel[layer] = at_or_above(sc, ids, *pos_thr)
        if self.layout.is_cand_source(layer):
            bmax, bids = self._scores.pop(("blk", layer))
            keep = at_or_above(bmax, bids, *blk_thr)  # [S, n_blocks]
            bs = self.layout.candidate_block_size
            self._cand = np.repeat(keep, bs, axis=1)[:, : sc.shape[1]]

    def selected(self, layer: int) -> list:
        """Global compressed positions this part attends with at ``layer``, per query."""
        src, tk = self.layout.kv_src(layer), self.layout.topk_src(layer)
        t = self._table(src)
        ids = t.c0 + np.arange(t.kv.shape[0], dtype=np.int64)
        return [ids[m] for m in self._sel[tk]]

    def attend(self, layer: int, q, scale: float):
        """(O [S, H, D], lse [S, H]) over this part's selected rows for every query."""
        src, tk = self.layout.kv_src(layer), self.layout.topk_src(layer)
        t = self._table(src)
        sel = self._sel[tk]
        outs = [partial_softmax(q[s], t.kv[sel[s]], scale) for s in range(q.shape[0])]
        return np.stack([o for o, _ in outs]), np.stack([lse for _, lse in outs])

    def end_step(self) -> None:
        self._scores.clear()
        self._sel.clear()
        self._cand = None

    # -- wire
    def handle(self, mtype: int, p: bytes) -> tuple[int, bytes]:
        try:
            return self._handle(mtype, p)
        except (ValueError, KeyError, RuntimeError, struct.error) as e:
            return V41_ERR, str(e).encode()

    def _handle(self, mtype: int, p: bytes) -> tuple[int, bytes]:
        if mtype == V41_LOAD:
            src, c0, n, d, di = LOAD_REQ.unpack_from(p)
            body = np.frombuffer(p, np.float32, offset=LOAD_REQ.size)
            kv, ik = body[: n * d].reshape(n, d), body[n * d : n * d + n * di].reshape(n, di)
            self.load(src, c0, kv, ik)
            return V41_OK, b""
        if mtype == V41_SCORE:
            layer, s, hi, di = SCORE_REQ.unpack_from(p)
            o = SCORE_REQ.size
            q_idx = np.frombuffer(p, np.float32, s * hi * di, o).reshape(s, hi, di)
            o += q_idx.nbytes
            w = np.frombuffer(p, np.float32, s * hi, o).reshape(s, hi)
            o += w.nbytes
            clens = np.frombuffer(p, np.int64, s, o)
            pins = np.frombuffer(p, np.int64, s, o + 8 * s)
            rep = self.score(layer, q_idx, w, clens, pins)
            blk = rep.get("blk", (np.zeros((s, 0)), np.zeros((s, 0), np.int64)))
            pos = rep["pos"]
            head = SCORE_REP.pack(s, pos[0].shape[1], blk[0].shape[1])
            body = [pos[0].astype(np.float64), pos[1].astype(np.int64)]
            body += [blk[0].astype(np.float64), blk[1].astype(np.int64)]
            return V41_SCORE_OK, head + b"".join(a.tobytes() for a in body)
        if mtype == V41_ATTEND:
            layer, s, h, d, flags, scale = ATTEND_REQ.unpack_from(p)
            o = ATTEND_REQ.size
            thr = []
            for bit in (1, 2):
                if flags & bit:
                    ts = np.frombuffer(p, np.float64, s, o)
                    ti = np.frombuffer(p, np.int64, s, o + 8 * s)
                    thr.append((ts, ti))
                    o += 16 * s
                else:
                    thr.append(None)
            if thr[0] is not None:
                self.select(layer, thr[0], thr[1])
            q = np.frombuffer(p, np.float32, s * h * d, o).reshape(s, h, d)
            out, lse = self.attend(layer, q, scale)
            rep = out.astype(np.float32).tobytes() + lse.astype(np.float32).tobytes()
            return V41_ATTEND_OK, ATTEND_REP.pack(s, h, d) + rep
        return V41_ERR, f"unknown v41 message {mtype}".encode()


# ---------------------------------------------------------------- wire helpers (host side)


def pack_load(src: int, c0: int, kv, ik) -> bytes:
    kv, ik = np.asarray(kv, np.float32), np.asarray(ik, np.float32)
    return (
        LOAD_REQ.pack(src, c0, kv.shape[0], kv.shape[1], ik.shape[1]) + kv.tobytes() + ik.tobytes()
    )


def pack_score(layer: int, q_idx, w, clens, pins) -> bytes:
    q_idx = np.asarray(q_idx, np.float32)
    s, hi, di = q_idx.shape
    pins = np.full(s, NO_PIN, np.int64) if pins is None else np.asarray(pins, np.int64)
    return (
        SCORE_REQ.pack(layer, s, hi, di)
        + q_idx.tobytes()
        + np.asarray(w, np.float32).tobytes()
        + np.asarray(clens, np.int64).tobytes()
        + pins.tobytes()
    )


def unpack_score(p: bytes) -> dict:
    s, m, mb = SCORE_REP.unpack_from(p)
    o = SCORE_REP.size
    ps = np.frombuffer(p, np.float64, s * m, o).reshape(s, m)
    o += ps.nbytes
    pi = np.frombuffer(p, np.int64, s * m, o).reshape(s, m)
    o += pi.nbytes
    bs = np.frombuffer(p, np.float64, s * mb, o).reshape(s, mb)
    o += bs.nbytes
    bi = np.frombuffer(p, np.int64, s * mb, o).reshape(s, mb)
    out = {"pos": (ps, pi)}
    if mb:
        out["blk"] = (bs, bi)
    return out


def pack_attend(layer: int, q, scale: float, pos_thr=None, blk_thr=None) -> bytes:
    q = np.asarray(q, np.float32)
    s, h, d = q.shape
    flags = (1 if pos_thr is not None else 0) | (2 if blk_thr is not None else 0)
    parts = [ATTEND_REQ.pack(layer, s, h, d, flags, scale)]
    for thr in (pos_thr, blk_thr):
        if thr is not None:
            parts += [
                np.asarray(thr[0], np.float64).tobytes(),
                np.asarray(thr[1], np.int64).tobytes(),
            ]
    return b"".join(parts) + q.tobytes()


def unpack_attend(p: bytes):
    s, h, d = ATTEND_REP.unpack_from(p)
    o = ATTEND_REP.size
    out = np.frombuffer(p, np.float32, s * h * d, o).reshape(s, h, d)
    lse = np.frombuffer(p, np.float32, s * h, o + out.nbytes).reshape(s, h)
    return out, lse


# ---------------------------------------------------------------- coordinator (host side)


@dataclass
class V41Coordinator:
    """Drives one step across parts (holders + the host's own slice) for every layer."""

    layout: V41Layout
    parts: list

    def index_layer(self, layer: int, q_idx, w, clens, end_pos: int) -> dict:
        """Rounds 1+2 for an index source: local candidates -> global thresholds."""
        lay = self.layout
        r = lay.ratio(layer)
        width = end_pos // r
        pins = None
        if lay.is_cand_source(layer):
            cl = np.asarray(clens)
            pins = np.where(cl > 0, (cl - 1) // lay.candidate_block_size, NO_PIN)
        reps = [p.score(layer, q_idx, w, clens, pins) for p in self.parts]
        thr = {"pos": global_threshold([x["pos"] for x in reps], min(lay.index_topk, width))}
        if lay.is_cand_source(layer):
            n_blk = -(-width // lay.candidate_block_size)
            thr["blk"] = global_threshold(
                [x["blk"] for x in reps], min(lay.candidate_topk_blocks, n_blk)
            )
        for p in self.parts:
            p.select(layer, thr["pos"], thr.get("blk"))
        return thr

    def compressed_partials(self, layer: int, q, scale: float) -> list:
        return [p.attend(layer, q, scale) for p in self.parts]

    def end_step(self) -> None:
        for p in self.parts:
            p.end_step()


class V41HolderClient:
    """Host-side stand-in for a remote V41Holder over any ``transport(mtype, payload)``.

    Same interface the coordinator uses (load / score / select / attend). ``select`` is free:
    the thresholds ride on the next ATTEND for that layer, so an index layer costs two round
    trips (SCORE, ATTEND) and every other compressed layer one (ATTEND)."""

    def __init__(self, transport) -> None:
        _need_numpy()
        self._call = transport
        self._pending: dict = {}

    def _rpc(self, mtype: int, payload: bytes, expect: int) -> bytes:
        rt, rep = self._call(mtype, payload)
        if rt != expect:
            raise RuntimeError(f"v41 holder: {rep.decode(errors='replace')}")
        return rep

    def load(self, src: int, c0: int, kv, ik) -> None:
        self._rpc(V41_LOAD, pack_load(src, c0, kv, ik), V41_OK)

    def score(self, layer: int, q_idx, w, clens, pins=None):
        return unpack_score(
            self._rpc(V41_SCORE, pack_score(layer, q_idx, w, clens, pins), V41_SCORE_OK)
        )

    def select(self, layer: int, pos_thr, blk_thr=None) -> None:
        self._pending[layer] = (pos_thr, blk_thr)

    def attend(self, layer: int, q, scale: float):
        pos_thr, blk_thr = self._pending.pop(layer, (None, None))
        rep = self._rpc(V41_ATTEND, pack_attend(layer, q, scale, pos_thr, blk_thr), V41_ATTEND_OK)
        return unpack_attend(rep)

    def end_step(self) -> None:
        self._pending.clear()
