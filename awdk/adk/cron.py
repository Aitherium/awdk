"""Standalone cron scheduler for ADK agents."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("adk.cron")

_FIELD_RANGES: list[tuple[int, int]] = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0=Sunday)
]

_MAX_SCAN_MINUTES = 366 * 24 * 60  # one leap-year of minutes


def _parse_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field (*, */N, N, N-M, N-M/S, comma lists) into matching ints."""
    result: set[int] = set()

    for part in field_str.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Empty element in cron field: {field_str!r}")

        # --- */N or * ---
        m = re.fullmatch(r"\*(?:/(\d+))?", part)
        if m:
            step = int(m.group(1)) if m.group(1) else 1
            if step == 0:
                raise ValueError("Step value must not be zero")
            result.update(range(min_val, max_val + 1, step))
            continue

        # --- N-M or N-M/S ---
        m = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            step = int(m.group(3)) if m.group(3) else 1
            if step == 0:
                raise ValueError("Step value must not be zero")
            if lo < min_val or hi > max_val or lo > hi:
                raise ValueError(
                    f"Range {lo}-{hi} out of bounds [{min_val}-{max_val}]"
                )
            result.update(range(lo, hi + 1, step))
            continue

        # --- plain integer ---
        m = re.fullmatch(r"(\d+)", part)
        if m:
            val = int(m.group(1))
            if val < min_val or val > max_val:
                raise ValueError(
                    f"Value {val} out of bounds [{min_val}-{max_val}]"
                )
            result.add(val)
            continue

        raise ValueError(f"Unrecognised cron field token: {part!r}")

    return result


def _cron_matches(expression: str, dt: datetime) -> bool:
    """Return True if all five cron fields match *dt*."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron expression must have 5 fields, got {len(fields)}: {expression!r}"
        )

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    # Python weekday: Mon=0 .. Sun=6.  Cron weekday: Sun=0 .. Sat=6.
    values[4] = (values[4] + 1) % 7

    for token, val, (lo, hi) in zip(fields, values, _FIELD_RANGES):
        if val not in _parse_field(token, lo, hi):
            return False
    return True


def _next_match(expression: str, after: datetime) -> datetime:
    """Scan forward minute-by-minute (up to 366 days) for the next matching datetime."""
    # Start from the next whole minute after *after*.
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(_MAX_SCAN_MINUTES):
        if _cron_matches(expression, candidate):
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(
        f"No match for {expression!r} within 366 days after {after.isoformat()}"
    )


@dataclass
class CronJob:
    """A single scheduled job."""

    name: str
    expression: str
    task: Callable[[], Any] | None = None
    last_run: datetime | None = None
    next_run: datetime | None = None
    enabled: bool = True


class CronScheduler:
    """Async cron scheduler with JSON persistence.

    Args:
        data_dir: Directory for ``cron.json``.  Defaults to ``~/.aither``.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        # GATED: autonomous scheduled (unattended) execution is a paid-tier
        # capability. Raises LicenseError unless entitled (or enforcement
        # disabled / tier INTERNAL).
        try:
            from adk.licensing import get_license_manager
            get_license_manager().require("cron", friendly="Autonomous scheduling")
        except ImportError:
            pass

        self._jobs: dict[str, CronJob] = {}
        self._data_dir = data_dir or Path.home() / ".aither"
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, expression: str, task: Callable[..., Any], name: str = "") -> CronJob:
        """Register a cron job.  Raises ValueError on bad expression or duplicate name."""
        # Validate expression eagerly.
        _cron_matches(expression, datetime.now(tz=timezone.utc))

        if not name:
            name = f"job_{len(self._jobs) + 1}"
        if name in self._jobs:
            raise ValueError(f"Job {name!r} already exists")

        now = datetime.now(tz=timezone.utc)
        job = CronJob(
            name=name,
            expression=expression,
            task=task,
            next_run=_next_match(expression, now),
        )
        self._jobs[name] = job
        self._save()
        logger.info("Added job %s [%s], next run %s", name, expression, job.next_run)
        return job

    def remove(self, name: str) -> bool:
        """Remove a job by name.  Returns True if found and removed."""
        if name not in self._jobs:
            return False
        del self._jobs[name]
        self._save()
        logger.info("Removed job %s", name)
        return True

    def list_jobs(self) -> list[CronJob]:
        """Return a snapshot of all registered jobs."""
        return list(self._jobs.values())

    async def start(self) -> None:
        """Start the scheduler loop (checks every 30 seconds)."""
        if self._running:
            logger.warning("Scheduler already running")
            return
        self._load()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started (%d jobs loaded)", len(self._jobs))

    async def stop(self) -> None:
        """Cancel the scheduler loop and persist state."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._save()
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main tick loop, runs until cancelled."""
        while self._running:
            now = datetime.now(tz=timezone.utc)
            for job in list(self._jobs.values()):
                if not job.enabled or job.task is None:
                    continue
                if job.next_run is not None and now >= job.next_run:
                    await self._fire(job, now)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break

    async def _fire(self, job: CronJob, now: datetime) -> None:
        """Execute a job and update its bookkeeping."""
        logger.info("Firing job %s", job.name)
        try:
            result = job.task()
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        except Exception:
            logger.exception("Job %s raised an exception", job.name)
        job.last_run = now
        try:
            job.next_run = _next_match(job.expression, now)
        except ValueError:
            logger.error("Cannot compute next run for %s, disabling", job.name)
            job.enabled = False
        self._save()

    # ------------------------------------------------------------------
    # Persistence (names + expressions only; callables are not serialised)
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Persist job metadata to ``cron.json``."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / "cron.json"
        payload: list[dict[str, Any]] = []
        for job in self._jobs.values():
            payload.append({
                "name": job.name,
                "expression": job.expression,
                "enabled": job.enabled,
                "last_run": job.last_run.isoformat() if job.last_run else None,
                "next_run": job.next_run.isoformat() if job.next_run else None,
            })
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load(self) -> None:
        """Load job metadata from ``cron.json``.  Unknown names become disabled stubs."""
        path = self._data_dir / "cron.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load %s: %s", path, exc)
            return

        for entry in data:
            name = entry.get("name", "")
            if not name:
                continue
            expr = entry.get("expression", "")
            last_raw = entry.get("last_run")
            next_raw = entry.get("next_run")
            enabled = entry.get("enabled", True)

            last_run = (
                datetime.fromisoformat(last_raw) if last_raw else None
            )

            if name in self._jobs:
                # Merge persisted state into the live job.
                job = self._jobs[name]
                job.last_run = last_run
                job.enabled = enabled
                try:
                    job.next_run = _next_match(
                        job.expression, datetime.now(tz=timezone.utc)
                    )
                except ValueError:
                    job.enabled = False
            else:
                # Stub entry (no callable).
                next_run = (
                    datetime.fromisoformat(next_raw) if next_raw else None
                )
                self._jobs[name] = CronJob(
                    name=name,
                    expression=expr,
                    task=None,
                    last_run=last_run,
                    next_run=next_run,
                    enabled=False,  # no task -> cannot fire
                )
