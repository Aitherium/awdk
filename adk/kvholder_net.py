"""Reach a KV holder wherever it is: USB, LAN, a mesh overlay, or a public tunnel.

The engine always talks PATN v3 over plain TCP to ``127.0.0.1:50062``. The relay
here answers that port and forwards every PATN message, one WebSocket binary
message each, to a holder that DIALED IN. The holder dials out, so the same
relay works for:

- a phone on USB (``adb reverse`` maps the phone's localhost to this machine),
- a phone or laptop on the LAN or a mesh overlay (``--lan``, token-gated),
- anything on the internet through an HTTPS tunnel (``wss://``, token-gated),

and a phone needs no app: the relay serves a holder page that runs in the
phone's browser (WebGPU when the page is a secure context, CPU otherwise).
A Python holder can dial in too: ``adk kvholder serve --connect URL``.

Stdlib only (the holder's math needs numpy; the relay does not).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from adk import kvholder as kv

log = logging.getLogger("adk.kvholder.net")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_WS_PORT = 50063
CALL_TIMEOUT_S = 60.0
KEEPALIVE_S = 25.0  # under Cloudflare's 100 s idle cut
_MAX_WS = 1 << 31
_HELLO_MAX = 4096

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


# ---------------------------------------------------------------- minimal RFC 6455


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


class WebSocket:
    """One side of a WebSocket. ``client=True`` masks outgoing frames (RFC 6455 5.3)."""

    def __init__(self, sock: socket.socket, client: bool):
        self.sock = sock
        self.client = client
        self.wlock = threading.Lock()

    def send(self, data: bytes | str, opcode: int | None = None) -> None:
        if isinstance(data, str):
            data, op = data.encode(), OP_TEXT if opcode is None else opcode
        else:
            op = OP_BIN if opcode is None else opcode
        n = len(data)
        head = bytearray([0x80 | op])
        mbit = 0x80 if self.client else 0
        if n < 126:
            head.append(mbit | n)
        elif n < 1 << 16:
            head.append(mbit | 126)
            head += struct.pack(">H", n)
        else:
            head.append(mbit | 127)
            head += struct.pack(">Q", n)
        if self.client:
            mask = os.urandom(4)
            head += mask
            data = _xor_mask(data, mask)
        with self.wlock:
            self.sock.sendall(bytes(head) + data)

    def recv(self, limit: int = _MAX_WS) -> tuple[int, bytes]:
        """Next data message (text or binary). Answers pings; raises ConnectionError on close.

        ``limit`` caps the whole message: an unauthenticated peer gets a few KB, not 2 GB.
        """
        parts: list[bytes] = []
        total = 0
        first_op = None
        while True:
            b0, b1 = _recv_exact(self.sock, 2)
            fin, op = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", _recv_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", _recv_exact(self.sock, 8))[0]
            total += n
            if n > _MAX_WS or total > limit:
                raise ConnectionError("frame too large")
            mask = _recv_exact(self.sock, 4) if b1 & 0x80 else b""
            payload = _recv_exact(self.sock, n) if n else b""
            if mask:
                payload = _xor_mask(payload, mask)
            if op == OP_PING:
                self.send(payload, OP_PONG)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                raise ConnectionError("websocket closed")
            if op != OP_CONT:
                first_op = op
            parts.append(payload)
            if fin:
                return first_op if first_op is not None else OP_BIN, b"".join(parts)

    def close(self) -> None:
        try:
            self.send(b"", OP_CLOSE)
        except OSError as e:
            log.debug("kvholder: close frame not sent: %s", e)
        self.sock.close()


def _xor_mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return data
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "little")
    return (int.from_bytes(data, "little") ^ m).to_bytes(n, "little")


def ws_connect(url: str, timeout: float = 15.0) -> WebSocket:
    """Client handshake for ws:// or wss:// (verified TLS)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("ws", "wss"):
        raise ValueError("holder URL must be ws:// or wss://")
    port = u.port or (443 if u.scheme == "wss" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if u.scheme == "wss":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        "User-Agent: adk-kvholder\r\n\r\n"
    )
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake: peer closed")
        head += chunk
        if len(head) > 65536:
            raise ConnectionError("handshake: header too large")
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status + b" ":
        raise ConnectionError(f"handshake refused: {status.decode(errors='replace')}")
    hdrs = _parse_headers(head.decode("latin-1"))
    if hdrs.get("sec-websocket-accept") != ws_accept(key):
        raise ConnectionError("handshake: bad Sec-WebSocket-Accept")
    sock.settimeout(None)
    return WebSocket(sock, client=True)


def _parse_headers(text: str) -> dict[str, str]:
    out = {}
    for line in text.split("\r\n")[1:]:
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip().lower()] = v.strip()
    return out


# ---------------------------------------------------------------- relay


@dataclass
class _Attached:
    ws: WebSocket
    device: str
    since: float
    max_bytes: int | None = None  # what the holder said it lends; None = unbounded
    store: str = "wire"  # how it stores a key: as received ("wire"), or dequantized "f16"/"f32"
    off: int = 0  # first global key position this holder keeps (same for every layer)
    cap: int | None = None  # keys per layer it keeps, from max_bytes and the CONFIG
    lock: threading.Lock = field(default_factory=threading.Lock)
    session: str = ""  # lets the same holder reattach after a dropped link
    calls: int = 0  # attention calls it answered
    last_ms: float = 0.0  # its own compute time on the last call (from its reply)
    sum_ms: float = 0.0
    rtt_ms: float = 0.0  # the last call's round trip as the relay saw it
    seen: float = field(default_factory=time.time)  # last successful exchange


class HolderLostError(ConnectionError):
    pass


class Relay:
    """Engine-side PATN over TCP  <->  any number of dialed-in holders over WebSocket.

    The engine sees ONE holder. With several, the relay gives each a contiguous range of key
    positions (the same range on every layer, sized from the memory it lends), splits APPENDs
    by range, sends each attention call to the holders that hold keys in parallel, and merges
    their partials (exact log-sum-exp merge). A holder that joins later takes the next range,
    so capacity grows while the engine runs. A holder lost while holding keys makes every call
    fail loudly until the engine re-CONFIGs: the engine recomputes, never gets a partial answer.
    """

    def __init__(self, token: str):
        self.token = token
        self.lock = threading.Lock()  # one engine op at a time, as the protocol requires
        self.holders: list[_Attached] = []
        self.cfg: kv.Config | None = None
        self.n: list[int] = []  # keys held per layer, across all holders
        self.broken = ""
        self.calls = 0
        self.attn_calls = 0
        self.stop = threading.Event()
        self.joins: dict[str, float] = {}  # join token -> expiry: handed to an on-demand holder
        self.sessions: set[str] = set()  # issued on a join, so that holder can reconnect

    # ------------------------------------------------------------ membership

    @property
    def holder(self) -> _Attached | None:
        return self.holders[0] if self.holders else None

    def check_token(self, token: str) -> bool:
        return self.admit(token) is not None

    def admit(self, token: str) -> str | None:
        """'master', 'session', 'join' (consumed) or None. Constant-time per candidate."""
        b = token.encode()
        if hmac.compare_digest(b, self.token.encode()):
            return "master"
        if any(hmac.compare_digest(b, t.encode()) for t in list(self.sessions)):
            return "session"
        now = time.time()
        for t, exp in list(self.joins.items()):
            if exp < now:
                self.joins.pop(t, None)
            elif hmac.compare_digest(b, t.encode()):
                self.joins.pop(t, None)  # one holder per join token
                return "join"
        return None

    def mint_join(self, ttl_s: float = 900.0) -> tuple[str, float]:
        """A token an on-demand holder (a CI runner, a container) uses ONCE to attach.

        It may sit in a CI log or a job's inputs; it expires in ``ttl_s`` and dies on first
        use. The master token never leaves this machine.
        """
        tok, exp = "j-" + secrets.token_urlsafe(18), time.time() + ttl_s
        self.joins[tok] = exp
        return tok, exp

    def attach(
        self,
        ws: WebSocket,
        device: str,
        max_bytes: int | None = None,
        store: str = "wire",
        session: str = "",
        held: int | None = None,
    ) -> None:
        h = _Attached(ws, device, time.time(), max_bytes=max_bytes, store=store, session=session)
        with self.lock:
            old = next((o for o in self.holders if session and o.session == session), None)
            if old is not None:
                expect = self._local(old, max(self.n) if self.n else 0)
                if held == expect:  # same holder, same keys: swap the link, lose nothing
                    with old.lock:
                        dead, old.ws = old.ws, ws
                    dead.close()
                    print(f"kvholder: holder reattached: {device} ({expect} keys kept)", flush=True)
                    return
                self.detach(old)  # it came back without its keys: fail loudly, never guess
            self.holders.append(h)
            if self.cfg is not None:
                self._place(h)
                try:
                    # short: the engine waits behind this lock while a newcomer configures
                    rep = self._call(h, _msg(kv.CONFIG, self.cfg.pack()), timeout=10.0)
                except HolderLostError:
                    return
                if _type(rep) != kv.OK:
                    self.holders.remove(h)
                    ws.close()
                    return
        lend = "unbounded" if max_bytes is None else f"lends {max_bytes >> 20} MB"
        print(f"kvholder: holder attached: {device} ({lend})", flush=True)

    def _place(self, h: _Attached) -> None:
        """Give ``h`` the next free key range (the CONFIG must be known)."""
        assert self.cfg is not None
        per_key = self._key_bytes(h) * self.cfg.n_layer
        h.cap = None if h.max_bytes is None else (h.max_bytes // per_key) // 64 * 64
        h.off = 0
        for o in self.holders:
            if o is h:
                break
            if o.cap is None:
                h.off = 1 << 62  # behind an unbounded holder: never reached
                break
            h.off = max(h.off, o.off + o.cap)

    def _key_bytes(self, h: _Attached) -> int:
        """Bytes ``h`` spends on one key position of one layer (keys and values)."""
        cfg = self.cfg
        if cfg is None:
            return 0
        elem = {"f16": 2, "f32": 4}.get(h.store)
        if elem:
            return cfg.n_head_kv * (cfg.k_dim + cfg.v_dim) * elem
        if h.store == "tq4":
            return cfg.n_head_kv * (cfg.k_dim // 2 + 4 + cfg.v_dim // 2 + 4)
        return cfg.rs + cfg.v_rs

    def detach(self, h: _Attached) -> None:
        if h not in self.holders:
            return
        self.holders.remove(h)
        held = self._local(h, max(self.n) if self.n else 0)
        if held:
            self.broken = f"holder lost: {h.device} held keys [{h.off}, {h.off + held})"
        print(f"kvholder: holder gone: {h.device}" + (" (keys lost)" if held else ""), flush=True)

    def _local(self, h: _Attached, n_global: int) -> int:
        """How many of the first ``n_global`` positions ``h`` holds."""
        top = n_global - h.off
        if h.cap is not None:
            top = min(top, h.cap)
        return max(0, top)

    # ------------------------------------------------------------ one holder call

    def _call(
        self, h: _Attached, msg: bytes, expect_reply: bool = True, timeout: float = CALL_TIMEOUT_S
    ) -> bytes:
        with h.lock:
            try:
                t0 = time.perf_counter()
                h.ws.sock.settimeout(timeout)
                h.ws.send(msg)
                if not expect_reply:
                    return b""
                op, reply = h.ws.recv()
                if op != OP_BIN or len(reply) < kv.HDR.size:
                    raise ConnectionError("holder sent a non-PATN reply")
                h.seen = time.time()
                if _type(reply) == kv.ATTN_OK and len(reply) >= kv.HDR.size + kv.ATTN_REP.size:
                    h.rtt_ms = (time.perf_counter() - t0) * 1000.0
                    h.last_ms = float(kv.ATTN_REP.unpack_from(reply, kv.HDR.size)[1])
                    h.calls += 1
                    h.sum_ms += h.last_ms
                return reply
            except (OSError, ConnectionError) as e:
                self.detach(h)
                raise HolderLostError(f"holder lost: {h.device}: {e}") from e
            finally:
                if h in self.holders:
                    h.ws.sock.settimeout(None)

    # ------------------------------------------------------------ the engine's view

    def call(self, msg: bytes, expect_reply: bool = True) -> bytes:
        """One PATN request from the engine -> the reply message (header + payload)."""
        _, mtype, n = kv.HDR.unpack_from(msg)
        p = msg[kv.HDR.size : kv.HDR.size + n]
        with self.lock:
            if mtype == kv.BYE:
                for h in list(self.holders):
                    try:
                        self._call(h, msg, expect_reply=False)
                    except HolderLostError:
                        continue
                return b""
            if not self.holders:
                return _err("no holder attached")
            try:
                self.calls += 1
                if mtype in (kv.HELLO, kv.STATS, kv.PING):
                    return self._info(mtype, msg)
                if mtype == kv.CONFIG:
                    return self._config(msg, p)
                if self.broken and mtype in (kv.APPEND, kv.ATTN, kv.ATTN_BIG):
                    return _err(self.broken)
                if mtype == kv.APPEND:
                    return self._append(p)
                if mtype == kv.TRUNCATE:
                    return self._truncate(p)
                if mtype in (kv.ATTN, kv.ATTN_BIG):
                    self.attn_calls += 1
                    return self._attn(mtype, p)
                return self._call(self.holders[0], msg)
            except HolderLostError as e:
                return _err(self.broken or str(e))

    def _info(self, mtype: int, msg: bytes) -> bytes:
        if len(self.holders) == 1:
            return self._call(self.holders[0], msg)
        if mtype == kv.PING:
            _, _, n = kv.HDR.unpack_from(msg)
            k = struct.unpack_from("<I", msg, kv.HDR.size)[0] if n >= 4 else 0
            return _msg(kv.OK, b"\x5a" * min(k, 64 << 20))
        if mtype == kv.HELLO:
            mb = sum(h.max_bytes or 0 for h in self.holders) >> 20
            dev = f"adk-relay holders={len(self.holders)} max_mb={mb}".encode()[:63]
            return _msg(kv.HELLO_OK, kv.HELLO_REP.pack(kv.VERSION, 0, dev))
        lines = [f"relay holders={len(self.holders)} held={self.n[0] if self.n else 0}"]
        for h in list(self.holders):
            rep = self._call(h, msg)
            lines.append(f"[{h.device} off={h.off}] {_text(rep)}")
        return _msg(kv.OK, " | ".join(lines).encode())

    def _config(self, msg: bytes, p: bytes) -> bytes:
        if len(p) < kv.CONFIG_REQ.size:
            return _err("short CONFIG")
        self.cfg = kv.Config.unpack(p)
        self.n = [0] * self.cfg.n_layer
        self.broken = ""
        for h in self.holders:
            self._place(h)
        for h in list(self.holders):
            rep = self._call(h, msg)
            if _type(rep) != kv.OK:
                self.cfg = None
                return rep
        return _msg(kv.OK, b"")

    def _append(self, p: bytes) -> bytes:
        if self.cfg is None or len(p) < kv.APPEND_REQ.size:
            return _err("short APPEND")
        layer, pos0, n = kv.APPEND_REQ.unpack_from(p)
        rs, v_rs = self.cfg.rs, self.cfg.v_rs
        if layer >= self.cfg.n_layer or len(p) != kv.APPEND_REQ.size + n * (rs + v_rs):
            return _err("bad APPEND")
        if pos0 != self.n[layer]:
            return _err(f"APPEND pos0 {pos0} != held {self.n[layer]}")
        keys = p[kv.APPEND_REQ.size : kv.APPEND_REQ.size + n * rs]
        vals = p[kv.APPEND_REQ.size + n * rs : kv.APPEND_REQ.size + n * (rs + v_rs)]
        plan, done = [], 0  # place every row BEFORE sending any: a refused APPEND changes nothing
        for h in self.holders:
            if done == n:
                break
            pos = pos0 + done
            end = pos0 + n if h.cap is None else min(pos0 + n, h.off + h.cap)
            if pos < h.off or end <= pos:
                continue
            plan.append((h, pos, end - pos, done))
            done += end - pos
        if done != n:
            return _err(f"out of memory at {pos0 + done} keys (no holder has room)")
        for i, (h, pos, take, at) in enumerate(plan):
            sub = kv.APPEND_REQ.pack(layer, pos - h.off, take)
            ka, kb = at * rs, (at + take) * rs
            va, vb = at * v_rs, (at + take) * v_rs
            rep = self._call(h, _msg(kv.APPEND, sub + keys[ka:kb] + vals[va:vb]))
            if _type(rep) != kv.OK:
                if i:  # earlier holders already took their rows: the state is split, say so
                    self.broken = f"APPEND split failed at {pos} keys ({h.device}: {_text(rep)})"
                return _err(f"out of memory at {pos} keys ({h.device}: {_text(rep)})")
        self.n[layer] += n
        return _msg(kv.OK, struct.pack("<I", self.n[layer]))

    def _truncate(self, p: bytes) -> bytes:
        keep = struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0
        for h in list(self.holders):
            rep = self._call(h, _msg(kv.TRUNCATE, struct.pack("<I", self._local(h, keep))))
            if _type(rep) != kv.OK:
                return rep
        self.n = [min(x, keep) for x in self.n]
        return _msg(kv.OK, b"")

    def _attn(self, mtype: int, p: bytes) -> bytes:
        if self.cfg is None or len(p) < kv.ATTN_REQ.size:
            return _err("short ATTN")
        layer, n_tok, nk, scale = kv.ATTN_REQ.unpack_from(p)
        if layer >= self.cfg.n_layer:
            return _err("bad ATTN")
        q = p[kv.ATTN_REQ.size :]
        nk_all = self.n[layer] if nk == 0 else min(nk, self.n[layer])
        parts = [(h, self._local(h, nk_all)) for h in self.holders]
        parts = [(h, k) for h, k in parts if k > 0]
        if len(parts) == 1 and parts[0][0].off == 0:  # one holder: pass straight through
            req = kv.ATTN_REQ.pack(layer, n_tok, nk_all, scale) + q
            return self._call(parts[0][0], _msg(mtype, req))
        qn = self.cfg.n_head_kv * self.cfg.rows * self.cfg.k_dim
        dv = self.cfg.v_dim
        ng = len(q) // (2 * qn)
        rows = ng * self.cfg.n_head_kv * self.cfg.rows
        if ng < 1 or len(q) != ng * 2 * qn:
            return _err("bad ATTN")
        if not parts:
            lse = struct.pack(f"<{rows}f", *([float("-inf")] * rows))
            head = kv.ATTN_REP.pack(0, 0.0, 0.0, 0.0, 0, 0)
            return _msg(kv.ATTN_OK, head + bytes(rows * dv * 2) + lse)
        np = kv.np
        if np is None:
            return _err("the relay needs numpy to merge several holders")
        replies: list = [None] * len(parts)

        def run(i: int, h: _Attached, k: int) -> None:
            try:
                req = kv.ATTN_REQ.pack(layer, n_tok, k, scale) + q
                replies[i] = self._call(h, _msg(mtype, req))
            except HolderLostError as e:
                replies[i] = e

        threads = [threading.Thread(target=run, args=(i, h, k)) for i, (h, k) in enumerate(parts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        merged, ms, pages = [], 0.0, 0
        for rep in replies:
            if isinstance(rep, Exception):
                raise rep
            if _type(rep) != kv.ATTN_OK:
                return rep
            body = rep[kv.HDR.size :]
            _, t_ms, _, _, _, pg = kv.ATTN_REP.unpack_from(body)
            o = np.frombuffer(body, np.float16, rows * dv, kv.ATTN_REP.size)
            lse = np.frombuffer(body, np.float32, rows, kv.ATTN_REP.size + rows * dv * 2)
            merged.append((o.astype(np.float32).reshape(rows, dv), lse))
            ms, pages = max(ms, t_ms), pages + pg
        o, lse = kv.merge_partials(merged)
        head = kv.ATTN_REP.pack(nk_all, ms, 0.0, 0.0, 0, pages)
        return _msg(kv.ATTN_OK, head + o.astype(np.float16).tobytes() + lse.tobytes())

    # ------------------------------------------------------------ upkeep

    def keepalive(self) -> None:
        while not self.stop.wait(KEEPALIVE_S):
            for h in list(self.holders):
                if not h.lock.acquire(timeout=0.1):
                    continue
                try:
                    h.ws.send(b"ka", OP_PING)
                    ok = True
                    h.seen = time.time()
                except OSError:
                    ok = False
                finally:
                    h.lock.release()
                if not ok:
                    with self.lock:
                        self.detach(h)

    def status(self) -> dict:
        """What /status serves: no token, ever. The original keys keep their meaning."""
        hs = list(self.holders)
        cfg, now = self.cfg, time.time()
        n_glob = max(self.n) if self.n else 0
        holders = []
        for h in hs:
            held = self._local(h, n_glob)
            used = held * self._key_bytes(h) * (cfg.n_layer if cfg else 0)
            holders.append(
                {
                    "device": h.device,
                    "off": h.off,
                    "cap": h.cap,
                    "max_bytes": h.max_bytes,
                    "since": h.since,
                    "store": h.store,
                    "held": held,  # key positions it keeps, on every layer
                    "used_bytes": used,
                    "calls": h.calls,
                    "last_ms": round(h.last_ms, 3),
                    "mean_ms": round(h.sum_ms / h.calls, 3) if h.calls else 0.0,
                    "rtt_ms": round(h.rtt_ms, 3),
                    "seen_s": round(now - h.seen, 1),
                }
            )
        shape = None
        if cfg is not None:
            shape = {
                "n_layer": cfg.n_layer,
                "n_head_kv": cfg.n_head_kv,
                "k_dim": cfg.k_dim,
                "v_dim": cfg.v_dim,
            }
        return {
            "attached": bool(hs),
            "device": hs[0].device if hs else None,
            "since": hs[0].since if hs else None,
            "holders": holders,
            "held": self.n[0] if self.n else 0,
            "held_per_layer": list(self.n),
            "warnings": tq4_warnings(hs),
            "broken": self.broken or None,
            "calls": self.calls,
            "attn_calls": self.attn_calls,
            "configured": cfg is not None,
            "shape": shape,
            "lent_bytes": sum(h.max_bytes or 0 for h in hs),
            "used_bytes": sum(x["used_bytes"] for x in holders),
            "now": now,
        }


def _msg(mtype: int, payload: bytes) -> bytes:
    return kv.HDR.pack(kv.MAGIC, mtype, len(payload)) + payload


def _type(rep: bytes) -> int:
    return kv.HDR.unpack_from(rep)[1]


def _text(rep: bytes) -> str:
    return rep[kv.HDR.size :].decode(errors="replace")


def _err(text: str) -> bytes:
    return _msg(kv.ERR, text.encode())


class _EngineHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        from adk import kvpool  # sessions + shared prefix blocks; plain Relay.call without them

        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        kv._tune(sock)
        conn = kvpool.EngineConn()
        try:
            while True:
                head = kv._recv_all(sock, kv.HDR.size)
                magic, mtype, n = kv.HDR.unpack(head)
                if magic != kv.MAGIC or n > (1 << 31):
                    return
                payload = kv._recv_all(sock, n) if n else b""
                if mtype == kv.BYE:
                    kvpool.relay_call(relay, conn, head + payload, expect_reply=False)
                    return
                sock.sendall(kvpool.relay_call(relay, conn, head + payload))
        except (ConnectionError, OSError):
            return


class _HTTPHandler(socketserver.BaseRequestHandler):
    """GET / -> holder page, GET /status -> JSON, GET /holder (Upgrade) -> attach."""

    def handle(self) -> None:
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        sock.settimeout(15)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk or len(head) > 65536:
                    return
                head += chunk
        except OSError:
            return
        text = head.decode("latin-1")
        method, path = (text.split("\r\n", 1)[0].split(" ") + ["", ""])[:2]
        hdrs = _parse_headers(text)
        route = urllib.parse.urlsplit(path).path
        if method != "GET":
            return self._send(405, "text/plain", b"GET only")
        if route == "/holder" and hdrs.get("upgrade", "").lower() == "websocket":
            return self._upgrade(sock, hdrs, relay)
        if route == "/status":
            return self._send(200, "application/json", json.dumps(relay.status()).encode())
        if route == "/join":
            return self._join(path, hdrs, relay)
        if route == "/swarm":
            if not self._loopback(hdrs):
                return self._send(403, "text/plain", b"the swarm view is for this machine only")
            from adk.kvholder_page import SWARM_HTML

            return self._send(200, "text/html; charset=utf-8", SWARM_HTML.encode())
        if route == "/swarm/join":
            return self._swarm_join(path, hdrs, relay)
        if route.startswith("/mesh"):
            from adk import kvholder_mesh

            return kvholder_mesh.handle_http(self, path, hdrs, relay)
        if route in ("/", "/index.html"):
            from adk.kvholder_page import PAGE_HTML

            return self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
        if route == "/holder.js":
            from adk.kvholder_page import HOLDER_JS

            return self._send(200, "text/javascript; charset=utf-8", HOLDER_JS.encode())
        if route == "/state.js":
            from adk.kvholder_page import STATE_JS

            return self._send(200, "text/javascript; charset=utf-8", STATE_JS.encode())
        return self._send(404, "text/plain", b"not found")

    def _join(self, path: str, hdrs: dict, relay: Relay) -> None:
        """Mint a join token: this machine only, and only with the master token."""
        peer = self.client_address[0]
        if peer not in ("127.0.0.1", "::1") or relay.admit(hdrs.get("x-kv-token", "")) != "master":
            return self._send(403, "text/plain", b"forbidden")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        try:
            ttl = min(max(float(q.get("ttl", ["900"])[0]), 30.0), 86400.0)
        except ValueError:
            ttl = 900.0
        tok, exp = relay.mint_join(ttl)
        return self._send(
            200, "application/json", json.dumps({"token": tok, "expires": exp}).encode()
        )

    def _loopback(self, hdrs: dict) -> bool:
        """A request from this machine, addressed to this machine (no DNS rebinding)."""
        host = hdrs.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
        return self.client_address[0] in ("127.0.0.1", "::1") and host in (
            "127.0.0.1",
            "localhost",
            "::1",
        )

    def _swarm_join(self, path: str, hdrs: dict, relay: Relay) -> None:
        """The swarm view's "add a phone": a single-use join link per way a phone can reach
        this relay, each with a QR. Loopback and the master token, like /join."""
        if not self._loopback(hdrs) or relay.admit(hdrs.get("x-kv-token", "")) != "master":
            return self._send(403, "text/plain", b"forbidden")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        try:
            ttl = min(max(float(q.get("ttl", ["900"])[0]), 30.0), 86400.0)
        except ValueError:
            ttl = 900.0
        tok, exp = relay.mint_join(ttl)
        port = self.server.server_address[1]
        links = [
            {"via": via, "url": holder_url(base, tok), "svg": qr_svg(holder_url(base, tok))}
            for via, base in phone_bases(port, self.server.server_address[0])
        ]
        body = {"token": tok, "expires": exp, "links": links}
        return self._send(200, "application/json", json.dumps(body).encode())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}.get(
            code, ""
        )
        head = (
            f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        )
        try:
            self.request.sendall(head.encode() + body)
        except OSError as e:
            log.debug("kvholder: http reply not sent: %s", e)

    def _upgrade(self, sock: socket.socket, hdrs: dict, relay: Relay) -> None:
        key = hdrs.get("sec-websocket-key")
        if not key:
            return self._send(404, "text/plain", b"no key")
        sock.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept(key)}\r\n\r\n"
            ).encode()
        )
        ws = WebSocket(sock, client=False)
        try:
            # first message {"hello": "kvholder", "token", "device"}; nothing big before auth
            op, data = ws.recv(limit=_HELLO_MAX)
            hello = json.loads(data.decode()) if op == OP_TEXT else {}
        except (ConnectionError, OSError, ValueError):
            return
        kind = (
            relay.admit(str(hello.get("token", ""))) if hello.get("hello") == "kvholder" else None
        )
        if kind is None:
            ws.send(json.dumps({"ok": False, "error": "bad token"}))
            ws.close()
            return
        ack: dict = {"ok": True}
        session = (
            str(hello.get("token", "")) if kind == "session" else "s-" + secrets.token_urlsafe(18)
        )
        if kind != "session":
            relay.sessions.add(session)
            ack["session"] = session
        held = hello.get("held")
        device = str(hello.get("device", "holder"))[:80]
        mb = hello.get("max_bytes")
        max_bytes = int(mb) if isinstance(mb, (int, float)) and mb > 0 else None
        store = str(hello.get("store", "wire"))
        sock.settimeout(None)
        ws.send(json.dumps(ack))
        relay.attach(
            ws,
            device,
            max_bytes,
            store if store in ("wire", "f16", "f32", "tq4") else "f32",
            session=session,
            held=held if isinstance(held, int) else None,
        )
        _note_tq4(relay, ws, hello)
        # This thread now only parks: the relay drives the socket under the holder's lock.
        while any(h.ws is ws for h in relay.holders):
            time.sleep(0.5)


TQ4_UNCENTERED = "tq4 uncentered: approximate, known-bad on real models"


def _note_tq4(relay: Relay, ws: WebSocket, hello: dict) -> None:
    """Record whether a tq4 holder centers its keys (hello "tq4": "centered").

    Centering is internal to the holder (its ATTN reply is already exact), so nothing is
    requested; an old holder that does not announce it is only flagged, loudly.
    """
    for h in list(relay.holders):
        if h.ws is ws:
            h.tq4 = str(hello.get("tq4", ""))
            if h.store == "tq4" and h.tq4 != "centered":
                print(f"kvholder: WARNING {h.device}: {TQ4_UNCENTERED}", flush=True)


def tq4_warnings(holders: list) -> list[str]:
    return [
        f"{h.device}: {TQ4_UNCENTERED}"
        for h in holders
        if h.store == "tq4" and getattr(h, "tq4", "") != "centered"
    ]


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # On Windows SO_REUSEADDR lets a second relay bind the SAME port and steal half the
    # connections: its token differs, so a phone gets "bad token" from a relay it never asked
    # for (measured 2026-10-03). There, bind exclusively so a second relay fails loudly.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def __init__(self, addr, handler, relay: Relay):
        super().__init__(addr, handler)
        self.relay = relay

    def server_bind(self) -> None:
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
        super().server_bind()


def start_relay(
    token: str,
    engine_addr: tuple[str, int] = ("127.0.0.1", kv.DEFAULT_PORT),
    ws_addr: tuple[str, int] = ("127.0.0.1", DEFAULT_WS_PORT),
) -> tuple[Relay, list[_Server]]:
    relay = Relay(token)
    engine = _Server(engine_addr, _EngineHandler, relay)
    try:
        web = _Server(ws_addr, _HTTPHandler, relay)
    except OSError:
        engine.server_close()  # do not keep the engine port of a relay that never started
        raise
    servers = [engine, web]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    threading.Thread(target=relay.keepalive, daemon=True).start()
    return relay, servers


def stop_relay(relay: Relay, servers: list[_Server]) -> None:
    relay.stop.set()
    for h in list(relay.holders):
        h.ws.close()
    for s in servers:
        s.shutdown()
        s.server_close()


# ---------------------------------------------------------------- a Python holder that dials in


def dial_holder(url: str, token: str, holder: "kv.KVHolder", once: bool = False) -> int:
    """Attach ``holder`` to a relay at ``url`` (ws[s]://host[:port]/holder); serve until stopped."""
    backoff = 1.0
    while True:
        try:
            ws = ws_connect(url)
            ws.send(
                json.dumps(
                    {
                        "hello": "kvholder",
                        "token": token,
                        "device": holder.device,
                        "max_bytes": holder.st.max_bytes,
                        "held": max(holder.st.n) if holder.st.n else 0,
                        "store": holder.st.store,
                        "tq4": "centered" if getattr(holder, "tq4_center", False) else "",
                    }
                )
            )
            op, data = ws.recv()
            ack = json.loads(data.decode()) if op == OP_TEXT else {}
            if not ack.get("ok"):
                print(f"kvholder: relay refused: {ack.get('error', 'no reason')}", file=sys.stderr)
                return 1
            token = ack.get("session") or token  # a join token is single-use: reconnect with this
            print(f"kvholder: attached to {url.split('#')[0]}", flush=True)
            backoff = 1.0
            while True:
                op, msg = ws.recv()
                if op != OP_BIN or len(msg) < kv.HDR.size:
                    continue
                _, mtype, n = kv.HDR.unpack_from(msg)
                rep = holder.handle(mtype, msg[kv.HDR.size : kv.HDR.size + n])
                if rep is not None:
                    ws.send(kv.HDR.pack(kv.MAGIC, rep[0], len(rep[1])) + rep[1])
        except (ConnectionError, OSError, ValueError) as e:
            if once:
                print(f"kvholder: {e}", file=sys.stderr)
                return 1
            print(f"kvholder: link down ({e}); retrying in {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------- ways to reach a phone


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def adb_path() -> str | None:
    return shutil.which("adb")


def adb_devices() -> list[str]:
    adb = adb_path()
    if not adb:
        return []
    out = subprocess.run(
        [adb, "devices"], capture_output=True, text=True, encoding="utf-8", timeout=15
    )
    return [ln.split()[0] for ln in out.stdout.splitlines()[1:] if ln.strip().endswith("device")]


PHONE_BROWSERS = ("com.android.chrome", "com.chrome.beta", "com.chrome.dev")


def adb_link(port: int, url: str, serial: str | None = None) -> str:
    """Map the phone's localhost:port to ours and open the holder page on the phone."""
    adb = adb_path()
    if not adb:
        return "adb not found (install Android platform-tools)"
    base = [adb] + (["-s", serial] if serial else [])
    r = subprocess.run(
        base + ["reverse", f"tcp:{port}", f"tcp:{port}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    if r.returncode != 0:
        return f"adb reverse failed: {(r.stderr or r.stdout).strip()}"
    view = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{url}'"]
    # Chrome first: a bare VIEW goes to the default browser, and one without WebGPU (Edge on
    # Android, measured on a Pixel 10) silently runs the CPU holder.
    for pkg in PHONE_BROWSERS:
        r = subprocess.run(
            base + view + ["-p", pkg],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if r.returncode == 0 and "Error" not in (r.stdout + r.stderr):
            return ""
    subprocess.run(base + view, capture_output=True, text=True, encoding="utf-8", timeout=15)
    return ""


def tunnel_up(port: int, wait_s: float = 90.0) -> tuple[str, subprocess.Popen | None]:
    """A public https URL for the relay's web port via awtunnel, and the process holding it open.

    ``awtunnel up`` stays in the foreground for as long as the tunnel lives, so it is started,
    read until it prints the URL, and left running; ('', None) when awtunnel is absent or silent.
    """
    exe = shutil.which("awtunnel")
    base = [exe] if exe else [sys.executable, "-m", "awtunnel"]
    _down_stale_tunnel(base, port)
    cmd = base + ["up", "--port", str(port)]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        log.info("kvholder: awtunnel unavailable: %s", e)
        return "", None
    found: list[str] = []

    def _read() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            for word in line.split():
                url = _tunnel_url(word)
                if url and not found:
                    found.append(url)

    threading.Thread(target=_read, daemon=True).start()
    end = time.time() + wait_s
    while time.time() < end and not found and proc.poll() is None:
        time.sleep(0.2)
    if found and not _relay_answers(found[0], max(10.0, end - time.time())):
        print(f"kvholder: tunnel {found[0]} never served this relay; taking it down")
        found.clear()
    if not found:
        proc.terminate()
        subprocess.run(
            base + ["down"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        return "", None
    return found[0], proc


# cloudflared names its own API in the log line of a failed quick-tunnel request; awtunnel has
# recorded that as "the tunnel URL" (measured 2026-10-03). It is never the relay.
_NOT_A_TUNNEL = {"api.trycloudflare.com"}


def _tunnel_url(word: str) -> str:
    word = word.strip("\"'|").rstrip(".,)")
    if not word.startswith("https://"):
        return ""
    host = urllib.parse.urlsplit(word).hostname or ""
    return "" if not host or host in _NOT_A_TUNNEL else word


def _relay_answers(url: str, wait_s: float) -> bool:
    """Does ``url``/status come back from this relay? A fresh quick-tunnel name takes seconds
    to resolve, so retry. A browser User-Agent: Cloudflare refuses Python's (error 1010)."""
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + "/status", headers={"User-Agent": "Mozilla/5.0 (adk kvholder)"}
    )
    end = time.time() + wait_s
    while True:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return "attached" in json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            pass
        if time.time() >= end:
            return False
        time.sleep(2.0)


def _down_stale_tunnel(base: list[str], port: int) -> None:
    """A relay killed hard leaves its cloudflared running: a public URL still pointing at this
    port. We just bound the port, so a recorded tunnel to it belongs to a dead relay: take it
    down. A tunnel to any other port is someone else's and is left alone."""
    try:
        r = subprocess.run(
            base + ["status"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        st = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return
    if st.get("alive") and st.get("port") == port:
        subprocess.run(
            base + ["down"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        print(f"kvholder: took down a stale tunnel to :{port} ({st.get('url', '')})")


def phone_bases(web_port: int, bind: str) -> list[tuple[str, str]]:
    """Every base URL a phone could open this relay's page on, best first: the tunnel the
    running relay recorded, the LAN address when the page listens beyond loopback, and
    localhost on the phone (USB, ``adb reverse``)."""
    out: list[tuple[str, str]] = []
    st = read_state() or {}
    if st.get("web_port") == web_port:
        if st.get("public"):
            out.append(("tunnel", str(st["public"])))
        if st.get("lan"):
            out.append(("lan", str(st["lan"])))
    if bind not in ("127.0.0.1", "::1", "localhost") and not any(v == "lan" for v, _ in out):
        host = lan_ip() if bind in ("", "0.0.0.0", "::") else bind
        out.append(("lan", f"http://{host}:{web_port}"))
    out.append(("usb", f"http://localhost:{web_port}"))
    return out


def qr_svg(text: str) -> str:
    """``text`` as an inline SVG QR (one path, currentColor on a light quiet zone); '' when
    ``qrcode`` is missing. No xmlns: it is pasted into HTML, never served as a file."""
    try:
        import qrcode
    except ImportError:
        return ""
    qr = qrcode.QRCode(border=2)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    n = len(m)
    d = "".join(f"M{x} {y}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (
        f'<svg viewBox="0 0 {n} {n}" shape-rendering="crispEdges" role="img" '
        f'aria-label="QR code"><rect width="{n}" height="{n}" fill="#fff"/>'
        f'<path d="{d}" fill="#050507"/></svg>'
    )


def holder_url(base: str, token: str) -> str:
    """The page URL a phone opens. The token rides the #fragment: never sent to any server log."""
    return f"{base.rstrip('/')}/#t={token}"


def phone_qr(url: str) -> str:
    """The URL as a terminal QR code for the phone's camera; '' when it cannot be drawn.

    Uses ``qrcode`` (an awdk dependency). A console whose encoding lacks the block glyphs
    (cp1252) gets '' instead of a crash: the printed URL is the essential part.
    """
    try:
        import io

        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        block = buf.getvalue()
        block.encode(sys.stdout.encoding or "utf-8")
        return block
    except Exception:  # noqa: BLE001 - a QR is a convenience, never a failure
        return ""


def _show_url(label: str, url: str) -> None:
    print(f"{label}: {url}")
    qr = phone_qr(url)
    if qr:
        print(qr, end="" if qr.endswith("\n") else "\n")


def _firewall_hint(port: int) -> str:
    """Windows blocks inbound on a Private network unless python.exe has a rule for it: the
    phone then times out on the page with no error on this side. Name the fix."""
    if os.name != "nt":
        return ""
    return (
        "  no page on the phone? Windows Firewall may block inbound to this python "
        f"(admin shell): netsh advfirewall firewall add rule name=adk-kvholder dir=in "
        f"action=allow protocol=TCP localport={port} profile=private"
    )


# ---------------------------------------------------------------- CLI


def run_phone(args) -> int:
    token = args.token or secrets.token_urlsafe(18)
    bind = "127.0.0.1"
    if args.via == "lan":
        bind = args.host or "0.0.0.0"
    try:
        relay, servers = start_relay(token, ("127.0.0.1", args.port), (bind, args.web_port))
    except OSError as e:
        print(f"kvholder: cannot listen ({e}); is another relay running?", file=sys.stderr)
        return 1
    tproc, public, door = None, "", None
    print(f"kvholder: engine port 127.0.0.1:{args.port} (point LLAMA_KV_REMOTE here)")
    if getattr(args, "mesh", False):
        from adk import kvholder_mesh

        door = kvholder_mesh.open_door(relay, args)
    if args.via == "usb":
        devs = adb_devices()
        url = holder_url(f"http://localhost:{args.web_port}", token)
        if not devs:
            print(
                "kvholder: no phone on USB (enable USB debugging, accept the prompt, "
                "check `adb devices`)",
                file=sys.stderr,
            )
        else:
            err = adb_link(args.web_port, url, args.serial or None)
            print(
                f"kvholder: {err}"
                if err
                else f"kvholder: opened the holder page on {args.serial or devs[0]}"
            )
        print(f"phone URL: {url}   (localhost on the phone = WebGPU allowed)")
    elif args.via == "lan":
        adv = args.host or lan_ip()
        _show_url("phone URL", holder_url(f"http://{adv}:{args.web_port}", token))
        print(
            "  plain http on a LAN IP is not a secure context: the page runs the CPU holder."
            " Use --via usb or --via tunnel for WebGPU."
        )
        hint = _firewall_hint(args.web_port)
        if hint:
            print(hint)
    elif args.via == "tunnel":
        pub, tproc = tunnel_up(args.web_port)
        if not pub:
            print(
                "kvholder: awtunnel gave no URL (pip install awtunnel; awtunnel up --port "
                f"{args.web_port}); the relay is still up locally",
                file=sys.stderr,
            )
        else:
            public = pub
            _show_url("phone URL", holder_url(pub, token))
    else:
        print(f"page URL: {holder_url(f'http://localhost:{args.web_port}', token)}")
    print(
        "python holder: adk kvholder serve --connect "
        f"ws://<this-host>:{args.web_port}/holder --token {token}"
    )
    print("more holders on demand: adk kvholder elastic --count N")
    write_state(
        {
            "engine_port": args.port,
            "web_port": args.web_port,
            "token": token,
            "public": public,
            "lan": f"http://{args.host or lan_ip()}:{args.web_port}" if args.via == "lan" else "",
            "pid": os.getpid(),
        }
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("kvholder: relay stopped")
    finally:
        stop_relay(relay, servers)
        if door is not None:
            door.close()
        if tproc is not None:
            tproc.terminate()
        st = read_state()
        if st is None or st.get("pid") == os.getpid():  # never another relay's record
            state_path().unlink(missing_ok=True)
    return 0


# ---------------------------------------------------------------- elastic: holders on demand


def state_path() -> Path:
    return Path(
        os.environ.get("AITHER_KVHOLDER_STATE")
        or Path.home() / ".aither" / "kvholder" / "relay.json"
    )


def write_state(st: dict) -> None:
    """The running relay's ports, master token and public URL, readable by this user only."""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f)


def read_state() -> dict | None:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mint_join_remote(st: dict, ttl_s: float) -> str:
    """Ask the local relay for a join token (loopback + master token only)."""
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{st['web_port']}/join?ttl={int(ttl_s)}",
        headers={"X-KV-Token": st["token"]},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]


def relay_ws_url(base: str) -> str:
    u = urllib.parse.urlsplit(base)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}/holder"


def launch_argv(args, relay: str, join: str) -> list[list[str]]:
    """The commands that start one on-demand holder: awrun when installed, else gh."""
    fields = {
        "relay": relay,
        "join": join,
        "minutes": str(args.minutes),
        "max_mb": str(args.max_mb),
    }
    try:
        import awrun  # noqa: F401  (presence check)

        have_awrun = True
    except ImportError:
        have_awrun = False
    if have_awrun:
        sub = [
            sys.executable,
            "-m",
            "awrun",
            "submit",
            "--kind",
            "ci",
            "--workflow",
            args.workflow,
            "--ref",
            args.ref,
            "--priority",
            str(args.priority),
        ]
        for k, v in fields.items():
            sub += ["--field", f"{k}={v}"]
        return [sub, [sys.executable, "-m", "awrun.dispatcher", "--once"]]
    gh = ["gh", "workflow", "run", args.workflow, "--ref", args.ref]
    for k, v in fields.items():
        gh += ["-f", f"{k}={v}"]
    return [gh]


def run_elastic(args) -> int:
    st = read_state()
    if not st:
        print(
            "kvholder elastic: no relay running here; start `adk kvholder phone --via tunnel`",
            file=sys.stderr,
        )
        return 2
    base = st.get("public") or ""
    if not base and not args.print_only:
        print(
            "kvholder elastic: runners reach the relay over the internet; start it with "
            "`adk kvholder phone --via tunnel` (or use --print-only for machines that can reach "
            "this one)",
            file=sys.stderr,
        )
        return 2
    relay = relay_ws_url(base or st.get("lan") or f"http://<this-host>:{st['web_port']}")
    ttl = max(3600.0, args.minutes * 60.0)  # a queued runner may start late; the token dies on use
    rc = 0
    for i in range(args.count):
        try:
            join = mint_join_remote(st, ttl) if not args.dry_run else "j-DRYRUN"
        except OSError as e:
            print(f"kvholder elastic: the relay did not mint a token ({e})", file=sys.stderr)
            return 1
        if args.print_only:
            print(f"adk kvholder serve --connect {relay} --token {join} --max-mb {args.max_mb}")
            continue
        for argv in launch_argv(args, relay, join):
            shown = " ".join(
                a if not a.startswith(("join=", "j-")) else a.split("=")[0] + "=***" for a in argv
            )
            if args.dry_run:
                print(f"[{i + 1}/{args.count}] would run: {shown}")
                continue
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=180)
            out = (r.stdout + r.stderr).strip().splitlines()
            print(
                f"[{i + 1}/{args.count}] {shown.split(' --field')[0]} -> exit {r.returncode}"
                + (f": {out[-1]}" if out else "")
            )
            rc = rc or r.returncode
    return rc


def run_relay_status(args) -> int:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.web_port}/status", timeout=5) as r:
            st = json.loads(r.read())
    except OSError as e:
        print(f"kvholder: no relay on :{args.web_port} ({e})", file=sys.stderr)
        return 1
    print(json.dumps(st))
    return 0 if st.get("attached") else 1
