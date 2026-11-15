"""adk.file_index: enrolment installs the device's file-index job (owner 2026-11-07).

Fails on the code before it: there was no adk.file_index, and `adk pair` installed only
the heartbeat -- a newly enrolled device was never indexed.
"""

from __future__ import annotations

import json
import types

import pytest

from adk import file_index


class _Run:
    """A recording subprocess.run: answers by the argv's shape."""

    def __init__(self, *, present=False, pip_ok=True, pep668=False, schedule=None):
        self.calls: list = []
        self.present, self.pip_ok, self.pep668 = present, pip_ok, pep668
        self.installed: set = set()
        self.schedule = schedule or {"ok": True, "scheduler": "windows-task", "node": "adk-1",
                                     "every_hours": 6, "index_dir": "D:/AitherOS-Data/awstorage"}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        py = str(argv[0])
        if argv[1:3] == ["-c", "import awstorage.autoindex"]:
            ok = self.present or py in self.installed
            return types.SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
        if argv[1:4] == ["-m", "pip", "install"]:
            if self.pep668 and "venvs" not in py:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="error: externally-managed-environment")
            if self.pip_ok:
                self.installed.add(py)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="no matching dist")
        if argv[1:3] == ["-m", "venv"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1:5] == ["-m", "awstorage", "files", "schedule"]:
            return types.SimpleNamespace(returncode=0 if self.schedule.get("ok") else 1,
                                         stdout=json.dumps(self.schedule), stderr="")
        raise AssertionError(f"unexpected argv {argv}")


@pytest.fixture(autouse=True)
def _allow(monkeypatch, tmp_path):
    monkeypatch.delenv("AITHER_NO_FILE_INDEX", raising=False)
    monkeypatch.setattr(file_index, "venv_dir", lambda: tmp_path / "venvs" / "awstorage")


def test_enrolment_installs_and_schedules_the_index_job():
    run = _Run(present=True)
    out = file_index.install(types.SimpleNamespace(no_autostart=False), run=run, python="py")
    assert out["file_index"] == "scheduled" and out["installed"] == "present"
    assert ["py", "-m", "awstorage", "files", "schedule", "--json"] in run.calls
    assert "every 6h via windows-task, as node adk-1" in file_index.summary_line(out)


def test_pep668_python_gets_a_private_venv(tmp_path):
    run = _Run(pep668=True)
    out = file_index.install(None, run=run, python="/usr/bin/python3")
    assert out["file_index"] == "scheduled" and out["installed"].startswith("venv")
    vpy = str(file_index._venv_python(tmp_path / "venvs" / "awstorage"))
    assert [vpy, "-m", "awstorage", "files", "schedule", "--json"] in run.calls


def test_a_failed_install_or_schedule_is_reported_never_silent():
    out = file_index.install(None, run=_Run(pip_ok=False), python="py")
    assert out["file_index"] == "failed" and "did not install" in out["detail"]
    assert "NOT scheduled" in file_index.summary_line(out)
    bad = _Run(present=True, schedule={"ok": False, "detail": "not enrolled"})
    out = file_index.install(None, run=bad, python="py")
    assert out["file_index"] == "failed" and out["detail"] == "not enrolled"


def test_no_autostart_and_env_opt_out_touch_nothing(monkeypatch):
    run = _Run(present=True)
    assert file_index.install(types.SimpleNamespace(no_autostart=True), run=run)["file_index"] \
        == "off"
    monkeypatch.setenv("AITHER_NO_FILE_INDEX", "1")
    assert file_index.install(None, run=run)["file_index"] == "off"
    assert run.calls == []
