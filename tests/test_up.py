"""Unit tests for ``adk up`` (awdk 4.0 one-command stack boot)."""

from __future__ import annotations

from pathlib import Path

from adk import up as adk_up


def test_check_bricks_reports_awdk():
    rows = adk_up.check_bricks()
    by_id = {r["id"]: r for r in rows}
    assert "awdk" in by_id
    assert by_id["awdk"]["installed"] is True


def test_ensure_daemons_dry_run_and_status():
    dry = adk_up.ensure_daemons(dry_run=True, probe=lambda _u: (False, "refused"))
    by_id = {d["id"]: d for d in dry}
    assert by_id["hookd"]["action"] == "would-start"
    assert by_id["awsh"]["action"] == "would-start"
    assert by_id["gateway"]["action"] == "down"  # read_only probe never spawns

    up_all = adk_up.ensure_daemons(status_only=True, probe=lambda _u: (True, "HTTP 200"))
    assert all(d["up"] and d["action"] == "already-up" for d in up_all)


def test_run_up_dry_run_wires_no_files(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "ag-cfg"
    cfg.mkdir()
    monkeypatch.setenv("ANTIGRAVITY_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "aither"))

    rep = adk_up.run_up(dry_run=True, no_daemons=True)
    assert rep["ok"] is True
    assert rep["mode"] == "dry-run"
    assert not (cfg / "plugins").exists()
    ag_rows = [a for a in rep["agents"] if a["agent"] == "antigravity"]
    assert ag_rows and ag_rows[0]["action"] == "would-setup"


def test_stop_daemons_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    rdir = tmp_path / "run"
    rdir.mkdir()
    (rdir / "awsh-daemon.pid").write_text("999999\n", encoding="utf-8")

    stopped = adk_up.stop_daemons(dry_run=True)
    by_id = {d["id"]: d for d in stopped}
    assert by_id["awsh"]["action"] == "would-stop"
    assert by_id["awsh"]["pid"] == 999999
