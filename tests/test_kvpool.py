"""Shared KV pool: sessions attach to prefix blocks already on holders, exactly."""

from __future__ import annotations

import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from adk import kvholder as kv
from adk import kvholder_net as net
from adk import kvpool as pool

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only

H, LAYERS, BT = 2, 2, pool.BLOCK_TOKENS
PREFIX = 4096  # 16 blocks
F32_KEY = H * (kv.HD + kv.HD) * 4  # holder bytes per key per layer (f32 store)
FP = pool.model_fingerprint("test-model", "q-f16")


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Counting:
    """Wraps a client socket: bytes sent per PATN message type (one sendall per message)."""

    def __init__(self, sock):
        self._s = sock
        self.by_type: dict[int, int] = {}

    def sendall(self, data):
        _, mtype, _ = kv.HDR.unpack_from(data)
        self.by_type[mtype] = self.by_type.get(mtype, 0) + len(data)
        return self._s.sendall(data)

    def __getattr__(self, name):
        return getattr(self._s, name)


def _ref(q, keys, vals, scale):
    s = np.einsum("hrd,nhd->hrn", q.astype(np.float64) * scale, keys.astype(np.float64))
    p = np.exp(s - s.max(-1, keepdims=True))
    return np.einsum("hrn,nhd->hrd", p / p.sum(-1, keepdims=True), vals.astype(np.float64))


def _kv(seed, n):
    """Deterministic K/V for positions of a token stream (f16-exact, as the wire carries)."""
    rng = np.random.default_rng(seed)
    shape = (LAYERS, n, H, kv.HD)
    k = rng.standard_normal(shape).astype(np.float16).astype(np.float32)
    v = rng.standard_normal(shape).astype(np.float16).astype(np.float32)
    return k, v


PK, PV = _kv(1, PREFIX)  # the shared prefix's keys: what any session computes for it
TOKENS = list(range(1000, 1000 + PREFIX))
HASHES = pool.chain_hashes(TOKENS, FP)


class Engine:
    """One engine session: the keys it has handed to the holder(s), checked against numpy."""

    def __init__(self, port, sid=None, kv_type="f16"):
        self.c = kv.KVHolderClient("127.0.0.1", port, timeout=60)
        self.c.sock = self.w = _Counting(self.c.sock)
        self.pooled = pool.open_session(self.c, sid) if sid is not None else False
        self.c.configure(kv.Config.for_model(LAYERS, H, kv_type))
        self.k = [np.zeros((0, H, kv.HD), np.float32) for _ in range(LAYERS)]
        self.v = [np.zeros((0, H, kv.HD), np.float32) for _ in range(LAYERS)]
        self.rng = np.random.default_rng(7)

    def start_with_prefix(self, publish=True):
        """Attach what is resident, append the rest, offer it for sharing."""
        got = pool.attach(self.c, FP, HASHES)
        for layer in range(LAYERS):
            self.k[layer], self.v[layer] = PK[layer, :got], PV[layer, :got]
        self.push(PK[:, got:], PV[:, got:])
        if publish:
            pool.publish(self.c, FP, HASHES)
        return got

    def push(self, k, v, step=512):
        for layer in range(LAYERS):
            base = len(self.k[layer])
            for s in range(0, k.shape[1], step):
                n = self.c.append(layer, base + s, k[layer, s : s + step], v[layer, s : s + step])
                assert n == base + min(s + step, k.shape[1])
            self.k[layer] = np.concatenate([self.k[layer], k[layer]])
            self.v[layer] = np.concatenate([self.v[layer], v[layer]])

    def truncate(self, n):
        self.c.truncate(n)
        self.k = [x[:n] for x in self.k]
        self.v = [x[:n] for x in self.v]

    def check(self, n_tok=8):
        for layer in range(LAYERS):
            q = self.rng.standard_normal((H, kv.NR, kv.HD)).astype(np.float32)
            o, lse, meta = self.c.attn(layer, q, kv.HD**-0.5, n_tok=n_tok)
            assert meta["nk"] == len(self.k[layer])
            ref = _ref(q, self.k[layer], self.v[layer], kv.HD**-0.5)
            rows = [i for i in range(kv.NR) if i % 8 < n_tok]
            np.testing.assert_allclose(o[:, rows], ref[:, rows], atol=3e-3)

    @property
    def append_bytes(self) -> int:
        return self.w.by_type.get(kv.APPEND, 0)


@pytest.fixture()
def server():
    servers = []

    def make(max_keys=4 * PREFIX, store="f32"):
        h = kv.KVHolder(max_keys * F32_KEY * LAYERS, device="pool-test", store=store)
        srv = kv.HolderServer(("127.0.0.1", 0), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return h, srv.server_address[1]

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


# ---------------------------------------------------------------- keys and wire format


def test_chain_hashes_name_the_whole_prefix():
    a = pool.chain_hashes(TOKENS, FP)
    assert len(a) == PREFIX // BT and a == HASHES
    b = pool.chain_hashes(TOKENS[:-1] + [7], FP)  # last token differs: only the last block
    assert b[:-1] == a[:-1] and b[-1] != a[-1]
    c = pool.chain_hashes([7] + TOKENS[1:], FP)  # first token differs: every block differs
    assert not set(c) & set(a)
    assert pool.chain_hashes(TOKENS, pool.model_fingerprint("other"))[0] != a[0]
    assert pool.chain_hashes(TOKENS[: BT - 1], FP) == []  # a partial block is not shareable
    fp, bt, hs = pool.unpack_pool_req(pool.pack_pool_req(FP, BT, a))
    assert (fp, bt, hs) == (FP, BT, a)


# ---------------------------------------------------------------- one holder, two sessions


def test_second_session_appends_zero_prefix_bytes_and_both_stay_exact(server):
    h, port = server()
    a = Engine(port, sid=1)
    assert a.pooled and a.start_with_prefix() == 0
    a_prefix_bytes = a.append_bytes
    assert a_prefix_bytes >= PREFIX * LAYERS * 2 * H * kv.HD * 2  # K+V f16 rows, every layer

    b = Engine(port, sid=2)
    assert b.start_with_prefix() == PREFIX  # all 16 blocks were resident
    assert b.append_bytes == 0  # measured on the wire: not one prefix byte
    st = pool.status(b.c)
    assert st["pool"]["blocks"] == 16 and st["pool"]["referenced_blocks"] == 16
    assert st["pool"]["attached_tokens"] == PREFIX
    # one copy of the prefix on the holder, not two
    assert st["used_bytes"] == PREFIX * F32_KEY * LAYERS

    # diverge: each session's own tail lands in its private range
    ak, av = _kv(11, 300)
    bk, bv = _kv(22, 200)
    a.push(ak, av)
    b.push(bk, bv)
    assert b.append_bytes < a_prefix_bytes / 10
    a.check()
    b.check(n_tok=3)
    a.check(n_tok=1)


def test_truncate_into_the_shared_prefix_is_copy_on_write(server):
    h, port = server()
    a = Engine(port, sid=1)
    a.start_with_prefix()
    b = Engine(port, sid=2)
    assert b.start_with_prefix() == PREFIX
    b.truncate(3000)  # mid block 11: the kept 184 rows of block 11 are copied privately
    nk, nv = _kv(33, 700)
    b.push(nk, nv)
    b.check()
    a.check()  # the shared blocks were never written: A still sees the original prefix
    st = pool.status(b.c)
    mine = next(s for s in st["sessions"] if s["sid"] == 2)
    assert mine["shared_tokens"] == 11 * BT and mine["held"] == 3700
    assert st["pool"]["referenced_blocks"] == 16  # A still holds every block


def test_a_late_session_publishes_only_what_is_new(server):
    h, port = server()
    a = Engine(port, sid=1)
    pool.attach(a.c, FP, HASHES[:8])
    a.push(PK[:, :2048], PV[:, :2048])
    a.k = [PK[layer, :2048] for layer in range(LAYERS)]
    a.v = [PV[layer, :2048] for layer in range(LAYERS)]
    assert pool.publish(a.c, FP, HASHES[:8]) == 8
    b = Engine(port, sid=2)
    assert b.start_with_prefix() == 2048  # half resident: B appends the other half and adds it
    assert pool.status(b.c)["pool"]["blocks"] == 16
    c = Engine(port, sid=3)
    assert c.start_with_prefix() == PREFIX
    assert c.append_bytes == 0
    for e in (a, b, c):
        e.check()


def test_tq4_sessions_read_the_same_shared_codes(server):
    """tq4 is approximate, but two sessions on one shared prefix see identical bits."""
    h, port = server(store="tq4")
    a, b = Engine(port, sid=1), Engine(port, sid=2)
    a.start_with_prefix()
    assert b.start_with_prefix() == PREFIX
    q = np.random.default_rng(3).standard_normal((H, kv.NR, kv.HD)).astype(np.float32)
    oa, la, _ = a.c.attn(1, q, kv.HD**-0.5)
    ob, lb, _ = b.c.attn(1, q, kv.HD**-0.5)
    assert np.array_equal(oa, ob) and np.array_equal(la, lb)


# ---------------------------------------------------------------- refcounts and eviction


def test_eviction_takes_cold_blocks_never_referenced_ones(server):
    h, port = server(max_keys=PREFIX + 1024)
    a = Engine(port, sid=1)
    a.start_with_prefix()
    a.c.close()  # BYE: A's references go; the blocks stay resident (warm)
    st = pool.status(Engine(port, sid=9).c)
    assert st["pool"]["blocks"] == 16 and st["pool"]["referenced_blocks"] == 0

    warm = Engine(port, sid=2)
    assert warm.start_with_prefix() == PREFIX  # reused after its writer left
    warm.check()

    # now in use: an unrelated session cannot push it out; it is refused cleanly
    other = Engine(port, sid=3)
    ok, ov = _kv(44, 2048)
    with pytest.raises(RuntimeError, match="out of memory"):
        other.push(ok, ov)
    warm.check()  # untouched and exact

    warm.c.close()  # unreferenced again: now the same pressure evicts it, oldest first
    other2 = Engine(port, sid=4)
    other2.push(ok, ov)
    other2.check()
    st = pool.status(other2.c)
    assert st["pool"]["evicted"] == 8 and st["pool"]["referenced_blocks"] == 0
    late = Engine(port, sid=5)
    got = pool.attach(late.c, FP, HASHES)  # the deepest blocks went first: the head survives
    assert got == 8 * BT
    late.k = [PK[layer, :got] for layer in range(LAYERS)]
    late.v = [PV[layer, :got] for layer in range(LAYERS)]
    late.check()


# ---------------------------------------------------------------- backwards compatibility


def test_a_holder_without_the_pool_still_serves_the_engine(server, monkeypatch):
    monkeypatch.setattr(kv, "_pool", None)  # a PATN v3/v4 holder: pool messages are unknown
    h, port = server()
    e = Engine(port, sid=1)
    assert e.pooled is False
    assert e.start_with_prefix() == 0  # attach says 0, publish says 0: the engine appends it all
    assert e.append_bytes > 0
    e.check()
    assert pool.status(e.c) is None


def test_default_session_is_the_plain_protocol(server):
    """No SESSION: one shared state across reconnects, as PATN v3 always was."""
    h, port = server()
    e = Engine(port)
    e.push(PK[:, :300], PV[:, :300])
    e.c.close()
    e2 = Engine.__new__(Engine)
    e2.c = kv.KVHolderClient("127.0.0.1", port)
    e2.c.cfg = kv.Config.for_model(LAYERS, H, "f16")
    e2.k, e2.v, e2.rng = e.k, e.v, np.random.default_rng(5)
    e2.check()
    assert "sessions=" not in e2.c.stats()


# ---------------------------------------------------------------- through the relay


@pytest.fixture()
def relay():
    token = "pool-" + str(time.time_ns())
    ep, wp = _free_port(), _free_port()
    r, servers = net.start_relay(token, ("127.0.0.1", ep), ("127.0.0.1", wp))
    yield r, token, ep, wp
    net.stop_relay(r, servers)


def _add_holder(r, token, wp, name, keys):
    before = len(r.holders)
    h = kv.KVHolder(keys * F32_KEY * LAYERS, device=name, store="f32")
    seen = {"append": 0}
    real = h.handle

    def counted(mtype, p, conn=None):
        if mtype == kv.APPEND:
            seen["append"] += len(p)
        return real(mtype, p, conn)

    h.handle = counted  # relay -> holder bytes, as the holder receives them
    h.seen = seen
    url = f"ws://127.0.0.1:{wp}/holder"
    threading.Thread(target=net.dial_holder, args=(url, token, h), daemon=True).start()
    end = time.time() + 10
    while len(r.holders) <= before and time.time() < end:
        time.sleep(0.05)
    assert len(r.holders) > before, f"{name} never attached"
    return h


def test_relay_two_holders_second_session_ships_no_prefix(relay):
    r, token, ep, wp = relay
    ha = _add_holder(r, token, wp, "a", 3072)  # the prefix spans both holders
    hb = _add_holder(r, token, wp, "b", 3072)
    a = Engine(ep, sid=1)
    assert a.pooled and a.start_with_prefix() == 0
    assert [x["off"] for x in r.status()["holders"]] == [0, 3072]
    wire_a = ha.seen["append"] + hb.seen["append"]
    assert wire_a >= PREFIX * LAYERS * 2 * H * kv.HD * 2

    b = Engine(ep, sid=2)
    assert b.start_with_prefix() == PREFIX
    assert b.append_bytes == 0  # engine -> relay
    assert ha.seen["append"] + hb.seen["append"] == wire_a  # relay -> holders
    ak, av = _kv(55, 250)
    bk, bv = _kv(66, 400)
    a.push(ak, av)
    b.push(bk, bv)
    a.check()
    b.check()
    a.check(n_tok=2)  # switching sessions back and forth keeps each one's state
    st = pool.status(b.c)
    assert st["relay"] and [x["status"]["pool"]["blocks"] for x in st["holders"]] == [12, 4]
    b.c.close()
    a.check()


def test_relay_refuses_sessions_on_a_holder_without_the_pool(relay, monkeypatch):
    r, token, ep, wp = relay
    _add_holder(r, token, wp, "old", 8192)
    monkeypatch.setattr(kv, "_pool", None)
    e = Engine(ep, sid=1)
    assert e.pooled is False  # refused: the engine keeps one connection = one context
    plain = Engine(ep)
    plain.push(PK[:, :600], PV[:, :600])
    plain.check()


# ---------------------------------------------------------------- CLI and the platform directory


def test_cli_pool_status(server, capsys):
    h, port = server()
    Engine(port, sid=1).start_with_prefix()
    assert kv.main(["pool", "status", "--target", f"127.0.0.1:{port}"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pool"]["blocks"] == 16


def test_cli_pool_status_on_a_plain_holder(server, capsys, monkeypatch):
    monkeypatch.setattr(kv, "_pool", None)
    h, port = server()
    assert kv.main(["pool", "status", "--target", f"127.0.0.1:{port}"]) == 1
    assert "no shared pool" in capsys.readouterr().out


class _FakeNexus(BaseHTTPRequestHandler):
    """The two Nexus WSPD routes, same request and response shapes."""

    entries: dict = {}

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.headers.get("X-Internal-Key") != "k":
            return self._json(401, {"detail": "auth"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = (body["tenant_slug"], body["workspace_id"], body["content_hash"])
        if body["released"]:
            e = self.entries.get(key)
            if e:
                e["node_ids"].discard(body["node_id"])
                if not e["node_ids"]:
                    del self.entries[key]
            return self._json(200, {"success": True, "action": "released"})
        e = self.entries.setdefault(key, {**body, "node_ids": set()})
        e["node_ids"].add(body["node_id"])
        self._json(200, {"success": True, "action": "registered"})

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        key = (q["tenant_slug"], q["workspace_id"], q.get("content_hash", ""))
        e = self.entries.get(key)
        out = [{**e, "node_ids": sorted(e["node_ids"])}] if e else []
        self._json(200, {"entries": out, "total": len(out)})


def test_published_and_evicted_blocks_reach_the_prefix_directory(server):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeNexus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = pool.NexusPrefixDirectory(
            f"http://127.0.0.1:{srv.server_address[1]}",
            "acme",
            "ws1",
            "relay-1",
            headers={"X-Internal-Key": "k"},
            entitlement_fingerprint=("ws:ws1",),
            background=False,
        )
        h, port = server(max_keys=PREFIX + 512)
        h.pool.listeners.append(d)
        a = Engine(port, sid=1)
        a.start_with_prefix()
        head = HASHES[-1].hex()
        assert d.lookup(head) == ["relay-1"]
        assert _FakeNexus.entries[("acme", "ws1", head)]["token_count"] == PREFIX
        a.c.close()
        other = Engine(port, sid=2)
        k, v = _kv(77, 1024)
        other.push(k, v)  # evicts two blocks, deepest first: the directory forgets them
        assert d.lookup(HASHES[-1].hex()) == [] and d.lookup(HASHES[-2].hex()) == []
        assert d.lookup(HASHES[-3].hex()) == ["relay-1"]
        assert d.errors == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_directory_down_is_never_fatal():
    d = pool.NexusPrefixDirectory(
        f"http://127.0.0.1:{_free_port()}", "t", "w", "n", timeout=0.5, background=False
    )
    assert d.lookup("ab") == [] and d.register("ab", 1, 256) is False and d.errors == 2


def test_pool_messages_are_outside_the_v3_range():
    assert min(pool.POOL_TYPES) > kv.ATTN_BIG
    assert struct.calcsize("<32sII") == pool.POOL_REQ.size
