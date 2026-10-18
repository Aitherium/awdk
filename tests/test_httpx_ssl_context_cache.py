"""Every httpx client in the daemon shares one SSLContext per trust decision.

py-spy on the :9001 daemon, 2026-10-14: 27% of the event loop's time in a turn was
ssl.create_default_context, built by httpx for each client constructed per call; every
recall stage's timer then fired late together and the turn got no memory at all.
"""

from __future__ import annotations

import httpx
import httpx._transports.default as transports

from adk import _tls


def _reset(monkeypatch, counter):
    real = transports.create_ssl_context  # monkeypatch restores it after the test

    def counting(verify=True, cert=None, trust_env=True):
        counter.append(verify)
        return real(verify=verify, cert=cert, trust_env=trust_env)

    monkeypatch.setattr(transports, "create_ssl_context", counting)
    monkeypatch.setattr(_tls, "_HTTPX_CTX", {})


def test_clients_share_one_context_per_trust_decision(monkeypatch, tmp_path):
    built: list = []
    _reset(monkeypatch, built)
    assert _tls.install_httpx_ssl_cache() is True
    assert _tls.install_httpx_ssl_cache() is True  # idempotent: not wrapped twice
    for _ in range(5):
        httpx.Client(verify=True).close()
    assert len(built) == 1, "five clients, one context"
    ctx_a = httpx.Client(verify=True)._transport._pool._ssl_context
    ctx_b = httpx.Client(verify=True)._transport._pool._ssl_context
    assert ctx_a is ctx_b


def test_a_client_certificate_or_an_explicit_context_is_never_cached(monkeypatch):
    import ssl

    built: list = []
    _reset(monkeypatch, built)
    _tls.install_httpx_ssl_cache()
    own = ssl.create_default_context()
    httpx.Client(verify=own).close()
    httpx.Client(verify=own).close()
    assert len(built) == 2, "an explicit SSLContext passes straight through"
