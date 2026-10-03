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
from dataclasses import dataclass

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


class Relay:
    """Engine-side PATN over TCP  <->  one dialed-in holder over WebSocket."""

    def __init__(self, token: str):
        self.token = token
        self.lock = threading.Lock()  # one PATN call in flight, as the protocol requires
        self.holder: _Attached | None = None
        self.calls = 0
        self.stop = threading.Event()

    def check_token(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.token.encode())

    def attach(self, ws: WebSocket, device: str) -> None:
        with self.lock:
            if self.holder:
                self.holder.ws.close()
            self.holder = _Attached(ws, device, time.time())
        log.info("kvholder relay: holder attached: %s", device)
        print(f"kvholder: holder attached: {device}", flush=True)

    def detach(self, ws: WebSocket | None = None) -> None:
        if self.holder and (ws is None or self.holder.ws is ws):
            print(f"kvholder: holder gone: {self.holder.device}", flush=True)
            self.holder = None

    def call(self, msg: bytes, expect_reply: bool = True) -> bytes:
        """Forward one PATN message; returns the reply message (header + payload)."""
        with self.lock:
            h = self.holder
            if h is None:
                return _err("no holder attached")
            try:
                h.ws.sock.settimeout(CALL_TIMEOUT_S)
                h.ws.send(msg)
                if not expect_reply:
                    return b""
                op, reply = h.ws.recv()
                if op != OP_BIN or len(reply) < kv.HDR.size:
                    raise ConnectionError("holder sent a non-PATN reply")
                self.calls += 1
                return reply
            except (OSError, ConnectionError) as e:
                self.detach(h.ws)
                return _err(f"holder lost: {e}")
            finally:
                if self.holder is h:
                    h.ws.sock.settimeout(None)

    def keepalive(self) -> None:
        while not self.stop.wait(KEEPALIVE_S):
            if not self.lock.acquire(timeout=0.1):
                continue
            try:
                if self.holder:
                    self.holder.ws.send(b"ka", OP_PING)
            except OSError:
                self.detach()
            finally:
                self.lock.release()

    def status(self) -> dict:
        h = self.holder
        return {
            "attached": bool(h),
            "device": h.device if h else None,
            "since": h.since if h else None,
            "calls": self.calls,
        }


def _err(text: str) -> bytes:
    b = text.encode()
    return kv.HDR.pack(kv.MAGIC, kv.ERR, len(b)) + b


class _EngineHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        relay: Relay = self.server.relay  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        kv._tune(sock)
        try:
            while True:
                head = kv._recv_all(sock, kv.HDR.size)
                magic, mtype, n = kv.HDR.unpack(head)
                if magic != kv.MAGIC or n > (1 << 31):
                    return
                payload = kv._recv_all(sock, n) if n else b""
                if mtype == kv.BYE:
                    relay.call(head + payload, expect_reply=False)
                    return
                sock.sendall(relay.call(head + payload))
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
        if route in ("/", "/index.html"):
            from adk.kvholder_page import PAGE_HTML

            return self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
        if route == "/holder.js":
            from adk.kvholder_page import HOLDER_JS

            return self._send(200, "text/javascript; charset=utf-8", HOLDER_JS.encode())
        return self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(code, "")
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
        if hello.get("hello") != "kvholder" or not relay.check_token(str(hello.get("token", ""))):
            ws.send(json.dumps({"ok": False, "error": "bad token"}))
            ws.close()
            return
        device = str(hello.get("device", "holder"))[:80]
        sock.settimeout(None)
        ws.send(json.dumps({"ok": True}))
        relay.attach(ws, device)
        # This thread now only parks: the relay drives the socket under its lock.
        while self.server.relay.holder and self.server.relay.holder.ws is ws:  # type: ignore[attr-defined]
            time.sleep(0.5)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, relay: Relay):
        super().__init__(addr, handler)
        self.relay = relay


def start_relay(
    token: str,
    engine_addr: tuple[str, int] = ("127.0.0.1", kv.DEFAULT_PORT),
    ws_addr: tuple[str, int] = ("127.0.0.1", DEFAULT_WS_PORT),
) -> tuple[Relay, list[_Server]]:
    relay = Relay(token)
    servers = [_Server(engine_addr, _EngineHandler, relay), _Server(ws_addr, _HTTPHandler, relay)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    threading.Thread(target=relay.keepalive, daemon=True).start()
    return relay, servers


def stop_relay(relay: Relay, servers: list[_Server]) -> None:
    relay.stop.set()
    if relay.holder:
        relay.holder.ws.close()
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
            ws.send(json.dumps({"hello": "kvholder", "token": token, "device": holder.device}))
            op, data = ws.recv()
            ack = json.loads(data.decode()) if op == OP_TEXT else {}
            if not ack.get("ok"):
                print(f"kvholder: relay refused: {ack.get('error', 'no reason')}", file=sys.stderr)
                return 1
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
    subprocess.run(
        base + ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{url}'"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    return ""


def tunnel_up(port: int, wait_s: float = 90.0) -> tuple[str, subprocess.Popen | None]:
    """A public https URL for the relay's web port via awtunnel, and the process holding it open.

    ``awtunnel up`` stays in the foreground for as long as the tunnel lives, so it is started,
    read until it prints the URL, and left running; ('', None) when awtunnel is absent or silent.
    """
    exe = shutil.which("awtunnel")
    cmd = ([exe] if exe else [sys.executable, "-m", "awtunnel"]) + ["up", "--port", str(port)]
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
                if word.startswith("https://") and not found:
                    found.append(word.rstrip(".,)"))

    threading.Thread(target=_read, daemon=True).start()
    end = time.time() + wait_s
    while time.time() < end and not found and proc.poll() is None:
        time.sleep(0.2)
    if not found:
        proc.terminate()
        return "", None
    return found[0], proc


def holder_url(base: str, token: str) -> str:
    """The page URL a phone opens. The token rides the #fragment: never sent to any server log."""
    return f"{base.rstrip('/')}/#t={token}"


# ---------------------------------------------------------------- CLI


def register(s) -> None:
    ph = s.add_parser(
        "phone", help="Use a phone (or any browser) as the holder: USB, LAN, mesh or tunnel"
    )
    ph.add_argument(
        "--via",
        choices=["usb", "lan", "tunnel", "local"],
        default="usb",
        help="usb: adb reverse + open the page on the phone (default); lan: bind every "
        "interface (mesh too); tunnel: public https via awtunnel; local: this machine",
    )
    ph.add_argument(
        "--port", type=int, default=kv.DEFAULT_PORT, help="engine-side PATN port (loopback)"
    )
    ph.add_argument("--web-port", type=int, default=DEFAULT_WS_PORT, help="page + WebSocket port")
    ph.add_argument("--host", default="", help="address to bind/advertise for --via lan (mesh IP)")
    ph.add_argument("--token", default="", help="reuse a token (default: a fresh one)")
    ph.add_argument("--serial", default="", help="adb device serial when several are plugged in")
    st = s.add_parser("relay-status", help="Is a holder attached to the local relay")
    st.add_argument("--web-port", type=int, default=DEFAULT_WS_PORT)


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
    tproc = None
    print(f"kvholder: engine port 127.0.0.1:{args.port} (point LLAMA_KV_REMOTE here)")
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
        print(f"phone URL: {holder_url(f'http://{adv}:{args.web_port}', token)}")
        print(
            "  plain http on a LAN IP is not a secure context: the page runs the CPU holder."
            " Use --via usb or --via tunnel for WebGPU."
        )
    elif args.via == "tunnel":
        pub, tproc = tunnel_up(args.web_port)
        if not pub:
            print(
                "kvholder: awtunnel gave no URL (pip install awtunnel; awtunnel up --port "
                f"{args.web_port}); the relay is still up locally",
                file=sys.stderr,
            )
        else:
            print(f"phone URL: {holder_url(pub, token)}")
    else:
        print(f"page URL: {holder_url(f'http://localhost:{args.web_port}', token)}")
    print(
        "python holder: adk kvholder serve --connect "
        f"ws://<this-host>:{args.web_port}/holder --token {token}"
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("kvholder: relay stopped")
    finally:
        stop_relay(relay, servers)
        if tproc is not None:
            tproc.terminate()
    return 0


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
