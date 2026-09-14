"""The well must never hand an agent a confident wrong answer.

Every test here guards a failure mode where the well would still LOOK like it worked.
The one that matters most is the lease filter: the first draft guessed the store's field
names (id/path/until/state instead of lease_id/target/expires_ts/status), so the status
check matched nothing and — because it defaulted to "active" — every record passed. The
store held 644 records of which zero were live, and the well was about to tell every
agent that 644 files were locked by other sessions. An agent that believes a file is
contended backs off from work nobody is doing, so a wrong answer here is worse than an
unavailable one.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from adk.harnesses.rooms import RoomRegistry
from adk.harnesses.well import ContextWell, _parse_iso, git_state, lease_state


def _iso(delta_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _write_store(tmp_path, entries) -> None:
    root = tmp_path / "vcs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "leases.json").write_text(json.dumps({"leases": entries}), encoding="utf-8")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "vcs"))
    return tmp_path


class TestLeaseTruth:
    def test_expired_leases_are_not_reported_as_held(self, store):
        """The defect, exactly: expired records must not count as contention."""
        _write_store(store, [
            {"lease_id": "a", "actor": "peer", "target": "x.py",
             "expires_ts": _iso(-600), "status": "active"},
            {"lease_id": "b", "actor": "peer", "target": "y.py",
             "expires_ts": _iso(-60), "status": "active"},
        ])
        state = lease_state()
        assert state["ok"] is True
        assert state["count"] == 0, "expired leases reported as held"
        assert state["expired_or_released"] == 2

    def test_a_live_lease_is_reported_with_its_target(self, store):
        """The positive half. A lease with no target names no file and is useless."""
        _write_store(store, [
            {"lease_id": "a", "actor": "peer", "target": "lib/core/Thing.py",
             "expires_ts": _iso(300), "status": "active", "reason": "editing"},
        ])
        state = lease_state()
        assert state["count"] == 1
        held = state["leases"][0]
        assert held["target"] == "lib/core/Thing.py"
        assert held["actor"] == "peer"

    def test_released_status_is_excluded(self, store):
        _write_store(store, [
            {"lease_id": "a", "actor": "peer", "target": "x.py",
             "expires_ts": _iso(300), "status": "released"},
        ])
        assert lease_state()["count"] == 0

    def test_unreadable_expiry_is_counted_never_assumed(self, store):
        """Neither provably live nor provably dead — so it is counted, not guessed."""
        _write_store(store, [
            {"lease_id": "a", "actor": "peer", "target": "x.py",
             "expires_ts": "not-a-timestamp", "status": "active"},
        ])
        state = lease_state()
        assert state["count"] == 0
        assert state["unparsable"] == 1

    def test_mutation_guard_the_permissive_filter_reports_everything(self, store):
        """Reproduce the original bug and prove this file would have caught it.

        The old filter read `state` (a field that does not exist) with a default of
        "active", so every record passed regardless of expiry.
        """
        entries = [
            {"lease_id": str(i), "actor": "peer", "target": f"f{i}.py",
             "expires_ts": _iso(-600), "status": "active"}
            for i in range(5)
        ]
        _write_store(store, entries)

        old_style = [e for e in entries if str(e.get("state", "active")).lower() == "active"]
        assert len(old_style) == 5, "the old filter really did pass everything"
        assert lease_state()["count"] == 0, "the fixed filter must pass none of them"

    def test_missing_store_is_unavailable_not_empty(self, tmp_path, monkeypatch):
        """'No lease store' must not read as 'nobody holds anything'."""
        monkeypatch.setenv("VCS_DATA_ROOT", str(tmp_path / "nope"))
        state = lease_state()
        assert state["ok"] is False
        assert "no lease store" in state["reason"].lower()


class TestParseIso:
    def test_round_trips_the_store_format(self):
        assert _parse_iso(_iso(0)) == pytest.approx(time.time(), abs=5)

    def test_returns_none_rather_than_guessing(self):
        for bad in ("", "yesterday", "2026-13-45T99:99:99"):
            assert _parse_iso(bad) is None


class TestGitState:
    def test_a_non_repo_is_reported_with_a_reason(self, tmp_path):
        state = git_state(str(tmp_path))
        assert state["ok"] is False
        assert state["reason"]

    def test_a_missing_directory_is_reported_with_a_reason(self):
        state = git_state("/definitely/not/here")
        assert state["ok"] is False


class TestDraw:
    def test_before_the_first_build_it_says_so(self):
        """It must NOT rebuild inline — that would reintroduce the stall it removes."""
        well = ContextWell(registry=RoomRegistry())
        drawn = well.draw()
        assert drawn["ready"] is False
        assert "not completed its first build" in drawn["reason"]

    def test_render_returns_empty_when_not_ready(self):
        """An empty string is a checkable answer; a fabricated section is not."""
        well = ContextWell(registry=RoomRegistry())
        assert well.render_context() == ""

    def test_a_built_snapshot_always_names_its_sources(self, store, monkeypatch):
        """`sources` is the contract, not debug output."""
        monkeypatch.setenv("AITHER_WELL_FLEET", "0")
        _write_store(store, [])
        well = ContextWell(registry=RoomRegistry())
        well.rebuild()
        drawn = well.draw()
        assert drawn["ready"] is True
        for source in ("git", "leases", "rooms", "fleet"):
            assert source in drawn["sources"], f"source {source} not reported"

    def test_tier_is_host_when_the_fleet_did_not_answer(self, store, monkeypatch):
        """Claiming a tier you did not reach is how a confident empty briefing ships."""
        monkeypatch.setenv("AITHER_WELL_FLEET", "0")
        _write_store(store, [])
        well = ContextWell(registry=RoomRegistry())
        well.rebuild()
        assert well.draw()["tier"] == "host"
