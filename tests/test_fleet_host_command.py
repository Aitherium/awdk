"""``adk fleet-host`` -- argv building, repo discovery, the heartbeat summary.

The engine (AitherOS/dev/tools/fleet_host.py) is never run: this is the shell.
"""

from __future__ import annotations

import argparse
import json
import time

from adk.commands import fleet_host as fh


def _parse(*argv: str) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    fh.register_parser(ap.add_subparsers(dest="command"))
    return ap.parse_args(list(argv))


def test_status_is_the_default_and_read_only(tmp_path):
    tool = tmp_path / "fleet_host.py"
    a = _parse("fleet-host", "status")
    assert fh.build_argv(a, tool)[2:] == ["status"]
    assert fh.build_argv(_parse("fleet-host"), tool)[2:] == ["status"]


def test_mutating_verbs_need_execute(tmp_path):
    tool = tmp_path / "fleet_host.py"
    assert "--execute" not in fh.build_argv(_parse("fleet-host", "restart"), tool)
    live = fh.build_argv(_parse("fleet-host", "stop", "--execute", "--terminate"), tool)
    assert live[2:] == ["stop", "--execute", "--terminate"]


def test_migrate_carries_its_mode(tmp_path):
    argv = fh.build_argv(_parse("fleet-host", "migrate", "--mode", "rehearse"), tmp_path / "t.py")
    assert argv[2:] == ["migrate", "--mode", "rehearse"]


def test_find_tool_prefers_aitheros_root(tmp_path, monkeypatch):
    t = tmp_path / fh.TOOL_REL
    t.parent.mkdir(parents=True)
    t.write_text("", encoding="utf-8")
    assert fh.find_tool(env={"AITHEROS_ROOT": str(tmp_path)}) == t


def test_cached_summary_fresh_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    assert fh.cached_summary() is None
    (tmp_path / "fleet-host-status.json").write_text(
        json.dumps(
            {"distro": "aitheros-fleet", "verdict": "HEALTHY", "checked_epoch": time.time()}
        ),
        encoding="utf-8",
    )
    assert fh.cached_summary()["distro"] == "aitheros-fleet"
    assert fh.cached_summary(now=time.time() + 3600) is None


def test_shell_plugin_maps_words_to_the_engine():
    from adk.shell.plugins.builtins.fleet_host import build_args

    assert build_args([]) == ["status"]
    assert build_args(["status", "--execute"]) == ["status"]
    assert build_args(["restart"]) == ["restart"]
    assert build_args(["stop", "--execute", "--terminate"]) == ["stop", "--execute", "--terminate"]
    assert build_args(["migrate", "rehearse"]) == ["migrate", "--mode", "rehearse"]
