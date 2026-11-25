"""adk.harness_supervisor: the awdk daemon keeps the awsh session service (Claude Code's host) up.

Each test fails on the code it pins: a running harness is never started twice, a remote harness
URL is never spawned locally, a crash loop is rate limited, and the daemon wires the loop in.
"""

import asyncio

import pytest

from adk import harness_supervisor as hs


@pytest.fixture(autouse=True)
def _real_autostart(monkeypatch):
    # pytest sets PYTEST_CURRENT_TEST for every phase, so the guard itself is patched here and
    # tested on its own below.
    monkeypatch.setattr(hs, "autostart_enabled", lambda: True)
    monkeypatch.delenv("AITHER_HARNESS_AUTOSTART", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_URL", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_PORT", raising=False)


def test_a_running_harness_is_left_alone():
    spawned = []
    assert hs.ensure_started(url="http://127.0.0.1:8362", up=lambda u: True,
                             spawn=lambda p: spawned.append(p) or True) == "running"
    assert spawned == []


def test_a_missing_harness_is_started_on_its_own_port():
    spawned = []
    out = hs.ensure_started(url="http://127.0.0.1:9555", up=lambda u: False,
                            spawn=lambda p: spawned.append(p) or True)
    assert out == "started" and spawned == [9555]


def test_a_failed_spawn_is_reported_not_hidden():
    assert hs.ensure_started(url="http://127.0.0.1:8362", up=lambda u: False,
                             spawn=lambda p: False) == "failed"


def test_a_harness_on_another_machine_is_never_spawned_here():
    spawned = []
    out = hs.ensure_started(url="http://10.0.0.7:8362", up=lambda u: False,
                            spawn=lambda p: spawned.append(p) or True)
    assert out == "external" and spawned == []


def test_the_off_switch_is_honoured(monkeypatch):
    monkeypatch.setattr(hs, "autostart_enabled", lambda: False)
    spawned = []
    out = hs.ensure_started(url="http://127.0.0.1:8362", up=lambda u: False,
                            spawn=lambda p: spawned.append(p) or True)
    assert out == "disabled" and spawned == []


def test_under_pytest_nothing_is_ever_spawned(monkeypatch):
    monkeypatch.undo()  # the real guard, with pytest's own PYTEST_CURRENT_TEST set
    assert hs.autostart_enabled() is False


def test_the_off_switch_env_var(monkeypatch):
    monkeypatch.undo()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("AITHER_HARNESS_AUTOSTART", "0")
    assert hs.autostart_enabled() is False
    monkeypatch.setenv("AITHER_HARNESS_AUTOSTART", "1")
    assert hs.autostart_enabled() is True


def test_a_crash_loop_is_rate_limited():
    spawned = []
    now = [1000.0]

    async def run():
        task = asyncio.create_task(hs.supervise(
            url="http://127.0.0.1:8362", interval=0.01, first_wait=0.01, up=lambda u: False,
            spawn=lambda p: spawned.append(p) or True, clock=lambda: now[0]))
        await asyncio.sleep(0.4)  # many looks, the clock never advances
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(spawned) == 1


def test_the_loop_starts_it_again_once_the_gap_has_passed():
    spawned = []
    now = [1000.0]

    def spawn(p):
        spawned.append(p)
        now[0] += hs.MIN_SPAWN_GAP_S + 1
        return True

    async def run():
        task = asyncio.create_task(hs.supervise(
            url="http://127.0.0.1:8362", interval=0.01, first_wait=0.01,
            up=lambda u: False, spawn=spawn, clock=lambda: now[0]))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(spawned) >= 2


def test_the_daemon_lifespan_runs_the_supervisor():
    import inspect

    from adk import server

    src = inspect.getsource(server)
    assert "harness_supervisor" in src and "harness_supervisor_task" in src
