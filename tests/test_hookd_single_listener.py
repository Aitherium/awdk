"""hookd: one listener per port, and the port is held before the worker pool starts.

Two hookd daemons once shared :8369 (Windows SO_REUSEADDR), each with a 24-worker
pool. These tests fail on a server that reuses the address or binds after the pool.
"""

from __future__ import annotations

import socket

import pytest

from adk.harnesses import hookd


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StopError(Exception):
    pass


def test_a_second_listener_on_the_same_port_is_refused():
    port = _free_port()
    first = hookd._bind(port)
    try:
        assert first is not None
        assert hookd._bind(port) is None
    finally:
        first.server_close()


def test_the_port_is_bound_before_the_pool_spawns(monkeypatch):
    port = _free_port()
    seen = {}

    class FakePool:
        def __init__(self, size):
            seen["size"] = size
            seen["bound"] = hookd._port_bound(port)
            raise _StopError

    monkeypatch.setattr(hookd, "Pool", FakePool)
    with pytest.raises(_StopError):
        hookd.serve(port, 6)
    assert seen == {"size": hookd._MIN_WORKERS, "bound": True}


def test_serve_exits_without_a_pool_when_the_port_is_owned(monkeypatch):
    port = _free_port()
    owner = hookd._bind(port)

    def no_pool(size):
        raise AssertionError("a duplicate hookd started its worker pool")

    monkeypatch.setattr(hookd, "Pool", no_pool)
    try:
        assert hookd.serve(port, 6) == 1
    finally:
        owner.server_close()
