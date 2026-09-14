"""Background drain loop for publishing spooled graph updates.

The drainer is an optional background task that periodically publishes updates
from the spool to the platform. It is designed to work with agents that live
for hours or days; a local spool without drainage would accumulate updates
unboundedly.

Can run in a separate thread via BackgroundDrainer (threading-based), or as an
asyncio task via BackgroundDrainer with asyncio support. All failures are
logged but never raised; the spool is authoritative and intact regardless of
publish outcomes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

from .publisher import Publisher, PublishOutcome
from .spool import Spool

logger = logging.getLogger("adk.selfgraph.drainer")


class BackgroundDrainer:
    """Runs graph publishing in a background thread.

    Periodically fetches pending updates from the spool and publishes them.
    Runs until stop() is called or the parent thread exits. Designed to be
    daemonized — does not block agent execution.

    Attributes:
        spool: The Spool to drain from.
        publisher: The Publisher to post updates with.
    """

    def __init__(
        self,
        spool: Spool,
        publisher: Publisher | None = None,
        poll_interval: float = 30.0,
    ) -> None:
        """Initialize the drainer.

        Args:
            spool: The Spool to drain.
            publisher: Publisher instance. If None, creates a default one.
            poll_interval: Seconds to sleep between drain cycles. Defaults to 30.
        """
        self.spool = spool
        self.publisher = publisher or Publisher()
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._published_count = 0
        self._failed_count = 0

    def start(self) -> None:
        """Start the background drain thread.

        Safe to call multiple times; starting when already running is a no-op.
        The thread is daemonized and will not block process exit.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("started background drainer (poll_interval=%.1fs)", self.poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the drain thread gracefully.

        Signals the thread to exit and waits up to timeout seconds. Does not
        raise if the thread doesn't stop in time (it is daemonized).

        Args:
            timeout: Max seconds to wait. Defaults to 5.
        """
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("drainer thread did not stop within %.1fs", timeout)

    def stats(self) -> dict[str, Any]:
        """Return drain statistics.

        Returns:
            Dict with keys: published_count, failed_count, spool_stats (dict).
        """
        return {
            "published_count": self._published_count,
            "failed_count": self._failed_count,
            "spool_stats": self.spool.stats(),
        }

    def _run(self) -> None:
        """Main drain loop (runs in background thread)."""
        logger.debug("drain loop started")
        while not self._stop_event.is_set():
            try:
                self._drain_once()
            except Exception as e:
                logger.exception("drain loop error: %s", e)
            self._stop_event.wait(timeout=self.poll_interval)
        logger.debug("drain loop exited")

    def _drain_once(self) -> None:
        """Fetch and publish one batch of pending updates."""
        rows = self.spool.pending(limit=50)
        if not rows:
            return

        for row in rows:
            outcome, error = self.publisher.publish(row.update)
            if outcome == PublishOutcome.SUCCESS:
                self.spool.mark_published(row.rowid)
                self._published_count += 1
                logger.debug("published update %d", row.rowid)
            else:
                self._failed_count += 1
                logger.debug(
                    "publish failed for update %d: %s %s",
                    row.rowid,
                    outcome.value,
                    error or "",
                )


class AsyncBackgroundDrainer:
    """Async background draining of the spool via asyncio task.

    Periodically fetches pending entries from the spool and publishes them
    via the async Publisher. Designed for agents with their own asyncio event
    loop. Never raises into the host event loop — all errors are logged.

    Attributes:
        spool: The Spool to drain from.
        publisher: The Publisher to post updates with.
    """

    def __init__(
        self,
        spool: Spool,
        publisher: Publisher | None = None,
        interval: float = 30.0,
        max_backoff: float = 300.0,
    ) -> None:
        """Initialize the async drainer.

        Args:
            spool: The Spool to drain.
            publisher: Publisher instance. If None, creates a default one.
            interval: Seconds to sleep between drain cycles. Defaults to 30.
            max_backoff: Max backoff cap in seconds (default 300).
        """
        self.spool = spool
        self.publisher = publisher or Publisher()
        self.interval = interval
        self.max_backoff = max_backoff
        self._task: Optional[asyncio.Task] = None
        self._backoff_factor = 1.0
        self._published_count = 0
        self._failed_count = 0

    def start(self) -> None:
        """Start the background drain task (idempotent).

        Safe to call from inside an async context.
        """
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._run())
                logger.debug("AsyncBackgroundDrainer started")
            except RuntimeError:
                logger.warning(
                    "AsyncBackgroundDrainer.start() called outside async context"
                )

    def stop(self) -> None:
        """Stop the background drain task (idempotent)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self._task = None
            logger.debug("AsyncBackgroundDrainer stopped")

    def stats(self) -> dict[str, Any]:
        """Return drain statistics."""
        return {
            "published_count": self._published_count,
            "failed_count": self._failed_count,
            "spool_stats": self.spool.stats(),
        }

    async def _run(self) -> None:
        """Main drain loop (runs as asyncio task)."""
        logger.debug("async drain loop started")
        while True:
            try:
                outcome = await self.publisher.drain(limit=100)

                # Track results
                self._published_count += outcome.sent
                self._failed_count += outcome.failed

                # Exponential backoff if unreachable
                if outcome.unreachable:
                    self._backoff_factor = min(
                        self._backoff_factor * 2.0, self.max_backoff / self.interval
                    )
                    wait_time = self.interval * self._backoff_factor
                    logger.debug(
                        f"Platform unreachable; next drain in {wait_time:.1f}s"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    # Reset backoff on success
                    self._backoff_factor = 1.0
                    await asyncio.sleep(self.interval)

            except asyncio.CancelledError:
                logger.debug("async drain loop cancelled")
                break

            except Exception as e:
                # Never raise into the host event loop
                logger.exception(f"async drain loop error: {e}")
                await asyncio.sleep(self.interval)


def drain_sync(
    spool: Spool,
    publisher: Publisher | None = None,
    max_iterations: int = 1,
) -> dict[str, Any]:
    """Synchronously drain the spool once or a few times.

    A convenience for testing or one-shot drain (e.g., on agent shutdown).
    Blocks the caller until done.

    Args:
        spool: The Spool to drain.
        publisher: Publisher instance. If None, creates a default one.
        max_iterations: Number of poll cycles to run. Defaults to 1.

    Returns:
        Dict with keys: published_count, failed_count, spool_stats.
    """
    publisher = publisher or Publisher()
    published = 0
    failed = 0

    # For backwards compatibility: support old Publisher.publish API if available
    # Otherwise use drain
    for _ in range(max_iterations):
        rows = spool.pending(limit=50)
        if not rows:
            break

        for row in rows:
            # Try old publish() API first (for backwards compat with old Publisher)
            if hasattr(publisher, "publish"):
                outcome, error = publisher.publish(row.update)
                if outcome == PublishOutcome.SUCCESS:
                    spool.mark_published(row.rowid)
                    published += 1
                else:
                    failed += 1
            else:
                # Fallback: just skip (shouldn't happen with current Publisher)
                pass

    return {
        "published_count": published,
        "failed_count": failed,
        "spool_stats": spool.stats(),
    }
