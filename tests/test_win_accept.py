"""The listener survives a client reset during AcceptEx (Windows proactor)."""

import asyncio
import socket
import sys

import pytest
from adk.harnesses import _win_accept

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="IOCP proactor is Windows-only")


def _winerr(code: int) -> OSError:
    exc = OSError(22, "simulated")
    exc.winerror = code
    return exc


def test_transient_classifier():
    assert _win_accept.is_transient(_winerr(64))
    assert not _win_accept.is_transient(_winerr(5))
    assert not _win_accept.is_transient(ValueError())


def test_transient_error_rearms_and_real_error_propagates(monkeypatch):
    from asyncio import windows_events

    outcomes = [_winerr(64), _winerr(10054), ("conn", ("127.0.0.1", 1)), _winerr(5)]
    calls = []

    def fake_accept(self, listener):
        fut = self._loop.create_future()
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if isinstance(outcome, OSError):
            self._loop.call_soon(fut.set_exception, outcome)
        else:
            self._loop.call_soon(fut.set_result, outcome)
        return fut

    monkeypatch.setattr(windows_events.IocpProactor, "accept", fake_accept)
    monkeypatch.setattr(_win_accept, "_installed", False)
    assert _win_accept.install()

    loop = asyncio.ProactorEventLoop()
    listener = socket.socket()
    try:
        first = loop.run_until_complete(loop._proactor.accept(listener))
        assert first == ("conn", ("127.0.0.1", 1))
        assert len(calls) == 3  # two resets swallowed, re-armed on the same listener
        with pytest.raises(OSError) as info:
            loop.run_until_complete(loop._proactor.accept(listener))
        assert info.value.winerror == 5  # a non-transient error still reaches the loop
    finally:
        listener.close()
        loop.close()


def test_unpatched_proactor_closes_the_listener(monkeypatch):
    """Control arm: without install(), one reset closes the listening socket."""
    from asyncio import windows_events

    def fake_accept(self, listener):
        fut = self._loop.create_future()
        self._loop.call_soon(fut.set_exception, _winerr(64))
        return fut

    monkeypatch.setattr(windows_events.IocpProactor, "accept", fake_accept)
    loop = asyncio.ProactorEventLoop()
    loop.set_exception_handler(lambda *_: None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    try:
        loop._start_serving(lambda: asyncio.Protocol(), sock)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert sock.fileno() == -1
    finally:
        loop.close()
