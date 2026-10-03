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
import logging
import socket
import socketserver
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

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

    def pack(self) -> bytes:
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
        return cls(*CONFIG_REQ.unpack_from(b))

    @classmethod
    def for_model(cls, n_layer: int, n_head_kv: int, kv: str = "q8_0") -> "Config":
        t = KV_TYPES[kv]
        hb = head_bytes(t)
        return cls(n_layer=n_layer, n_head_kv=n_head_kv, rs=n_head_kv * hb, hb=hb, is_q8=t)


# ---------------------------------------------------------------- ggml row codecs


def dequant_rows(raw: np.ndarray, n: int, cfg: Config) -> np.ndarray:
    """``raw`` uint8 [n * rs] -> float32 [n, n_head_kv, HD]."""
    rows = raw.reshape(n, cfg.rs)[:, : cfg.n_head_kv * cfg.hb].reshape(n, cfg.n_head_kv, cfg.hb)
    if cfg.is_q8 == KV_F16:
        return rows.copy().view(np.float16).astype(np.float32)
    nb = HD // 32
    if cfg.is_q8 == KV_Q8_0:
        blk = rows.reshape(n, cfg.n_head_kv, nb, 34)
        d = blk[..., :2].copy().view(np.float16).astype(np.float32)  # [n, h, nb, 1]
        q = blk[..., 2:].view(np.int8).astype(np.float32)  # [n, h, nb, 32]
        return (q * d).reshape(n, cfg.n_head_kv, HD)
    if cfg.is_q8 == KV_Q4_0:
        blk = rows.reshape(n, cfg.n_head_kv, nb, 18)
        d = blk[..., :2].copy().view(np.float16).astype(np.float32)
        qs = blk[..., 2:]
        lo = (qs & 0x0F).astype(np.float32) - 8.0
        hi = (qs >> 4).astype(np.float32) - 8.0
        return (np.concatenate([lo, hi], axis=-1) * d).reshape(n, cfg.n_head_kv, HD)
    raise ValueError(f"unknown kv type {cfg.is_q8}")


def quant_rows(x: np.ndarray, kv_type: int) -> bytes:
    """float32 [n, n_head_kv, HD] -> ggml rows (no padding: rs = n_head_kv * hb).

    For hosts and tests.
    """
    n, h, _ = x.shape
    if kv_type == KV_F16:
        return x.astype(np.float16).tobytes()
    nb = HD // 32
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


def partial_attention(
    q: np.ndarray, keys: np.ndarray, vals: np.ndarray, scale: float
) -> tuple[np.ndarray, np.ndarray]:
    """One holder's share.

    Q [H, R, D] unscaled, K/V [N, H, D] -> (O normalized [H, R, D], lse [H, R]).

    lse = m + log(l) of the scaled scores; -inf (and O = 0) where there are no keys.
    """
    n_h, n_r, n_d = q.shape
    if keys.shape[0] == 0:
        return np.zeros((n_h, n_r, n_d), np.float32), np.full((n_h, n_r), -np.inf, np.float32)
    out = np.zeros((n_h, n_r, n_d), np.float64)
    m = np.full((n_h, n_r), -np.inf)
    den = np.zeros((n_h, n_r))
    q_scaled = q.astype(np.float64) * scale
    for s in range(0, keys.shape[0], _ATTN_CHUNK):
        k = keys[s : s + _ATTN_CHUNK].astype(np.float64)
        v = vals[s : s + _ATTN_CHUNK].astype(np.float64)
        scores = np.einsum("hrd,nhd->hrn", q_scaled, k)
        mc = scores.max(axis=-1)
        mx = np.maximum(m, mc)
        a = np.exp(m - mx)
        probs = np.exp(scores - mx[..., None])
        out = out * a[..., None] + np.einsum("hrn,nhd->hrd", probs, v)
        den = den * a + probs.sum(axis=-1)
        m = mx
    return (out / den[..., None]).astype(np.float32), (m + np.log(den)).astype(np.float32)


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
    "bonsai2-27b": {"n_layer": 16, "n_head_kv": 4},  # Qwen3.x-27B base
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


class KVHolder:
    """Protocol state machine.

    Transport-free, so tests and other transports (a mesh relay) can drive it.
    """

    def __init__(self, max_bytes: int, device: str = "adk-kvholder"):
        self.st = HolderState(max_bytes=max_bytes)
        self.device = device

    def handle(self, mtype: int, p: bytes) -> tuple[int, bytes] | None:
        """One request -> (reply type, payload); None = close the session."""
        st = self.st
        if mtype == HELLO:
            dev = f"{self.device} numpy max_mb={st.max_bytes >> 20}".encode()[:63]
            return HELLO_OK, HELLO_REP.pack(VERSION, 0, dev)
        if mtype == CONFIG:
            if len(p) < CONFIG_REQ.size:
                return ERR, b"short CONFIG"
            c = Config.unpack(p)
            if not (0 < c.n_head_kv <= 16 and 0 < c.n_layer <= 256 and c.rs >= c.n_head_kv * c.hb):
                return ERR, b"bad CONFIG"
            if c.is_q8 not in (KV_F16, KV_Q8_0, KV_Q4_0) or c.hb != head_bytes(c.is_q8):
                return ERR, b"bad CONFIG: unsupported row format"
            with st.lock:
                st.cfg = c
                st.raw = [[] for _ in range(c.n_layer)]
                st.raw_v = [[] for _ in range(c.n_layer)]
                st.n = [0] * c.n_layer
                st.held_bytes = 0
                st.cache.clear()
            return OK, b""
        if mtype == APPEND:
            return self._append(p)
        if mtype == TRUNCATE:
            keep = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0
            with st.lock:
                if st.cfg:
                    for layer in range(st.cfg.n_layer):
                        if st.n[layer] > keep:
                            self._truncate_layer(layer, keep)
            return OK, b""
        if mtype in (ATTN, ATTN_BIG):
            return self._attn(p, big=mtype == ATTN_BIG)
        if mtype == STATS:
            with st.lock:
                mean = st.sum_attn_ms / st.attn_calls if st.attn_calls else 0.0
                held = st.n[0] if st.n else 0
                s = (
                    f"state=idle attn_calls={st.attn_calls} last_ms={st.last_attn_ms:.3f} "
                    f"mean_ms={mean:.3f} "
                    f"held={held} appended={st.appended} held_mb={st.held_bytes / 1048576:.1f} "
                    f"max_mb={st.max_bytes >> 20}"
                )
            return OK, s.encode()
        if mtype == PING:
            n = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0
            return OK, b"\x5a" * min(n, 64 << 20)
        if mtype == BYE:
            return None
        return ERR, f"unknown message {mtype}".encode()

    def _append(self, p: bytes) -> tuple[int, bytes]:
        st = self.st
        if len(p) < APPEND_REQ.size or st.cfg is None:
            return ERR, b"short APPEND"
        layer, pos0, n = APPEND_REQ.unpack_from(p)
        cfg = st.cfg
        nbytes = n * cfg.rs
        if layer >= cfg.n_layer or len(p) != APPEND_REQ.size + 2 * nbytes:
            return ERR, b"bad APPEND"
        with st.lock:
            if pos0 != st.n[layer]:
                return ERR, f"APPEND pos0 {pos0} != held {st.n[layer]}".encode()
            if st.held_bytes + 2 * nbytes > st.max_bytes:
                return ERR, f"out of memory at {st.n[layer]} keys".encode()
            off = APPEND_REQ.size
            st.raw[layer].append(p[off : off + nbytes])
            st.raw_v[layer].append(p[off + nbytes : off + 2 * nbytes])
            st.n[layer] += n
            st.held_bytes += 2 * nbytes
            st.appended += n
            st.cache.pop(layer, None)
            return OK, struct.pack("<I", st.n[layer])

    def _truncate_layer(self, layer: int, keep: int) -> None:
        st = self.st
        rs = st.cfg.rs
        k = b"".join(st.raw[layer])[: keep * rs]
        v = b"".join(st.raw_v[layer])[: keep * rs]
        st.held_bytes -= 2 * (st.n[layer] - keep) * rs
        st.raw[layer], st.raw_v[layer], st.n[layer] = [k], [v], keep
        st.cache.pop(layer, None)

    def _kv(self, layer: int) -> tuple[np.ndarray, np.ndarray]:
        st = self.st
        hit = st.cache.get(layer)
        if hit and hit[0] == st.n[layer]:
            return hit[1], hit[2]
        n = st.n[layer]
        keys = dequant_rows(np.frombuffer(b"".join(st.raw[layer]), np.uint8), n, st.cfg)
        vals = dequant_rows(np.frombuffer(b"".join(st.raw_v[layer]), np.uint8), n, st.cfg)
        st.cache = {
            layer: (n, keys, vals)
        }  # one layer hot: decode walks layers in order, memory stays bounded
        return keys, vals

    def _attn(self, p: bytes, big: bool) -> tuple[int, bytes]:
        st = self.st
        if st.cfg is None or len(p) < ATTN_REQ.size:
            return ERR, b"short ATTN"
        layer, n_tok, nk, scale = ATTN_REQ.unpack_from(p)
        cfg = st.cfg
        qn = cfg.n_head_kv * NR * HD
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
        with st.lock:
            held = st.n[layer]
            nk = held if nk == 0 else min(nk, held)
            keys, vals = self._kv(layer)
        keys, vals = keys[:nk], vals[:nk]
        q = np.frombuffer(body, np.float16).astype(np.float32).reshape(ng, cfg.n_head_kv, NR, HD)
        outs, lses = [], []
        for g in range(ng):
            o, s = partial_attention(q[g], keys, vals, scale)
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
        try:
            while True:
                mtype, payload = _recv_msg(sock)
                rep = holder.handle(mtype, payload)
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
        on = ng * h * NR * HD
        out = (
            np.frombuffer(rep, np.float16, on, ATTN_REP.size)
            .astype(np.float32)
            .reshape(ng, h, NR, HD)
        )
        lse = np.frombuffer(rep, np.float32, ng * h * NR, ATTN_REP.size + on * 2).reshape(ng, h, NR)
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
    p = sub.add_parser(
        "kvholder",
        help="Lend this device's memory to another host's context window (PATN v3 KV holder)",
    )
    s = p.add_subparsers(dest="kvholder_action")
    sv = s.add_parser("serve", help="Hold old KV pages and answer attention over them")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
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
    if action == "serve":
        max_b = args.max_mb << 20 if args.max_mb else max(0, _free_bytes() - (2 << 30))
        if max_b <= 0:
            print("kvholder: cannot size the loan; pass --max-mb", file=sys.stderr)
            return 2
        srv = HolderServer(
            (args.host, args.port), KVHolder(max_b, device=f"adk-kvholder@{socket.gethostname()}")
        )
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
    print("usage: adk kvholder {serve,probe,plan}", file=sys.stderr)
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
