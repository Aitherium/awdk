"""kvpool transport: backend selection, loud Homa->TCP fallback, real block pulls on loopback."""

from __future__ import annotations

import logging
import threading

import pytest

from adk import kvpool as pool
from adk import kvpool_transport as tr

np = pytest.importorskip("numpy")  # optional for awdk; the payload lane has core deps only

FP = pool.model_fingerprint("test-model", "q-f16")
NS = (FP, pool.layout_fingerprint(b"cfg", "f32"), pool.BLOCK_TOKENS)
SHAPE = (2, 2, 2, 64, 64)  # layers, K/V, heads, tokens, head_dim


@pytest.fixture(autouse=True)
def _int8(monkeypatch):
    """Pin the codec so assertions do not depend on whether AitherKVCache is installed."""
    monkeypatch.setattr(tr, "_tq", lambda: (None, None))


class _FakeTransport:
    def __init__(self, name, ok, why="", conn_error=None):
        self.name, self._ok, self._why, self._err = name, ok, why, conn_error

    def available(self):
        return self._ok, self._why

    def connect(self, host, port, timeout=10.0):
        raise self._err


class _Sock:
    def __init__(self, *a):
        self.args = a

    def close(self):
        self.closed = True


def _blocks(n, seed=0):
    rng = np.random.default_rng(seed)
    keys = pool.chain_hashes(list(range(n * pool.BLOCK_TOKENS)), FP)
    return keys, [rng.standard_normal(SHAPE).astype(np.float32) for _ in range(n)]


# ---------------------------------------------------------------- selection + fallback


def test_auto_falls_back_to_tcp_loudly_when_homa_is_unavailable(caplog):
    homa = _FakeTransport("homa", False, "the homa kernel module is not loaded")
    with caplog.at_level(logging.WARNING, logger="adk.kvpool.transport"):
        t = tr.select_transport("auto", [homa, tr.TcpTransport()])
    assert t.name == "tcp"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "FALLBACK to tcp" in msg and "homa unavailable" in msg and "not loaded" in msg


def test_homa_is_chosen_without_a_warning_when_usable(caplog):
    homa = tr.HomaTransport(probe=lambda: (True, "probe ok"), binding=object())
    with caplog.at_level(logging.WARNING, logger="adk.kvpool.transport"):
        t = tr.select_transport("auto", [homa, tr.TcpTransport()])
    assert t.name == "homa" and not caplog.records


def test_homa_probe_never_assumes_the_module():
    def raises(*a):
        raise OSError(93, "Protocol not supported")

    assert tr.homa_probe(_Sock, platform="win32")[0] is False
    ok, why = tr.homa_probe(_Sock, platform="linux", path_exists=lambda p: False)
    assert not ok and "not loaded" in why
    ok, why = tr.homa_probe(raises, platform="linux", path_exists=lambda p: True)
    assert not ok and "refused" in why and "Protocol not supported" in why
    ok, why = tr.homa_probe(_Sock, platform="linux", path_exists=lambda p: True)
    assert ok and str(tr.homa_proto()) in why


def test_malformed_homa_proto_env_falls_back_instead_of_crashing(monkeypatch, caplog):
    monkeypatch.setenv("ADK_HOMA_PROTO", "homa")
    ok, why = tr.homa_probe(_Sock, platform="linux", path_exists=lambda p: True)
    assert not ok and "ADK_HOMA_PROTO" in why
    homa = tr.HomaTransport(
        probe=lambda: tr.homa_probe(_Sock, platform="linux", path_exists=lambda p: True),
        binding=object(),
    )
    with caplog.at_level(logging.WARNING, logger="adk.kvpool.transport"):
        assert tr.select_transport("auto", [homa, tr.TcpTransport()]).name == "tcp"
    assert "ADK_HOMA_PROTO" in " ".join(r.getMessage() for r in caplog.records)


def test_probe_positive_without_a_binding_is_still_unavailable():
    ok, why = tr.HomaTransport(probe=lambda: (True, "socket opens")).available()
    assert not ok and "binding" in why
    with pytest.raises(tr.TransportUnavailableError):
        tr.HomaTransport(probe=lambda: (True, "x")).connect("127.0.0.1", 1)


def test_explicit_tcp_and_env_and_unknown(monkeypatch):
    assert tr.select_transport("tcp").name == "tcp"
    monkeypatch.setenv("ADK_KVPOOL_TRANSPORT", "tcp")
    assert tr.select_transport().name == "tcp"
    with pytest.raises(ValueError):
        tr.select_transport("carrier-pigeon")


def test_every_backend_unusable_raises():
    with pytest.raises(tr.TransportUnavailableError, match="no kvpool transport usable"):
        tr.select_transport("auto", [_FakeTransport("homa", False, "nope")])


def test_homa_connect_failure_falls_back_to_tcp(caplog):
    store = tr.BlockStore()
    keys, arrs = _blocks(1)
    store.put(NS, keys[0], arrs[0])
    srv = tr.TcpTransport().serve(store)
    homa = _FakeTransport("homa", True, conn_error=ConnectionRefusedError("peer has no homa"))
    try:
        with caplog.at_level(logging.WARNING, logger="adk.kvpool.transport"):
            c = tr.BlockClient(*srv.address, candidates=[homa])
        assert c.transport == "tcp"
        assert "FALLBACK to tcp" in caplog.text and "peer has no homa" in caplog.text
        assert c.fetch(NS, keys)[0] is not None
        c.close()
    finally:
        srv.close()


# ---------------------------------------------------------------- real pulls over TCP loopback


def test_concurrent_pullers_get_exact_encoded_blocks_over_tcp():
    n_blocks, pullers = 12, 8
    keys, arrs = _blocks(n_blocks, seed=3)
    store = tr.BlockStore()
    blobs = {}
    for k, a in zip(keys, arrs):
        store.put(NS, k, a)
        blobs[k] = store.lookup(tr.ns_digest(NS), k, tr.NO_TOKEN)
    raw = sum(a.nbytes for a in arrs)
    assert store.bytes * 3 < raw  # int8 + per-vector scale, not float32, at rest

    srv = tr.TcpTransport().serve(store)
    errors, results = [], {}

    def pull(i):
        try:
            with tr.BlockClient(*srv.address, prefer="tcp") as c:
                order = keys[i % n_blocks :] + keys[: i % n_blocks]  # different orders
                got = c.fetch_encoded(NS, order)
                results[i] = dict(zip(order, got))
        except Exception as e:  # noqa: BLE001 -- surfaced by the assert below
            errors.append(repr(e))

    th = [threading.Thread(target=pull, args=(i,)) for i in range(pullers)]
    for t in th:
        t.start()
    for t in th:
        t.join(30)
    srv.close()
    assert not errors and len(results) == pullers
    for got in results.values():
        assert got == blobs  # byte-exact: the wire adds nothing and drops nothing
    back, codec = tr.decode_kv(blobs[keys[0]])
    assert codec == "int8-absmax" and back.shape == SHAPE
    rel = np.linalg.norm(back - arrs[0]) / np.linalg.norm(arrs[0])
    assert rel < 0.02


def test_kvpool_fetch_blocks_and_misses():
    keys, arrs = _blocks(2, seed=4)
    store = tr.BlockStore()
    store.put(NS, keys[0], arrs[0])
    srv = tr.TcpTransport().serve(store)
    try:
        got = pool.fetch_blocks(*srv.address, NS, keys, prefer="tcp")
    finally:
        srv.close()
    assert got[1] is None and np.allclose(got[0], arrs[0], atol=0.05)


def test_tagged_block_answers_miss_to_a_wrong_token():
    keys, arrs = _blocks(1, seed=5)
    good, bad = b"\x11" * 32, b"\x22" * 32
    store = tr.BlockStore()
    store.put(NS, keys[0], arrs[0], token=good)
    srv = tr.TcpTransport().serve(store)
    try:
        with tr.BlockClient(*srv.address, prefer="tcp") as c:
            assert c.fetch_encoded(NS, keys, bad) == [None]
            assert c.fetch_encoded(NS, keys) == [None]  # no token at all
            assert c.fetch_encoded(NS, keys, good)[0] is not None
            # another namespace (model / layout) never resolves the same key
            other = (pool.model_fingerprint("other"), NS[1], NS[2])
            assert c.fetch_encoded(other, keys, good) == [None]
    finally:
        srv.close()


def test_malformed_request_gets_err_not_a_crash():
    store = tr.BlockStore()
    assert tr.handle(store, tr.GET, b"short")[0] == tr.ERR
    assert tr.handle(store, 99, b"")[0] == tr.ERR
    with pytest.raises(ValueError):
        tr.pack_get(b"x" * 32, [b"k" * 32] * (tr.MAX_KEYS + 1))


def test_serve_pool_ships_resident_float_blocks():
    keys, arrs = _blocks(2, seed=6)
    kp = pool.KVPool()
    for j, (k, a) in enumerate(zip(keys, arrs)):
        layers = [(a[layer, 0], a[layer, 1]) for layer in range(a.shape[0])]
        kp.insert(pool.Block(ns=NS, key=k, index=j, tokens=64, layers=layers, nbytes=a.nbytes))
    kp.insert(pool.Block(ns=NS, key=b"q" * 32, index=9, tokens=64,
                         layers=[(1, 2, 3, 4, 5)], nbytes=1))  # a tq4 block: not shipped
    store = tr.BlockStore()
    assert tr.serve_pool(kp, store) == 2
    back, _ = tr.decode_kv(store.lookup(tr.ns_digest(NS), keys[1], tr.NO_TOKEN))
    assert np.allclose(back, arrs[1], atol=0.05)
