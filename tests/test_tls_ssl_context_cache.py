"""httpx built a fresh SSL context per client (the whole CA file each time); on the adk
daemon those builds were ~60 % of the event loop's busy time in a turn (2026-10-14)."""

import os
import ssl
import time

import httpx

from adk import _tls


def test_one_context_per_trust_decision():
    a, b = _tls.ssl_context(True), _tls.ssl_context(True)
    assert isinstance(a, ssl.SSLContext) and a is b
    assert _tls.ssl_context(False) is False


def test_a_changed_ca_file_gets_a_new_context(tmp_path):
    import certifi

    ca = tmp_path / "ca.pem"
    ca.write_bytes(open(certifi.where(), "rb").read())
    first = _tls.ssl_context(str(ca))
    assert _tls.ssl_context(str(ca)) is first
    os.utime(ca, (time.time() + 10, time.time() + 10))
    assert _tls.ssl_context(str(ca)) is not first


def test_reusing_the_context_builds_no_new_ssl_contexts(monkeypatch):
    ctx = _tls.ssl_context(True)
    builds = {"n": 0}
    real = ssl.create_default_context

    def counting(*a, **k):
        builds["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    for _ in range(20):
        httpx.AsyncClient(verify=_tls.ssl_context(True))
    assert builds["n"] == 0 and _tls.ssl_context(True) is ctx
    httpx.AsyncClient(verify=True)
    assert builds["n"] >= 1, "httpx builds a context per client without the cache"


def test_the_daemon_hot_paths_use_the_shared_context():
    import inspect

    from adk import embeddings, graph_memory, memory, services
    from adk.client import _gateway_mcp
    from adk.llm import openai_compat

    from adk import server

    for mod in (embeddings, graph_memory, memory, services, _gateway_mcp, openai_compat,
                server):
        src = inspect.getsource(mod)
        n_clients = src.count("httpx.AsyncClient(")
        assert n_clients and src.count("ssl_context(") >= n_clients, mod.__name__
