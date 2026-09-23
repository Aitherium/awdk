"""adk bricks and the OS brick (awnix via bootc): each truth rule pinned."""

from __future__ import annotations

import json

import pytest
from adk import bricks, bricks_os

STATUS = {
    "status": {
        "booted": {
            "image": {
                "image": {"image": "ghcr.io/x/awnix:latest"},
                "version": "2026.09.20",
                "imageDigest": "sha256:aaa",
            }
        },
        "staged": None,
        "rollback": {
            "image": {
                "image": {"image": "ghcr.io/x/awnix:latest"},
                "version": "2026.09.13",
                "imageDigest": "sha256:bbb",
            }
        },
        "rollbackQueued": False,
    }
}


def fake_run(answers):
    """answers: {tuple(cmd): (code, out)}; records every call."""
    calls = []

    def run(cmd, timeout=600):
        calls.append(tuple(cmd))
        return answers.get(tuple(cmd), (127, f"{cmd[0]}: not found"))

    run.calls = calls
    return run


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(bricks_os, "is_host", lambda: True)


def test_not_an_awnix_host_is_no_row_never_up_to_date(monkeypatch):
    monkeypatch.setattr(bricks_os, "is_host", lambda: False)
    assert bricks_os.row() is None
    assert "not an awnix" in bricks_os.upgrade()["error"]


def test_an_update_is_seen_through_bootc_check(host):
    run = fake_run(
        {
            ("bootc", "status", "--json"): (0, json.dumps(STATUS)),
            ("bootc", "upgrade", "--check"): (
                0,
                "Update available for: ghcr.io/x/awnix\n  Version: 2026.09.22\n",
            ),
        }
    )
    r = bricks_os.row(run=run)
    assert r["installed"] == "2026.09.20" and r["latest"] == "2026.09.22"
    assert r["outdated"] is True and r["action"] == "adk bricks upgrade awnix"


def test_no_changes_is_current(host):
    run = fake_run(
        {
            ("bootc", "status", "--json"): (0, json.dumps(STATUS)),
            ("bootc", "upgrade", "--check"): (0, "No changes in: ghcr.io/x/awnix\n"),
        }
    )
    r = bricks_os.row(run=run)
    assert r["outdated"] is False and r["latest"] == "2026.09.20" and r["error"] is None


def test_a_check_denied_root_is_an_error_not_no_update(host):
    run = fake_run(
        {
            ("bootc", "status", "--json"): (0, json.dumps(STATUS)),
            ("bootc", "upgrade", "--check"): (1, "error: Permission denied (os error 13)"),
        }
    )
    r = bricks_os.row(run=run)
    assert r["outdated"] is False and r["latest"] is None
    assert "root" in r["error"]


def test_a_staged_image_says_reboot_and_does_not_query(host):
    staged = json.loads(json.dumps(STATUS))
    staged["status"]["staged"] = {"image": {"image": {"image": "r"}, "version": "2026.09.22"}}
    run = fake_run({("bootc", "status", "--json"): (0, json.dumps(staged))})
    r = bricks_os.row(run=run)
    assert "reboot" in r["action"]
    assert ("bootc", "upgrade", "--check") not in run.calls


def test_upgrade_without_root_is_refused_before_bootc_runs(host, monkeypatch):
    monkeypatch.setattr(bricks_os, "_is_root", lambda: False)
    run = fake_run({})
    res = bricks_os.upgrade(run=run)
    assert res["ok"] is False and "sudo" in res["error"]
    assert run.calls == []


def test_upgrade_reports_staged_never_running(host, monkeypatch):
    monkeypatch.setattr(bricks_os, "_is_root", lambda: True)
    after = json.loads(json.dumps(STATUS))
    after["status"]["staged"] = {"image": {"image": {"image": "r"}, "version": "2026.09.22"}}
    seq = iter([json.dumps(STATUS), json.dumps(after)])

    def run(cmd, timeout=600):
        if cmd[:2] == ["bootc", "status"]:
            return 0, next(seq)
        return 0, "Queued for next boot"

    res = bricks_os.upgrade(run=run)
    assert res["ok"] is True and res["staged"] is True
    assert (res["from"], res["to"]) == ("2026.09.20", "2026.09.22")
    assert "reboot" in res["note"]


def test_failed_units_fail_the_os_test(host):
    run = fake_run(
        {
            ("bootc", "status", "--json"): (0, json.dumps(STATUS)),
            ("systemctl", "--failed", "--no-legend", "--plain"): (
                0,
                "awrelay.service loaded failed failed x\n",
            ),
        }
    )
    res = bricks_os.test(run=run)
    assert res["ok"] is False and "awrelay.service" in res["detail"]


def test_bricks_routes_awnix_to_the_os_module(monkeypatch, tmp_path):
    monkeypatch.setenv("ADK_BRICKS_HOME", str(tmp_path))
    monkeypatch.setattr(
        bricks_os,
        "upgrade",
        lambda: {"id": "awnix", "ok": True, "from": "a", "to": "b", "staged": True},
    )
    res = bricks.upgrade("awnix")
    assert res["staged"] is True
    assert bricks.history("awnix")[-1]["op"] == "upgrade"


def test_list_puts_the_os_row_first(monkeypatch):
    monkeypatch.setattr(bricks, "FAMILY", ())
    monkeypatch.setattr(bricks.md, "distributions", lambda: [])
    monkeypatch.setattr(bricks_os, "row", lambda check_latest=True: {"id": "awnix"})
    assert bricks.status()[0]["id"] == "awnix"


def test_published_picks_the_newest_per_flavour(monkeypatch):
    payload = [
        {"tag_name": "awnix-iso-full-2026.09.22", "html_url": "u1"},
        {"tag_name": "awnix-iso-full-2026.09.01", "html_url": "u0"},
        {"tag_name": "awnix-iso-ai-2026.09.22", "html_url": "u2"},
        {"tag_name": "media-demo-2026.08.25", "html_url": "x"},
    ]

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps(payload).encode()

    monkeypatch.setattr(bricks_os.urllib.request, "urlopen", lambda req, timeout=8.0: Resp())
    res = bricks_os.published()
    assert res["ok"] is True
    assert {r["flavour"]: r["version"] for r in res["releases"]} == {
        "ai": "2026.09.22",
        "full": "2026.09.22",
    }
