"""Regression tests for `adk onboard --quick` (the one-command self-service chain).

The chain reuses the sync cmd_quickstart_local / cmd_install / cmd_enroll helpers,
several of which call asyncio.run() internally. It therefore MUST run at top level,
never inside cmd_onboard's async _onboard() — otherwise cmd_enroll's asyncio.run()
raises "cannot be called from a running event loop", the try/except swallows it, and
enrollment silently never happens. These tests lock that in.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk import cli


def _args(**kw):
    kw.setdefault("agent", None)
    kw.setdefault("tenant", None)
    kw.setdefault("quick", True)
    kw.setdefault("pack", "openclaw")
    return argparse.Namespace(**kw)


def test_onboard_quick_runs_full_chain_at_top_level(monkeypatch):
    """enroll (which does asyncio.run) must actually run — no nested-loop error."""
    calls = []
    monkeypatch.setattr(cli, "cmd_quickstart_local", lambda a: calls.append("qs") or 0)
    monkeypatch.setattr(cli, "cmd_install", lambda a: calls.append("install") or 0)

    def fake_enroll(a):
        # Mirror the REAL cmd_enroll, which calls asyncio.run internally.
        async def _c():
            return 0
        rc = asyncio.run(_c())
        calls.append("enroll")
        return rc

    monkeypatch.setattr(cli, "cmd_enroll", fake_enroll)

    rc = cli.cmd_onboard(_args(api_key="aither_sk_live_x"))

    assert rc == 0
    assert "enroll" in calls, "enroll must run — nested-loop bug would have blocked it"
    assert "install" in calls
    assert "qs" not in calls, "local inference must be skipped when a cloud key is set"


def test_onboard_quick_runs_inference_when_no_key(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_quickstart_local", lambda a: calls.append("qs") or 0)
    monkeypatch.setattr(cli, "cmd_install", lambda a: calls.append("install") or 0)
    monkeypatch.setattr(cli, "cmd_enroll", lambda a: calls.append("enroll") or 0)

    rc = cli.cmd_onboard(_args(api_key=""))

    assert rc == 0
    assert calls[0] == "qs", "inference setup must run first when no key is present"


def test_onboard_quick_enroll_failure_is_nonfatal(monkeypatch):
    """A failing enroll must warn and continue (returns 0), never abort onboarding."""
    monkeypatch.setattr(cli, "cmd_quickstart_local", lambda a: 0)
    monkeypatch.setattr(cli, "cmd_install", lambda a: 0)

    def boom(a):
        raise RuntimeError("portal unreachable")

    monkeypatch.setattr(cli, "cmd_enroll", boom)

    rc = cli.cmd_onboard(_args(api_key="aither_sk_live_x"))
    assert rc == 0, "best-effort enroll failure must not fail the chain"
