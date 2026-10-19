"""adk kvpool transport — pull shared KV blocks from another node, over Homa or TCP.

``adk.kvpool`` keeps shared prefix blocks on one holder. This module moves those blocks
between nodes: a node that already holds a prefix warm SERVES its blocks, and an engine (or
another holder) on a different node PULLS them by chain-hash key instead of recomputing the
prefix. Many pullers fetching from one holder is a many-to-one incast, which is exactly the
traffic shape Homa (PlatformLab/HomaModule) was built for, so the transport is pluggable:

``homa``
    The HomaModule kernel protocol. It is never assumed: :func:`homa_probe` asks the running
    kernel (Linux, the module loaded, and an ``IPPROTO_HOMA`` socket that actually opens).
    HomaModule's message API (``homa_sendmsg_args`` / receive buffer pools) is versioned with
    the module, so the data path comes from a binding registered with
    :func:`register_homa_binding`; without one the backend reports itself unavailable.
``tcp``
    Framed request/reply messages over a persistent stream socket. Always available.
``auto``
    Homa when the probe passes and a binding is registered, else TCP, with a WARNING naming
    exactly why Homa was not used. A fallback is never silent.

Blocks travel and rest compressed: :func:`encode_kv` packs a KV tensor with AitherKVCache
(``aither_kvcache.TurboQuant``, 4-bit) when it is installed, else an int8 per-vector absmax
codec, and names the codec in the blob so the receiver decodes either one. The format is the
same one family-keyed blocks are sealed in, so a sealed family block is just opaque bytes here.

Access. A block may carry an access tag (``sha256`` of a 32-byte fetch token). A GET for a
tagged block with any other token answers MISS, the same as a block that is not there, so a
wrong token learns nothing. Chain-hash keys are already secret-seeded for a family; the tag
closes the case where a key leaks through a prefix directory.

Wire (one message each way per request; TCP frames it, Homa carries it as one message)::

    frame   <4s B I>   b"KVBF", type, payload length
    GET     <32s 32s I> + n*32   namespace digest, fetch token (zeros = none), n, keys
    BLOCKS  <I> + n * (<I len> + bytes)   len 0xFFFFFFFF = miss
    ERR     utf-8 reason
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
from typing import Callable, Iterable, Optional, Protocol

log = logging.getLogger("adk.kvpool.transport")

FRAME = struct.Struct("<4sBI")
MAGIC = b"KVBF"
GET, BLOCKS, ERR = 1, 2, 3
GET_HDR = struct.Struct("<32s32sI")
MISS = 0xFFFFFFFF
NO_TOKEN = b"\0" * 32
MAX_MSG = 256 << 20
MAX_KEYS = 4096
IPPROTO_HOMA_DEFAULT = 146  # HomaModule's protocol number (homa.h); overridable, never assumed


class TransportUnavailableError(OSError):
    """A backend that cannot run on this host (the reason says why)."""


# ---------------------------------------------------------------- codec


def _np():
    try:
        import numpy as np  # noqa: PLC0415 -- optional for awdk
    except ImportError as e:  # pragma: no cover - the payload lane has core deps only
        raise RuntimeError("the kv block codec needs numpy (pip install numpy)") from e
    return np


def _tq():
    try:
        import torch  # noqa: PLC0415
        from aither_kvcache import TurboQuant  # noqa: PLC0415

        return torch, TurboQuant
    except Exception:  # noqa: BLE001 -- not installed (or broken): the built-in int8 codec
        return None, None


def codec_name(head_dim: int = 128) -> str:
    """The codec :func:`encode_kv` would use on this host for ``head_dim``."""
    _, tq = _tq()
    return "aither_kvcache.turboquant4" if tq is not None and head_dim % 2 == 0 else "int8-absmax"


def encode_kv(kv) -> bytes:
    """Compress ``kv`` (``[..., head_dim]``, float). AitherKVCache 4-bit when present, else int8."""
    np = _np()
    a = np.asarray(kv, dtype=np.float32)
    if a.ndim < 1 or a.shape[-1] == 0:
        raise ValueError("kv is [..., head_dim]")
    torch, tq_cls = _tq()
    head_dim = int(a.shape[-1])
    if tq_cls is not None and head_dim % 2 == 0:
        tq = tq_cls(head_dim=head_dim, bits=4, device="cpu")
        packed, norms = tq.encode(torch.from_numpy(a).to(torch.float16))
        p = packed.cpu().numpy()
        n = norms.cpu().numpy().astype(np.float32)
        hdr = {
            "codec": "aither_kvcache.turboquant4",
            "shape": list(a.shape),
            "pshape": list(p.shape),
            "nshape": list(n.shape),
        }
        body = p.astype(np.uint8).tobytes() + n.tobytes()
    else:
        flat = a.reshape(-1, head_dim)
        scale = np.max(np.abs(flat), axis=1, keepdims=True) / 127.0
        scale[scale == 0] = 1.0
        q = np.clip(np.round(flat / scale), -127, 127).astype(np.int8)
        hdr = {"codec": "int8-absmax", "shape": list(a.shape)}
        body = q.tobytes() + scale.astype(np.float32).tobytes()
    h = json.dumps(hdr, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(h)) + h + body


def decode_kv(blob: bytes):
    """``(array, codec)`` from :func:`encode_kv` output."""
    np = _np()
    n = struct.unpack("<I", blob[:4])[0]
    hdr = json.loads(blob[4 : 4 + n].decode("utf-8"))
    body = blob[4 + n :]
    shape = tuple(hdr["shape"])
    if hdr["codec"] == "int8-absmax":
        rows = int(np.prod(shape[:-1])) if len(shape) > 1 else 1
        hd = shape[-1]
        q = np.frombuffer(body[: rows * hd], dtype=np.int8).reshape(rows, hd).astype(np.float32)
        scale = np.frombuffer(body[rows * hd :], dtype=np.float32).reshape(rows, 1)
        return (q * scale).reshape(shape), hdr["codec"]
    if hdr["codec"] == "aither_kvcache.turboquant4":
        torch, tq_cls = _tq()
        if tq_cls is None:
            raise RuntimeError("block was packed with AitherKVCache, which is not installed here")
        ps, ns = tuple(hdr["pshape"]), tuple(hdr["nshape"])
        pn = int(np.prod(ps))
        p = np.frombuffer(body[:pn], dtype=np.uint8).reshape(ps)
        nrm = np.frombuffer(body[pn:], dtype=np.float32).reshape(ns)
        tq = tq_cls(head_dim=shape[-1], bits=4, device="cpu")
        out = tq.decode(torch.from_numpy(p.copy()), torch.from_numpy(nrm.copy()))
        return out.float().numpy().reshape(shape), hdr["codec"]
    raise ValueError(f"unknown kv codec {hdr['codec']}")


# ---------------------------------------------------------------- namespaces, tokens, store


def ns_digest(ns) -> bytes:
    """32 bytes naming a kvpool namespace ``(model fp, layout fp, block tokens)``."""
    if isinstance(ns, bytes) and len(ns) == 32:
        return ns
    fp, layout, block = ns
    return hashlib.sha256(b"adk-kvbf-ns\0" + fp + layout + struct.pack("<I", block)).digest()


def access_tag(token: bytes) -> bytes:
    if len(token) != 32:
        raise ValueError("a fetch token is 32 bytes")
    return hashlib.sha256(b"adk-kvbf-access\0" + token).digest()


class BlockStore:
    """Encoded blocks a node serves, keyed by (namespace digest, chain-hash key). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._blocks: dict[tuple, tuple[bytes, Optional[bytes]]] = {}
        self.bytes = 0

    def put_encoded(self, ns, key: bytes, blob: bytes, token: Optional[bytes] = None) -> None:
        if len(key) != 32:
            raise ValueError("block keys are 32 bytes")
        k = (ns_digest(ns), key)
        tag = access_tag(token) if token is not None else None
        with self._lock:
            old = self._blocks.get(k)
            if old is not None:
                self.bytes -= len(old[0])
            self._blocks[k] = (bytes(blob), tag)
            self.bytes += len(blob)

    def put(self, ns, key: bytes, kv, token: Optional[bytes] = None) -> int:
        """Compress ``kv`` with :func:`encode_kv` and serve it; returns encoded bytes."""
        blob = encode_kv(kv)
        self.put_encoded(ns, key, blob, token)
        return len(blob)

    def lookup(self, nsd: bytes, key: bytes, token: bytes) -> Optional[bytes]:
        with self._lock:
            row = self._blocks.get((nsd, key))
        if row is None:
            return None
        blob, tag = row
        if tag is not None and not hmac.compare_digest(tag, access_tag(token)):
            return None  # indistinguishable from absent: a wrong token learns nothing
        return blob

    def __len__(self) -> int:
        with self._lock:
            return len(self._blocks)


def pool_block_tensor(blk):
    """A kvpool ``Block`` with float ``(K, V)`` layers as one ``[L, 2, H, n, D]`` array."""
    np = _np()
    rows = []
    for layer in blk.layers:
        if len(layer) != 2:
            raise ValueError("only float (K, V) stores ship as tensors; this block is quantized")
        k, v = layer
        if k.shape != v.shape:
            raise ValueError("K and V shapes differ; ship them as two blocks")
        rows.append(np.stack([np.asarray(k, np.float32), np.asarray(v, np.float32)]))
    return np.stack(rows)


def serve_pool(pool, store: BlockStore, lock=None, token: Optional[bytes] = None) -> int:
    """Copy every float block resident in a ``KVPool`` into ``store``; returns blocks served."""
    lock = lock or threading.Lock()
    with lock:
        blocks = list(pool.blocks.values())
    n = 0
    for b in blocks:
        try:
            store.put(b.ns, b.key, pool_block_tensor(b), token)
            n += 1
        except ValueError as e:
            log.debug("kvpool transport: block %s not served: %s", b.key.hex()[:12], e)
    return n


# ---------------------------------------------------------------- messages


def pack_get(nsd: bytes, keys: list[bytes], token: Optional[bytes] = None) -> bytes:
    tok = token if token is not None else NO_TOKEN
    if len(nsd) != 32 or len(tok) != 32 or any(len(k) != 32 for k in keys):
        raise ValueError("namespace, token and keys are 32 bytes each")
    if len(keys) > MAX_KEYS:
        raise ValueError(f"at most {MAX_KEYS} keys per GET")
    return GET_HDR.pack(nsd, tok, len(keys)) + b"".join(keys)


def unpack_get(p: bytes) -> tuple[bytes, bytes, list[bytes]]:
    if len(p) < GET_HDR.size:
        raise ValueError("short GET")
    nsd, tok, n = GET_HDR.unpack_from(p)
    if n > MAX_KEYS or len(p) != GET_HDR.size + 32 * n:
        raise ValueError("bad GET")
    o = GET_HDR.size
    return nsd, tok, [p[o + 32 * i : o + 32 * (i + 1)] for i in range(n)]


def pack_blocks(blobs: list[Optional[bytes]]) -> bytes:
    out = [struct.pack("<I", len(blobs))]
    for b in blobs:
        out.append(struct.pack("<I", MISS if b is None else len(b)))
        if b is not None:
            out.append(b)
    return b"".join(out)


def unpack_blocks(p: bytes) -> list[Optional[bytes]]:
    (n,) = struct.unpack_from("<I", p)
    o, out = 4, []
    for _ in range(n):
        (ln,) = struct.unpack_from("<I", p, o)
        o += 4
        if ln == MISS:
            out.append(None)
            continue
        if o + ln > len(p):
            raise ValueError("truncated BLOCKS reply")
        out.append(p[o : o + ln])
        o += ln
    return out


def handle(store: BlockStore, mtype: int, payload: bytes) -> tuple[int, bytes]:
    """Server logic shared by every backend: one request message in, one reply out."""
    if mtype != GET:
        return ERR, b"unknown message"
    try:
        nsd, tok, keys = unpack_get(payload)
    except ValueError as e:
        return ERR, str(e).encode()
    return BLOCKS, pack_blocks([store.lookup(nsd, k, tok) for k in keys])


# ---------------------------------------------------------------- backends


class Conn(Protocol):
    def rpc(self, mtype: int, payload: bytes) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


class Server(Protocol):
    address: tuple

    def close(self) -> None: ...


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("peer closed")
        got += k
    return bytes(buf)


def _send_frame(sock, mtype: int, payload: bytes) -> None:
    sock.sendall(FRAME.pack(MAGIC, mtype, len(payload)) + payload)


def _recv_frame(sock) -> tuple[int, bytes]:
    magic, mtype, n = FRAME.unpack(_recv_exact(sock, FRAME.size))
    if magic != MAGIC:
        raise ConnectionError(f"bad magic {magic!r}")
    if n > MAX_MSG:
        raise ConnectionError(f"message too large: {n}")
    return mtype, _recv_exact(sock, n) if n else b""


def _tune(sock) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as e:
        log.debug("kvpool transport: TCP_NODELAY not set: %s", e)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 4 << 20)
        except OSError as e:  # capped buffers still work, just slower
            log.debug("kvpool transport: socket buffer %s not raised: %s", opt, e)


class _TcpConn:
    def __init__(self, host: str, port: int, timeout: float, connect=socket.create_connection):
        self.sock = connect((host, port), timeout=timeout)
        _tune(self.sock)
        self._lock = threading.Lock()

    def rpc(self, mtype: int, payload: bytes) -> tuple[int, bytes]:
        with self._lock:  # one request in flight per connection; open one per puller
            _send_frame(self.sock, mtype, payload)
            return _recv_frame(self.sock)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError as e:
            log.debug("kvpool transport: close: %s", e)


class _TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class TcpTransport:
    name = "tcp"

    def __init__(self, connect: Callable = socket.create_connection) -> None:
        self._connect = connect

    def available(self) -> tuple[bool, str]:
        return True, "stream sockets"

    def connect(self, host: str, port: int, timeout: float = 10.0) -> Conn:
        return _TcpConn(host, port, timeout, self._connect)

    def serve(self, store: BlockStore, host: str = "127.0.0.1", port: int = 0) -> Server:
        class _H(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                _tune(self.request)
                while True:
                    try:
                        mtype, p = _recv_frame(self.request)
                    except (ConnectionError, OSError):
                        return
                    rt, rp = handle(store, mtype, p)
                    try:
                        _send_frame(self.request, rt, rp)
                    except OSError:
                        return

        srv = _TcpServer((host, port), _H)
        t = threading.Thread(target=srv.serve_forever, name="kvbf-tcp", daemon=True)
        t.start()

        class _S:
            address = srv.server_address

            def close(self) -> None:
                srv.shutdown()
                srv.server_close()

        return _S()


def homa_proto() -> int:
    env = os.environ.get("ADK_HOMA_PROTO", "").strip()
    if env:
        return int(env)
    return int(getattr(socket, "IPPROTO_HOMA", IPPROTO_HOMA_DEFAULT))


def homa_probe(sock_factory: Callable = socket.socket, platform: str = sys.platform,
               path_exists: Callable[[str], bool] = os.path.exists) -> tuple[bool, str]:
    """``(True, detail)`` only when THIS kernel can open a Homa socket right now."""
    if not platform.startswith("linux"):
        return False, f"Homa is a Linux kernel module; this host is {platform}"
    if not (path_exists("/sys/module/homa") or path_exists("/proc/net/homa")):
        return False, "the homa kernel module is not loaded (/sys/module/homa absent)"
    try:
        proto = homa_proto()
    except ValueError as e:  # a malformed ADK_HOMA_PROTO must fall back, not crash auto
        return False, f"ADK_HOMA_PROTO is not an integer: {e}"
    try:
        s = sock_factory(socket.AF_INET, socket.SOCK_DGRAM, proto)
    except OSError as e:
        return False, f"socket(AF_INET, SOCK_DGRAM, {proto}) refused: {e}"
    try:
        s.close()
    except OSError as e:
        log.debug("kvpool transport: homa probe close: %s", e)
    return True, f"IPPROTO_HOMA={proto} socket opens"


_HOMA_BINDING = None


def register_homa_binding(binding) -> None:
    """Install the Homa data path: an object with ``connect(host, port, timeout) -> Conn`` and
    ``serve(handler, host, port) -> Server`` built against the loaded module's uapi."""
    global _HOMA_BINDING
    _HOMA_BINDING = binding


class HomaTransport:
    name = "homa"

    def __init__(self, probe: Callable[[], tuple[bool, str]] = homa_probe, binding=None) -> None:
        self._probe = probe
        self._binding = binding

    def _bound(self):
        return self._binding if self._binding is not None else _HOMA_BINDING

    def available(self) -> tuple[bool, str]:
        ok, why = self._probe()
        if not ok:
            return False, why
        if self._bound() is None:
            return False, f"{why}, but no Homa message binding is registered for this module"
        return True, why

    def connect(self, host: str, port: int, timeout: float = 10.0) -> Conn:
        ok, why = self.available()
        if not ok:
            raise TransportUnavailableError(why)
        return self._bound().connect(host, port, timeout)

    def serve(self, store: BlockStore, host: str = "0.0.0.0", port: int = 0) -> Server:
        ok, why = self.available()
        if not ok:
            raise TransportUnavailableError(why)
        return self._bound().serve(lambda t, p: handle(store, t, p), host, port)


BACKENDS = ("homa", "tcp")


def make_transport(name: str):
    if name == "homa":
        return HomaTransport()
    if name == "tcp":
        return TcpTransport()
    raise ValueError(f"unknown kvpool transport {name!r} (homa, tcp, auto)")


def select_transport(prefer: Optional[str] = None, candidates: Optional[Iterable] = None):
    """The first usable backend. ``prefer`` = 'homa', 'tcp' or 'auto' (homa, then tcp);
    None reads ``ADK_KVPOOL_TRANSPORT`` (default auto).

    A preferred backend that cannot run is logged at WARNING with its reason, then the next
    one is used; TCP always runs, so this only raises when every candidate is unusable.
    """
    prefer = (prefer or os.environ.get("ADK_KVPOOL_TRANSPORT") or "auto").lower()
    if candidates is None:
        order = ["homa", "tcp"] if prefer == "auto" else [prefer] + [
            b for b in BACKENDS if b != prefer
        ]
        candidates = [make_transport(n) for n in order]
    tried = []
    for t in candidates:
        ok, why = t.available()
        if ok:
            if tried:
                log.warning(
                    "kvpool transport: FALLBACK to %s -- %s",
                    t.name,
                    "; ".join(f"{n} unavailable: {w}" for n, w in tried),
                )
            return t
        tried.append((t.name, why))
    raise TransportUnavailableError(
        "no kvpool transport usable: " + "; ".join(f"{n}: {w}" for n, w in tried)
    )


# ---------------------------------------------------------------- client


class BlockClient:
    """Pull encoded blocks from a peer's :class:`BlockStore`.

    ``connect`` falls back from Homa to TCP on a connect-time failure too (a peer without
    Homa refuses the message), loudly, once per client.
    """

    def __init__(self, host: str, port: int, prefer: Optional[str] = None, timeout: float = 10.0,
                 candidates: Optional[list] = None):
        self.host, self.port, self.timeout = host, port, timeout
        cands = list(candidates) if candidates is not None else None
        t = select_transport(prefer, cands)
        try:
            self.conn = t.connect(host, port, timeout)
        except OSError as e:
            if t.name == "tcp":
                raise
            log.warning("kvpool transport: FALLBACK to tcp -- %s connect to %s:%s failed: %s",
                        t.name, host, port, e)
            t = TcpTransport()
            self.conn = t.connect(host, port, timeout)
        self.transport = t.name

    def fetch_encoded(self, ns, keys: list[bytes], token: Optional[bytes] = None
                      ) -> list[Optional[bytes]]:
        mtype, rep = self.conn.rpc(GET, pack_get(ns_digest(ns), list(keys), token))
        if mtype == ERR:
            raise RuntimeError(rep.decode("utf-8", "replace"))
        if mtype != BLOCKS:
            raise ConnectionError(f"unexpected reply type {mtype}")
        out = unpack_blocks(rep)
        if len(out) != len(keys):
            raise ConnectionError("reply block count differs from the request")
        return out

    def fetch(self, ns, keys: list[bytes], token: Optional[bytes] = None) -> list:
        """Decoded arrays (None for a miss)."""
        return [None if b is None else decode_kv(b)[0] for b in self.fetch_encoded(ns, keys, token)]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = [
    "BACKENDS",
    "BlockClient",
    "BlockStore",
    "HomaTransport",
    "TcpTransport",
    "TransportUnavailableError",
    "codec_name",
    "decode_kv",
    "encode_kv",
    "homa_probe",
    "make_transport",
    "ns_digest",
    "pool_block_tensor",
    "register_homa_binding",
    "select_transport",
    "serve_pool",
]
