# SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
# © 2026 Aitherium, LLC. Original work.
"""The /wakes route family on the harness daemon: awrise's one read/mutate window.

Every mutation here spawns a FAKE awrise (a script that records its argv, then
exits with whatever code the test asked for), so the assertions are on the exact
argv list the daemon built, the exit code it propagated, and — for the refusal
paths — on the fact that NOTHING was spawned at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "wakes"
TOKEN = "test-token-for-testing-only"


# ── fake awrise ──────────────────────────────────────────────────────────────


def make_fake_awrise(root: Path, *, exit_code: int = 0, sleep_s: float = 0.0,
                     marker: Path | None = None) -> tuple[Path, Path]:
    """Write a fake ``awrise`` CLI. Returns (binary, argv capture file).

    The capture file gets one JSON line per invocation with the argv the child
    saw; ``marker`` is written AFTER ``sleep_s`` so a test can prove a child
    was left alive past the daemon's wait window.
    """
    capture = root / "argv.jsonl"
    script = root / "fake_awrise.py"
    script.write_text(
        "import json, sys, time\n"
        f"open({str(capture)!r}, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"time.sleep({float(sleep_s)!r})\n"
        + (f"open({str(marker)!r}, 'w').write('done')\n" if marker else "")
        + "print('fake awrise: ' + ' '.join(sys.argv[1:]))\n"
        + "sys.stderr.write('fake stderr\\n')\n"
        + f"sys.exit({int(exit_code)})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        binary = root / "awrise.cmd"
        binary.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            encoding="utf-8",
        )
    else:
        binary = root / "awrise"
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                          encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, capture


def spawned(capture: Path) -> list[list[str]]:
    if not capture.exists():
        return []
    return [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A schema-2 AWRISE_HOME with the fixture ledger, pointed at by env."""
    base = tmp_path / "awrise"
    base.mkdir()
    shutil.copy(FIXTURES / "jobs_v2.json", base / "jobs.json")
    shutil.copytree(FIXTURES / "ledger", base / "ledger")
    monkeypatch.setenv("AWRISE_HOME", str(base))
    return base


@pytest.fixture
def fake(tmp_path, monkeypatch) -> dict:
    """A fake awrise on AWRISE_BIN (exit 0). Tests re-point AWRISE_BIN as needed."""
    root = tmp_path / "fake"
    root.mkdir()
    binary, capture = make_fake_awrise(root, exit_code=0)
    monkeypatch.setenv("AWRISE_BIN", str(binary))
    return {"root": root, "binary": binary, "capture": capture}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(tmp_path / "harness_tokens.json"))
    import adk.harnesses.daemon as daemon

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", tmp_path / "harness_tokens.json")
    daemon._RUNNING.clear()
    app = daemon.create_app()
    c = TestClient(app)
    c.headers = {"Authorization": f"Bearer {TOKEN}"}
    return c


def _abs(p: Path) -> str:
    return os.path.abspath(str(p))


# ── GET /wakes ───────────────────────────────────────────────────────────────


class TestWakesList:
    def test_installed_shape(self, client, home):
        resp = client.get("/wakes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is True
        assert data["schema"] == 2 and data["migration"] is None
        assert data["home"] == str(home)
        assert data["count"] == 3 and data["failing"] == 1 and data["disabled"] == 1
        assert data["running"] == 0
        assert data["last_tick_at"] == "2026-09-18T07:41:00.000000+00:00"
        assert isinstance(data["clock_stale"], bool)
        assert data["error"] is None
        ns = next(w for w in data["wakes"] if w["name"] == "nightly-sync")
        for key in ("enabled", "every", "interval_s", "run", "timeout_s", "at",
                    "last_wake_id", "last_started_at", "last_finished_at", "last_state",
                    "last_reason", "consecutive_failures", "report", "running",
                    "running_wake_id", "running_since", "next_due_at"):
            assert key in ns
        assert ns["last_reason"] == "exit 1"

    def test_absent_home_is_200_not_installed(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.get("/wakes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is False
        assert data["wakes"] == [] and data["schema"] is None
        assert data["last_tick_at"] is None and data["clock_stale"] is False

    def test_malformed_jobs_json_is_200_with_error(self, client, home):
        (home / "jobs.json").write_text("{oops", encoding="utf-8")
        resp = client.get("/wakes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is True and data["wakes"] == []
        assert data["error"].startswith("jobs.json unreadable:")

    def test_v1_file_answers_schema_1_with_migration_hint(self, client, home):
        shutil.copy(FIXTURES / "jobs_v1.json", home / "jobs.json")
        data = client.get("/wakes").json()
        assert data["schema"] == 1
        assert "awrise list" in data["migration"]
        j1 = next(w for w in data["wakes"] if w["name"] == "job1")
        assert j1["every"] == "15m" and j1["interval_s"] == 900.0
        assert j1["last_started_at"].endswith("+00:00")

    def test_clock_stale_on_old_tick(self, client, home):
        # the fixture's newest tick is 07:41 on 2026-09-18 — long past "now"
        data = client.get("/wakes").json()
        assert data["clock_stale"] is True

    def test_clock_stale_when_no_tick_row(self, client, home):
        shutil.rmtree(home / "ledger")
        data = client.get("/wakes").json()
        assert data["last_tick_at"] is None and data["clock_stale"] is True

    def test_running_from_open_started_row(self, client, home):
        with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-09-18T07:41:30+00:00", "job": "nightly-sync",
                                 "event": "started", "wake_id": "w-live", "reason": "m"}) + "\n")
        data = client.get("/wakes").json()
        ns = next(w for w in data["wakes"] if w["name"] == "nightly-sync")
        assert ns["running"] is True and ns["running_wake_id"] == "w-live"
        assert data["running"] == 1
        assert [w["name"] for w in client.get("/wakes?state=running").json()["wakes"]] == \
            ["nightly-sync"]

    def test_state_filter(self, client, home):
        assert [w["name"] for w in client.get("/wakes?state=failing").json()["wakes"]] == \
            ["nightly-sync"]
        assert client.get("/wakes?state=bogus").status_code == 400

    def test_requires_bearer(self, client, home):
        client.headers = {}
        assert client.get("/wakes").status_code == 401


class TestWakesMalformedStateIsStill200:
    """One bad row or field is that job's problem, never a 500 on the family."""

    def _bad_ledger(self, home: Path) -> None:
        with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "started", "job": "nightly-sync",
                                 "wake_id": "w-int", "ts": 123}) + "\n")
            fh.write(json.dumps({"event": "started", "job": "nightly-sync",
                                 "wake_id": "w-str", "ts": "2026-09-18T07:42:00+00:00"}) + "\n")
            fh.write(json.dumps({"event": "started", "job": "nightly-sync",
                                 "wake_id": [1], "ts": "2026-09-18T07:42:01+00:00"}) + "\n")

    def test_malformed_ledger_rows_all_three_reads_200(self, client, home):
        self._bad_ledger(home)
        for path in ("/wakes", "/wakes/count", "/wakes/nightly-sync", "/wakes/ledger"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text)
        assert client.get("/wakes/nightly-sync").json()["running_wake_id"] == "w-str"

    def test_untyped_jobs_fields_all_reads_200(self, client, home):
        raw = json.loads((home / "jobs.json").read_text(encoding="utf-8"))
        raw["jobs"]["nightly-sync"]["consecutive_failures"] = "3"
        raw["jobs"]["nightly-sync"]["interval_s"] = 1e308
        raw["jobs"]["nightly-sync"]["last_started_at"] = "2026-09-18T07:00:00+00:00"
        other = next(n for n in raw["jobs"] if n != "nightly-sync")
        raw["jobs"][other]["consecutive_failures"] = [1]
        (home / "jobs.json").write_text(json.dumps(raw), encoding="utf-8")
        for path in ("/wakes", "/wakes?state=failing", "/wakes/count", "/wakes/nightly-sync"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text)
        data = client.get("/wakes").json()
        ns = next(w for w in data["wakes"] if w["name"] == "nightly-sync")
        assert ns["consecutive_failures"] == 3 and ns["next_due_at"] is None
        assert ns["error"] == "interval unreadable"
        assert data["failing"] >= 1
        failing = client.get("/wakes?state=failing").json()["wakes"]
        assert "nightly-sync" in [w["name"] for w in failing]

    def test_v1_inf_interval_200(self, client, home):
        (home / "jobs.json").write_text(json.dumps({"j": {"interval": "inf", "command": "x"}}),
                                        encoding="utf-8")
        resp = client.get("/wakes")
        assert resp.status_code == 200 and resp.json()["schema"] == 1
        assert resp.json()["wakes"][0]["error"] == "interval unreadable"


class TestWakesCount:
    def test_count_shape(self, client, home):
        resp = client.get("/wakes/count")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data) == {"count", "failing", "disabled", "running", "installed", "schema",
                             "last_tick_at", "clock_stale"}
        assert data["count"] == 3 and data["failing"] == 1

    def test_count_absent(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        data = client.get("/wakes/count").json()
        assert data == {"count": 0, "failing": 0, "disabled": 0, "running": 0,
                        "installed": False, "schema": None, "last_tick_at": None,
                        "clock_stale": False}


class TestWakesLedger:
    def test_rows_newest_first(self, client, home):
        data = client.get("/wakes/ledger").json()
        assert data["count"] == 7
        assert data["rows"][0]["event"] == "tick"
        assert data["skipped"] == 0

    def test_job_and_event_filters(self, client, home):
        rows = client.get("/wakes/ledger?job=nightly-sync").json()["rows"]
        assert {r["job"] for r in rows} == {"nightly-sync"}
        rows = client.get("/wakes/ledger?event=finished").json()["rows"]
        assert {r["event"] for r in rows} == {"finished"}
        assert all("output_tail" in r and "exit_code" in r for r in rows)

    def test_limit_capped_at_500(self, client, home):
        with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
            for i in range(600):
                fh.write(json.dumps({"ts": f"2026-09-18T08:{i % 60:02d}:00+00:00",
                                     "event": "tick", "reason": "x"}) + "\n")
        assert client.get("/wakes/ledger?limit=100000").json()["count"] == 500
        assert client.get("/wakes/ledger?limit=3").json()["count"] == 3

    def test_bad_job_name_400(self, client, home):
        assert client.get("/wakes/ledger?job=../x").status_code == 400

    def test_malformed_row_skipped_and_counted(self, client, home):
        with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-09-18T09:00:00+00:00", "event": "ti')
        data = client.get("/wakes/ledger").json()
        assert data["count"] == 7 and data["skipped"] == 1


class TestWakesGet:
    def test_one_job_with_recent(self, client, home):
        resp = client.get("/wakes/nightly-sync")
        assert resp.status_code == 200
        job = resp.json()
        assert job["name"] == "nightly-sync"
        assert [r["event"] for r in job["recent"]] == ["finished", "started"]
        assert job["recent"][0]["output_tail"] == "Traceback: boom"
        assert job["recent"][0]["state"] == "failure"
        assert job["recent"][0]["reason"] == "exit 1"
        assert job["recent"][0]["exit_code"] == 1

    def test_404_vs_503(self, client, home, tmp_path, monkeypatch):
        resp = client.get("/wakes/nope")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "no such wake: nope"
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.get("/wakes/nightly-sync")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "awrise not installed"

    def test_bad_name_400(self, client, home):
        # a slash-bearing name never reaches the handler (the router has no
        # such path); an option-shaped or space-bearing one does and is refused
        assert client.get("/wakes/..%2Fx").status_code in (400, 404)
        assert client.get("/wakes/-name").status_code == 400
        assert client.get("/wakes/.hidden").status_code == 400
        assert client.get("/wakes/a%20b").status_code == 400


# ── mutations ────────────────────────────────────────────────────────────────


class TestWakesMutate:
    def test_disable_spawns_exact_argv(self, client, home, fake):
        resp = client.post("/wakes/nightly-sync/disable")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True and data["action"] == "disable"
        assert data["exit_code"] == 0 and data["via"] == "owner"
        assert data["argv"] == [_abs(fake["binary"]), "disable", "--name", "nightly-sync"]
        assert "fake awrise: disable --name nightly-sync" in data["stdout_tail"]
        assert "fake stderr" in data["stderr_tail"]
        assert spawned(fake["capture"]) == [["disable", "--name", "nightly-sync"]]

    def test_enable_is_the_same_shape(self, client, home, fake):
        resp = client.post("/wakes/nightly-sync/enable", json={"note": "from a test"})
        assert resp.status_code == 200
        assert resp.json()["note"] == "from a test"
        assert spawned(fake["capture"]) == [["enable", "--name", "nightly-sync"]]

    def test_nonzero_exit_is_502_with_exit_code(self, client, home, fake, monkeypatch):
        root = fake["root"] / "e3"
        root.mkdir()
        binary, capture = make_fake_awrise(root, exit_code=3)
        monkeypatch.setenv("AWRISE_BIN", str(binary))
        resp = client.post("/wakes/nightly-sync/disable")
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["exit_code"] == 3
        assert detail["error"] == "awrise disable exited 3"
        assert "fake stderr" in detail["stderr_tail"]
        assert spawned(capture) == [["disable", "--name", "nightly-sync"]]

    def test_no_bearer_401_no_spawn(self, client, home, fake):
        client.headers = {}
        assert client.post("/wakes/nightly-sync/disable").status_code == 401
        assert spawned(fake["capture"]) == []

    def test_wrong_bearer_403_no_spawn(self, client, home, fake):
        client.headers = {"Authorization": "Bearer nope"}
        assert client.post("/wakes/nightly-sync/disable").status_code == 403
        assert spawned(fake["capture"]) == []

    def test_payload_cannot_name_scope(self, client, home, fake):
        for body in ({"home": "x"}, {"bin": "x"}, {"argv": ["x"]}, {"cwd": "x"},
                     {"env": {"A": "1"}}):
            resp = client.post("/wakes/nightly-sync/disable", json=body)
            assert resp.status_code in (400, 422), body
        assert spawned(fake["capture"]) == []

    def test_traversal_name_400_no_spawn(self, client, home, fake):
        assert client.post("/wakes/..%2Fx/disable").status_code in (400, 404)
        assert client.post("/wakes/-name/run").status_code == 400
        assert client.post("/wakes/.hidden/disable").status_code == 400
        assert client.post("/wakes/a%20b/run").status_code == 400
        assert client.post("/wakes/%2D%2Dhelp/run").status_code == 400
        assert spawned(fake["capture"]) == []

    def test_unknown_job_404_no_spawn(self, client, home, fake):
        resp = client.post("/wakes/typo/disable")
        assert resp.status_code == 404
        assert spawned(fake["capture"]) == []

    def test_missing_bin_file_503_no_spawn(self, client, home, fake, monkeypatch):
        monkeypatch.setenv("AWRISE_BIN", str(fake["root"] / "missing.exe"))
        resp = client.post("/wakes/nightly-sync/disable")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "awrise not installed"
        assert spawned(fake["capture"]) == []

    def test_not_installed_503_on_mutation(self, client, tmp_path, fake, monkeypatch):
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.post("/wakes/nightly-sync/disable")
        assert resp.status_code == 503
        assert spawned(fake["capture"]) == []


class TestWakesOrigin:
    def _channels(self, tmp_path, owner: str = "111", dm: bool = True) -> None:
        d = tmp_path / "decisions"
        d.mkdir(exist_ok=True)
        (d / "channels.json").write_text(json.dumps({"discord": {
            "enabled": True, "owner_user_id": owner, "require_direct_message": dm}}))

    def _body(self, user_id: str = "111", dm: bool = True) -> dict:
        return {"note": f"discord:{user_id}", "origin": {
            "platform": "discord", "user_id": user_id, "is_direct_message": dm}}

    def test_mismatched_owner_403_no_spawn(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="999")
        resp = client.post("/wakes/nightly-sync/disable", json=self._body("111"))
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "origin not authorized"
        assert "not the bound owner" in detail["reason"]
        assert spawned(fake["capture"]) == []

    def test_matching_owner_dm_200(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111")
        resp = client.post("/wakes/nightly-sync/disable", json=self._body("111"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["via"] == "owner:discord:111"
        assert spawned(fake["capture"]) == [["disable", "--name", "nightly-sync"]]

    def test_guild_message_refused_when_dm_required(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111", dm=True)
        resp = client.post("/wakes/nightly-sync/disable", json=self._body("111", dm=False))
        assert resp.status_code == 403
        assert "direct message" in resp.json()["detail"]["reason"]
        assert spawned(fake["capture"]) == []

    def test_unknown_platform_403(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111")
        body = self._body("111")
        body["origin"]["platform"] = "carrier-pigeon"
        resp = client.post("/wakes/nightly-sync/disable", json=body)
        assert resp.status_code == 403
        assert spawned(fake["capture"]) == []

    def test_no_channels_file_403(self, client, home, fake):
        resp = client.post("/wakes/nightly-sync/disable", json=self._body("111"))
        assert resp.status_code == 403
        assert "no configuration" in resp.json()["detail"]["reason"]
        assert spawned(fake["capture"]) == []

    def test_origin_missing_dm_flag_is_422_no_spawn(self, client, home, fake, tmp_path):
        self._channels(tmp_path, owner="111")
        resp = client.post("/wakes/nightly-sync/disable",
                           json={"origin": {"platform": "discord", "user_id": "111"}})
        assert resp.status_code in (400, 422)
        assert spawned(fake["capture"]) == []


class TestWakesEntitlement:
    def test_principal_without_wakes_mutate_is_403_on_post_200_on_get(
            self, client, home, fake, tmp_path):
        limited = "limited-token-for-testing"
        registry = {hashlib.sha256(limited.encode()).hexdigest(): {
            "principal": "limited", "plan": "free", "entitlements": ["decisions"]}}
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))
        client.headers = {"Authorization": f"Bearer {limited}"}
        assert client.get("/wakes").status_code == 200
        assert client.get("/wakes/nightly-sync").status_code == 200
        resp = client.post("/wakes/nightly-sync/disable")
        assert resp.status_code == 403
        assert "wakes:mutate" in resp.json()["detail"]
        assert client.post("/wakes/nightly-sync/run").status_code == 403
        assert spawned(fake["capture"]) == []
        # the owner bearer keeps working beside the registry
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.post("/wakes/nightly-sync/disable").status_code == 200


class TestWakesRun:
    def test_run_that_finishes_inside_the_window(self, client, home, fake):
        resp = client.post("/wakes/nightly-sync/run?wait_s=30")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["running"] is False and data["exit_code"] == 0
        assert isinstance(data["pid"], int) and data["pid"] > 0
        assert "fake awrise: run --name nightly-sync" in data["stdout_tail"]
        assert data["argv"] == [_abs(fake["binary"]), "run", "--name", "nightly-sync"]
        assert spawned(fake["capture"]) == [["run", "--name", "nightly-sync"]]

    def test_run_nonzero_exit_502(self, client, home, fake, monkeypatch):
        root = fake["root"] / "e2"
        root.mkdir()
        binary, capture = make_fake_awrise(root, exit_code=2)
        monkeypatch.setenv("AWRISE_BIN", str(binary))
        resp = client.post("/wakes/nightly-sync/run?wait_s=30")
        assert resp.status_code == 502
        assert resp.json()["detail"]["exit_code"] == 2
        assert spawned(capture) == [["run", "--name", "nightly-sync"]]

    def test_long_run_is_202_not_killed_then_409_then_free(self, client, home, fake,
                                                              monkeypatch):
        root = fake["root"] / "slow"
        root.mkdir()
        marker = root / "completed.marker"
        binary, capture = make_fake_awrise(root, exit_code=0, sleep_s=3.0, marker=marker)
        monkeypatch.setenv("AWRISE_BIN", str(binary))

        t0 = time.monotonic()
        resp = client.post("/wakes/nightly-sync/run?wait_s=1")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["ok"] is True and data["running"] is True
        assert data["exit_code"] is None
        assert isinstance(data["pid"], int) and data["pid"] > 0
        assert data["outcome"] == "GET /wakes/nightly-sync .recent"
        assert elapsed < 3.0, "the daemon waited for the child instead of answering 202"
        assert not marker.exists(), "child finished before the window closed?"

        # second POST while the child is alive -> 409, NO second spawn
        resp2 = client.post("/wakes/nightly-sync/run?wait_s=0")
        assert resp2.status_code == 409
        assert resp2.json()["detail"]["error"] == "already running"
        assert resp2.json()["detail"]["pid"] == data["pid"]
        assert len(spawned(capture)) == 1

        # the child was NOT killed: its completion marker appears after the response
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert marker.exists(), "child never completed — was it killed at the deadline?"

        # once the reaper freed the slot a third POST spawns again
        import adk.harnesses.daemon as daemon

        deadline = time.monotonic() + 10
        while "nightly-sync" in daemon._RUNNING and time.monotonic() < deadline:
            time.sleep(0.1)
        assert "nightly-sync" not in daemon._RUNNING
        resp3 = client.post("/wakes/nightly-sync/run?wait_s=30")
        assert resp3.status_code == 200, resp3.text
        assert len(spawned(capture)) == 2

    def test_wait_clamp_is_pinned_in_source(self):
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        assert "WAKE_RUN_WAIT_MAX_S = 120.0" in src
        assert "min(max(float(wait_s), 0.0), WAKE_RUN_WAIT_MAX_S)" in src
        assert daemon.WAKE_RUN_WAIT_MAX_S == 120.0
        assert daemon.WAKE_RUN_WAIT_DEFAULT_S == 20.0

    def test_run_never_uses_subprocess_run_with_timeout(self):
        """A ``subprocess.run(timeout=)`` KILLS the child at the deadline."""
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        start = src.index("def _wake_spawn(")
        end = src.index('@app.post("/sessions/{session_id}/resize"')
        run_section = src[start:end]
        assert "subprocess.Popen(" in run_section
        assert "subprocess.run(" not in run_section
        assert ".kill()" not in run_section and ".terminate()" not in run_section


class TestRouteOrder:
    def test_static_wakes_routes_precede_the_parameterised_one(self):
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        count_pos = src.index('@app.get("/wakes/count"')
        ledger_pos = src.index('@app.get("/wakes/ledger"')
        name_pos = src.index('@app.get("/wakes/{name}"')
        list_pos = src.index('@app.get("/wakes",')
        assert list_pos < name_pos
        assert count_pos < name_pos, "/wakes/count registered AFTER /wakes/{name}"
        assert ledger_pos < name_pos, "/wakes/ledger registered AFTER /wakes/{name}"

    def test_mutations_carry_the_entitlement_gate(self):
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        assert src.count('require_entitlement("wakes:mutate")') == 3

    def test_wakes_reader_is_imported_lazily(self):
        import adk.harnesses.daemon as daemon

        src = Path(daemon.__file__).read_text(encoding="utf-8")
        head = src[:src.index("def create_app(")]
        assert "adk.wakes" not in head, "adk.wakes must be imported inside handlers only"
        assert "from adk.wakes import" in src


# ── the CARD door: /decisions must not be a way around /wakes ────────────────


class TestWakeCardRecipeDoor:
    """`POST /decisions` + `POST /decisions/{id}/answer` spawn awrise too.

    Measured 2026-09-18: the three /wakes mutations were the only routes carrying
    `wakes:mutate`, while the card door was gated on the bearer alone — and the
    store's answer transition calls `card_recipes.apply_answer`, which spawns
    `awrise disable|run --name <job>`. So a token refused at /wakes/x/run reached
    the identical spawn by raising a `wake-failed` card and answering it.

    Every arm here asserts on the fake awrise's argv CAPTURE, so "refused" means
    nothing was started — not merely that the response said 403.
    """

    LIMITED = "limited-token-for-testing"
    ENTITLED = "entitled-token-for-testing"

    def _registry(self, tmp_path):
        """Two scoped principals: one without `wakes:mutate`, one with it.

        The second is the positive twin — a gate that refuses everyone passes a
        test that only checks for refusals.
        """
        registry = {
            hashlib.sha256(self.LIMITED.encode()).hexdigest(): {
                "principal": "tenant:ci", "plan": "pro", "entitlements": ["atlas"]},
            hashlib.sha256(self.ENTITLED.encode()).hexdigest(): {
                "principal": "tenant:ops", "plan": "pro",
                "entitlements": ["atlas", "wakes:mutate"]},
        }
        (tmp_path / "harness_tokens.json").write_text(json.dumps(registry))

    def _store(self, tmp_path, monkeypatch):
        """A decisions store rooted in THIS test's tmp_path.

        `get_store()` is a process-wide singleton, so the env var alone does not
        isolate a test that runs after another file has already built one.
        """
        import adk.decisions.store as store

        monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
        monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
        monkeypatch.setattr(store, "_STORE", None)
        return store

    def _raise(self, client, job, *, ts="2026-09-18T05:00:01+00:00", n="3"):
        return client.post("/decisions", json={
            "title": "ignored - the recipe owns the card",
            "card_recipe": "wake-failed",
            "recipe_vars": {"job": job, "first_failure_ts": ts, "n": n},
        })

    def test_unentitled_principal_cannot_raise_a_spawning_card(
            self, client, home, fake, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch)
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.LIMITED}"}
        resp = self._raise(client, "nightly-sync")
        assert resp.status_code == 403, resp.text
        assert "wakes:mutate" in resp.text
        assert not list((tmp_path / "decisions").glob("d-*.json"))
        assert spawned(fake["capture"]) == []

    def test_unentitled_principal_cannot_answer_an_owner_raised_card(
            self, client, home, fake, tmp_path, monkeypatch):
        """THE BYPASS, in one test. The owner raises; the scoped token answers."""
        self._store(tmp_path, monkeypatch)
        self._registry(tmp_path)
        raised = self._raise(client, "nightly-sync")
        assert raised.status_code == 200, raised.text
        card_id = raised.json()["id"]

        client.headers = {"Authorization": f"Bearer {self.LIMITED}"}
        for choice in ("run_now", "disable", "keep"):
            resp = client.post(f"/decisions/{card_id}/answer", json={"choice": choice})
            assert resp.status_code == 403, f"{choice}: {resp.text}"
            assert "wakes:mutate" in resp.text
        assert spawned(fake["capture"]) == [], "a refused answer still spawned awrise"

        # and the card is STILL OPEN — a refused answer must not close the ask.
        client.headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get(f"/decisions/{card_id}").json()["status"] == "open"

    def test_the_entitled_principal_answers_and_it_spawns(
            self, client, home, fake, tmp_path, monkeypatch):
        """The positive twin: with the entitlement the whole path still works."""
        self._store(tmp_path, monkeypatch)
        self._registry(tmp_path)
        client.headers = {"Authorization": f"Bearer {self.ENTITLED}"}
        raised = self._raise(client, "daily-report", ts="2026-09-18T06:00:00+00:00")
        assert raised.status_code == 200, raised.text
        card_id = raised.json()["id"]
        resp = client.post(f"/decisions/{card_id}/answer", json={"choice": "disable"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"]["answer"] == "disable"
        assert spawned(fake["capture"]) == [["disable", "--name", "daily-report"]]

    def test_the_owner_bearer_is_unaffected(
            self, client, home, fake, tmp_path, monkeypatch):
        """A registry must never lock the owner out of their own daemon."""
        self._store(tmp_path, monkeypatch)
        self._registry(tmp_path)
        raised = self._raise(client, "never-ran", ts="2026-09-18T06:30:00+00:00")
        assert raised.status_code == 200, raised.text
        resp = client.post(f"/decisions/{raised.json()['id']}/answer",
                           json={"choice": "disable"})
        assert resp.status_code == 200, resp.text
        assert spawned(fake["capture"]) == [["disable", "--name", "never-ran"]]

    def test_a_card_whose_job_is_gone_is_404_and_spawns_nothing(
            self, client, home, fake, tmp_path, monkeypatch):
        """A card outlives its job. `_wake_prepare` catches that; so must this."""
        self._store(tmp_path, monkeypatch)
        raised = self._raise(client, "deleted-job", ts="2026-09-18T06:45:00+00:00")
        assert raised.status_code == 200, raised.text
        resp = client.post(f"/decisions/{raised.json()['id']}/answer",
                           json={"choice": "run_now"})
        assert resp.status_code == 404, resp.text
        assert "deleted-job" in resp.text
        assert spawned(fake["capture"]) == []

    def test_a_run_already_in_flight_is_409_and_spawns_nothing(
            self, client, home, fake, tmp_path, monkeypatch):
        """The per-name slot /wakes/{name}/run holds applies to the card too."""
        import adk.harnesses.daemon as daemon

        self._store(tmp_path, monkeypatch)
        raised = self._raise(client, "nightly-sync", ts="2026-09-18T07:15:00+00:00")
        assert raised.status_code == 200, raised.text
        daemon._RUNNING["nightly-sync"] = 4242
        try:
            resp = client.post(f"/decisions/{raised.json()['id']}/answer",
                               json={"choice": "run_now"})
        finally:
            daemon._RUNNING.pop("nightly-sync", None)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["pid"] == 4242
        assert spawned(fake["capture"]) == []

    def test_awrise_absent_is_503_not_a_silent_answer(
            self, client, home, fake, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch)
        raised = self._raise(client, "nightly-sync", ts="2026-09-18T07:30:00+00:00")
        assert raised.status_code == 200, raised.text
        monkeypatch.setenv("AWRISE_HOME", str(tmp_path / "nowhere"))
        resp = client.post(f"/decisions/{raised.json()['id']}/answer",
                           json={"choice": "run_now"})
        assert resp.status_code == 503, resp.text
        assert spawned(fake["capture"]) == []

    def test_the_unknown_recipe_entitlement_matches_the_registry(self):
        """The daemon duplicates the constant so it can deny with the module GONE.

        Duplication is the point (it must still refuse when card_recipes cannot be
        imported), so the two are asserted equal here rather than left to drift.
        """
        import adk.harnesses.daemon as daemon
        from adk.decisions.card_recipes import UNKNOWN_RECIPE_ENTITLEMENT

        assert daemon.UNKNOWN_RECIPE_ENTITLEMENT == UNKNOWN_RECIPE_ENTITLEMENT

    def test_every_spawning_recipe_names_its_entitlement(self):
        """A recipe added later cannot be ungated: the invariant refuses it."""
        from adk.decisions.card_recipes import (
            CARD_RECIPES,
            UNKNOWN_RECIPE_ENTITLEMENT,
            check_all,
            recipe_entitlement,
            spawns_on,
        )

        assert check_all() == []
        for recipe_id, recipe in CARD_RECIPES.items():
            spawns = any(v is not None for v in (recipe.get("steerback") or {}).values())
            if spawns:
                assert recipe_entitlement(recipe_id), f"{recipe_id} spawns and is ungated"
        assert recipe_entitlement("wake-failed") == "wakes:mutate"
        assert recipe_entitlement("nope") == UNKNOWN_RECIPE_ENTITLEMENT
        assert spawns_on("wake-failed", "run_now")
        assert not spawns_on("wake-failed", "keep")
