"""
AitherShell Entitlement Heartbeat
==================================

For long-running daemons (``aither mcp serve``, ``aither chat --watch``,
agent runners, etc.) - keeps the cached entitlement fresh and refuses to
keep running when the subscription is revoked / lapsed / out of quota.

Usage::

    from adk.shell.heartbeat import start_heartbeat, stop_heartbeat

    stop = start_heartbeat(feature="mcp_serve",
                            on_revoke=lambda e: shutdown_handler(e))
    try:
        run_forever()
    finally:
        stop()
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from typing import Callable, Optional

from adk.shell.entitlement import (
    Entitlement, EntitlementError, refresh, require,
)

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(minutes=5)


class _Heartbeat:
    def __init__(
        self,
        feature: str,
        interval: timedelta,
        on_revoke: Optional[Callable[[Entitlement], None]],
        on_refresh: Optional[Callable[[Entitlement], None]],
    ):
        self.feature = feature
        self.interval = interval
        self.on_revoke = on_revoke
        self.on_refresh = on_refresh
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _tick(self) -> None:
        try:
            ent = asyncio.run(refresh(force=True))
        except Exception as e:           # noqa: BLE001
            log.warning("entitlement heartbeat failed: %s", e)
            return

        if self.on_refresh:
            try:
                self.on_refresh(ent)
            except Exception:            # noqa: BLE001
                log.exception("heartbeat on_refresh raised")

        try:
            asyncio.run(require(self.feature))
        except EntitlementError as e:
            log.error("entitlement revoked (%s): %s", e.code, e)
            if self.on_revoke:
                try:
                    self.on_revoke(ent)
                except Exception:        # noqa: BLE001
                    log.exception("heartbeat on_revoke raised")

    def _run(self) -> None:
        # First tick happens after the interval - the caller already
        # validated entitlement at startup time.
        while not self._stop.wait(self.interval.total_seconds()):
            self._tick()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"aither-entitlement-heartbeat-{self.feature}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def start_heartbeat(
    feature: str,
    *,
    interval: timedelta = DEFAULT_INTERVAL,
    on_revoke: Optional[Callable[[Entitlement], None]] = None,
    on_refresh: Optional[Callable[[Entitlement], None]] = None,
) -> Callable[[], None]:
    """Start a background heartbeat. Returns a stop callable."""
    hb = _Heartbeat(feature, interval, on_revoke, on_refresh)
    hb.start()
    return hb.stop


def stop_heartbeat(stop_fn: Callable[[], None]) -> None:
    """Convenience: call the stop callable returned by start_heartbeat."""
    try:
        stop_fn()
    except Exception:                    # noqa: BLE001
        pass
