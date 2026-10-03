"""Find a KV relay on the mesh and join it without copying a URL or a token.

The relay owner opens a **door** (``adk kvholder phone --via lan --mesh``). A holder on
another machine runs ``adk kvholder serve --mesh``: it finds relays, asks one to let it in,
and waits. The owner approves the request by its short code (``adk kvholder mesh approve
CODE``, the awsh tool or the gateway tool). A ``--mesh-admit`` network admits at once only a
holder that signed in as a workspace device the owner lets lend (``Door._device_ok``); any
other device on that network still waits for the code.
The relay then hands that holder a **join token**: single-use, short-lived, minted on the
engine host. The master token never leaves the engine host.

Where relays are looked for, in order (every source is best-effort, none is required):

- ``--peer HOST`` and ``AITHER_KVHOLDER_PEERS`` (comma-separated hosts),
- this machine (``127.0.0.1``),
- a UDP query to the LAN broadcast address (port :data:`DISCOVERY_PORT`),
- the online peers of the mesh overlay: ``tailscale status --json`` (the platform's
  headscale tailnet) and ``wg show all allowed-ips`` (plain WireGuard peers).

A broadcast never crosses a WireGuard overlay, which is why the overlay's own peer list
is asked as well. Each candidate is probed with ``GET /mesh`` on the relay's page port.

What the door exposes, and to whom:

======================  ===============================  =====================================
route                   caller                           does
======================  ===============================  =====================================
``GET /mesh``           anyone who reaches the port      says a relay is here (name, holders)
``GET /mesh/request``   anyone who reaches the port      files a pending request (capped)
``GET /mesh/claim``     the holder that filed it         collects the join token once approved
``GET /mesh/pending``   loopback + the master token      lists pending requests
``GET /mesh/approve``   loopback + the master token      approves one (mints its join token)
``GET /mesh/deny``      loopback + the master token      refuses one
======================  ===============================  =====================================

A request carries ``sha256(claim)``; only the process that knows ``claim`` collects the
token, and only once. Without ``--mesh`` none of these routes exist (404). The join token
travels over the transport the holder used to reach the relay: WireGuard-encrypted on the
overlay, plain http on a LAN (the same exposure as ``--via lan`` today).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DISCOVERY_PORT = 50064  # UDP: "is a relay here?" (the page port is 50063)
DEFAULT_WEB_PORT = 50063
PENDING_MAX = 32  # open requests a door holds; more is refused (429)
PENDING_TTL_S = 300.0  # an unanswered request expires
JOIN_TTL_S = 600.0  # an approved join token must be used within this
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
_QUERY = b'{"kvholder":"discover","v":1}'
_LOOPBACK = ("127.0.0.1", "::1")


def _now() -> float:
    return time.time()


def _code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def _clean(text: str, limit: int = 80) -> str:
    return "".join(c for c in str(text) if c.isprintable())[:limit] or "holder"


# ---------------------------------------------------------------- the relay's side: the door


class Door:
    """Pending mesh requests for one relay, its auto-admit networks and its UDP responder."""

    def __init__(self, relay, web_port: int, admit: list[str] | None = None, name: str = ""):
        self.relay = relay
        self.web_port = web_port
        self.name = _clean(name or socket.gethostname(), 64)
        self.admit = [ipaddress.ip_network(a, strict=False) for a in (admit or [])]
        self.requests: dict[str, dict] = {}  # id -> request
        self.lock = threading.Lock()
        self.udp: socket.socket | None = None
        self.stop = threading.Event()

    # -- UDP: answer "is a relay here?" so a LAN holder needs no address
    def listen_udp(self, port: int = DISCOVERY_PORT, host: str = "0.0.0.0") -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            print(
                f"kvholder mesh: no UDP discovery on :{port} ({e}); peers can still probe",
                file=sys.stderr,
            )
            s.close()
            return False
        s.settimeout(1.0)
        self.udp = s
        threading.Thread(target=self._udp_loop, daemon=True).start()
        return True

    def _udp_loop(self) -> None:
        reply = json.dumps(self.hello()).encode()
        while not self.stop.is_set() and self.udp is not None:
            try:
                data, peer = self.udp.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            if data.strip() == _QUERY:
                try:
                    self.udp.sendto(reply, peer)
                except OSError:
                    continue

    def close(self) -> None:
        self.stop.set()
        if self.udp is not None:
            self.udp.close()
            self.udp = None

    def hello(self) -> dict:
        return {"kvholder": "relay", "v": 1, "name": self.name, "web_port": self.web_port}

    def _expire(self) -> None:
        now = _now()
        for rid, r in list(self.requests.items()):
            if r["until"] <= now:
                del self.requests[rid]

    def _admitted(self, peer: str) -> bool:
        try:
            ip = ipaddress.ip_address(peer.split("%")[0])
        except ValueError:
            return False
        return any(ip in net for net in self.admit if net.version == ip.version)

    def _device_ok(self, signed: dict | None) -> bool:
        """An auto-admit network admits a holder only when it signed in as a workspace device
        the owner lets lend (adk.kvholder_workspace: local allow, or the household registry's
        kv_lend; a child's phone is refused unless the owner switched it on). A network alone
        never admits: an unknown device on it waits for the owner's code like any other."""
        gate = getattr(self.relay, "device_gate", None)
        if gate is None or not isinstance(signed, dict):
            return False
        did, _why = gate.admit(signed)
        return bool(did)

    def request(
        self, peer: str, device: str, claim_hash: str, signed: dict | None = None
    ) -> tuple[int, dict]:
        if len(claim_hash) != 64 or any(c not in "0123456789abcdef" for c in claim_hash):
            return 400, {"error": "x-kv-claim-hash must be sha256 hex"}
        with self.lock:
            self._expire()
            if len(self.requests) >= PENDING_MAX:
                return 429, {"error": "too many pending requests"}
            codes = {r["code"] for r in self.requests.values()}
            code = _code()
            while code in codes:
                code = _code()
            rid = secrets.token_urlsafe(12)
            r = {
                "id": rid,
                "code": code,
                "device": _clean(device),
                "peer": peer,
                "hash": claim_hash,
                "at": _now(),
                "until": _now() + PENDING_TTL_S,
                "status": "pending",
                "token": "",
            }
            self.requests[rid] = r
            if self._admitted(peer) and self._device_ok(signed):
                self._approve(r, by="admit")
        return 200, {"id": rid, "code": code, "status": r["status"]}

    def _approve(self, r: dict, by: str) -> None:
        tok, exp = self.relay.mint_join(JOIN_TTL_S)
        r.update(status="approved", token=tok, until=exp, by=by)

    def claim(self, rid: str, claim: str) -> tuple[int, dict]:
        with self.lock:
            self._expire()
            r = self.requests.get(rid)
            if r is None:
                return 404, {"status": "unknown"}
            digest = hashlib.sha256(claim.encode()).hexdigest()
            if not hmac.compare_digest(digest, r["hash"]):
                return 403, {"status": "forbidden"}
            if r["status"] == "denied":
                del self.requests[rid]
                return 403, {"status": "denied"}
            if r["status"] != "approved":
                return 202, {"status": "pending", "code": r["code"]}
            del self.requests[rid]  # one claim per request
            return 200, {"status": "approved", "token": r["token"], "expires": r["until"]}

    def pending(self) -> list[dict]:
        with self.lock:
            self._expire()
            now = _now()
            return [
                {
                    "code": r["code"],
                    "device": r["device"],
                    "peer": r["peer"],
                    "status": r["status"],
                    "age_s": round(now - r["at"], 1),
                }
                for r in self.requests.values()
            ]

    def decide(self, code: str, approve: bool) -> tuple[int, dict]:
        code = code.strip().upper().replace("-", "")
        with self.lock:
            self._expire()
            for r in self.requests.values():
                if r["code"] == code and r["status"] == "pending":
                    if approve:
                        self._approve(r, by="owner")
                    else:
                        r["status"] = "denied"
                    return 200, {
                        "ok": True,
                        "code": code,
                        "device": r["device"],
                        "status": r["status"],
                    }
        return 404, {"ok": False, "error": f"no pending request {code}"}


def open_door(relay, args) -> Door:
    """Attach a door to ``relay`` (``adk kvholder phone --mesh``) and answer UDP queries."""
    door = Door(relay, args.web_port, list(getattr(args, "mesh_admit", None) or []))
    relay.mesh = door
    door.listen_udp(getattr(args, "mesh_udp_port", DISCOVERY_PORT))
    if args.via != "lan":
        print(
            "kvholder mesh: the page port listens on 127.0.0.1 only; holders on other "
            "machines cannot find it (use --via lan)",
            file=sys.stderr,
        )
    nets = ", ".join(str(n) for n in door.admit) or "none: approve each by code"
    print(f"kvholder mesh: door open as {door.name!r}; auto-admit {nets}")
    print("  approve a holder: adk kvholder mesh approve CODE")
    return door


def handle_http(handler, path: str, hdrs: dict, relay) -> None:
    """The ``/mesh*`` routes, called from the relay's HTTP handler."""
    door: Door | None = getattr(relay, "mesh", None)
    if door is None:
        return handler._send(404, "text/plain", b"not found")
    u = urllib.parse.urlsplit(path)
    q = urllib.parse.parse_qs(u.query)
    peer = handler.client_address[0]

    def reply(code: int, doc) -> None:
        handler._send(code, "application/json", json.dumps(doc).encode())

    route = u.path.rstrip("/")
    if route == "/mesh":
        return reply(200, {**door.hello(), "holders": len(relay.holders)})
    if route == "/mesh/request":
        try:
            signed = json.loads(hdrs.get("x-kv-device-auth", "") or "null")
        except ValueError:
            signed = None
        code, doc = door.request(
            peer, hdrs.get("x-kv-device", ""), hdrs.get("x-kv-claim-hash", "").lower(), signed
        )
        return reply(code, doc)
    if route == "/mesh/claim":
        code, doc = door.claim(q.get("id", [""])[0], hdrs.get("x-kv-claim", ""))
        return reply(code, doc)
    owner = peer in _LOOPBACK and relay.admit(hdrs.get("x-kv-token", "")) == "master"
    if route in ("/mesh/pending", "/mesh/approve", "/mesh/deny") and not owner:
        return reply(403, {"error": "forbidden"})
    if route == "/mesh/pending":
        return reply(200, {"pending": door.pending(), "name": door.name})
    if route in ("/mesh/approve", "/mesh/deny"):
        code, doc = door.decide(q.get("code", [""])[0], route == "/mesh/approve")
        return reply(code, doc)
    return reply(404, {"error": "not found"})


# ---------------------------------------------------------------- the owner's side (local)


def _state() -> dict:
    from adk.kvholder_net import read_state

    st = read_state()
    if not st:
        raise RuntimeError("no relay running here (start: adk kvholder phone --via lan --mesh)")
    return st


def _owner_get(route: str, st: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
    st = st or _state()
    req = urllib.request.Request(
        f"http://127.0.0.1:{st['web_port']}{route}", headers={"X-KV-Token": st["token"]}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": f"http {e.code}"}


def status() -> dict:
    """The local relay as an owner sees it: ports, holders, the door, no secret."""
    st = _state()
    with urllib.request.urlopen(f"http://127.0.0.1:{st['web_port']}/status", timeout=5) as r:
        out = {"relay": json.loads(r.read())}
    out.update(
        web_port=st["web_port"],
        engine_port=st.get("engine_port"),
        public=st.get("public", ""),
        lan=st.get("lan", ""),
    )
    code, doc = _owner_get("/mesh/pending", st)
    out["mesh"] = doc if code == 200 else {"open": False}
    return out


def pending() -> list[dict]:
    code, doc = _owner_get("/mesh/pending")
    if code != 200:
        raise RuntimeError(doc.get("error") or f"http {code}")
    return doc["pending"]


def decide(code: str, approve: bool = True) -> dict:
    route = "/mesh/approve" if approve else "/mesh/deny"
    _, doc = _owner_get(f"{route}?code={urllib.parse.quote(code)}")
    return doc


# ---------------------------------------------------------------- the holder's side


def _tailnet_peers() -> list[str]:
    exe = shutil.which("tailscale")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=10
        )
        doc = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    hosts = []
    for p in (doc.get("Peer") or {}).values():
        if p.get("Online"):
            hosts += [ip for ip in p.get("TailscaleIPs") or [] if ":" not in ip][:1]
    return hosts


def _wireguard_peers() -> list[str]:
    exe = shutil.which("wg")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "show", "all", "allowed-ips"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    hosts = []
    for line in out.stdout.splitlines():
        for cidr in line.split()[2:]:
            if cidr.endswith("/32"):
                hosts.append(cidr[:-3])
    return hosts


def _udp_query(
    port: int = DISCOVERY_PORT,
    wait_s: float = 1.5,
    targets: tuple[str, ...] = ("255.255.255.255", "127.0.0.1"),
) -> list[tuple]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.2)
    found = []
    try:
        for t in targets:
            try:
                s.sendto(_QUERY, (t, port))
            except OSError:
                continue
        end = _now() + wait_s
        while _now() < end:
            try:
                data, (addr, _) = s.recvfrom(512)
            except (socket.timeout, ConnectionResetError):
                continue  # Windows reports an ICMP port-unreachable as a reset: keep listening
            except OSError:
                break
            try:
                doc = json.loads(data)
            except ValueError:
                continue
            if doc.get("kvholder") == "relay" and isinstance(doc.get("web_port"), int):
                found.append((addr, doc["web_port"]))
    finally:
        s.close()
    return found


def probe(host: str, port: int = DEFAULT_WEB_PORT, timeout: float = 1.5) -> dict | None:
    """``GET /mesh`` on one candidate; the relay's hello, or None."""
    h = f"[{host}]" if ":" in host and not host.startswith("[") else host
    base = f"http://{h}:{port}"
    try:
        with urllib.request.urlopen(f"{base}/mesh", timeout=timeout) as r:
            doc = json.loads(r.read())
    except (OSError, ValueError):
        return None
    if doc.get("kvholder") != "relay":
        return None
    return {**doc, "base": base, "host": host}


def candidates(
    peers: list[str] | None = None,
    port: int = DEFAULT_WEB_PORT,
    overlay: bool = True,
    udp: bool = True,
) -> list[tuple[str, int, str]]:
    """``(host, port, source)`` to probe, explicit peers first, no duplicates."""
    out: list[tuple[str, int, str]] = []
    env = [p.strip() for p in os.environ.get("AITHER_KVHOLDER_PEERS", "").split(",")]
    for p in list(peers or []) + env:
        if p:
            host, _, pp = p.rpartition(":") if p.count(":") == 1 else (p, "", "")
            out.append((host or p, int(pp) if pp.isdigit() else port, "peer"))
    out.append(("127.0.0.1", port, "local"))
    if udp:
        out += [(a, p, "lan") for a, p in _udp_query()]
    if overlay:
        out += [(h, port, "tailnet") for h in _tailnet_peers()]
        out += [(h, port, "wireguard") for h in _wireguard_peers()]
    seen, uniq = set(), []
    for c in out:
        if (c[0], c[1]) not in seen:
            seen.add((c[0], c[1]))
            uniq.append(c)
    return uniq


def discover(
    peers: list[str] | None = None,
    port: int = DEFAULT_WEB_PORT,
    overlay: bool = True,
    udp: bool = True,
    timeout: float = 1.5,
) -> list[dict]:
    """Every relay with an open door that answers, in candidate order."""
    cands = candidates(peers, port, overlay, udp)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(cands) or 1)) as ex:
        hellos = list(ex.map(lambda c: probe(c[0], c[1], timeout), cands))
    return [{**h, "source": c[2]} for c, h in zip(cands, hellos) if h]


def request_join(
    base: str, device: str, wait_s: float = 600.0, poll_s: float = 2.0, on_code=None, sign=None
) -> str:
    """File a request at ``base`` and wait for the owner's answer. Returns the join token.

    ``sign``: the signed device fields (adk.kvholder_workspace.device_hello); only a signed,
    owner-allowed device is admitted by the relay's network policy without a code."""
    claim = secrets.token_urlsafe(24)
    headers = {
        "X-KV-Device": _clean(device),
        "X-KV-Claim-Hash": hashlib.sha256(claim.encode()).hexdigest(),
    }
    if sign is not None:
        headers["X-KV-Device-Auth"] = json.dumps(sign())
    req = urllib.request.Request(f"{base}/mesh/request", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        doc = json.loads(r.read())
    rid = doc["id"]
    if on_code is not None:
        on_code(doc["code"], doc["status"])
    end = _now() + wait_s
    while True:
        req = urllib.request.Request(
            f"{base}/mesh/claim?id={urllib.parse.quote(rid)}", headers={"X-KV-Claim": claim}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code, doc = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                code, doc = e.code, json.loads(e.read() or b"{}")
            except ValueError:
                code, doc = e.code, {}
        if code == 200 and doc.get("token"):
            return str(doc["token"])
        if code in (403, 404):
            raise PermissionError(f"the relay {doc.get('status') or 'refused'} the request")
        if _now() >= end:
            raise TimeoutError("nobody approved the request in time")
        time.sleep(poll_s)


def ws_url(base: str) -> str:
    u = urllib.parse.urlsplit(base)
    return f"{'wss' if u.scheme == 'https' else 'ws'}://{u.netloc}/holder"


def resolve_serve(args) -> int:
    """``adk kvholder serve --mesh``: find a relay, get let in, set --connect and --token."""
    relays = discover(
        getattr(args, "peer", None) or [], getattr(args, "mesh_port", 0) or DEFAULT_WEB_PORT
    )
    want = getattr(args, "relay_name", "") or ""
    if want:
        relays = [r for r in relays if want in (r["name"], r["host"])]
    if not relays:
        print(
            "kvholder mesh: no relay found (on the engine host: adk kvholder phone --via lan "
            "--mesh; or pass --peer HOST)",
            file=sys.stderr,
        )
        return 1
    r = relays[0]
    others = f" (+{len(relays) - 1} more; --relay-name picks)" if len(relays) > 1 else ""
    print(f"kvholder mesh: relay {r['name']!r} at {r['base']} via {r['source']}{others}")

    def shown(code: str, state: str) -> None:
        if state == "approved":
            print("kvholder mesh: admitted by the relay's network policy")
        else:
            print(
                f"kvholder mesh: waiting for approval, code {code}. On the relay host: "
                f"adk kvholder mesh approve {code}",
                flush=True,
            )

    try:
        token = request_join(
            r["base"],
            f"adk-kvholder@{socket.gethostname()}",
            wait_s=float(getattr(args, "mesh_wait", 600) or 600),
            on_code=shown,
        )
    except (OSError, PermissionError, TimeoutError, ValueError, KeyError) as e:
        print(f"kvholder mesh: not admitted ({e})", file=sys.stderr)
        return 1
    args.connect, args.token = ws_url(r["base"]), token
    return 0


# ---------------------------------------------------------------- `adk kvholder mesh ...`


def register(sub) -> None:
    """The ``mesh`` verb under ``adk kvholder``."""
    p = sub.add_parser("mesh", help="Holders found on the mesh: list, approve or deny requests")
    s = p.add_subparsers(dest="mesh_action")
    s.add_parser("pending", help="requests waiting at the local relay's door")
    for verb in ("approve", "deny"):
        v = s.add_parser(verb, help=f"{verb} a request by its code")
        v.add_argument("code")
    d = s.add_parser("discover", help="list relays this machine can find")
    d.add_argument("--peer", action="append", default=[], help="also probe HOST[:PORT]")
    d.add_argument("--no-overlay", action="store_true", help="skip tailscale / wg peers")
    s.add_parser("status", help="the local relay, its holders and its door")


def run(args) -> int:
    action = getattr(args, "mesh_action", None)
    try:
        if action == "discover":
            for r in discover(args.peer, overlay=not args.no_overlay):
                print(json.dumps({k: r[k] for k in ("name", "base", "source", "holders")}))
            return 0
        if action == "pending":
            rows = pending()
            for r in rows:
                print(
                    f"{r['code']}  {r['status']:<8} {r['device']}  from {r['peer']}  "
                    f"{r['age_s']:.0f}s ago"
                )
            if not rows:
                print("no pending requests")
            return 0
        if action in ("approve", "deny"):
            doc = decide(args.code, action == "approve")
            print(json.dumps(doc))
            return 0 if doc.get("ok") else 1
        if action == "status":
            print(json.dumps(status()))
            return 0
    except (OSError, RuntimeError) as e:
        print(f"kvholder mesh: {e}", file=sys.stderr)
        return 1
    print("usage: adk kvholder mesh {pending,approve,deny,discover,status}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- tools (awsh, the daemon)


def elastic(
    count: int = 1,
    minutes: int = 30,
    max_mb: int = 4096,
    workflow: str = "kvholder-runner.yml",
    ref: str = "develop",
    priority: int = 5,
    dry_run: bool = True,
) -> dict:
    """``adk kvholder elastic`` for a tool caller: launches (or dry-runs), never returns tokens."""
    import argparse

    from adk import kvholder_net as net

    count = max(1, min(int(count), 16))
    st = _state()
    base = st.get("public") or ""
    if not base and not dry_run:
        return {
            "ok": False,
            "error": "runners need a public relay: adk kvholder phone --via tunnel",
        }
    relay = net.relay_ws_url(base or st.get("lan") or f"http://127.0.0.1:{st['web_port']}")
    a = argparse.Namespace(
        minutes=int(minutes), max_mb=int(max_mb), workflow=workflow, ref=ref, priority=int(priority)
    )
    runs = []
    for i in range(count):
        join = "j-DRYRUN" if dry_run else net.mint_join_remote(st, max(3600.0, minutes * 60.0))
        for argv in net.launch_argv(a, relay, join):
            shown = [x.split("=")[0] + "=***" if x.startswith(("join=", "j-")) else x for x in argv]
            if dry_run:
                runs.append({"holder": i + 1, "would_run": shown})
                continue
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=180)
            tail = (r.stdout + r.stderr).strip().splitlines()[-1:]
            runs.append(
                {
                    "holder": i + 1,
                    "ran": shown[:4],
                    "exit": r.returncode,
                    "last": tail[0].replace(join, "***") if tail else "",
                }
            )
    return {
        "ok": all(r.get("exit", 0) == 0 for r in runs),
        "relay": relay,
        "dry_run": dry_run,
        "runs": runs,
    }
