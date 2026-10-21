"""The awsh plugin's awgit-ensure hook: finds the repo a `git commit` targets, installs
awgit's commit guards there once per session, and never fails or blocks the command."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parents[1] / "adk" / "harnesses" / "claude_mod" / "hooks"
        / "awgit_ensure.py")


def _load():
    spec = importlib.util.spec_from_file_location("_awgit_ensure_t", HOOK)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


ens = _load()
BASE = os.path.normpath(os.path.abspath("work"))


def _n(p: str) -> str:
    return os.path.normpath(p)


@pytest.mark.parametrize("cmd,expect", [
    ('git commit -m "x"', [BASE]),
    ('git add a && git commit -m x', [BASE]),
    ('GIT_INDEX_FILE=/tmp/i git commit -q -m x', [BASE]),
    ('git -C sub commit -m x', [os.path.join(BASE, "sub")]),
    ('git -c user.name=a -C sub -C deeper commit', [os.path.join(BASE, "sub", "deeper")]),
    ('cd other && git commit -m x', [os.path.join(BASE, "other")]),
    ('Set-Location other; git commit -m x', [os.path.join(BASE, "other")]),
    ('git log --grep commit', []),
    ('git commit-tree HEAD^{tree}', []),
    ('echo commit && git status', []),
    ('ls', []),
])
def test_commit_dirs(cmd, expect):
    assert [_n(d) for d in ens.commit_dirs(cmd, BASE)] == [_n(e) for e in expect]


@pytest.mark.skipif(os.name != "nt", reason="Git Bash drive paths are a Windows shape")
def test_git_bash_drive_path_resolves():
    assert ens.commit_dirs("git -C /d/dark-matters commit", BASE) == [_n("D:/dark-matters")]


def test_ensure_without_awgit_is_a_silent_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(ens, "_awgit_argv", lambda: None)
    assert ens.ensure([str(tmp_path)], "s1") == []


def test_ensure_runs_once_per_repo_per_session(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ens, "_awgit_argv", lambda: ["awgit"])
    monkeypatch.setattr(ens.subprocess, "run", fake_run)
    monkeypatch.setattr(ens.tempfile, "gettempdir", lambda: str(tmp_path))
    repo = tmp_path / "r"
    repo.mkdir()
    assert ens.ensure([str(repo)], "sess") == [str(repo)]
    assert ens.ensure([str(repo)], "sess") == []
    assert len(calls) == 1 and calls[0][1:4] == ["hooks", "ensure", "--repo"]
    assert ens.ensure([str(repo)], "other-session") == [str(repo)]


def test_main_never_fails_on_garbage(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert ens.main(["x", "pre-bash"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": None})))
    assert ens.main(["x", "pre-bash"]) == 0


def test_plugin_registers_the_hook_exec_form():
    hooks = json.loads((HOOK.parent / "portable.json").read_text(encoding="utf-8"))["hooks"]
    pre = hooks["PreToolUse"][0]
    assert "Bash" in pre["matcher"]
    h = pre["hooks"][0]
    assert h["command"] == "python" and h["args"][-1] == "pre-bash"
    assert any(x["hooks"][0].get("args", [""])[-1] == "session-start"
               for x in hooks["SessionStart"])


def test_adk_git_guards_uses_the_same_parser():
    from adk import git_guards

    assert git_guards.commit_dirs("git -C sub commit", BASE) == [os.path.join(BASE, "sub")]
