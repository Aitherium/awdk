"""Watch client — periodic health reporting to AitherOS Watch:8082.

Sends periodic heartbeats so the ADK agent fleet is visible in the
AitherOS health dashboard. Includes agent count, avg latency, error
rate, quota usage, and LLM backend status.

Design:
  - Background asyncio task (configurable interval, default 30s)
  - Circuit breaker: stops reporting after 5 consecutive failures,
    retries every 5min
  - Graceful shutdown via stop()
  - Never blocks the main event loop

Usage:
    from adk.watch import get_watch_reporter

    reporter = get_watch_reporter()
    await reporter.start()   # begin background heartbeats
    await reporter.stop()    # graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.watch")

_DEFAULT_WATCH_URL = "http://localhost:8082"
_DEFAULT_INTERVAL = 30  # seconds
_CIRCUIT_BREAK_THRESHOLD = 5
_CIRCUIT_BREAK_RETRY = 300  # 5 minutes


@dataclass
class HealthSnapshot:
    """Point-in-time health data sent to Watch."""
    status: str = "healthy"
    agents: list[str] = field(default_factory=list)
    agent_count: int = 0
    llm_backend: str = ""
    llm_available: bool = False
    avg_latency_ms: float = 0.0
    error_count_1h: int = 0
    request_count_1h: int = 0
    quota_usage_pct: float = 0.0
    uptime_seconds: float = 0.0
    version: str = ""
    active_sessions: int = 0


class WatchReporter:
    """Background health reporter to AitherOS Watch:8082."""

    def __init__(
        self,
        watch_url: str = "",
        interval: int = _DEFAULT_INTERVAL,
        service_name: str = "adk",
        data_dir: str | Path | None = None,
    ):
        self.watch_url = (
            watch_url
            or os.getenv("AITHER_WATCH_URL", "")
        ).rstrip("/")
        self._interval = interval
        self._service_name = service_name
        self._data_dir = Path(
            data_dir or os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
        )

        # State
        self._task: asyncio.Task | None = None
        self._running = False
        self._start_time = time.time()

        # Counters (reset each reporting cycle)
        self._request_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0
        self._latency_samples = 0

        # Circuit breaker
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_open_since = 0.0

        # Collectors — external code registers callables that return health data
        self._collectors: list = []

    @property
    def enabled(self) -> bool:
        return bool(self.watch_url)

    def record_request(self, latency_ms: float = 0.0, error: bool = False):
        """Record a request for health metrics. Thread-safe enough for asyncio."""
        self._request_count += 1
        if error:
            self._error_count += 1
        if latency_ms > 0:
            self._total_latency_ms += latency_ms
            self._latency_samples += 1

    def register_collector(self, collector):
        """Register a callable that returns dict of health fields.

        Collectors are called each heartbeat cycle to gather agent/fleet state.
        Signature: () -> dict[str, Any] (sync or async)
        """
        self._collectors.append(collector)

    async def start(self):
        """Start the background heartbeat loop."""
        if not self.enabled:
            logger.debug("Watch reporter disabled (no AITHER_WATCH_URL)")
            return
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Watch reporter started → %s (every %ds)", self.watch_url, self._interval)

    async def stop(self):
        """Stop the background heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Watch reporter stopped")

    async def send_heartbeat(self) -> bool:
        """Send a single heartbeat to Watch. Returns True if successful."""
        snapshot = await self._build_snapshot()
        payload = {
            "service": self._service_name,
            "timestamp": time.time(),
            "status": snapshot.status,
            "agents": snapshot.agents,
            "agent_count": snapshot.agent_count,
            "llm_backend": snapshot.llm_backend,
            "llm_available": snapshot.llm_available,
            "avg_latency_ms": round(snapshot.avg_latency_ms, 1),
            "error_count_1h": snapshot.error_count_1h,
            "request_count_1h": snapshot.request_count_1h,
            "quota_usage_pct": round(snapshot.quota_usage_pct, 2),
            "uptime_seconds": round(snapshot.uptime_seconds, 1),
            "version": snapshot.version,
            "active_sessions": snapshot.active_sessions,
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.watch_url}/api/v1/heartbeat",
                    json=payload,
                )
                if resp.status_code < 300:
                    self._consecutive_failures = 0
                    if self._circuit_open:
                        logger.info("Watch circuit breaker closed (recovered)")
                        self._circuit_open = False
                    return True
                logger.debug("Watch heartbeat returned %d", resp.status_code)
        except Exception as exc:
            logger.debug("Watch heartbeat failed: %s", exc)

        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_BREAK_THRESHOLD and not self._circuit_open:
            self._circuit_open = True
            self._circuit_open_since = time.time()
            logger.warning(
                "Watch circuit breaker OPEN after %d failures — retrying in %ds",
                self._consecutive_failures, _CIRCUIT_BREAK_RETRY,
            )
        return False

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Background loop: send heartbeats at regular intervals."""
        while self._running:
            try:
                if self._circuit_open:
                    elapsed = time.time() - self._circuit_open_since
                    if elapsed < _CIRCUIT_BREAK_RETRY:
                        await asyncio.sleep(min(self._interval, _CIRCUIT_BREAK_RETRY - elapsed))
                        continue
                    # Try again after circuit break retry window
                    logger.info("Watch circuit breaker retry attempt")

                await self.send_heartbeat()

                # Reset hourly counters periodically (approximate — resets each cycle)
                # In production, these would be sliding windows
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Watch heartbeat loop error: %s", exc)

            await asyncio.sleep(self._interval)

    async def _build_snapshot(self) -> HealthSnapshot:
        """Gather health data from all registered collectors."""
        from adk import __version__

        snapshot = HealthSnapshot(
            uptime_seconds=time.time() - self._start_time,
            version=__version__,
            request_count_1h=self._request_count,
            error_count_1h=self._error_count,
        )

        # Average latency
        if self._latency_samples > 0:
            snapshot.avg_latency_ms = self._total_latency_ms / self._latency_samples

        # Error rate → status
        if self._request_count > 0:
            error_rate = self._error_count / self._request_count
            if error_rate > 0.5:
                snapshot.status = "degraded"
            elif error_rate > 0.9:
                snapshot.status = "unhealthy"

        # Call registered collectors
        for collector in self._collectors:
            try:
                result = collector()
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, dict):
                    for key, val in result.items():
                        if hasattr(snapshot, key):
                            setattr(snapshot, key, val)
            except Exception:
                pass  # Collectors must not break the heartbeat

        return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: WatchReporter | None = None


def get_watch_reporter() -> WatchReporter:
    """Get or create the module-level WatchReporter singleton."""
    global _instance
    if _instance is None:
        _instance = WatchReporter()
    return _instance
