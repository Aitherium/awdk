"""`adk doctor` finds the lane doctor from a SNAPSHOT install.

refresh_agent_tools.py archives only awdk/, so ``parents[2]/AitherOS/dev/tools``
does not exist beside a snapshot and the Claude-lane check went silently dead
(measured 2026-09-30, D-36). The resolver must find the tool through
AITHER_REPO_ROOT or the snapshot marker's ``repo_root``, and say so when it
cannot.
"""
from __future__ import annotations

import json
from pathlib import Path

from adk import doctor_local


def _fake_repo(root: Path) -> Path:
    tool = root / "AitherOS" / "dev" / "tools" / "aither_doctor.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("print('ok')\n", encoding="utf-8")
    return tool


def _snapshot(tmp_path: Path, monkeypatch, marker: "dict | None") -> None:
    pkg = tmp_path / "snap" / "awdk"
    (pkg / "adk").mkdir(parents=True)
    if marker is not None:
        (pkg / ".aither-snapshot.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(doctor_local, "__file__", str(pkg / "adk" / "doctor_local.py"))
    monkeypatch.setattr(doctor_local.sys, "platform", "linux")  # no default-checkout guess
    monkeypatch.setattr(doctor_local.Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.delenv("AITHER_REPO_ROOT", raising=False)


def test_resolves_through_the_env_var(tmp_path, monkeypatch):
    _snapshot(tmp_path, monkeypatch, None)
    tool = _fake_repo(tmp_path / "repo")
    monkeypatch.setenv("AITHER_REPO_ROOT", str(tmp_path / "repo"))
    path, source, _ = doctor_local._find_repo_tool("aither_doctor.py")
    assert path == tool and source == "AITHER_REPO_ROOT"


def test_resolves_through_the_snapshot_marker(tmp_path, monkeypatch):
    tool = _fake_repo(tmp_path / "repo")
    _snapshot(tmp_path, monkeypatch, {"path": "awdk", "repo_root": str(tmp_path / "repo")})
    path, source, _ = doctor_local._find_repo_tool("aither_doctor.py")
    assert path == tool and source == "snapshot marker"


def test_missing_names_the_real_fix(tmp_path, monkeypatch):
    _snapshot(tmp_path, monkeypatch, {"path": "awdk"})
    lines = doctor_local._doctor_local()
    assert len(lines) == 1 and "NOT FOUND" in lines[0]
    assert "AITHER_REPO_ROOT" in lines[0] and "git pull" not in lines[0]
    chain = doctor_local._chain_lines()
    assert chain and "chain doctor NOT FOUND" in chain[0]
