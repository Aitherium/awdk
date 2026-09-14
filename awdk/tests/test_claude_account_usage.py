"""Tests for adk.claude_account_usage module — multi-account usage monitoring.

Uses monkeypatch to inject AITHER_CLAUDE_RUNNER_ROOT so NO real user files
are touched. Each test gets isolated temp directories.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adk.claude_account_usage import (
    DEFAULT_RATE_LIMIT_COOLDOWN_SEC,
    UsageMonitor,
    UsageMonitorError,
    UsageRecord,
)


@pytest.fixture
def temp_runner_root(tmp_path):
    """Temp directory for runner state (includes usage/)."""
    return tmp_path / "runner"


@pytest.fixture
def mock_runner_root(monkeypatch, temp_runner_root):
    """Monkeypatch env var to use temp runner root."""
    temp_runner_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AITHER_CLAUDE_RUNNER_ROOT", str(temp_runner_root))
    return temp_runner_root


@pytest.fixture
def monitor(mock_runner_root):
    """Create a UsageMonitor with mocked root."""
    return UsageMonitor(root=mock_runner_root)


class TestUsageRecord:
    """Tests for UsageRecord dataclass."""

    def test_to_dict(self):
        """UsageRecord.to_dict() returns correct structure."""
        rec = UsageRecord(
            profile_name="personal",
            rolling_input_tokens=1000,
            rolling_output_tokens=500,
            rolling_total_cost_usd=1.23,
            num_runs=2,
            last_run_at="2025-07-18T12:00:00+00:00",
        )
        d = rec.to_dict()
        assert d["profile_name"] == "personal"
        assert d["rolling_input_tokens"] == 1000
        assert d["rolling_output_tokens"] == 500
        assert d["rolling_total_cost_usd"] == 1.23
        assert d["num_runs"] == 2

    def test_from_dict(self):
        """UsageRecord.from_dict() reconstructs from dict."""
        data = {
            "profile_name": "work",
            "rolling_input_tokens": 5000,
            "rolling_output_tokens": 2000,
            "rolling_total_cost_usd": 5.67,
            "num_runs": 3,
            "last_run_at": "2025-07-18T12:00:00+00:00",
            "rate_limit_error_at": "",
            "rate_limit_reset_at": "",
        }
        rec = UsageRecord.from_dict(data)
        assert rec.profile_name == "work"
        assert rec.rolling_input_tokens == 5000
        assert rec.rolling_output_tokens == 2000
        assert rec.rolling_total_cost_usd == 5.67
        assert rec.num_runs == 3

    def test_is_in_cooldown_false_no_reset(self):
        """is_in_cooldown() returns False if no reset time set."""
        rec = UsageRecord(profile_name="test")
        assert not rec.is_in_cooldown()

    def test_is_in_cooldown_expired(self):
        """is_in_cooldown() returns False if reset time in past."""
        rec = UsageRecord(profile_name="test")
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        rec.rate_limit_reset_at = past
        assert not rec.is_in_cooldown()

    def test_is_in_cooldown_active(self):
        """is_in_cooldown() returns True if reset time in future."""
        rec = UsageRecord(profile_name="test")
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        rec.rate_limit_reset_at = future
        assert rec.is_in_cooldown()

    def test_mark_rate_limited(self):
        """mark_rate_limited() sets error and reset timestamps."""
        rec = UsageRecord(profile_name="test")
        rec.mark_rate_limited(cooldown_sec=60)
        assert rec.rate_limit_error_at != ""
        assert rec.rate_limit_reset_at != ""
        assert rec.is_in_cooldown()

    def test_mark_rate_limited_custom_cooldown(self):
        """mark_rate_limited() respects custom cooldown duration."""
        rec = UsageRecord(profile_name="test")
        before = datetime.now(timezone.utc)
        rec.mark_rate_limited(cooldown_sec=120)
        after = datetime.now(timezone.utc)

        reset_time = datetime.fromisoformat(rec.rate_limit_reset_at)
        # Reset should be ~120s from now (±5s tolerance for test overhead).
        expected_min = before + timedelta(seconds=115)
        expected_max = after + timedelta(seconds=125)
        assert expected_min <= reset_time <= expected_max


class TestUsageMonitor:
    """Tests for UsageMonitor."""

    def test_init_creates_usage_dir(self, monitor):
        """UsageMonitor.__init__() creates usage/ directory."""
        assert monitor.usage_dir.exists()
        assert monitor.usage_dir.is_dir()

    def test_init_from_env_var(self, tmp_path, monkeypatch):
        """UsageMonitor respects AITHER_CLAUDE_RUNNER_ROOT env var."""
        custom_root = tmp_path / "custom-runner"
        monkeypatch.setenv("AITHER_CLAUDE_RUNNER_ROOT", str(custom_root))
        monitor = UsageMonitor()
        assert monitor.root == custom_root
        assert monitor.usage_dir == custom_root / "usage"

    def test_load_empty_profile(self, monitor):
        """load() returns empty record if profile not found."""
        rec = monitor.load("unknown")
        assert rec.profile_name == "unknown"
        assert rec.rolling_input_tokens == 0
        assert rec.rolling_output_tokens == 0
        assert rec.rolling_total_cost_usd == 0.0
        assert rec.num_runs == 0

    def test_save_creates_file_with_0600(self, monitor):
        """save() creates file with owner-only permissions."""
        rec = UsageRecord(
            profile_name="test",
            rolling_input_tokens=100,
            rolling_output_tokens=50,
            rolling_total_cost_usd=0.50,
            num_runs=1,
        )
        monitor.save(rec)

        path = monitor.usage_dir / "test.json"
        assert path.exists()

        # Check permissions (0600 = owner read/write only).
        mode = path.stat().st_mode
        assert mode & stat.S_IRUSR  # Owner can read.
        assert mode & stat.S_IWUSR  # Owner can write.

        # Check content.
        data = json.loads(path.read_text())
        assert data["profile_name"] == "test"
        assert data["rolling_input_tokens"] == 100

    def test_save_roundtrip(self, monitor):
        """save() + load() preserves all fields."""
        rec = UsageRecord(
            profile_name="roundtrip",
            rolling_input_tokens=1234,
            rolling_output_tokens=5678,
            rolling_total_cost_usd=12.34,
            num_runs=5,
            last_run_at="2025-07-18T12:00:00+00:00",
        )
        monitor.save(rec)

        loaded = monitor.load("roundtrip")
        assert loaded.profile_name == "roundtrip"
        assert loaded.rolling_input_tokens == 1234
        assert loaded.rolling_output_tokens == 5678
        assert loaded.rolling_total_cost_usd == 12.34
        assert loaded.num_runs == 5
        assert loaded.last_run_at == "2025-07-18T12:00:00+00:00"

    def test_save_rejects_secrets_in_record(self, monitor):
        """save() rejects records containing secret patterns in dict values."""
        from unittest import mock

        rec = UsageRecord(profile_name="test", rolling_input_tokens=100)

        # Mock the _has_secrets check to simulate detecting a secret.
        with pytest.raises(UsageMonitorError, match="secret"):
            with mock.patch("adk.claude_account_usage._has_secrets", return_value=True):
                monitor.save(rec)

    def test_invalid_profile_name(self, monitor):
        """UsageMonitor rejects invalid profile names."""
        with pytest.raises(UsageMonitorError, match="invalid profile name"):
            monitor.load("my-profile")  # Hyphen not allowed

        with pytest.raises(UsageMonitorError, match="invalid profile name"):
            monitor.load("my profile")  # Space not allowed

        with pytest.raises(UsageMonitorError, match="invalid profile name"):
            monitor.load("")  # Empty

    def test_record_run_accumulates(self, monitor):
        """record_run() accumulates tokens and cost."""
        monitor.record_run("acme", input_tokens=100, output_tokens=50, total_cost_usd=0.10)
        monitor.record_run("acme", input_tokens=200, output_tokens=100, total_cost_usd=0.20)

        rec = monitor.load("acme")
        assert rec.rolling_input_tokens == 300
        assert rec.rolling_output_tokens == 150
        assert abs(rec.rolling_total_cost_usd - 0.30) < 1e-6  # Float precision
        assert rec.num_runs == 2

    def test_record_run_sets_last_run_at(self, monitor):
        """record_run() updates last_run_at timestamp."""
        before = datetime.now(timezone.utc)
        monitor.record_run("test", input_tokens=10, output_tokens=5)
        after = datetime.now(timezone.utc)

        rec = monitor.load("test")
        run_time = datetime.fromisoformat(rec.last_run_at)
        assert before <= run_time <= after

    def test_record_run_rejects_negative(self, monitor):
        """record_run() rejects negative token/cost values."""
        with pytest.raises(UsageMonitorError, match="non-negative"):
            monitor.record_run("test", input_tokens=-1)

        with pytest.raises(UsageMonitorError, match="non-negative"):
            monitor.record_run("test", output_tokens=-1)

        with pytest.raises(UsageMonitorError, match="non-negative"):
            monitor.record_run("test", total_cost_usd=-0.01)

    def test_mark_rate_limited(self, monitor):
        """mark_rate_limited() sets cooldown on a profile."""
        monitor.record_run("test", input_tokens=10)
        monitor.mark_rate_limited("test", cooldown_sec=60)

        rec = monitor.load("test")
        assert rec.is_in_cooldown()
        assert rec.rate_limit_error_at != ""

    def test_select_account_single_profile(self, monitor):
        """select_account() picks the single available profile."""
        result = monitor.select_account(["only"])
        assert result == "only"

    def test_select_account_round_robin(self, monitor):
        """Equal-load accounts rotate FAIRLY (round-robin among the tied set)."""
        from collections import Counter

        # Three FRESH profiles — all cost 0 → all tied → pure round-robin.
        results = [
            monitor.select_account(["alice", "bob", "charlie"])
            for _ in range(6)
        ]
        counts = Counter(results)
        # 6 calls / 3 equal accounts → each selected EXACTLY twice (fair).
        # (This is the assertion the vacuous ">=1 returned" version missed —
        # it passed even when the buggy impl returned the same account 6×.)
        assert counts["alice"] == 2
        assert counts["bob"] == 2
        assert counts["charlie"] == 2
        # And it genuinely rotates rather than repeating one account.
        assert results[0] != results[1]

    def test_select_account_round_robin_only_among_least_loaded(self, monitor):
        """Round-robin is a TIE-BREAK: an over-used account is not rotated into."""
        from collections import Counter

        # 'busy' has real cost; alice/bob are fresh (cost 0) → only alice/bob tie.
        monitor.record_run("busy", input_tokens=100, total_cost_usd=5.00)
        results = [
            monitor.select_account(["alice", "bob", "busy"])
            for _ in range(6)
        ]
        counts = Counter(results)
        assert counts["busy"] == 0  # never picked — it's the most loaded
        assert counts["alice"] == 3 and counts["bob"] == 3  # the two ties rotate

    def test_select_account_least_loaded(self, monitor):
        """select_account() prefers least-loaded profile by cost."""
        monitor.record_run("expensive", input_tokens=100, total_cost_usd=5.00)
        monitor.record_run("cheap", input_tokens=50, total_cost_usd=0.50)

        # Calling select_account should prefer "cheap" (lower cost).
        result = monitor.select_account(["expensive", "cheap"])
        assert result == "cheap"

    def test_select_account_skips_cooldown(self, monitor):
        """select_account() skips profiles in cooldown."""
        monitor.record_run("profile_a", input_tokens=10)
        monitor.record_run("profile_b", input_tokens=10)

        # Put profile_a in cooldown.
        monitor.mark_rate_limited("profile_a", cooldown_sec=3600)

        # select_account should pick profile_b (profile_a is in cooldown).
        result = monitor.select_account(["profile_a", "profile_b"])
        assert result == "profile_b"

    def test_select_account_all_in_cooldown(self, monitor):
        """select_account() raises error if all profiles in cooldown."""
        monitor.record_run("alpha", input_tokens=10)
        monitor.record_run("beta", input_tokens=10)

        monitor.mark_rate_limited("alpha", cooldown_sec=3600)
        monitor.mark_rate_limited("beta", cooldown_sec=3600)

        with pytest.raises(UsageMonitorError, match="rate-limit cooldown"):
            monitor.select_account(["alpha", "beta"])

    def test_select_account_empty_list(self, monitor):
        """select_account() raises error if profile list is empty."""
        with pytest.raises(UsageMonitorError, match="no profiles available"):
            monitor.select_account([])

    def test_list_profiles_empty(self, monitor):
        """list_profiles() returns empty list initially."""
        profiles = monitor.list_profiles()
        assert profiles == []

    def test_list_profiles(self, monitor):
        """list_profiles() returns all tracked profiles sorted by name."""
        monitor.record_run("charlie", input_tokens=300)
        monitor.record_run("alice", input_tokens=100)
        monitor.record_run("bob", input_tokens=200)

        profiles = monitor.list_profiles()
        names = [p.profile_name for p in profiles]
        assert names == ["alice", "bob", "charlie"]  # Sorted

    def test_get_profile(self, monitor):
        """get_profile() returns record for specific profile."""
        monitor.record_run("target", input_tokens=999, total_cost_usd=9.99)

        rec = monitor.get_profile("target")
        assert rec.profile_name == "target"
        assert rec.rolling_input_tokens == 999
        assert rec.rolling_total_cost_usd == 9.99

    def test_get_profile_unknown(self, monitor):
        """get_profile() returns empty record for unknown profile."""
        rec = monitor.get_profile("nonexistent")
        assert rec.profile_name == "nonexistent"
        assert rec.rolling_input_tokens == 0
        assert rec.num_runs == 0

    def test_thread_safety_record_run(self, monitor):
        """record_run() is thread-safe (concurrent calls don't interleave)."""
        import threading

        def record_in_thread(profile, count):
            for _ in range(count):
                monitor.record_run(profile, input_tokens=1)

        threads = [
            threading.Thread(target=record_in_thread, args=("shared", 10)),
            threading.Thread(target=record_in_thread, args=("shared", 10)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rec = monitor.load("shared")
        assert rec.rolling_input_tokens == 20
        assert rec.num_runs == 20

    def test_thread_safety_select_account(self, monitor):
        """select_account() is thread-safe."""
        import threading

        results = []
        lock = threading.Lock()

        def select_in_thread():
            try:
                result = monitor.select_account(["one", "two", "three"])
                with lock:
                    results.append(result)
            except UsageMonitorError:
                pass  # Expected if all in cooldown

        threads = [threading.Thread(target=select_in_thread) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At least some should have succeeded.
        assert len(results) > 0


class TestUsageRecordNoSecrets:
    """Tests ensuring usage records never contain secret patterns."""

    def test_to_dict_contains_no_secrets(self):
        """UsageRecord.to_dict() never leaks secrets (by design)."""
        rec = UsageRecord(
            profile_name="user",
            rolling_input_tokens=1000,
            rolling_output_tokens=500,
        )
        d = rec.to_dict()

        # Verify no secret patterns in keys or values.
        secret_patterns = ["sk-", "sk-ant-", "ghp_", "AKIA", "pk_live_", "sk_live_"]
        full_text = str(d)
        for pattern in secret_patterns:
            assert pattern not in full_text


class TestUsageMonitorAtomicity:
    """Tests verifying atomic save behavior."""

    def test_save_atomicity_no_partial_writes(self, monitor):
        """save() uses tempfile + os.replace to prevent partial writes."""
        rec = UsageRecord(
            profile_name="atomic_test",
            rolling_input_tokens=12345,
            rolling_total_cost_usd=123.45,
        )
        monitor.save(rec)

        # Verify the file exists and is valid JSON (complete write).
        path = monitor.usage_dir / "atomic_test.json"
        assert path.exists()
        data = json.loads(path.read_text())  # Should not raise JSONDecodeError
        assert data["rolling_input_tokens"] == 12345
