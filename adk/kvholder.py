"""adk kvholder — lend a device's memory to someone else's context window.

A KV holder keeps the OLDEST key/value pages of every full-attention layer of a
model that runs somewhere else, and computes that slice's share of each
attention op. The host merges the holder's partial with its own (log-sum-exp
merge), which is exact: the merged output equals attention over all keys.

So a machine whose context is capped by its own memory (a 24 GB laptop holding
64k tokens next to a 27B model) can keep going to 128k-229k with the old pages
living on a phone, a second laptop, a DGX, or any AitherNet node with free RAM.

Wire protocol: PATN v3, byte-compatible with Backburner's phone-attn
(github.com/StayLameBro/backburner, MIT, ``phone-attn/phone-attn.h``). A host
built against Backburner's llama.cpp fork (``LLAMA_KV_REMOTE=host:port``) can
use this server, and this client can drive a Backburner iPhone running Sidecar.
The protocol was reimplemented from that header; no upstream code is carried.

This is the portable reference holder (numpy, any OS). The Apple holders in
Backburner reach ~1 ms per 4k keys on SME2/Metal; this one is for capacity,
correctness and mesh plumbing, and for hosts where the link, not the compute,
is the bottleneck.

    adk kvholder serve [--port 50062] [--max-mb N]
    adk kvholder probe HOST[:PORT]
    adk kvholder plan --free-gb 8 [--profile qwen38-27b] [--kv q8_0]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import socketserver
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

try:  # numpy is optional for awdk; only the holder's math needs it (serve, tests)
    import numpy as np
except ImportError:  # `adk kvholder plan|probe` and the CLI parser still work
    np = None  # type: ignore[assignment]

try:  # the shared pool (sessions, prefix blocks); absent when this file is copied standalone
    from adk import kvpool as _pool
except ImportError:
    _pool = None  # type: ignore[assignment]

log = logging.getLogger("adk.kvholder")

# ---------------------------------------------------------------- protocol (PATN v3)

MAGIC = 0x4E544150  # "PATN" little-endian
VERSION = 3
DEFAULT_PORT = 50062
NR = 48  # query rows per KV head per group (8 tokens x GQA 6)
HD = 256  # head dim
PAGE = 4096  # keys per storage page
MAX_GROUPS = 64  # ATTN_BIG: up to 512 tokens per call

HELLO, HELLO_OK, CONFIG, APPEND, TRUNCATE, ATTN, ATTN_OK, STATS, OK, ERR, BYE, PING, ATTN_BIG = (
    range(1, 14)
)

HDR = struct.Struct("<IIQ")
HELLO_REP = struct.Struct("<II64s")
CONFIG_REQ = struct.Struct("<11I")
APPEND_REQ = struct.Struct("<III")
ATTN_REQ = struct.Struct("<IIIf")
ATTN_REP = struct.Struct("<IfffII")
# PATN v4 (adk extension): appended to CONFIG when a model's attention is not Qwen3.8-shaped
# (DeepSeek MLA: one latent KV head, keys 576 wide, values 512 wide, many query rows).
# tag, k_dim, v_dim, rows per KV head, V row stride, V bytes per head. A v3 holder reads
# only the first 44 bytes; ours checks the tag. Default shapes never send it.
CONFIG_V4 = struct.Struct("<6I")
V4_TAG = 4

KV_F16, KV_Q8_0, KV_Q4_0 = 0, 1, 2
KV_TYPES = {"f16": KV_F16, "q8_0": KV_Q8_0, "q4_0": KV_Q4_0}
_MAX_PAYLOAD = 1 << 31
_ATTN_CHUNK = 16384  # keys per numpy pass; bounds the score matrix


def head_bytes(kv_type: int, head_dim: int = HD) -> int:
    """Bytes one KV head occupies in one row, for a ggml storage type."""
    if kv_type == KV_F16:
        return head_dim * 2
    if kv_type == KV_Q8_0:
        return head_dim // 32 * 34
    if kv_type == KV_Q4_0:
        return head_dim // 32 * 18
    raise ValueError(f"unknown kv type {kv_type}")


@dataclass
class Config:
    n_layer: int
    n_head_kv: int
    rs: int  # row stride in bytes (all heads of one position)
    hb: int  # bytes per head inside a row
    is_q8: int = KV_Q8_0  # 0 f16, 1 q8_0, 2 q4_0
    sme_workers: int = 0
    sme_helpers: int = 0
    gpu_permille: int = 0
    gpu_chunk: int = 0
    store_f16: int = 0
    gpu_variant: int = 0
    k_dim: int = HD
    v_dim: int = HD
    rows: int = NR  # query rows per KV head per group of 8 tokens (r = g*8 + t)
    rs_v: int = 0  # 0: same as rs
    hb_v: int = 0  # 0: same as hb

    @property
    def v_rs(self) -> int:
        return self.rs_v or self.rs

    @property
    def v_hb(self) -> int:
        return self.hb_v or self.hb

    @property
    def is_v3(self) -> bool:
        return (
            self.k_dim == HD
            and self.v_dim == HD
            and self.rows == NR
            and self.v_rs == self.rs
            and self.v_hb == self.hb
        )

    def side(self, which: str) -> tuple[int, int, int]:
        """(dim, row stride, bytes per head) for "k" or "v"."""
        if which == "v":
            return self.v_dim, self.v_rs, self.v_hb
        return self.k_dim, self.rs, self.hb

    def pack(self) -> bytes:
        return self._pack_v3() + (
            b""
            if self.is_v3
            else CONFIG_V4.pack(V4_TAG, self.k_dim, self.v_dim, self.rows, self.v_rs, self.v_hb)
        )

    def _pack_v3(self) -> bytes:
        return CONFIG_REQ.pack(
            self.n_layer,
            self.n_head_kv,
            self.rs,
            self.hb,
            self.is_q8,
            self.sme_workers,
            self.sme_helpers,
            self.gpu_permille,
            self.gpu_chunk,
            self.store_f16,
            self.gpu_variant,
        )

    @classmethod
    def unpack(cls, b: bytes) -> "Config":
        c = cls(*CONFIG_REQ.unpack_from(b))
        if len(b) >= CONFIG_REQ.size + CONFIG_V4.size:
            tag, kd, vd, rows, rs_v, hb_v = CONFIG_V4.unpack_from(b, CONFIG_REQ.size)
            if tag == V4_TAG:
                c.k_dim, c.v_dim, c.rows, c.rs_v, c.hb_v = kd, vd, rows, rs_v, hb_v
        return c

    @classmethod
    def for_model(
        cls,
        n_layer: int,
        n_head_kv: int,
        kv: str = "q8_0",
        k_dim: int = HD,
        v_dim: int = HD,
        rows: int = NR,
    ) -> "Config":
        t = KV_TYPES[kv]
        hb, hb_v = head_bytes(t, k_dim), head_bytes(t, v_dim)
        return cls(
            n_layer=n_layer,
            n_head_kv=n_head_kv,
            rs=n_head_kv * hb,
            hb=hb,
            is_q8=t,
            k_dim=k_dim,
            v_dim=v_dim,
            rows=rows,
            rs_v=n_head_kv * hb_v if hb_v != hb else 0,
            hb_v=hb_v if hb_v != hb else 0,
        )


# ---------------------------------------------------------------- ggml row codecs


def dequant_rows(raw: np.ndarray, n: int, cfg: Config, side: str = "k") -> np.ndarray:
    """``raw`` uint8 [n * row stride] -> float32 [n, n_head_kv, dim] for the K or V side."""
    dim, rs, hb = cfg.side(side)
    rows = raw.reshape(n, rs)[:, : cfg.n_head_kv * hb].reshape(n, cfg.n_head_kv, hb)
    if cfg.is_q8 == KV_F16:
        return rows.copy().view(np.float16).astype(np.float32)
    nb = dim // 32
    if cfg.is_q8 == KV_Q8_0:
        blk = rows.reshape(n, cfg.n_head_kv, nb, 34)
        d = blk[..., :2].copy().view(np.float16).astype(np.float32)  # [n, h, nb, 1]
        q = blk[..., 2:].view(np.int8).astype(np.float32)  # [n, h, nb, 32]
        return (q * d).reshape(n, cfg.n_head_kv, dim)
    if cfg.is_q8 == KV_Q4_0:
        blk = rows.reshape(n, cfg.n_head_kv, nb, 18)
        d = blk[..., :2].copy().view(np.float16).astype(np.float32)
        qs = blk[..., 2:]
        lo = (qs & 0x0F).astype(np.float32) - 8.0
        hi = (qs >> 4).astype(np.float32) - 8.0
        return (np.concatenate([lo, hi], axis=-1) * d).reshape(n, cfg.n_head_kv, dim)
    raise ValueError(f"unknown kv type {cfg.is_q8}")


def quant_rows(x: np.ndarray, kv_type: int) -> bytes:
    """float32 [n, n_head_kv, HD] -> ggml rows (no padding: rs = n_head_kv * hb).

    For hosts and tests.
    """
    n, h, dim = x.shape
    if kv_type == KV_F16:
        return x.astype(np.float16).tobytes()
    nb = dim // 32
    b = x.reshape(n, h, nb, 32).astype(np.float32)
    amax = np.abs(b).max(axis=-1, keepdims=True)
    if kv_type == KV_Q8_0:
        d = amax / 127.0
        inv = np.where(d > 0, 1.0 / np.where(d > 0, d, 1), 0)
        q = np.clip(np.rint(b * inv), -127, 127).astype(np.int8)
        out = np.empty((n, h, nb, 34), np.uint8)
        out[..., :2] = d.astype(np.float16).view(np.uint8).reshape(n, h, nb, 2)
        out[..., 2:] = q.view(np.uint8)
        return out.tobytes()
    if kv_type == KV_Q4_0:
        # ggml q4_0: d = max/-8 by signed max, q = round(x/d + 8.5) clamped to 15
        idx = np.abs(b).argmax(axis=-1)[..., None]
        mx = np.take_along_axis(b, idx, axis=-1)
        d = mx / -8.0
        inv = np.where(d != 0, 1.0 / np.where(d != 0, d, 1), 0)
        q = np.clip(np.floor(b * inv + 8.5), 0, 15).astype(np.uint8)
        out = np.empty((n, h, nb, 18), np.uint8)
        out[..., :2] = d.astype(np.float16).view(np.uint8).reshape(n, h, nb, 2)
        out[..., 2:] = q[..., :16] | (q[..., 16:] << 4)
        return out.tobytes()
    raise ValueError(f"unknown kv type {kv_type}")


# ---------------------------------------------------------------- the math


def _attend(
    qs: np.ndarray, chunks, v_dim: int | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Online softmax over key chunks. qs [H, r, Dk] scaled f32; chunks of (K [H,n,Dk], V [H,n,Dv]).

    Returns the unnormalized accumulator [H, r, Dv], running max and denominator.
    """
    n_h, n_r, n_d = qs.shape
    acc = np.zeros((n_h, n_r, v_dim or n_d), np.float32)
    m = np.full((n_h, n_r), -np.inf, np.float32)
    den = np.zeros((n_h, n_r), np.float32)
    for k, v in chunks:
        if k.shape[1] == 0:
            continue
        scores = np.matmul(qs, k.transpose(0, 2, 1))  # BLAS, transposed view: no copy
        mx = np.maximum(m, scores.max(axis=-1))
        a = np.exp(m - mx)
        probs = np.exp(scores - mx[..., None])
        acc = acc * a[..., None] + np.matmul(probs, v)
        den = den * a + probs.sum(axis=-1)
        m = mx
    return acc, m, den


def _rows(n_r: int, n_tok: int | None) -> np.ndarray:
    """PATN query rows r = g*8 + t (8 tokens per group); t >= n_tok are padding."""
    if n_tok is None or n_r % 8:
        return np.arange(n_r)
    return np.flatnonzero(np.arange(n_r) % 8 < n_tok)


def _finish(
    shape: tuple, rows: np.ndarray, acc: np.ndarray, m: np.ndarray, den: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    out_o = np.zeros(shape, np.float32)
    out_l = np.full(shape[:2], -np.inf, np.float32)
    ok = den > 0
    sel_o = np.where(ok[..., None], acc / np.where(ok, den, 1)[..., None], 0)
    sel_l = np.where(ok, m + np.log(np.where(ok, den, 1)), -np.inf)
    out_o[:, rows] = sel_o
    out_l[:, rows] = sel_l
    return out_o, out_l


def partial_attention(
    q: np.ndarray, keys: np.ndarray, vals: np.ndarray, scale: float, n_tok: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """One holder's share.

    Q [H, R, D] unscaled, K/V [N, H, D] -> (O normalized [H, R, D], lse [H, R]).

    lse = m + log(l) of the scaled scores; -inf (and O = 0) where there are no keys. With
    ``n_tok`` (PATN rows r = g*8 + t), rows with t >= n_tok are padding: skipped, left -inf.
    """
    rows = _rows(q.shape[1], n_tok)
    out_shape = q.shape[:2] + (vals.shape[-1],)
    if keys.shape[0] == 0 or rows.size == 0:
        return np.zeros(out_shape, np.float32), np.full(q.shape[:2], -np.inf, np.float32)
    qs = np.ascontiguousarray(q[:, rows].astype(np.float32) * np.float32(scale))
    chunks = (
        (
            np.ascontiguousarray(keys[c : c + _ATTN_CHUNK].transpose(1, 0, 2), np.float32),
            np.ascontiguousarray(vals[c : c + _ATTN_CHUNK].transpose(1, 0, 2), np.float32),
        )
        for c in range(0, keys.shape[0], _ATTN_CHUNK)
    )
    return _finish(out_shape, rows, *_attend(qs, chunks, vals.shape[-1]))


# ---------------------------------------------------------------- TurboQuant-style 4-bit store

# Rotate each vector by a fixed orthogonal matrix, then quantize every coordinate with the
# 16-level Lloyd-Max quantizer for a unit Gaussian, keeping one float32 norm per vector
# (TurboQuant, Zandieh et al., arXiv:2504.19874). After the rotation the coordinates of a
# unit vector are close to N(0, 1/d), so one fixed codebook fits every head. Dot products
# survive the rotation: the holder rotates the query once, scores against the stored
# rotated keys, and rotates the output back once. 132 bytes per key per head (q8 is 272).
_TQ4_CENTROIDS = (
    np.array(
        [
            -2.7326,
            -2.0690,
            -1.6181,
            -1.2562,
            -0.9424,
            -0.6568,
            -0.3881,
            -0.1284,
            0.1284,
            0.3881,
            0.6568,
            0.9424,
            1.2562,
            1.6181,
            2.0690,
            2.7326,
        ],
        dtype=np.float32,
    )
    if np is not None
    else None
)
_TQ4_EDGES = (_TQ4_CENTROIDS[1:] + _TQ4_CENTROIDS[:-1]) / 2 if np is not None else None
# every packed byte -> its (low, high) centroid pair, so decoding is a single gather
_TQ4_PAIRS = (
    np.stack(
        [_TQ4_CENTROIDS[np.arange(256) & 15], _TQ4_CENTROIDS[np.arange(256) >> 4]], axis=1
    ).astype(np.float32)
    if np is not None
    else None
)
_ROT: dict = {}


def tq4_rotation(d: int) -> np.ndarray:
    """A deterministic orthogonal d x d matrix (randomized Hadamard; d a power of two)."""
    if d not in _ROT:
        h = np.array([[1.0]])
        while h.shape[0] < d:
            h = np.block([[h, h], [h, -h]])
        signs = np.random.default_rng(0x7A5).choice([-1.0, 1.0], d)
        _ROT[d] = (h * signs / np.sqrt(d)).astype(np.float32)
    return _ROT[d]


def tq4_encode(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """[..., D] float -> (packed 4-bit codes [..., D/2] uint8, norms [...] float32)."""
    d = x.shape[-1]
    xr = np.asarray(x, np.float32) @ tq4_rotation(d).T
    norms = np.linalg.norm(xr, axis=-1).astype(np.float32)
    u = xr * (np.sqrt(d) / np.where(norms > 0, norms, 1))[..., None]
    codes = np.searchsorted(_TQ4_EDGES, u).astype(np.uint8)
    return codes[..., 0::2] | (codes[..., 1::2] << 4), norms


def tq4_decode_rotated(packed: np.ndarray, norms: np.ndarray) -> np.ndarray:
    """Packed codes -> vectors in the ROTATED basis (what attention needs), float32."""
    d = packed.shape[-1] * 2
    out = np.take(_TQ4_PAIRS, packed, axis=0).reshape(packed.shape[:-1] + (d,))  # one gather
    out *= (norms / np.float32(np.sqrt(d)))[..., None]
    return out


def _attend_tq4(qs: np.ndarray, chunks) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """_attend over 4-bit chunks (kc, kn, vc, vn[, mu]), all in the rotated basis.

    Vectors are decoded at unit scale with one gather; each key's norm is folded into its
    score and each value's norm into its weight, so no full-size multiply is spent on norms.
    A chunk with ``mu`` [H, Dk] holds keys k - mu (centered): every score of that chunk gets
    q . mu back, so the softmax, the output and the lse are those of the uncentered keys.
    """
    n_h, n_r, n_d = qs.shape
    inv = np.float32(1.0 / np.sqrt(n_d))
    acc = None
    m = np.full((n_h, n_r), -np.inf, np.float32)
    den = np.zeros((n_h, n_r), np.float32)
    shift: dict = {}  # id(mu) -> q . mu [H, r]; one mean serves many chunks
    for kc, kn, vc, vn, *mu in chunks:
        n = kc.shape[1]
        if n == 0:
            continue
        k = np.take(_TQ4_PAIRS, kc, axis=0).reshape(n_h, n, n_d)
        scores = np.matmul(qs, k.transpose(0, 2, 1)) * (kn * inv)[:, None, :]
        if mu:
            c = shift.get(id(mu[0]))
            if c is None:
                c = shift[id(mu[0])] = np.einsum("hrd,hd->hr", qs, mu[0])
            scores += c[..., None]
        mx = np.maximum(m, scores.max(axis=-1))
        a = np.exp(m - mx)
        probs = np.exp(scores - mx[..., None])
        v_d = vc.shape[-1] * 2
        v = np.take(_TQ4_PAIRS, vc, axis=0).reshape(n_h, n, v_d)
        inv_v = np.float32(1.0 / np.sqrt(v_d))
        if acc is None:
            acc = np.zeros((n_h, n_r, v_d), np.float32)
        acc = acc * a[..., None] + np.matmul(probs * (vn * inv_v)[:, None, :], v)
        den = den * a + probs.sum(axis=-1)
        m = mx
    return acc, m, den


def _cut(parts: tuple, lo: int, hi: int | None = None, copy: bool = False) -> tuple:
    """Positions [lo, hi) of one chunk. Per-chunk constants (a tq4 chunk's key mean, index
    4) are not per position: they ride along unsliced."""
    out = []
    for i, a in enumerate(parts):
        if i >= 4:
            out.append(a)
            continue
        a = a[:, lo:hi]
        out.append(np.ascontiguousarray(a) if copy else a)
    return tuple(out)


def tq4_decode(packed: np.ndarray, norms: np.ndarray) -> np.ndarray:
    return tq4_decode_rotated(packed, norms) @ tq4_rotation(packed.shape[-1] * 2)


def merge_partials(parts: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Exact log-sum-exp merge of normalized partials [(O, lse), ...] over disjoint key sets."""
    lses = np.stack([p[1] for p in parts]).astype(np.float64)
    mx = lses.max(axis=0)
    safe = np.where(np.isfinite(mx), mx, 0.0)
    w = np.where(np.isfinite(lses), np.exp(lses - safe), 0.0)
    tot = w.sum(axis=0)
    out = sum(w[i][..., None] * parts[i][0].astype(np.float64) for i in range(len(parts)))
    out = np.where(tot[..., None] > 0, out / np.where(tot > 0, tot, 1)[..., None], 0.0)
    lse = np.where(tot > 0, safe + np.log(np.where(tot > 0, tot, 1)), -np.inf)
    return out.astype(np.float32), lse.astype(np.float32)


# ---------------------------------------------------------------- capacity

# Full-attention layer counts and KV heads of models we serve. Hybrid models (GDN + attention)
# only page their full-attention layers; recurrent state is O(1) and stays on the host.
PROFILES = {
    "qwen38-27b": {"n_layer": 16, "n_head_kv": 4},  # Backburner's measured target
    "bonsai2-27b": {"n_layer": 16, "n_head_kv": 4},  # Qwen3.8-27B base (same attention)
}


def plan_capacity(
    free_bytes: int, n_layer: int, n_head_kv: int, kv: str = "q8_0", reserve_bytes: int = 400 << 20
) -> dict:
    """Old tokens a holder with ``free_bytes`` can keep, page-aligned.

    Backburner sizes its phone the same way.
    """
    per_token = n_layer * n_head_kv * head_bytes(KV_TYPES[kv]) * 2
    usable = max(0, free_bytes - reserve_bytes)
    tokens = usable // per_token // PAGE * PAGE
    return {
        "tokens": int(tokens),
        "bytes_per_token": per_token,
        "bytes": int(tokens * per_token),
        "reserve_bytes": reserve_bytes,
        "kv": kv,
    }


# ---------------------------------------------------------------- framing


def _recv_all(sock: socket.socket, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("peer closed")
        got += k
    return bytes(buf)


def _send_msg(sock: socket.socket, mtype: int, *parts: bytes) -> None:
    sock.sendall(HDR.pack(MAGIC, mtype, sum(len(p) for p in parts)) + b"".join(parts))


def _recv_msg(sock: socket.socket) -> tuple[int, bytes]:
    magic, mtype, n = HDR.unpack(_recv_all(sock, HDR.size))
    if magic != MAGIC:
        raise ConnectionError(f"bad magic 0x{magic:08x}")
    if n > _MAX_PAYLOAD:
        raise ConnectionError(f"payload too large: {n}")
    return mtype, _recv_all(sock, n) if n else b""


def _tune(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
        except OSError as e:  # some stacks cap buffers; the default still works, just slower
            log.debug("kvholder: socket buffer %s not raised: %s", opt, e)


# ---------------------------------------------------------------- holder (server)


@dataclass
class HolderState:
    max_bytes: int
    cfg: Config | None = None
    raw: list[list[bytes]] = field(default_factory=list)  # per layer: appended row blocks (K)
    raw_v: list[list[bytes]] = field(default_factory=list)
    n: list[int] = field(default_factory=list)
    held_bytes: int = 0
    attn_calls: int = 0
    appended: int = 0
    last_attn_ms: float = 0.0
    sum_attn_ms: float = 0.0
    cache: dict = field(default_factory=dict)  # layer -> (n, K f32, V f32)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # "f32": keys dequantized once at APPEND into [H, n, D] float32 chunks (fast, 3.8x the q8
    # bytes); "wire": rows kept as received (most context per MB, dequantized per call)
    store: str = "f32"
    chunks: list = field(default_factory=list)  # per layer: [(K [H,n,D], V [H,n,D]), ...]
    # shared pool (adk.kvpool): positions [0, base) are read-only pool blocks, the same on every
    # layer; ``chunks``/``raw`` hold this session's private keys, positions [base, n)
    shared: list = field(default_factory=list)
    base: int = 0
    ns: tuple | None = None
    mu: dict = field(default_factory=dict)  # tq4: layer -> (mean key [H, D], rotated)


class _Conn:
    """Which session one connection (or the relay link) is bound to."""

    def __init__(self) -> None:
        self.sid = 0


class KVHolder:
    """Protocol state machine.

    Transport-free, so tests and other transports (a mesh relay) can drive it.
    """

    def __init__(
        self,
        max_bytes: int,
        device: str = "adk-kvholder",
        store: str = "f32",
        tq4_center: bool = True,
    ):
        if store not in ("f32", "wire", "tq4"):
            raise ValueError("store must be 'f32', 'wire' or 'tq4'")
        # tq4 keys are centered by default: a model's keys share a large per-head offset
        # (Qwen3-0.6B layer 0) that a 4-bit code cannot hold next to the detail. Exact: see
        # _tq4_chunk. Off only to reproduce the old, known-bad store.
        self.tq4_center = tq4_center
        self.st = HolderState(max_bytes=max_bytes, store=store)  # session 0: the PATN default
        self.device = device
        self.sessions: dict[int, HolderState] = {0: self.st}
        self.pool = _pool.KVPool() if _pool is not None else None
        self._link = _Conn()  # the binding for callers that pass no connection (a relay link)

    def _used(self) -> int:
        """Bytes held across every session plus the shared pool."""
        return sum(s.held_bytes for s in self.sessions.values()) + (
            self.pool.bytes if self.pool is not None else 0
        )

    def _per_key(self, st: HolderState | None = None) -> int:
        """Bytes one key costs on every layer it is appended to (K and V)."""
        cfg = (st or self.st).cfg
        if self.st.store == "f32":
            return cfg.n_head_kv * (cfg.k_dim + cfg.v_dim) * 4
        if self.st.store == "tq4":
            return cfg.n_head_kv * (cfg.k_dim // 2 + 4 + cfg.v_dim // 2 + 4)
        return cfg.rs + cfg.v_rs

    def handle(self, mtype: int, p: bytes, conn: _Conn | None = None) -> tuple[int, bytes] | None:
        """One request -> (reply type, payload); None = close the session.

        ``conn`` is the caller's session binding (one per TCP connection); without it the
        holder-wide link binding is used (a relay link carries one session at a time).
        """
        conn = conn or self._link
        if _pool is not None and mtype in _pool.POOL_TYPES:
            return self._pool_msg(conn, mtype, p)
        st = self.sessions.get(conn.sid)
        if st is None:  # the session ended: back to the default
            conn.sid, st = 0, self.st
        if mtype == HELLO:
            dev = f"{self.device} {st.store} max_mb={st.max_bytes >> 20}".encode()[:63]
            return HELLO_OK, HELLO_REP.pack(VERSION, 0, dev)
        if mtype == CONFIG:
            if len(p) < CONFIG_REQ.size:
                return ERR, b"short CONFIG"
            c = Config.unpack(p)
            shape_ok = (
                0 < c.n_head_kv <= 16
                and 0 < c.n_layer <= 256
                and 0 < c.rows <= 1024
                and c.rows % 8 == 0
                and all(0 < d <= 4096 and d % 32 == 0 for d in (c.k_dim, c.v_dim))
                and c.rs >= c.n_head_kv * c.hb
                and c.v_rs >= c.n_head_kv * c.v_hb
            )
            if not shape_ok:
                return ERR, b"bad CONFIG"
            if (
                c.is_q8 not in (KV_F16, KV_Q8_0, KV_Q4_0)
                or c.hb != head_bytes(c.is_q8, c.k_dim)
                or c.v_hb != head_bytes(c.is_q8, c.v_dim)
            ):
                return ERR, b"bad CONFIG: unsupported row format"
            pow2 = all(d & (d - 1) == 0 for d in (c.k_dim, c.v_dim))
            if st.store == "tq4" and not pow2:
                return ERR, b"bad CONFIG: tq4 needs power-of-two head dims (use f32 or wire)"
            with st.lock:
                if st.shared and self.pool is not None:
                    self.pool.release(st.shared)
                st.shared, st.base, st.ns = [], 0, None
                st.cfg = c
                st.mu = {}
                st.raw = [[] for _ in range(c.n_layer)]
                st.raw_v = [[] for _ in range(c.n_layer)]
                st.n = [0] * c.n_layer
                st.chunks = [[] for _ in range(c.n_layer)]
                st.held_bytes = 0
                st.cache.clear()
            return OK, b""
        if mtype == APPEND:
            return self._append(p, st)
        if mtype == TRUNCATE:
            keep = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0
            with st.lock:
                if st.cfg:
                    if keep < st.base:
                        self._unshare(st, keep)
                    for layer in range(st.cfg.n_layer):
                        if st.n[layer] > keep:
                            self._truncate_layer(layer, keep, st)
            return OK, b""
        if mtype in (ATTN, ATTN_BIG):
            return self._attn(p, big=mtype == ATTN_BIG, st=st)
        if mtype == STATS:
            with st.lock:
                mean = st.sum_attn_ms / st.attn_calls if st.attn_calls else 0.0
                held = st.n[0] if st.n else 0
                s = (
                    f"state=idle attn_calls={st.attn_calls} last_ms={st.last_attn_ms:.3f} "
                    f"mean_ms={mean:.3f} "
                    f"held={held} appended={st.appended} held_mb={st.held_bytes / 1048576:.1f} "
                    f"max_mb={st.max_bytes >> 20} store={st.store}"
                )
                if self.pool is not None and (self.pool.blocks or len(self.sessions) > 1):
                    s += (
                        f" shared={st.base} sessions={len(self.sessions)}"
                        f" pool_mb={self.pool.bytes / 1048576:.1f}"
                    )
            return OK, s.encode()
        if mtype == PING:
            n = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0
            return OK, b"\x5a" * min(n, 64 << 20)
        if mtype == BYE:
            if conn.sid:  # a pooled session ends: its references and private keys go
                with st.lock:
                    self._drop(st)
                    self.sessions.pop(conn.sid, None)
                conn.sid = 0
            return None
        return ERR, f"unknown message {mtype}".encode()

    # ------------------------------------------------------------ shared pool (adk.kvpool)

    def _drop(self, st: HolderState) -> None:
        if st.shared and self.pool is not None:
            self.pool.release(st.shared)
        st.shared, st.base, st.chunks, st.raw, st.raw_v = [], 0, [], [], []
        st.n, st.held_bytes, st.cfg = [], 0, None

    def _pool_msg(self, conn: _Conn, mtype: int, p: bytes) -> tuple[int, bytes]:
        if mtype == _pool.SESSION:
            if len(p) != _pool.SESSION_REQ.size:
                return ERR, b"bad SESSION"
            sid = _pool.SESSION_REQ.unpack(p)[0]
            with self.st.lock:
                if sid not in self.sessions:
                    if len(self.sessions) >= _pool.MAX_SESSIONS:
                        return ERR, b"too many sessions"
                    self.sessions[sid] = HolderState(
                        max_bytes=self.st.max_bytes, store=self.st.store, lock=self.st.lock
                    )
            conn.sid = sid
            return OK, b""
        st = self.sessions.get(conn.sid) or self.st
        if mtype == _pool.POOL_STATUS:
            with st.lock:
                rep = {
                    "device": self.device,
                    "store": self.st.store,
                    "max_bytes": self.st.max_bytes,
                    "used_bytes": self._used(),
                    "sessions": [
                        {
                            "sid": sid,
                            "held": s.n[0] if s.n else 0,
                            "shared_tokens": s.base,
                            "private_bytes": s.held_bytes,
                        }
                        for sid, s in sorted(self.sessions.items())
                    ],
                    "pool": self.pool.status(),
                }
            return OK, json.dumps(rep).encode()
        if st.store == "wire":
            return ERR, b"pool: needs --store f32 or tq4 (wire rows are not shared)"
        if st.cfg is None:
            return ERR, b"pool: CONFIG first"
        try:
            fp, block, hashes = _pool.unpack_pool_req(p)
        except ValueError as e:
            return ERR, str(e).encode()
        ns = (fp, _pool.layout_fingerprint(st.cfg.pack(), st.store), block)
        with st.lock:
            if mtype == _pool.POOL_ATTACH:
                if any(st.n) or st.shared:
                    return ERR, b"POOL_ATTACH needs an empty session (right after CONFIG)"
                blocks = self.pool.chain(ns, hashes)
                self.pool.acquire(blocks)
                st.shared, st.ns = blocks, ns
                st.base = len(blocks) * block
                st.n = [st.base] * st.cfg.n_layer
                self.pool.attached_tokens += st.base
                return OK, struct.pack("<I", st.base)
            return self._publish(st, ns, block, hashes)

    def _publish(self, st: HolderState, ns: tuple, block: int, hashes: list) -> tuple[int, bytes]:
        if st.shared and st.ns != ns:
            return ERR, b"POOL_PUBLISH: another model, layout or block size than the shared prefix"
        for b, h in zip(st.shared, hashes):
            if b.key != h:
                return ERR, b"POOL_PUBLISH: prefix differs from the shared blocks"
        k = min(len(hashes), min(st.n) // block)
        first = len(st.shared)
        if k <= first:
            return OK, struct.pack("<I", len(st.shared))
        cfg, base0 = st.cfg, st.base
        per_key = self._per_key(st)
        priv = []  # this session's private rows, one contiguous array set per layer
        for layer in range(cfg.n_layer):
            parts = st.chunks[layer]
            priv.append(
                tuple(
                    np.concatenate([c[i] for c in parts], axis=1) if i < 4 else parts[0][i]
                    for i in range(len(parts[0]))
                )
            )
        new = []
        for j in range(first, k):
            lo, hi = j * block - base0, (j + 1) * block - base0
            blk = _pool.Block(
                ns=ns,
                key=hashes[j],
                index=j,
                tokens=block,
                layers=[_cut(pl, lo, hi, copy=True) for pl in priv],
                nbytes=block * per_key * cfg.n_layer,
            )
            new.append(self.pool.insert(blk))  # an identical block already resident wins
        self.pool.acquire(new)
        cut = k * block - base0
        for layer in range(cfg.n_layer):
            tail = _cut(priv[layer], cut, copy=True)
            st.chunks[layer] = [tail] if tail[0].shape[1] else []
        st.held_bytes -= cut * per_key * cfg.n_layer
        st.shared, st.base, st.ns = st.shared + new, k * block, ns
        return OK, struct.pack("<I", len(st.shared))

    def _unshare(self, st: HolderState, keep: int) -> None:
        """TRUNCATE below the shared boundary: copy the kept part of the straddling block into
        the private range (copy-on-write) and drop the references past it."""
        block = st.ns[2]
        kb, part = divmod(keep, block)
        per_key = self._per_key(st)
        for layer in range(st.cfg.n_layer):
            st.chunks[layer] = (
                [_cut(st.shared[kb].layers[layer], 0, part, copy=True)] if part else []
            )
            st.n[layer] = keep
        self.pool.release(st.shared[kb:])
        st.held_bytes = part * per_key * st.cfg.n_layer
        st.shared, st.base = st.shared[:kb], kb * block

    def _tq4_chunk(self, st: HolderState, layer: int, k: np.ndarray, v: np.ndarray) -> tuple:
        """Encode one APPEND for the tq4 store; k, v are [H, n, D] float32.

        Keys are stored as k - mu, mu the per-head mean key of this session's FIRST append to
        the layer, fixed from then on (encoded keys never shift). mu travels with every chunk
        in the rotated basis; attention adds q . mu back per chunk, which is exact.
        """
        if not self.tq4_center:
            return (*tq4_encode(k), *tq4_encode(v))
        mu = st.mu.get(layer)
        if mu is None:
            m = k.mean(axis=1)
            mu = st.mu[layer] = (m, np.ascontiguousarray(m @ tq4_rotation(k.shape[-1]).T))
        return (*tq4_encode(k - mu[0][:, None, :]), *tq4_encode(v), mu[1])

    def _layer_chunks(self, st: HolderState, layer: int) -> list:
        return [b.layers[layer] for b in st.shared] + st.chunks[layer]

    def _append(self, p: bytes, st: HolderState | None = None) -> tuple[int, bytes]:
        st = st or self.st
        if len(p) < APPEND_REQ.size or st.cfg is None:
            return ERR, b"short APPEND"
        layer, pos0, n = APPEND_REQ.unpack_from(p)
        cfg = st.cfg
        nbytes, vbytes = n * cfg.rs, n * cfg.v_rs
        if layer >= cfg.n_layer or len(p) != APPEND_REQ.size + nbytes + vbytes:
            return ERR, b"bad APPEND"
        with st.lock:
            if pos0 != st.n[layer]:
                return ERR, f"APPEND pos0 {pos0} != held {st.n[layer]}".encode()
            cost = n * self._per_key(st)
            over = self._used() + cost - st.max_bytes
            if over > 0 and self.pool is not None:
                self.pool.evict(over)  # unreferenced shared blocks only, oldest first
                over = self._used() + cost - st.max_bytes
            if over > 0:
                return ERR, f"out of memory at {st.n[layer]} keys".encode()
            off = APPEND_REQ.size
            k_raw, v_raw = p[off : off + nbytes], p[off + nbytes : off + nbytes + vbytes]
            if st.store in ("f32", "tq4"):
                k = dequant_rows(np.frombuffer(k_raw, np.uint8), n, cfg).transpose(1, 0, 2)
                v = dequant_rows(np.frombuffer(v_raw, np.uint8), n, cfg, "v").transpose(1, 0, 2)
                if st.store == "tq4":
                    st.chunks[layer].append(self._tq4_chunk(st, layer, k, v))
                else:
                    st.chunks[layer].append((np.ascontiguousarray(k), np.ascontiguousarray(v)))
            else:
                st.raw[layer].append(k_raw)
                st.raw_v[layer].append(v_raw)
            st.n[layer] += n
            st.held_bytes += cost
            st.appended += n
            st.cache.pop(layer, None)
            return OK, struct.pack("<I", st.n[layer])

    def _truncate_layer(self, layer: int, keep: int, st: HolderState | None = None) -> None:
        st = st or self.st
        if st.store in ("f32", "tq4"):
            kept, have = [], 0
            for parts in st.chunks[layer]:
                size = parts[0].shape[1]
                take = min(size, keep - st.base - have)
                if take <= 0:
                    break
                kept.append(_cut(parts, 0, take, copy=True) if take < size else parts)
                have += take
            st.held_bytes -= (st.n[layer] - keep) * self._per_key(st)
            st.chunks[layer], st.n[layer] = kept, keep
            return
        rs, v_rs = st.cfg.rs, st.cfg.v_rs
        k = b"".join(st.raw[layer])[: keep * rs]
        v = b"".join(st.raw_v[layer])[: keep * v_rs]
        st.held_bytes -= (st.n[layer] - keep) * (rs + v_rs)
        st.raw[layer], st.raw_v[layer], st.n[layer] = [k], [v], keep
        st.cache.pop(layer, None)

    def _kv(self, layer: int, st: HolderState | None = None) -> tuple[np.ndarray, np.ndarray]:
        st = st or self.st
        hit = st.cache.get(layer)
        if hit and hit[0] == st.n[layer]:
            return hit[1], hit[2]
        n = st.n[layer]
        keys = dequant_rows(np.frombuffer(b"".join(st.raw[layer]), np.uint8), n, st.cfg)
        vals = dequant_rows(np.frombuffer(b"".join(st.raw_v[layer]), np.uint8), n, st.cfg, "v")
        st.cache = {
            layer: (n, keys, vals)
        }  # one layer hot: decode walks layers in order, memory stays bounded
        return keys, vals

    def _attn(self, p: bytes, big: bool, st: HolderState | None = None) -> tuple[int, bytes]:
        st = st or self.st
        if st.cfg is None or len(p) < ATTN_REQ.size:
            return ERR, b"short ATTN"
        layer, n_tok, nk, scale = ATTN_REQ.unpack_from(p)
        cfg = st.cfg
        qn = cfg.n_head_kv * cfg.rows * cfg.k_dim
        body = p[ATTN_REQ.size :]
        ng = len(body) // (2 * qn)
        if (
            layer >= cfg.n_layer
            or ng < 1
            or len(body) != ng * 2 * qn
            or (not big and ng != 1)
            or ng > MAX_GROUPS
        ):
            return ERR, b"bad ATTN"
        t0 = time.perf_counter()
        q = np.frombuffer(body, np.float16).astype(np.float32)
        q = q.reshape(ng, cfg.n_head_kv, cfg.rows, cfg.k_dim)
        out_shape = (cfg.n_head_kv, cfg.rows, cfg.v_dim)
        with st.lock:
            held = st.n[layer]
            nk = held if nk == 0 else min(nk, held)
            if st.store in ("f32", "tq4"):
                chunks, have = [], 0
                for parts in self._layer_chunks(st, layer):
                    take = min(parts[0].shape[1], nk - have)
                    if take <= 0:
                        break
                    chunks.append(_cut(parts, 0, take))
                    have += take
            else:
                keys, vals = self._kv(layer, st)
                keys, vals = keys[:nk], vals[:nk]
        outs, lses = [], []
        rows = _rows(cfg.rows, n_tok)
        for g in range(ng):
            if st.store in ("f32", "tq4"):
                if nk == 0 or rows.size == 0:
                    o = np.zeros(out_shape, np.float32)
                    s = np.full(out_shape[:2], -np.inf, np.float32)
                elif st.store == "tq4":  # scores in the rotated basis; rotate the output back
                    rot_k, rot_v = tq4_rotation(cfg.k_dim), tq4_rotation(cfg.v_dim)
                    qs = np.ascontiguousarray((q[g][:, rows] * np.float32(scale)) @ rot_k.T)
                    acc, m, den = _attend_tq4(qs, chunks)
                    o, s = _finish(out_shape, rows, acc @ rot_v, m, den)
                else:
                    qs = np.ascontiguousarray(q[g][:, rows] * np.float32(scale))
                    o, s = _finish(out_shape, rows, *_attend(qs, chunks, cfg.v_dim))
            else:
                o, s = partial_attention(q[g], keys, vals, scale, n_tok=n_tok)
            outs.append(o)
            lses.append(s)
        ms = (time.perf_counter() - t0) * 1000
        with st.lock:
            st.attn_calls += 1
            st.last_attn_ms = ms
            st.sum_attn_ms += ms
        pages = (nk + PAGE - 1) // PAGE
        rep = ATTN_REP.pack(nk, ms, 0.0, ms, 0, pages)
        return ATTN_OK, rep + np.stack(outs).astype(np.float16).tobytes() + np.stack(lses).astype(
            np.float32
        ).tobytes()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request
        _tune(sock)
        holder: KVHolder = self.server.holder  # type: ignore[attr-defined]
        conn = _Conn()  # each connection picks its own session (SESSION); default 0
        try:
            while True:
                mtype, payload = _recv_msg(sock)
                rep = holder.handle(mtype, payload, conn)
                if rep is None:
                    return
                _send_msg(sock, rep[0], rep[1])
        except (ConnectionError, OSError, struct.error):
            return


class HolderServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Strictly request -> reply per connection (the PATN contract).

    State survives reconnects until CONFIG.

    Threaded so a host that reconnects (a replugged cable, a restarted server) is served at once
    instead of queueing behind the dead connection; the holder's lock serializes the state.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], holder: KVHolder):
        super().__init__(addr, _Handler)
        self.holder = holder


# ---------------------------------------------------------------- client (host side)


class KVHolderClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        _tune(self.sock)
        self.cfg: Config | None = None

    def _call(self, mtype: int, *parts: bytes, expect: int = OK) -> bytes:
        _send_msg(self.sock, mtype, *parts)
        rtype, payload = _recv_msg(self.sock)
        if rtype == ERR:
            raise RuntimeError(payload.decode(errors="replace"))
        if rtype != expect:
            raise RuntimeError(f"unexpected reply {rtype}")
        return payload

    def hello(self) -> dict:
        v, sme2, dev = HELLO_REP.unpack(self._call(HELLO, expect=HELLO_OK))
        return {
            "version": v,
            "sme2": bool(sme2),
            "device": dev.split(b"\0", 1)[0].decode(errors="replace"),
        }

    def configure(self, cfg: Config) -> None:
        self._call(CONFIG, cfg.pack())
        self.cfg = cfg

    def append(self, layer: int, pos0: int, keys: np.ndarray, vals: np.ndarray) -> int:
        """K/V float32 [n, n_head_kv, HD]; quantized to the configured row format."""
        assert self.cfg is not None
        kb, vb = quant_rows(keys, self.cfg.is_q8), quant_rows(vals, self.cfg.is_q8)
        return struct.unpack(
            "<I", self._call(APPEND, APPEND_REQ.pack(layer, pos0, keys.shape[0]), kb, vb)
        )[0]

    def truncate(self, n: int) -> None:
        self._call(TRUNCATE, struct.pack("<I", n))

    def attn(
        self, layer: int, q: np.ndarray, scale: float, n_tok: int = 8, nk: int = 0
    ) -> tuple[np.ndarray, np.ndarray, dict]:
        """Q float [n_head_kv, 48, 256] (one group) or [ng, n_head_kv, 48, 256] (ATTN_BIG)."""
        assert self.cfg is not None
        big = q.ndim == 4
        q_groups = q if big else q[None]
        ng, h = q_groups.shape[0], self.cfg.n_head_kv
        rep = self._call(
            ATTN_BIG if big else ATTN,
            ATTN_REQ.pack(layer, n_tok, nk, scale),
            q_groups.astype(np.float16).tobytes(),
            expect=ATTN_OK,
        )
        nk_r, ms, gpu_ms, sme_ms, gp, pages = ATTN_REP.unpack_from(rep)
        nr, dv = self.cfg.rows, self.cfg.v_dim
        on = ng * h * nr * dv
        out = (
            np.frombuffer(rep, np.float16, on, ATTN_REP.size)
            .astype(np.float32)
            .reshape(ng, h, nr, dv)
        )
        lse = np.frombuffer(rep, np.float32, ng * h * nr, ATTN_REP.size + on * 2).reshape(ng, h, nr)
        meta = {
            "nk": nk_r,
            "ms": ms,
            "gpu_ms": gpu_ms,
            "sme_ms": sme_ms,
            "gpu_pages": gp,
            "pages": pages,
        }
        return (out, lse, meta) if big else (out[0], lse[0], meta)

    def stats(self) -> str:
        return self._call(STATS).decode(errors="replace")

    def ping(self, n: int = 0) -> float:
        t0 = time.perf_counter()
        got = self._call(PING, struct.pack("<I", n))
        if len(got) != n:
            raise RuntimeError("short PING")
        return (time.perf_counter() - t0) * 1000

    def close(self) -> None:
        try:
            _send_msg(self.sock, BYE)
        except OSError as e:  # holder already gone; closing is all that is left
            log.debug("kvholder: BYE not delivered: %s", e)
        self.sock.close()


# ---------------------------------------------------------------- CLI


def _free_bytes() -> int:
    try:
        import psutil  # optional

        return int(psutil.virtual_memory().available)
    except Exception:
        return 0


def register(sub) -> None:
    """The verbs live in adk.kvholder_cli (numpy-free); this copy is for a standalone file."""
    try:
        from adk.kvholder_cli import register as _register
    except ImportError:
        _register = None
    if _register is not None:
        _register(sub)
        return
    p = sub.add_parser(
        "kvholder",
        help="Lend this device's memory to another host's context window (PATN v3 KV holder)",
    )
    s = p.add_subparsers(dest="kvholder_action")
    sv = s.add_parser("serve", help="Hold old KV pages and answer attention over them")
    sv.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen address. PATN has no auth: bind a LAN address only on a network you trust; "
        "otherwise dial a relay with --connect",
    )
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument(
        "--connect",
        default="",
        help="Dial out to a relay instead of listening (ws://HOST:50063/holder or wss://...)",
    )
    sv.add_argument("--token", default="", help="Relay token (with --connect)")
    sv.add_argument(
        "--store",
        choices=["f32", "wire", "tq4"],
        default="f32",
        help="f32: exact and fastest; wire: exact, as received; tq4: ~2x the context of q8 "
        "per MB, approximate (TurboQuant-style 4-bit)",
    )
    sv.add_argument(
        "--max-mb", type=int, default=0, help="Memory to lend (default: free RAM minus 2 GB)"
    )
    pr = s.add_parser(
        "probe",
        help="HELLO + link latency + STATS against a holder (an adk holder or a Backburner iPhone)",
    )
    pr.add_argument("target", help="HOST[:PORT]")
    pl = s.add_parser("plan", help="How much context a holder with this much free memory can keep")
    pl.add_argument(
        "--free-gb", type=float, default=0.0, help="Default: this machine's available RAM"
    )
    pl.add_argument("--profile", choices=sorted(PROFILES), default="qwen38-27b")
    pl.add_argument("--kv", choices=sorted(KV_TYPES), default="q8_0")


def run(args) -> int:
    action = getattr(args, "kvholder_action", None)
    if action == "serve" and np is None:
        print("kvholder serve needs numpy: pip install numpy", file=sys.stderr)
        return 2
    if action == "serve":
        max_b = args.max_mb << 20 if args.max_mb else max(0, _free_bytes() - (2 << 30))
        if max_b <= 0:
            print("kvholder: cannot size the loan; pass --max-mb", file=sys.stderr)
            return 2
        holder = KVHolder(max_b, device=f"adk-kvholder@{socket.gethostname()}", store=args.store)
        if args.connect:
            from adk import kvholder_net

            print(f"kvholder: lending {max_b >> 20} MB to {args.connect}")
            sign = None
            if getattr(args, "device", False):  # sign in as this workspace device, no token
                from adk import kvholder_workspace

                sign = kvholder_workspace.device_hello(args.connect)
            return kvholder_net.dial_holder(args.connect, args.token, holder, sign=sign)
        srv = HolderServer((args.host, args.port), holder)
        print(f"kvholder: PATN v{VERSION} on {args.host}:{args.port}, lending {max_b >> 20} MB")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("kvholder: stopped")
        finally:
            srv.server_close()
        return 0
    if action == "probe":
        host, _, port = args.target.partition(":")
        try:
            c = KVHolderClient(host, int(port or DEFAULT_PORT), timeout=10)
            h = c.hello()
            rtt = min(c.ping(0) for _ in range(5))
            mb = 8 << 20
            t = c.ping(mb)
            print(f"holder   : {h['device']} (PATN v{h['version']}, sme2={h['sme2']})")
            print(f"rtt      : {rtt:.2f} ms")
            print(f"downlink : {mb * 8 / (t / 1000) / 1e9:.2f} Gb/s (8 MB PING)")
            print(f"stats    : {c.stats()}")
            c.close()
            return 0
        except (OSError, RuntimeError, ConnectionError) as e:
            print(f"kvholder probe {args.target}: {e}", file=sys.stderr)
            return 1
    if action == "plan":
        free = int(args.free_gb * (1 << 30)) if args.free_gb else _free_bytes()
        if free <= 0:
            print("kvholder plan: cannot read free memory; pass --free-gb", file=sys.stderr)
            return 2
        prof = PROFILES[args.profile]
        r = plan_capacity(free, prof["n_layer"], prof["n_head_kv"], args.kv)
        print(
            f"{args.profile} {args.kv}: {r['bytes_per_token']} B/token -> "
            f"{r['tokens']:,} old tokens "
            f"({r['bytes'] / 2**30:.2f} GiB of {free / 2**30:.2f} GiB free, "
            f"{r['reserve_bytes'] >> 20} MB reserve)"
        )
        return 0
    if action == "pool":
        from adk import kvpool

        return kvpool.run_pool(args)
    if action in ("phone", "relay-status", "elastic"):
        from adk import kvholder_net

        fn = {
            "phone": kvholder_net.run_phone,
            "relay-status": kvholder_net.run_relay_status,
            "elastic": kvholder_net.run_elastic,
        }[action]
        return fn(args)
    print("usage: adk kvholder {serve,probe,plan,phone,relay-status,elastic}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="adk")
    register(ap.add_subparsers(dest="command"))
    return run(ap.parse_args(["kvholder", *(argv if argv is not None else sys.argv[1:])]))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "Config",
    "HolderServer",
    "KVHolder",
    "KVHolderClient",
    "PROFILES",
    "dequant_rows",
    "merge_partials",
    "partial_attention",
    "plan_capacity",
    "quant_rows",
]
