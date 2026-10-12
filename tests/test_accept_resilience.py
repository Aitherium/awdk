"""A client that vanishes mid-accept must not close the daemon's listener."""

import asyncio

from adk import accept_resilience as ar


def _gone() -> OSError:
    exc = OSError("The specified network name is no longer available")
    exc.winerror = 64
    return exc


class _FakeProactor:
    """IocpProactor.accept stand-in: each call returns the next scripted outcome."""

    def __init__(self, loop, outcomes):
        self._loop = loop
        self.outcomes = list(outcomes)
        self.calls = 0

    def accept(self, listener):
        self.calls += 1
        fut = self._loop.create_future()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            fut.set_exception(outcome)
        elif outcome == "pending":
            pass
        else:
            fut.set_result(outcome)
        return fut


def _run(outcomes):
    async def go():
        p = _FakeProactor(asyncio.get_running_loop(), outcomes)
        wrapped = ar.resilient_accept(_FakeProactor.accept)
        fut = wrapped(p, "listener")
        try:
            return await asyncio.wait_for(fut, 1), p.calls
        except Exception as exc:  # noqa: BLE001 -- returned for the assertion
            return exc, p.calls
    return asyncio.run(go())


def test_a_departed_client_rearms_the_accept():
    result, calls = _run([_gone(), _gone(), ("conn", "addr")])
    assert result == ("conn", "addr")
    assert calls == 3


def test_any_other_error_still_reaches_the_loop():
    other = OSError("bad listener")
    other.winerror = 10038
    result, calls = _run([other])
    assert result is other and calls == 1


def test_cancelling_the_accept_cancels_the_pending_overlapped_call():
    async def go():
        p = _FakeProactor(asyncio.get_running_loop(), ["pending"])
        fut = ar.resilient_accept(_FakeProactor.accept)(p, "listener")
        await asyncio.sleep(0)
        fut.cancel()
        await asyncio.sleep(0)
        return fut.cancelled()
    assert asyncio.run(go())


def test_client_gone_shapes():
    assert ar.client_gone(_gone())
    assert not ar.client_gone(OSError("x"))
    assert not ar.client_gone(None)
