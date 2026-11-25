"""Keep the awsh session service running while the awdk daemon runs.

Claude Code on this machine is an awsh session (``adk.harnesses``), and awsh sessions live in
the harness daemon on loopback :8362. The awdk daemon used to leave that daemon to OS autostart,
so a machine where only awdk came up (a fresh install, a reboot that restarted one and not the
other) had no host for Claude Code and nothing said why. The daemon now owns it: if the harness
is not answering, start it, detached, and look again every half minute.

Never spawns when: the harness already answers; ``AITHER_HARNESS_AUTOSTART=0``; the configured
harness URL is not on this machine; or under pytest. A crash loop is rate limited so a broken
install cannot fork-bomb the box.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from adk.rc import default_harness_url

log = logging.getLogger("adk.harness_supervisor")

__all__ = ["harness_up", "ensure_started", "supervise", "autostart_enabled"]

_OFF = frozenset({"0", "false", "no", "off"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CHECK_EVERY_S = 30.0
MIN_SPAWN_GAP_S = 90.0
#: After this many starts in a row that did not bring it up, look only every ten minutes.
GIVE_UP_AFTER = 5
GIVE_UP_EVERY_S = 600.0
LOG_PATH = Path.home() / ".aither" / "logs" / "harness-supervised.log"


def autostart_enabled() -> bool:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return (os.environ.get("AITHER_HARNESS_AUTOSTART") or "1").strip().lower() not in _OFF


def harness_up(url: str, timeout: float = 1.5) -> bool:
    """Does the harness answer /health on ``url``? Never raises."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as resp:  # noqa: S310 -- loopback
            return resp.status == 200
    except Exception:  # noqa: BLE001 -- any failure is "not up"
        return False


def _spawn(port: int) -> bool:
    """Start ``adk harness serve`` detached. True when the process was created."""
    cmd: List[str] = [sys.executable, "-m", "adk", "harness", "serve", "--port", str(port)]
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP; never DETACHED_PROCESS (see claude_runner).
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        sink: Any = open(LOG_PATH, "ab")  # noqa: SIM115 -- handed to the child
    except OSError:
        sink = subprocess.DEVNULL
    try:
        subprocess.Popen(  # noqa: S603 -- a fixed argv, no shell
            cmd, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, close_fds=True, **kwargs)
    except (OSError, ValueError) as exc:
        log.warning("could not start the awsh session service: %s", exc)
        return False
    finally:
        if sink is not subprocess.DEVNULL:
            sink.close()
    return True


def ensure_started(*, url: Optional[str] = None, up: Callable[[str], bool] = harness_up,
                   spawn: Callable[[int], bool] = _spawn) -> str:
    """One look: ``running`` | ``started`` | ``failed`` | ``disabled`` | ``external``."""
    base = (url or default_harness_url()).rstrip("/")
    if up(base):
        return "running"
    if not autostart_enabled():
        return "disabled"
    parsed = urlparse(base)
    if (parsed.hostname or "") not in _LOOPBACK:
        return "external"
    return "started" if spawn(parsed.port or 8362) else "failed"


async def supervise(*, url: Optional[str] = None, interval: float = CHECK_EVERY_S,
                    up: Callable[[str], bool] = harness_up,
                    spawn: Callable[[int], bool] = _spawn,
                    clock: Callable[[], float] = time.monotonic,
                    first_wait: float = 3.0) -> None:
    """Run until cancelled: keep the harness answering, never faster than ``MIN_SPAWN_GAP_S``."""
    last_spawn = -MIN_SPAWN_GAP_S
    misses = 0
    wait = first_wait  # let uvicorn finish binding before the first look
    while True:
        await asyncio.sleep(wait)
        wait = interval
        try:
            base = url or default_harness_url()
            if await asyncio.to_thread(up, base):
                misses = 0
                continue
            now = clock()
            if now - last_spawn < MIN_SPAWN_GAP_S:
                continue
            if misses >= GIVE_UP_AFTER:
                wait = GIVE_UP_EVERY_S
            last_spawn = now
            outcome = await asyncio.to_thread(ensure_started, url=base, up=up, spawn=spawn)
            if outcome in ("disabled", "external"):
                return  # nothing here is ours to supervise
            if outcome in ("started", "failed"):
                misses += 1  # reset by the next look that finds it up
                log.info("started the awsh session service on %s", base)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a supervisor must outlive its own bug
            log.debug("harness supervisor look failed: %s", exc)
