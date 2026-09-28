"""adk's fleet verbs are argv builders over AitherOS/dev/tools/fleet_verbs.py -- nothing more.

`adk gpu status|sleep|wake`, `adk fleet sleep|wake|critical` and the shell's /gpu (/gaming)
and /fleet all land on the same tool with the owner's verb words."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
from adk.commands import fleet_verbs as fv
from adk.shell.plugins.builtins import gaming

TOOL = Path("/repo/AitherOS/dev/tools/fleet_verbs.py")


@pytest.mark.parametrize("noun,verb,kw,tail", [
    ("gpu", "sleep", {}, ["gpu", "sleep"]),
    ("gpu", "sleep", {"execute": True}, ["gpu", "sleep", "--execute"]),
    ("gpu", "wake", {"execute": True, "force": True}, ["gpu", "wake", "--execute", "--force"]),
    ("gpu", "status", {"execute": True, "as_json": True}, ["status", "--json"]),
    ("fleet", "sleep", {"execute": True}, ["fleet", "sleep", "--execute"]),
    ("fleet", "wake", {"as_json": True}, ["fleet", "wake", "--json"]),
    ("fleet", "critical", {"execute": True}, ["fleet", "critical", "--execute"]),
])
def test_build_argv(noun, verb, kw, tail):
    argv = fv.build_argv(noun, verb, TOOL, **kw)
    assert argv[0] == sys.executable and argv[1] == str(TOOL)
    assert argv[2:] == tail


def test_unknown_verbs_refused():
    with pytest.raises(ValueError):
        fv.build_argv("gpu", "critical", TOOL)
    with pytest.raises(ValueError):
        fv.build_argv("fleet", "explode", TOOL)


def test_parsers_register_without_touching_agent_fleet():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command")
    fleet_p = sub.add_parser("fleet")
    fleet_sub = fleet_p.add_subparsers(dest="fleet_command")
    fleet_sub.add_parser("status")  # the agent-fleet verb stays
    fv.register_fleet_verbs(fleet_sub)
    fv.register_gpu_parser(sub)
    a = ap.parse_args(["fleet", "sleep", "--execute"])
    assert (a.command, a.fleet_command, a.execute) == ("fleet", "sleep", True)
    assert ap.parse_args(["fleet", "status"]).fleet_command == "status"
    g = ap.parse_args(["gpu", "wake", "--execute", "--force"])
    assert (g.gpu_command, g.execute, g.force) == ("wake", True, True)
    assert not hasattr(ap.parse_args(["gpu", "status"]), "execute")


@pytest.mark.parametrize("noun,words,expect", [
    ("gpu", [], ["gpu", "sleep", "--execute"]),                 # /gaming alone = game on
    ("gpu", ["sleep"], ["gpu", "sleep", "--execute"]),
    ("gpu", ["resume"], ["gpu", "wake", "--execute"]),          # old word, new verb
    ("gpu", ["wake", "--force"], ["gpu", "wake", "--execute", "--force"]),
    ("gpu", ["sleep", "--dry-run"], ["gpu", "sleep"]),
    ("gpu", ["status"], ["status"]),
    ("fleet", [], ["status"]),
    ("fleet", ["down"], ["fleet", "sleep", "--execute"]),
    ("fleet", ["wake"], ["fleet", "wake", "--execute"]),
    ("fleet", ["critical", "--dry-run"], ["fleet", "critical"]),
])
def test_slash_words(noun, words, expect):
    assert gaming.build_verb_args(noun, words) == expect


def test_slash_unknown_word():
    with pytest.raises(ValueError):
        gaming.build_verb_args("fleet", ["explode"])


def test_plugins_answer_to_the_owner_names():
    assert gaming.GamingModePlugin.name == "gpu"
    assert {"gaming", "game"} <= set(gaming.GamingModePlugin().aliases)
    assert gaming.FleetVerbsPlugin.name == "fleet"


def test_find_tool_finds_fleet_verbs_by_rel(tmp_path):
    from adk.commands.fleet_host import find_tool

    tool = tmp_path / fv.TOOL_REL
    tool.parent.mkdir(parents=True)
    tool.write_text("", encoding="utf-8")
    assert find_tool(env={"AITHEROS_ROOT": str(tmp_path)}, rel=fv.TOOL_REL) == tool
