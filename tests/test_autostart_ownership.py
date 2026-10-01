"""`adk down` removes only the autostart entry that launches THIS agent home.

2026-10-01: a throwaway `adk down` under a fake HOME deleted the real machine's
AitherAgent logon task -- the task name is per Windows user, not per agent home.
"""

import subprocess
import sys
from pathlib import Path

from adk import agent_daemon as d


def _fake_run(task_xml, calls):
    def run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["schtasks", "/query"]:
            return subprocess.CompletedProcess(argv, 0 if task_xml else 1, task_xml or "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def _setup(monkeypatch, tmp_path, task_xml):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(d, "AITHER_HOME", tmp_path / "home-a")
    monkeypatch.setattr(d.subprocess, "run", _fake_run(task_xml, calls))
    monkeypatch.setattr(d, "_remove_hkcu_run", lambda wrapper=None: False)
    return calls


def test_task_of_another_home_is_left_alone(monkeypatch, tmp_path):
    other = Path("C:/Users/real/.aither/aither-agent.cmd")
    calls = _setup(monkeypatch, tmp_path, f"<Exec><Arguments>\"{other}\"</Arguments></Exec>")
    assert d.remove_autostart() is False
    assert not any(c[:2] == ["schtasks", "/delete"] for c in calls)


def test_own_task_is_removed(monkeypatch, tmp_path):
    ours = tmp_path / "home-a" / "aither-agent.cmd"
    calls = _setup(monkeypatch, tmp_path, f"<Exec><Arguments>\"{ours}\"</Arguments></Exec>")
    assert d.remove_autostart() is True
    assert any(c[:2] == ["schtasks", "/delete"] for c in calls)


def test_no_task_no_delete(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, None)
    assert d.remove_autostart() is False
    assert not any(c[:2] == ["schtasks", "/delete"] for c in calls)


def test_names_wrapper_normalizes_slashes_and_case():
    assert d._names_wrapper(r'wscript.exe "C:\Users\A\.aither\aither-agent.cmd"',
                            "c:/users/a/.aither/aither-agent.cmd")
    assert not d._names_wrapper(r'"C:\Users\B\.aither\aither-agent.cmd"',
                                "C:/Users/A/.aither/aither-agent.cmd")
