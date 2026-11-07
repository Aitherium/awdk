"""`adk family-drive backup now|status|restore|drill`: the verbs call the household API and
turn its answer into an exit code (0 ok, 1 the backup is wrong, 2 could not run)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from adk import family_drive_cli as cli


@pytest.fixture
def api(monkeypatch):
    calls: List[Tuple[str, str, Any]] = []
    answers: Dict[Tuple[str, str], Any] = {}

    def _request(method, path, *, json_body=None, content=None, params=None):
        calls.append((method, path, json_body))
        return 200, answers[(method, path)]

    monkeypatch.setattr(cli, "request", _request)
    return calls, answers


def test_backup_now_starts_then_waits_for_the_outcome(api, capsys):
    calls, answers = api
    answers[("POST", "/family/storage/backup/now")] = {"status": "started"}
    answers[("GET", "/family/storage/backup")] = {"running": False, "last_status": "ok",
                                                  "last_ok_snapshot": "a" * 64}
    assert cli.main(["backup", "now"]) == 0
    assert "backup ok" in capsys.readouterr().out
    for status, code in (("failed", 1), ("no_index", 1), ("waiting", 3), ("partial", 3)):
        answers[("GET", "/family/storage/backup")] = {"running": False, "last_status": status}
        assert cli.main(["backup", "now"]) == code, status
    answers[("GET", "/family/storage/backup")] = {"running": True}
    assert cli.main(["backup", "now", "--wait", "0.01"]) == 3  # still running
    assert cli.main(["backup", "now", "--wait", "0"]) == 3  # just started, did not wait


def test_backup_now_when_one_is_running_is_3(monkeypatch, capsys):
    def busy(method, path, **kw):
        raise cli.ApiError(409, "backup_running")

    monkeypatch.setattr(cli, "request", busy)
    assert cli.main(["backup", "now"]) == 3
    assert "already running" in capsys.readouterr().err


def test_backup_status_shows_the_signing_key_to_write_down(api, capsys):
    _calls, answers = api
    answers[("GET", "/family/storage/backup")] = {
        "configured": True, "overdue": False, "last_ok": 1.0, "snapshots": [], "keep": 7,
        "signing_key": "ab" * 32, "signing_fingerprint": "abcd:" * 7 + "abcd",
        "unverified_on_target": 2}
    assert cli.main(["backup", "status"]) == 0
    out = capsys.readouterr().out
    assert "ab" * 32 in out and "write these down" in out and "IGNORED" in out


def test_backup_status_overdue_is_1(api, capsys):
    _calls, answers = api
    answers[("GET", "/family/storage/backup")] = {"configured": True, "overdue": True,
                                                  "last_ok": None, "snapshots": [], "keep": 7}
    assert cli.main(["backup", "status"]) == 1
    assert "OVERDUE" in capsys.readouterr().out


def test_backup_drill_passes_the_server_exit_code(api):
    _calls, answers = api
    for code in (0, 1, 2):
        answers[("POST", "/family/storage/backup/drill")] = {"exit": code, "reason": "r"}
        assert cli.main(["backup", "drill"]) == code


def test_backup_restore_asks_first(api, monkeypatch):
    calls, answers = api
    answers[("POST", "/family/storage/backup/restore")] = {"snapshot_id": "b" * 64,
                                                           "put_back": 1, "failed": []}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("no\n"))
    assert cli.main(["backup", "restore"]) == 2
    assert calls == []  # nothing sent without the word
    assert cli.main(["backup", "restore", "--yes", "--snapshot", "b" * 64]) == 0
    assert calls[-1][2] == {"snapshot_id": "b" * 64, "confirm": True}
