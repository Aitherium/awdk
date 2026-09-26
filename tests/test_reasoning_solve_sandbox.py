"""h30 sandbox: the per-turn compute cap, stop signals that model code
cannot swallow, bounded print, and the import allow-list.

Parity port of h30 ``agent/tests/test_h30_sandbox.py`` at c076671233: the same tests, run
against the vendored core (only the import paths changed).
"""

from __future__ import annotations

import time

import pytest
from adk.reasoning.solve._vendor.sandbox import (
    ActionCap,
    GameStopped,
    Sandbox,
    SandboxTimeout,
    TurnEnd,
)


def test_infinite_loop_is_stopped_at_the_cap():
    sb = Sandbox(time_cap_s=0.3)
    t0 = time.perf_counter()
    res = sb.run("n = 0\nwhile True:\n    n += 1\n")
    dt = time.perf_counter() - t0
    assert res.stopped_by == "timeout" and not res.ok
    assert 0.25 <= dt < 2.0
    assert sb.ns["n"] > 0  # it really ran


def test_timeout_inside_a_model_function_and_bare_except_cannot_swallow_it():
    sb = Sandbox(time_cap_s=0.3)
    code = (
        "def spin():\n"
        "    while True:\n"
        "        try:\n"
        "            pass\n"
        "        except:\n"
        "            pass\n"
        "try:\n"
        "    spin()\n"
        "except BaseException:\n"
        "    print('swallowed')\n"
    )
    t0 = time.perf_counter()
    res = sb.run(code)
    assert res.stopped_by == "timeout"
    assert "swallowed" not in res.stdout
    assert time.perf_counter() - t0 < 2.0


def test_paused_clock_excludes_engine_waits():
    sb = Sandbox(time_cap_s=0.3)

    def wait_on_engine():
        with sb.paused():
            time.sleep(0.5)  # longer than the cap, but it is not compute
        return 1

    sb.ns["wait_on_engine"] = wait_on_engine
    res = sb.run("x = wait_on_engine() + wait_on_engine()\n")
    assert res.ok and sb.ns["x"] == 2


def test_the_cap_resets_every_turn_and_the_namespace_persists():
    sb = Sandbox(time_cap_s=0.5)
    assert sb.run("def f(k):\n    return k * 2\nacc = []\n").ok
    for i in range(3):
        res = sb.run("acc.append(f(%d))\n" % i)
        assert res.ok
    assert sb.ns["acc"] == [0, 2, 4]


@pytest.mark.parametrize("exc,label", [(ActionCap, "action_cap"), (TurnEnd, "turn_end")])
def test_stop_signals_end_the_turn_cleanly(exc, label):
    sb = Sandbox()

    def raiser():
        raise exc("stop")

    sb.ns["raiser"] = raiser
    res = sb.run("try:\n    raiser()\nexcept Exception:\n    print('caught')\nprint('after')\n")
    assert res.stopped_by == label and res.ok
    assert "caught" not in res.stdout and "after" not in res.stdout


def test_game_stopped_propagates_to_the_harness():
    sb = Sandbox()

    def gone():
        raise GameStopped("won")

    sb.ns["gone"] = gone
    with pytest.raises(GameStopped):
        sb.run("gone()\n")


def test_errors_are_reported_not_raised():
    sb = Sandbox()
    res = sb.run("x = [1][3]\n")
    assert not res.ok and "IndexError" in res.error
    res = sb.run("def broken(:\n")
    assert not res.ok and res.error.startswith("SyntaxError")


def test_print_is_bounded_at_creation():
    sb = Sandbox(print_cap=100)
    res = sb.run("for i in range(1000):\n    print('line', i)\n")
    assert res.ok
    assert len(res.stdout) < 200 and "[output capped at 100 chars]" in res.stdout


def test_import_allow_list_and_no_open():
    sb = Sandbox()
    assert sb.run("import math, collections\nfrom itertools import product\n").ok
    res = sb.run("import os\n")
    assert not res.ok and "not allowed" in res.error
    res = sb.run("open('x.txt', 'w')\n")
    assert not res.ok and "NameError" in res.error


def test_call_caps_a_model_function_outside_a_turn():
    sb = Sandbox(time_cap_s=5.0)
    sb.run("def slow(x):\n    while True:\n        x += 1\n")
    with pytest.raises(SandboxTimeout):
        sb.call(sb.ns["slow"], 0, cap_s=0.2)
    assert sb.call(abs, -3) == 3
