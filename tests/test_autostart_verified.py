"""`adk up` reports the autostart entry the OS HAS, and puts a missing one back.

2026-10-01: a throwaway `adk down` removed the real agent's logon entry while the
agent kept running. `adk up --yes` then printed `"autostart": "hkcu-run:AitherAgent",
"already_running": true` -- the value remembered in adk-up.json -- with neither the
scheduled task nor the Run value present. The already-running path never looked.

schtasks and the registry are fakes: a dict holds the one task and the one Run value
this Windows user can have under the shared name.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
from adk import agent_daemon as d
from adk import cli, doctor


class FakeWindows:
    """The scheduled task `AitherAgent` and the HKCU Run value `AitherAgent`."""

    def __init__(self, task: str = "", run: str = "", deny_task: bool = False):
        self.task, self.run_value, self.deny_task = task, run, deny_task
        self.calls: list[list[str]] = []

    def run(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[:2] == ["schtasks", "/query"]:
            return subprocess.CompletedProcess(argv, 0 if self.task else 1, self.task, "")
        if argv[:2] == ["schtasks", "/create"]:
            if self.deny_task:
                return subprocess.CompletedProcess(argv, 1, "", "Access is denied.")
            self.task = f"<Exec><Arguments>{argv[argv.index('/tr') + 1]}</Arguments></Exec>"
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def install_run(self, wrapper):
        self.run_value = f'wscript.exe //B //Nologo "run-hidden.vbs" "{wrapper}"'
        return True

    @property
    def created(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["schtasks", "/create"]]


@pytest.fixture
def win(monkeypatch, tmp_path):
    def _make(**kw) -> FakeWindows:
        fake = FakeWindows(**kw)
        home = tmp_path / "home-a"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(d, "AITHER_HOME", home)
        monkeypatch.setattr(d, "LOG_DIR", home / "logs")
        monkeypatch.setattr(d, "STATUS_PATH", home / "adk-up.json")
        monkeypatch.setattr(d.subprocess, "run", fake.run)
        monkeypatch.setattr(d, "_hkcu_run_value", lambda: fake.run_value)
        monkeypatch.setattr(d, "_install_hkcu_run", fake.install_run)
        return fake

    return _make


# The other home's wrapper is found by a drive-letter regex (a scheduled task only ever
# names one), and tmp_path only has a drive letter on Windows.
_DRIVE_TMP = pytest.mark.skipif(sys.platform != "win32",
                                reason="needs a drive-letter tmp_path (Windows)")


def _wrapper() -> Path:
    return d.AITHER_HOME / "aither-agent.cmd"


def _task_for(wrapper) -> str:
    return f'<Exec><Arguments>//B "run-hidden.vbs" "{wrapper}"</Arguments></Exec>'


UP = ["python", "-m", "adk.cli", "up", "--identity", "aither", "--port", "8080", "--yes"]


# ── agent_daemon ────────────────────────────────────────────────────────────────

def test_running_agent_with_no_entry_gets_it_back_from_the_wrapper_on_disk(win):
    fake = win()
    _wrapper().write_text("@echo off\r\noriginal command line\r\n", encoding="utf-8")
    assert d.autostart_state() == {"state": "missing", "entry": None}
    out = d.ensure_autostart(UP)
    assert out == {"state": "present", "entry": "windows-task:AitherAgent", "reinstalled": True}
    assert len(fake.created) == 1 and str(_wrapper()) in fake.created[0][-5]
    assert "original command line" in _wrapper().read_text(encoding="utf-8")   # not rewritten


def test_no_wrapper_on_disk_installs_from_the_given_command(win):
    fake = win()
    out = d.ensure_autostart(UP)
    assert out["state"] == "present" and out["reinstalled"] is True
    assert "--port 8080" in _wrapper().read_text(encoding="utf-8") and fake.created


def test_schtasks_refused_falls_back_to_the_run_value_and_reports_that(win):
    fake = win(deny_task=True)
    _wrapper().write_text("x", encoding="utf-8")
    out = d.ensure_autostart(UP)
    assert out == {"state": "present", "entry": "hkcu-run:AitherAgent", "reinstalled": True}
    assert str(_wrapper()) in fake.run_value


def test_an_entry_that_is_there_is_not_touched(win):
    fake = win()
    fake.task = _task_for(_wrapper())
    assert d.ensure_autostart(UP) == {"state": "present", "entry": "windows-task:AitherAgent"}
    assert fake.created == []
    fake.task, fake.run_value = "", f'wscript.exe "{_wrapper()}"'
    assert d.ensure_autostart(UP)["entry"] == "hkcu-run:AitherAgent"
    assert fake.created == []


@_DRIVE_TMP
@pytest.mark.parametrize("where", ["task", "run"])
def test_the_entry_of_another_home_is_left_alone(win, tmp_path, where):
    other = tmp_path / "home-b" / "aither-agent.cmd"
    other.parent.mkdir()
    other.write_text("the real agent", encoding="utf-8")
    fake = win(**{where: _task_for(other)})
    before = (fake.task, fake.run_value)
    for refresh in (False, True):
        out = d.ensure_autostart(UP, refresh=refresh)
        assert out == {"state": "other-home", "entry": None, "owner": str(other)}
    assert fake.created == [] and (fake.task, fake.run_value) == before


def test_an_entry_whose_home_is_gone_is_stale_and_replaced(win, tmp_path):
    fake = win(task=_task_for(tmp_path / "deleted-home" / "aither-agent.cmd"))
    out = d.ensure_autostart(UP)
    assert out["state"] == "present" and str(_wrapper()) in fake.task


def test_a_fresh_up_rewrites_its_own_entry(win):
    fake = win()
    fake.task = _task_for(_wrapper())
    _wrapper().write_text("old port", encoding="utf-8")
    out = d.ensure_autostart(UP, refresh=True)
    assert out == {"state": "present", "entry": "windows-task:AitherAgent"}
    assert "--port 8080" in _wrapper().read_text(encoding="utf-8") and len(fake.created) == 1


def test_nothing_could_be_installed_is_reported_missing_not_remembered(win, monkeypatch):
    win(deny_task=True)
    monkeypatch.setattr(d, "_install_hkcu_run", lambda wrapper: False)
    _wrapper().write_text("x", encoding="utf-8")
    assert d.ensure_autostart(UP) == {"state": "missing", "entry": None}


# ── adk up on an agent that is already running ──────────────────────────────────

def _up_args(**kw):
    base = dict(yes=True, offline=True, identity="aither", name="t", port=8080,
                foreground=False, no_persist=False, force=False, dry_run=False,
                require_register=False, provider="", approve=None, reach="tunnel",
                no_register=True, brain_pack="")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def running(win, monkeypatch):
    fake = win()
    _wrapper().write_text("x", encoding="utf-8")
    d.write_status({"ok": True, "identity": "aither", "port": 8080, "server_pid": 4242,
                    "autostart": "hkcu-run:AitherAgent"})          # the stale memory
    monkeypatch.setattr(d, "pid_alive", lambda pid: pid == 4242)
    return fake


def test_adk_up_already_running_reinstalls_and_reports_the_verified_entry(running, capsys):
    assert cli.cmd_up(_up_args()) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["already_running"] is True
    assert out["autostart"] == "windows-task:AitherAgent"           # what the OS has now
    assert out["autostart_state"] == "present" and out["autostart_reinstalled"] is True
    assert len(running.created) == 1
    saved = d.read_status()
    assert saved["autostart"] == "windows-task:AitherAgent" and "already_running" not in saved

    assert cli.cmd_up(_up_args()) == 0                              # second run: nothing to do
    again = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "autostart_reinstalled" not in again and len(running.created) == 1


def test_adk_up_already_running_reports_missing_when_it_cannot_install(running, capsys,
                                                                       monkeypatch):
    running.deny_task = True
    monkeypatch.setattr(d, "_install_hkcu_run", lambda wrapper: False)
    assert cli.cmd_up(_up_args()) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["autostart"] is None and out["autostart_state"] == "missing"


def test_adk_up_no_persist_does_not_touch_autostart(running, capsys):
    assert cli.cmd_up(_up_args(no_persist=True)) == 0
    capsys.readouterr()
    assert running.created == [] and running.run_value == ""


@_DRIVE_TMP
def test_adk_up_leaves_another_homes_entry_and_says_so(running, capsys, tmp_path):
    other = tmp_path / "home-b" / "aither-agent.cmd"
    other.parent.mkdir()
    other.write_text("the real agent", encoding="utf-8")
    running.task = _task_for(other)
    assert cli.cmd_up(_up_args()) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["autostart"] is None and out["autostart_state"] == "other-home"
    assert out["autostart_owner"] == str(other) and running.created == []


# ── status and doctor say "missing" loudly ──────────────────────────────────────

def test_status_shows_missing_not_the_remembered_entry(running, capsys, monkeypatch):
    monkeypatch.setattr(d, "wait_for_health", lambda *a, **k: True)
    cli.cmd_status(argparse.Namespace(json=True))
    out = capsys.readouterr().out
    state = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert state["autostart"] is None and state["autostart_state"] == "missing"


def test_doctor_fails_loudly_when_the_entry_is_missing(running, capsys):
    assert doctor.check_autostart() is False
    assert "Autostart: MISSING" in capsys.readouterr().out
    running.task = _task_for(_wrapper())
    assert doctor.check_autostart() is True
    assert "Autostart: windows-task:AitherAgent" in capsys.readouterr().out


def test_doctor_is_quiet_about_a_machine_that_never_used_adk_up(win, capsys):
    win()
    assert doctor.check_autostart() is True
    assert "not used" in capsys.readouterr().out
