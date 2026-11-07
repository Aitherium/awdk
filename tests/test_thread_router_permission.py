"""Router-spawned workers get an explicit, validated tool profile.

WHY THESE CASES
---------------
The thread router spawns a worker from a FIRST message on a thread nobody owns, so the
spawn is driven by an event rather than by the owner's click. Until now the worker got
only harness/cwd/title/prompt and ran under whatever permission mode the harness
defaulted to -- there was NO env var and no event-driven path to ``bypassPermissions``,
so this is defence in depth for a FUTURE variable, not a fix for a live hole. The mode is
carried through ``SessionConfig`` exactly as ``POST /sessions`` carries one, and the env
must never be able to say ``bypassPermissions``. The refusal arms are the point: an
allowed value proves the wiring, an unknown value (especially bypass) proves the gate.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from adk.harnesses import daemon

REQUEST = SimpleNamespace(surface="github", thread_id="Aitherium/AitherOS#1",
                          title="router worker")


def test_unset_env_keeps_todays_behaviour():
    assert daemon.thread_router_permission_mode({}) == ""


def test_blank_env_is_treated_as_unset():
    assert daemon.thread_router_permission_mode(
        {"AITHER_THREAD_ROUTER_PERMISSION_MODE": "   "}) == ""


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan"])
def test_each_allowed_mode_passes_through(mode):
    assert daemon.thread_router_permission_mode(
        {"AITHER_THREAD_ROUTER_PERMISSION_MODE": mode}) == mode


def test_surrounding_whitespace_is_stripped():
    assert daemon.thread_router_permission_mode(
        {"AITHER_THREAD_ROUTER_PERMISSION_MODE": "  plan \n"}) == "plan"


@pytest.mark.parametrize("mode", ["bypassPermissions", "BYPASSPERMISSIONS",
                                  "accept-edits", "acceptedits", "default,plan"])
def test_unknown_modes_are_refused_with_a_log_line(mode, capsys):
    assert daemon.thread_router_permission_mode(
        {"AITHER_THREAD_ROUTER_PERMISSION_MODE": mode}) is None
    err = capsys.readouterr().err
    assert "thread router refused" in err
    assert "AITHER_THREAD_ROUTER_PERMISSION_MODE" in err
    assert mode in err


def test_the_session_config_carries_the_profile(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = daemon.thread_router_session_config(
        REQUEST, harness="claude", permission_mode="acceptEdits")
    assert cfg.permission_mode == "acceptEdits"
    assert cfg.harness == "claude"
    assert cfg.title == "router worker"
    assert "thread router" in cfg.system_prompt_append
    assert cfg.cwd == str(tmp_path)


def test_an_unset_profile_leaves_the_harness_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = daemon.thread_router_session_config(
        REQUEST, harness="claude", permission_mode="")
    assert cfg.permission_mode == ""


def test_the_config_builder_refuses_bypass_even_without_the_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="bypassPermissions"):
        daemon.thread_router_session_config(
            REQUEST, harness="claude", permission_mode="bypassPermissions")


def test_the_daemon_imports_when_the_optional_router_module_is_absent():
    """The router is opt-in, so its module must never stop the daemon booting.

    ``thread_router.py`` is new work: a path-scoped commit of ``daemon.py`` without it
    (or a release cut before it landed) must still import the daemon and serve every
    non-router route. Without the guarded import this raises ImportError at module
    scope -- a daemon that cannot start at all. ``thread_subs`` is NOT in this arm: the
    already-tracked ``steer_dispatch`` imports it at module scope, so that file must be
    committed with the rest and cannot be made optional here.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = "\n".join([
        "import sys",
        "class Block:",
        "    def find_spec(self, name, path=None, target=None):",
        "        if name == 'adk.harnesses.thread_router':",
        "            raise ImportError('blocked for test: ' + name)",
        "        return None",
        "sys.meta_path.insert(0, Block())",
        "import adk.harnesses.daemon as d",
        "assert d.register_thread_router is None",
        "assert d.THREAD_ROUTER_IMPORT_ERROR",
        "assert callable(d.default_thread_subscriptions)",
        "print('IMPORTED')",
    ])
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "IMPORTED" in out.stdout
