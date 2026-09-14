"""Tests for adk.cron module."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_parse_field_star():
    from adk.cron import _parse_field

    result = _parse_field("*", 0, 59)
    assert result == set(range(0, 60))


def test_parse_field_specific():
    from adk.cron import _parse_field

    assert _parse_field("5", 0, 59) == {5}
    assert _parse_field("0", 0, 59) == {0}


def test_parse_field_range():
    from adk.cron import _parse_field

    assert _parse_field("1-5", 0, 59) == {1, 2, 3, 4, 5}


def test_parse_field_step():
    from adk.cron import _parse_field

    result = _parse_field("*/15", 0, 59)
    assert result == {0, 15, 30, 45}


def test_parse_field_list():
    from adk.cron import _parse_field

    assert _parse_field("1,3,5", 0, 59) == {1, 3, 5}


def test_parse_field_range_step():
    from adk.cron import _parse_field

    assert _parse_field("0-30/10", 0, 59) == {0, 10, 20, 30}


def test_cron_matches():
    from adk.cron import _cron_matches

    dt = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    assert _cron_matches("30 9 * * *", dt) is True
    assert _cron_matches("0 9 * * *", dt) is False
    assert _cron_matches("30 9 15 1 *", dt) is True
    assert _cron_matches("30 9 16 1 *", dt) is False


def test_cron_matches_weekday():
    from adk.cron import _cron_matches

    # 2026-01-15 is a Thursday (weekday=3, but cron uses 0=Sun..6=Sat → Thu=4)
    dt = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    assert _cron_matches("30 9 * * 4", dt) is True
    assert _cron_matches("30 9 * * 1", dt) is False


def test_next_match():
    from adk.cron import _next_match

    after = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    nxt = _next_match("0 10 * * *", after)
    assert nxt is not None
    assert nxt.hour == 10
    assert nxt.minute == 0


def test_scheduler_add_remove():
    from adk.cron import CronScheduler
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        sched = CronScheduler(data_dir=Path(tmpdir))

        def my_task():
            pass

        job = sched.add("*/5 * * * *", my_task, name="test-job")
        assert job.name == "test-job"
        assert len(sched.list_jobs()) == 1

        assert sched.remove("test-job") is True
        assert len(sched.list_jobs()) == 0
        assert sched.remove("nonexistent") is False


def test_scheduler_persistence():
    from adk.cron import CronScheduler
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        sched = CronScheduler(data_dir=Path(tmpdir))
        sched.add("0 9 * * *", None, name="daily-report")
        sched._save()

        # Load in new scheduler
        sched2 = CronScheduler(data_dir=Path(tmpdir))
        sched2._load()
        jobs = sched2.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].name == "daily-report"
        assert jobs[0].expression == "0 9 * * *"
