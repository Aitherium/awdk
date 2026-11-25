"""`adk up` waits long enough for a cold first start.

Measured 2026-11-16 in a clean Windows Sandbox with the cloud reachable: the agent needed
~105 s to answer /health on its first start (module compile plus a run of cloud calls), and
`adk up` gave up at 60 s, killed it and reported failure -- at install time and again from the
logon task. The wait is now 240 s by default and ADK_UP_HEALTH_TIMEOUT changes it.
"""

from __future__ import annotations

import pytest

from adk import cli


def test_default_wait_outlasts_a_cold_first_start(monkeypatch):
    monkeypatch.delenv("ADK_UP_HEALTH_TIMEOUT", raising=False)
    assert cli._up_health_timeout() >= 180


@pytest.mark.parametrize(
    "raw,expected",
    [("30", 30.0), ("  90.5 ", 90.5), ("", 240.0), ("soon", 240.0), ("0", 240.0), ("-5", 240.0)],
)
def test_the_wait_can_be_set_and_a_bad_value_falls_back(monkeypatch, raw, expected):
    monkeypatch.setenv("ADK_UP_HEALTH_TIMEOUT", raw)
    assert cli._up_health_timeout() == expected


def test_adk_up_uses_that_wait_and_not_a_fixed_sixty():
    import inspect

    src = inspect.getsource(cli.cmd_up)
    assert "health_wait = _up_health_timeout()" in src
    assert "timeout=health_wait" in src
    assert "timeout=60, pid=server_pid" not in src
