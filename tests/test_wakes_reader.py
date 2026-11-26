# SPDX-License-Identifier: LicenseRef-Aitherium-Proprietary
# © 2026 Aitherium, LLC. Original work.
"""Pure reader tests for ``adk.wakes`` — the one parser of awrise's on-disk state.

No daemon, no HTTP: these pin what the reader answers for every file shape it can
meet (absent, malformed, unsupported, v1, v2), the daily-vs-flat ledger fallback,
half-written ledger lines, clock liveness and the running-from-ledger rule.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from adk import wakes

FIXTURES = Path(__file__).parent / "fixtures" / "wakes"
NOW = datetime(2026, 9, 18, 7, 42, 0, tzinfo=timezone.utc)


def _home(tmp_path: Path, *, jobs: str = "jobs_v2.json", ledger: bool = True) -> Path:
    home = tmp_path / "awrise"
    home.mkdir()
    if jobs:
        shutil.copy(FIXTURES / jobs, home / "jobs.json")
    if ledger:
        shutil.copytree(FIXTURES / "ledger", home / "ledger")
    return home


# ── absence / malformed / unsupported ────────────────────────────────────────


def test_absent_home_is_not_installed(tmp_path):
    snap = wakes.snapshot(tmp_path / "nowhere", now=NOW)
    assert snap["installed"] is False
    assert snap["wakes"] == []
    assert snap["schema"] is None
    assert snap["last_tick_at"] is None
    assert snap["clock_stale"] is False
    assert snap["error"] is None


def test_home_without_jobs_json_is_not_installed(tmp_path):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "ledger").mkdir()
    snap = wakes.snapshot(home, now=NOW)
    assert snap["installed"] is False and snap["wakes"] == []


def test_malformed_jobs_json_is_installed_with_error(tmp_path):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text("{not json", encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    assert snap["installed"] is True
    assert snap["wakes"] == []
    assert snap["error"].startswith("jobs.json unreadable:")


@pytest.mark.parametrize("raw", [
    {"schema": 3, "jobs": {}},
    {"schema": 2, "jobs": []},
    {"schema": 1},
    {"a": {"interval": "5"}, "b": "not an object"},
    [1, 2, 3],
])
def test_unsupported_shapes_answer_error_not_crash(tmp_path, raw):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps(raw), encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    assert snap["installed"] is True
    assert snap["wakes"] == []
    assert "unreadable" in snap["error"]


# ── v1 mapping ───────────────────────────────────────────────────────────────


def test_v1_fixture_maps_like_awrise_migration(tmp_path):
    home = _home(tmp_path, jobs="jobs_v1.json", ledger=False)
    snap = wakes.snapshot(home, now=NOW)
    assert snap["schema"] == 1
    assert "v1" in snap["migration"] and "awrise list" in snap["migration"]
    by_name = {w["name"]: w for w in snap["wakes"]}
    assert set(by_name) == {"job1", "job2"}
    j1 = by_name["job1"]
    assert j1["interval_s"] == 900.0
    assert j1["every"] == "15m"
    assert j1["run"] == "echo job1 running"
    # naive last_run is stamped UTC and re-rendered tz-aware
    assert j1["last_started_at"] == "2026-08-24T19:54:11.513973+00:00"
    assert j1["last_state"] == "success"
    assert j1["last_reason"] == "migrated_from_v1"
    assert j1["enabled"] is True
    assert j1["consecutive_failures"] == 0
    assert j1["report"] is None and j1["at"] is None and j1["timeout_s"] is None
    assert j1["last_wake_id"] is None
    assert by_name["job2"]["every"] == "2h"
    # no ledger + enabled jobs = a clock that never proved itself
    assert snap["last_tick_at"] is None
    assert snap["clock_stale"] is True


def test_v1_unreadable_interval_marks_only_that_job(tmp_path):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({
        "good": {"interval": "60", "command": "echo"},
        "bad": {"interval": "soon", "command": "echo", "last_run": "garbage"},
    }), encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    assert snap["error"] is None and snap["schema"] == 1
    by_name = {w["name"]: w for w in snap["wakes"]}
    assert by_name["good"]["interval_s"] == 60.0 and by_name["good"]["every"] == "1m"
    assert by_name["bad"]["error"] == "interval unreadable"
    assert by_name["bad"]["interval_s"] is None
    assert by_name["bad"]["last_started_at"] is None
    assert by_name["bad"]["next_due_at"] is None


@pytest.mark.parametrize("seconds,every", [
    (900.0, "15m"), (7200.0, "2h"), (86400.0, "1d"), (604800.0, "1w"),
    (90.0, "90s"), (1.5, "1.5s"), (5400.0, "90m"),
])
def test_seconds_to_every_matches_awrise_rendering(seconds, every):
    assert wakes._seconds_to_every(seconds) == every


# ── v2 + ledger ──────────────────────────────────────────────────────────────


def test_v2_fixture_projection(tmp_path):
    home = _home(tmp_path)
    snap = wakes.snapshot(home, now=NOW)
    assert snap["schema"] == 2 and snap["migration"] is None
    assert snap["count"] == 3 and snap["failing"] == 1 and snap["disabled"] == 1
    assert snap["running"] == 0
    assert snap["last_tick_at"] == "2026-09-18T07:41:00.000000+00:00"
    assert snap["clock_stale"] is False
    ns = next(w for w in snap["wakes"] if w["name"] == "nightly-sync")
    assert ns["last_reason"] == "exit 1"
    assert ns["consecutive_failures"] == 3
    assert ns["report"]["card_id"] == "d-x7k2"
    assert ns["running"] is False and ns["running_wake_id"] is None
    assert ns["next_due_at"] == "2026-09-18T08:00:01.000000+00:00"


def test_next_due_null_for_at_anchored_and_never_run(tmp_path):
    home = _home(tmp_path)
    by_name = {w["name"]: w for w in wakes.snapshot(home, now=NOW)["wakes"]}
    assert by_name["daily-report"]["at"] == "07:00"
    assert by_name["daily-report"]["next_due_at"] is None
    assert by_name["never-ran"]["last_started_at"] is None
    assert by_name["never-ran"]["next_due_at"] is None


def test_state_filters(tmp_path):
    home = _home(tmp_path)
    assert [w["name"] for w in wakes.snapshot(home, now=NOW, state="failing")["wakes"]] == \
        ["nightly-sync"]
    assert [w["name"] for w in wakes.snapshot(home, now=NOW, state="disabled")["wakes"]] == \
        ["never-ran"]
    assert wakes.snapshot(home, now=NOW, state="running")["wakes"] == []


def test_clock_stale_when_newest_tick_is_six_minutes_old(tmp_path):
    home = _home(tmp_path)
    late = NOW + timedelta(minutes=6)
    snap = wakes.snapshot(home, now=late)
    assert snap["last_tick_at"] == "2026-09-18T07:41:00.000000+00:00"
    assert snap["clock_stale"] is True


def test_clock_stale_with_ledger_but_no_tick_row(tmp_path):
    home = _home(tmp_path, ledger=False)
    (home / "ledger").mkdir()
    (home / "ledger" / "2026-09-18.jsonl").write_text(json.dumps({
        "ts": "2026-09-18T07:41:00+00:00", "job": "nightly-sync", "event": "finished",
        "wake_id": "w-1", "state": "success", "reason": "exit 0",
    }) + "\n", encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    assert snap["last_tick_at"] is None and snap["clock_stale"] is True


def test_clock_not_stale_when_nothing_is_enabled_and_no_tick(tmp_path):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {
        "x": {"every": "1h", "interval_s": 3600, "run": "echo", "enabled": False}}}))
    snap = wakes.snapshot(home, now=NOW)
    assert snap["clock_stale"] is False and snap["last_tick_at"] is None


def test_running_derived_from_open_started_row(tmp_path):
    home = _home(tmp_path)
    with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": "2026-09-18T07:41:30+00:00", "job": "nightly-sync", "event": "started",
            "wake_id": "w-open1", "reason": "manual", "pid": 555,
        }) + "\n")
    snap = wakes.snapshot(home, now=NOW)
    ns = next(w for w in snap["wakes"] if w["name"] == "nightly-sync")
    assert ns["running"] is True
    assert ns["running_wake_id"] == "w-open1"
    assert ns["running_since"] == "2026-09-18T07:41:30.000000+00:00"
    assert snap["running"] == 1
    assert [w["name"] for w in wakes.snapshot(home, now=NOW, state="running")["wakes"]] == \
        ["nightly-sync"]


@pytest.mark.parametrize("closing", ["finished", "missed", "reconciled", "orphaned"])
def test_closing_events_end_a_running_wake(tmp_path, closing):
    home = _home(tmp_path)
    with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-09-18T07:41:30+00:00", "job": "nightly-sync",
                             "event": "started", "wake_id": "w-open2", "reason": "manual"})
                 + "\n")
        fh.write(json.dumps({"ts": "2026-09-18T07:41:40+00:00", "job": "nightly-sync",
                             "event": closing, "wake_id": "w-open2", "reason": "x"}) + "\n")
    ns = next(w for w in wakes.snapshot(home, now=NOW)["wakes"] if w["name"] == "nightly-sync")
    assert ns["running"] is False


# ── one malformed value never takes the LIST down ────────────────────────────
#
# Every consumer (awdesk, awask, the Discord bot, AitherDesktop) reads the
# scheduler through snapshot()/get_wake(); a TypeError/OverflowError here was an
# HTTP 500 on GET /wakes, /wakes/count AND /wakes/{name} at once, which every
# surface rendered as "scheduler down" because of ONE bad row or field.


def _append_rows(home: Path, rows: list[dict]) -> None:
    with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_open_started_rows_with_mixed_ts_types_do_not_raise(tmp_path):
    home = _home(tmp_path)
    _append_rows(home, [
        {"event": "started", "job": "nightly-sync", "wake_id": "w-int", "ts": 123},
        {"event": "started", "job": "nightly-sync", "wake_id": "w-str",
         "ts": "2026-09-18T07:42:00+00:00"},
    ])
    snap = wakes.snapshot(home, now=NOW)
    ns = next(w for w in snap["wakes"] if w["name"] == "nightly-sync")
    # the parseable timestamp wins; the int one sorts as the floor
    assert ns["running"] is True and ns["running_wake_id"] == "w-str"
    assert wakes.get_wake("nightly-sync", home, now=NOW)["running_wake_id"] == "w-str"
    # and the other order of the same two rows gives the same answer
    (tmp_path / "second").mkdir()
    home2 = _home(tmp_path / "second")
    _append_rows(home2, [
        {"event": "started", "job": "nightly-sync", "wake_id": "w-str",
         "ts": "2026-09-18T07:42:00+00:00"},
        {"event": "started", "job": "nightly-sync", "wake_id": "w-none", "ts": None},
    ])
    ns2 = next(w for w in wakes.snapshot(home2, now=NOW)["wakes"] if w["name"] == "nightly-sync")
    assert ns2["running_wake_id"] == "w-str"


@pytest.mark.parametrize("wake_id", [[1], {"a": 1}, 7, True])
def test_non_string_wake_id_row_is_ignored_for_liveness(tmp_path, wake_id):
    home = _home(tmp_path)
    _append_rows(home, [{"event": "started", "job": "nightly-sync", "wake_id": wake_id,
                         "ts": "2026-09-18T07:42:00+00:00"}])
    snap = wakes.snapshot(home, now=NOW)
    ns = next(w for w in snap["wakes"] if w["name"] == "nightly-sync")
    assert ns["running"] is False and snap["running"] == 0
    assert wakes.get_wake("nightly-sync", home, now=NOW)["running"] is False


@pytest.mark.parametrize("raw,expected,flagged", [
    ("3", 3, False), (2.0, 2, False), (0, 0, False),
    ([1], 0, True), ("many", 0, True), (True, 0, True), (-1, 0, True), (float("nan"), 0, True),
])
def test_consecutive_failures_is_typed_before_any_comparison(tmp_path, raw, expected, flagged):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {
        "n": {"every": "1h", "interval_s": 3600, "run": "x", "consecutive_failures": raw},
    }}), encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    job = snap["wakes"][0]
    assert job["consecutive_failures"] == expected
    assert (job["error"] == "consecutive_failures unreadable") is flagged
    assert snap["failing"] == (1 if expected >= 1 else 0)
    failing_names = [w["name"] for w in wakes.snapshot(home, now=NOW, state="failing")["wakes"]]
    assert failing_names == (["n"] if expected >= 1 else [])


@pytest.mark.parametrize("interval", ["inf", "1e308", "nan", "-inf", 1e308, True])
def test_v1_unbounded_interval_marks_only_that_job(tmp_path, interval):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({
        "good": {"interval": "60", "command": "echo"},
        "bad": {"interval": interval, "command": "echo"},
    }), encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    assert snap["error"] is None and snap["count"] == 2
    by_name = {w["name"]: w for w in snap["wakes"]}
    assert by_name["good"]["every"] == "1m"
    assert by_name["bad"]["error"] == "interval unreadable"
    assert by_name["bad"]["interval_s"] is None and by_name["bad"]["next_due_at"] is None


@pytest.mark.parametrize("interval_s", [1e308, float("inf"), 1e17, "inf", "soon"])
def test_v2_unbounded_interval_projects_no_due_time_and_does_not_raise(tmp_path, interval_s):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {
        "n": {"every": "x", "interval_s": interval_s, "run": "x",
              "last_started_at": "2026-09-18T07:00:00+00:00"},
        "ok": {"every": "1h", "interval_s": 3600, "run": "x",
               "last_started_at": "2026-09-18T07:00:00+00:00"},
    }}), encoding="utf-8")
    snap = wakes.snapshot(home, now=NOW)
    by_name = {w["name"]: w for w in snap["wakes"]}
    assert by_name["n"]["next_due_at"] is None
    assert by_name["n"]["error"] == "interval unreadable"
    assert by_name["ok"]["next_due_at"] == "2026-09-18T08:00:00.000000+00:00"
    assert by_name["ok"]["error"] is None
    assert wakes.get_wake("n", home, now=NOW)["next_due_at"] is None


def test_next_due_survives_a_last_started_at_near_datetime_max(tmp_path):
    job = {"at": None, "last_started_at": "9999-12-31T00:00:00+00:00", "interval_s": 86400 * 2}
    assert wakes._next_due_at(job) is None


def test_max_interval_is_below_every_overflow():
    # the ceiling itself must be renderable and projectable
    assert wakes._seconds_to_every(wakes.MAX_INTERVAL_S).endswith(("s", "m", "h", "d", "w"))
    assert wakes._next_due_at({"at": None, "last_started_at": "2026-09-18T07:00:00+00:00",
                               "interval_s": wakes.MAX_INTERVAL_S}) is not None


# ── ledger reading ───────────────────────────────────────────────────────────


def test_ledger_newest_first_and_projection(tmp_path):
    home = _home(tmp_path)
    out = wakes.read_ledger(home)
    assert out["count"] == 7 and out["skipped"] == 0
    assert out["rows"][0]["event"] == "tick"
    assert out["rows"][0]["ts"] == "2026-09-18T07:41:00.000000+00:00"
    fin = next(r for r in out["rows"] if r["event"] == "finished" and r["job"] == "nightly-sync")
    assert fin["exit_code"] == 1 and fin["output_tail"] == "Traceback: boom"
    assert set(fin) == {"ts", "job", "event", "state", "reason", "wake_id", "pass_id",
                        "duration_s", "exit_code", "output_tail", "pid", "why", "cause"}
    # a tick row has no state/exit_code — projected as None, never KeyError
    assert out["rows"][0]["state"] is None and out["rows"][0]["exit_code"] is None


def test_ledger_filters_and_limit_cap(tmp_path):
    home = _home(tmp_path)
    assert wakes.read_ledger(home, job="nightly-sync")["count"] == 2
    assert wakes.read_ledger(home, event="tick")["count"] == 2
    assert wakes.read_ledger(home, since="2026-09-18T07:30:00+00:00")["count"] == 1
    assert wakes.read_ledger(home, limit=1)["count"] == 1
    assert wakes.read_ledger(home, limit=100000)["count"] == 7  # capped, not an error


def test_half_written_last_line_is_skipped_and_counted(tmp_path):
    home = _home(tmp_path)
    with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-18T07:42:00+00:00", "event": "tick", "reas')
    out = wakes.read_ledger(home)
    assert out["count"] == 7
    assert out["skipped"] == 1
    snap = wakes.snapshot(home, now=NOW)
    assert snap["ledger_skipped"] == 1
    assert snap["last_tick_at"] == "2026-09-18T07:41:00.000000+00:00"


def test_non_object_ledger_line_is_skipped(tmp_path):
    home = _home(tmp_path)
    with open(home / "ledger" / "2026-09-18.jsonl", "a", encoding="utf-8") as fh:
        fh.write('[1, 2, 3]\n')
    assert wakes.read_ledger(home)["skipped"] == 1


def test_daily_files_newest_first_beat_flat_ledger(tmp_path):
    home = _home(tmp_path)
    (home / "ledger" / "2026-09-17.jsonl").write_text(json.dumps({
        "ts": "2026-09-17T23:59:00+00:00", "event": "tick", "reason": "old"}) + "\n")
    (home / "ledger.jsonl").write_text(json.dumps({
        "ts": "2026-09-18T07:50:00+00:00", "event": "tick", "reason": "flat"}) + "\n")
    files = wakes.ledger_files(home)
    assert [p.name for p in files] == ["2026-09-18.jsonl", "2026-09-17.jsonl"]
    rows = wakes.read_ledger(home)["rows"]
    assert rows[-1]["reason"] == "old"
    assert wakes.snapshot(home, now=NOW)["last_tick_at"] == "2026-09-18T07:41:00.000000+00:00"


def test_flat_ledger_fallback(tmp_path):
    home = _home(tmp_path, ledger=False)
    (home / "ledger.jsonl").write_text(json.dumps({
        "ts": "2026-09-18T07:41:30+00:00", "event": "tick", "reason": "flat"}) + "\n")
    assert [p.name for p in wakes.ledger_files(home)] == ["ledger.jsonl"]
    snap = wakes.snapshot(home, now=NOW)
    assert snap["last_tick_at"] == "2026-09-18T07:41:30.000000+00:00"
    assert snap["clock_stale"] is False


# ── get_wake ─────────────────────────────────────────────────────────────────


def test_get_wake_recent_rows_newest_first(tmp_path):
    home = _home(tmp_path)
    job = wakes.get_wake("nightly-sync", home, now=NOW)
    assert job["name"] == "nightly-sync"
    assert [r["event"] for r in job["recent"]] == ["finished", "started"]
    assert job["recent"][0]["output_tail"] == "Traceback: boom"
    assert wakes.get_wake("nope", home, now=NOW) is None
    assert wakes.get_wake("../x", home, now=NOW) is None


# ── name gate and argv ───────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "-name", "../x", "a b", "a/b", ".hidden", "x" * 65, None, 3])
def test_invalid_names_refused(bad):
    assert wakes.valid_name(bad) is False
    with pytest.raises(ValueError):
        wakes.build_argv("/bin/awrise", "run", bad)  # type: ignore[arg-type]


def test_argv_is_a_list_and_verbs_are_closed():
    assert wakes.build_argv("/bin/awrise", "disable", "nightly-sync") == \
        ["/bin/awrise", "disable", "--name", "nightly-sync"]
    with pytest.raises(ValueError):
        wakes.build_argv("/bin/awrise", "remove", "nightly-sync")


def test_resolve_bin_requires_an_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AWRISE_BIN", str(tmp_path / "missing"))
    assert wakes.resolve_bin() is None
    monkeypatch.setenv("AWRISE_BIN", str(tmp_path))  # a directory is not a CLI
    assert wakes.resolve_bin() is None
    real = tmp_path / "awrise"
    real.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AWRISE_BIN", str(real))
    assert Path(wakes.resolve_bin()) == real.resolve()


def test_reader_never_writes_under_home(tmp_path):
    home = _home(tmp_path, jobs="jobs_v1.json")
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    wakes.snapshot(home, now=NOW)
    wakes.get_wake("job1", home, now=NOW)
    wakes.read_ledger(home)
    after = sorted(p.relative_to(home) for p in home.rglob("*"))
    assert before == after
    assert json.loads((home / "jobs.json").read_text())["job1"]["interval"] == "900.0"


# ── parity with awrise (test-only import; skips when the package is absent) ──


def test_next_due_parity_with_awrise():
    awrise_clock = pytest.importorskip("awrise.clock")
    now = NOW
    interval_job = {"at": None, "last_started_at": "2026-09-18T07:00:01+00:00",
                    "interval_s": 3600.0}
    theirs = awrise_clock.next_due(interval_job, now)
    ours = wakes._next_due_at(interval_job)
    assert ours == theirs.isoformat(timespec="microseconds")
    # anchored and never-run: awrise computes something; we deliberately answer null
    assert wakes._next_due_at({"at": "07:00", "last_started_at": "2026-09-18T07:00:00+00:00",
                               "interval_s": 86400.0}) is None
    assert awrise_clock.next_due({"at": "07:00", "last_started_at": "2026-09-18T07:00:00+00:00",
                                  "interval_s": 86400.0}, now) is not None
    assert wakes._next_due_at({"at": None, "last_started_at": None, "interval_s": 60.0}) is None


def test_unaddressable_job_name_is_listed_but_flagged(tmp_path):
    home = tmp_path / "awrise"
    home.mkdir()
    (home / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {
        "-weird name": {"every": "1h", "interval_s": 3600, "run": "echo"},
        "fine": {"every": "1h", "interval_s": 3600, "run": "echo"}}}))
    snap = wakes.snapshot(home, now=NOW)
    assert snap["count"] == 2
    by_name = {w["name"]: w for w in snap["wakes"]}
    assert by_name["-weird name"]["error"] == "name not addressable"
    assert by_name["fine"]["error"] is None
    assert wakes.get_wake("-weird name", home, now=NOW) is None
