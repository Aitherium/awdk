"""Claude runner multi-account usage monitor with round-robin scheduler.

This module tracks per-account token usage and cost, manages rate-limit (429)
cooldowns, and provides fair scheduling across multiple Claude Code accounts.

Data persisted to ~/.aither/claude-runner/usage/<profile>.json (0600):
- rolling_input_tokens: cumulative tokens sent to Claude
- rolling_output_tokens: cumulative tokens received from Claude
- rolling_total_cost_usd: cumulative estimated cost in USD
- num_runs: count of completed runs
- last_run_at: ISO timestamp of most recent run
- rate_limit_error_at: ISO timestamp of last 429 error (if any)
- rate_limit_reset_at: ISO timestamp when cooldown expires

Credentials are NEVER persisted — usage records contain only counters/timestamps.
Fail-closed semantics: if no accounts available (all in cooldown), raise error.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from adk.core.logging import get_logger

_log = get_logger("aither_adk.claude_account_usage")

# Default cooldown duration for 429 errors (5 minutes; conservative, no reset-time
# known from API).
DEFAULT_RATE_LIMIT_COOLDOWN_SEC = 300

# Secret patterns — NEVER appear in usage records. Scanned at commit time.
_SECRET_RES = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{8,}",
        r"sk-[A-Za-z0-9_\-]{16,}",
        r"ghp_[A-Za-z0-9]{16,}",
        r"ghs_[A-Za-z0-9]{16,}",
        r"AKIA[0-9A-Z]{12,}",
        r"pk_live_[A-Za-z0-9]{8,}",
        r"sk_live_[A-Za-z0-9]{8,}",
        r"xox[bp]-[A-Za-z0-9\-]{8,}",
        r"aither_sk_live_[A-Za-z0-9_\-]{8,}",
    )
]


class UsageMonitorError(RuntimeError):
    """Raised on usage monitor initialization or scheduling errors."""


def _now_iso() -> str:
    """Current time as ISO 8601 string (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _has_secrets(data: dict[str, Any]) -> bool:
    """Scan a dict for secret patterns (keys and string values)."""
    for key, value in data.items():
        for rx in _SECRET_RES:
            if rx.search(key) or (isinstance(value, str) and rx.search(value)):
                return True
    return False


@dataclass(slots=True)
class UsageRecord:
    """Per-account usage snapshot."""

    profile_name: str
    rolling_input_tokens: int = 0
    rolling_output_tokens: int = 0
    rolling_total_cost_usd: float = 0.0
    num_runs: int = 0
    last_run_at: str = ""
    rate_limit_error_at: str = ""
    rate_limit_reset_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (for storage)."""
        return {
            "profile_name": self.profile_name,
            "rolling_input_tokens": self.rolling_input_tokens,
            "rolling_output_tokens": self.rolling_output_tokens,
            "rolling_total_cost_usd": self.rolling_total_cost_usd,
            "num_runs": self.num_runs,
            "last_run_at": self.last_run_at,
            "rate_limit_error_at": self.rate_limit_error_at,
            "rate_limit_reset_at": self.rate_limit_reset_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        """Deserialize from dict."""
        return cls(
            profile_name=str(data.get("profile_name") or ""),
            rolling_input_tokens=int(data.get("rolling_input_tokens") or 0),
            rolling_output_tokens=int(data.get("rolling_output_tokens") or 0),
            rolling_total_cost_usd=float(data.get("rolling_total_cost_usd") or 0.0),
            num_runs=int(data.get("num_runs") or 0),
            last_run_at=str(data.get("last_run_at") or ""),
            rate_limit_error_at=str(data.get("rate_limit_error_at") or ""),
            rate_limit_reset_at=str(data.get("rate_limit_reset_at") or ""),
        )

    def is_in_cooldown(self) -> bool:
        """Return True if this account is currently rate-limited."""
        if not self.rate_limit_reset_at:
            return False
        try:
            reset_time = datetime.fromisoformat(self.rate_limit_reset_at)
            now = datetime.now(timezone.utc)
            return now < reset_time
        except (ValueError, TypeError):
            return False

    def mark_rate_limited(self, cooldown_sec: int = DEFAULT_RATE_LIMIT_COOLDOWN_SEC) -> None:
        """Record a rate-limit error and set cooldown expiry."""
        self.rate_limit_error_at = _now_iso()
        reset_time = datetime.now(timezone.utc) + timedelta(seconds=cooldown_sec)
        self.rate_limit_reset_at = reset_time.isoformat()


class UsageMonitor:
    """Per-account usage tracker with round-robin / least-loaded scheduling.

    Thread-safe (holds internal lock during load/save). Persists atomically
    to ~/.aither/claude-runner/usage/<profile>.json via tempfile + os.replace().
    """

    def __init__(self, root: Path | None = None) -> None:
        """Initialize monitor with optional root override (for testing).

        Args:
            root: Usage root directory. Defaults to ~/.aither/claude-runner.
        """
        env_root = os.environ.get("AITHER_CLAUDE_RUNNER_ROOT", "")
        self.root = (
            Path(env_root)
            if env_root
            else root
            or (Path.home() / ".aither" / "claude-runner")
        )
        self.usage_dir = self.root / "usage"
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._round_robin_index = 0

    def _usage_path(self, profile_name: str) -> Path:
        """Validate and return path to usage file for a profile."""
        if not profile_name or not profile_name.isidentifier():
            raise UsageMonitorError(f"invalid profile name: {profile_name!r}")
        return self.usage_dir / f"{profile_name}.json"

    def _chmod_0600(self, path: Path) -> None:
        """Best-effort chmod 0600 (owner read/write only).

        On Windows, mode bits are not enforced; OSError is caught gracefully.
        """
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def load(self, profile_name: str) -> UsageRecord:
        """Load usage record for a profile (creates empty if not found).

        Args:
            profile_name: Profile identifier.

        Returns:
            UsageRecord with current/empty state.

        Raises:
            UsageMonitorError: If profile_name is invalid.
        """
        path = self._usage_path(profile_name)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return UsageRecord.from_dict(data)
            except (json.JSONDecodeError, OSError) as e:
                _log.warning(
                    "Failed to load usage record, starting fresh",
                    extra={"profile": profile_name, "err": str(e)},
                )
        return UsageRecord(profile_name=profile_name)

    def save(self, rec: UsageRecord) -> None:
        """Atomically save a usage record (tempfile + os.replace).

        Scans record for secret patterns and rejects if found (fail-closed).

        Args:
            rec: UsageRecord to save.

        Raises:
            UsageMonitorError: If record contains secrets or save fails.
        """
        path = self._usage_path(rec.profile_name)

        # Scan for secrets before commit.
        rec_dict = rec.to_dict()
        if _has_secrets(rec_dict):
            raise UsageMonitorError(
                f"usage record contains secret patterns: {rec.profile_name}"
            )

        # Atomic write: tempfile + os.replace.
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
                suffix=".tmp",
            ) as f:
                json.dump(rec_dict, f, indent=2)
                temp_path = Path(f.name)

            os.replace(temp_path, path)
            self._chmod_0600(path)
        except (OSError, json.JSONEncodeError) as e:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise UsageMonitorError(f"failed to save usage record: {e}") from e

    def record_run(
        self,
        profile_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_cost_usd: float = 0.0,
    ) -> UsageRecord:
        """Record a completed run's token usage and cost.

        Updates rolling counters and num_runs atomically. Validates inputs
        (no negative values, no secrets in profile_name).

        Args:
            profile_name: Profile identifier.
            input_tokens: Tokens sent to Claude (>= 0).
            output_tokens: Tokens received from Claude (>= 0).
            total_cost_usd: Estimated cost in USD (>= 0).

        Returns:
            Updated UsageRecord.

        Raises:
            UsageMonitorError: If inputs invalid or save fails.
        """
        if input_tokens < 0 or output_tokens < 0 or total_cost_usd < 0:
            raise UsageMonitorError(
                "usage counters must be non-negative: "
                f"input_tokens={input_tokens}, output_tokens={output_tokens}, "
                f"total_cost_usd={total_cost_usd}"
            )

        with self._lock:
            rec = self.load(profile_name)
            rec.rolling_input_tokens += input_tokens
            rec.rolling_output_tokens += output_tokens
            rec.rolling_total_cost_usd += total_cost_usd
            rec.num_runs += 1
            rec.last_run_at = _now_iso()
            self.save(rec)
            return rec

    def mark_rate_limited(
        self, profile_name: str, cooldown_sec: int = DEFAULT_RATE_LIMIT_COOLDOWN_SEC
    ) -> UsageRecord:
        """Mark a profile as rate-limited (429 error) and set cooldown.

        Args:
            profile_name: Profile identifier.
            cooldown_sec: Cooldown duration in seconds (default 300 = 5 min).

        Returns:
            Updated UsageRecord.

        Raises:
            UsageMonitorError: If profile invalid or save fails.
        """
        with self._lock:
            rec = self.load(profile_name)
            rec.mark_rate_limited(cooldown_sec)
            self.save(rec)
            return rec

    def select_account(
        self, available_profiles: list[str]
    ) -> str:
        """Select the next account using round-robin / least-loaded.

        Skips profiles currently in rate-limit cooldown (fail-closed: if all
        are in cooldown or available list is empty, raises error).

        Algorithm:
        1. Drop any profile in cooldown (is_in_cooldown() == True).
        2. Among the rest, take the least-loaded set (lowest
           rolling_total_cost_usd, within an epsilon) — steer work toward spare
           capacity so no single account gets hammered toward its limit.
        3. Round-robin WITHIN that tied set (advancing _round_robin_index), so
           equally-loaded accounts — e.g. all fresh at cost 0 — rotate fairly.
        4. If no candidates (empty list or all in cooldown), raise
           UsageMonitorError (fail-closed).

        Args:
            available_profiles: List of profile names to consider.

        Returns:
            Selected profile name.

        Raises:
            UsageMonitorError: If no eligible accounts (empty list or all
                in cooldown).
        """
        if not available_profiles:
            raise UsageMonitorError("no profiles available for scheduling")

        with self._lock:
            # Load usage records for all profiles.
            records = {name: self.load(name) for name in available_profiles}

            # Filter out profiles in cooldown.
            candidates = [
                name for name in available_profiles if not records[name].is_in_cooldown()
            ]

            if not candidates:
                raise UsageMonitorError(
                    f"all {len(available_profiles)} profiles in rate-limit cooldown"
                )

            # Least-loaded PRIMARY (steer work toward the account with the most
            # spare capacity, so we never pile onto one near its limit),
            # round-robin TIE-BREAK among equally-loaded accounts (so fresh
            # accounts — all cost 0 at startup — rotate fairly instead of always
            # hitting the same one). This actually USES _round_robin_index.
            min_cost = min(records[n].rolling_total_cost_usd for n in candidates)
            tied = [
                n for n in candidates
                if records[n].rolling_total_cost_usd <= min_cost + 1e-9
            ]
            selected = tied[self._round_robin_index % len(tied)]
            self._round_robin_index += 1
            return selected

    def list_profiles(self) -> list[UsageRecord]:
        """List all tracked profiles with current usage.

        Returns:
            List of UsageRecords, sorted by profile name.
        """
        records = []
        if self.usage_dir.exists():
            with self._lock:
                for path in sorted(self.usage_dir.glob("*.json")):
                    if path.name.startswith("."):
                        continue
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        records.append(UsageRecord.from_dict(data))
                    except (json.JSONDecodeError, OSError) as e:
                        _log.warning(
                            "Failed to load usage record",
                            extra={"path": str(path), "err": str(e)},
                        )
        return sorted(records, key=lambda r: r.profile_name)

    def get_profile(self, profile_name: str) -> UsageRecord:
        """Get usage record for a single profile.

        Args:
            profile_name: Profile identifier.

        Returns:
            UsageRecord (empty if profile has not been tracked yet).

        Raises:
            UsageMonitorError: If profile_name invalid.
        """
        return self.load(profile_name)
