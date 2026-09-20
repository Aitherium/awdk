"""Spool tailer — drains hook-written event files into rooms.

Producers that must not block cannot POST. A Claude Code hook runs synchronously in the
owner's session, so it appends one JSON line to ``~/.aither/aeon-spool/<session>.jsonl``
and returns; this tailer picks the line up and publishes it to the room. The seam is
deliberate: a daemon that is down, slow or restarting can never stall a coding session,
and the lines it missed are still on disk when it comes back.

WHY A THREAD, NOT AN ASYNC TASK
-------------------------------
Directory scans and file reads are blocking syscalls. This matters because a blocking
call on the event loop is not "slow", it is an outage for every concurrent request for
its full duration — a class root-caused four times in this codebase. So the tailer runs
on its own daemon thread and only touches the room registry, which is itself
lock-guarded and thread-safe. This is the same thread-publish/async-poll shape
``session.py`` already uses.

OFFSETS ARE PER FILE AND PERSISTED IN MEMORY ONLY
-------------------------------------------------
On restart the tailer resumes from the CURRENT END of each file it has never seen,
rather than replaying history. Replaying a week of hook lines into the room on every
daemon restart would bury live traffic under archaeology. The files remain on disk as
the durable record, and the room's own JSONL transcript is the replayable stream.

COUNTERS ARE MONOTONIC, SO THEY CANNOT ANSWER "IS THIS PRODUCER ALIVE?"
----------------------------------------------------------------------
``published`` and ``rejected`` count since boot, which means a health arm asserting
``published > 0`` passes forever on events published yesterday. Measured 2026-09-19:
``spool.published`` was 11 across 139 spool files, the newest spool file was ~7 h
stale, and the room's newest event was NOW — i.e. the counter said "working" while
this producer had delivered nothing all day. So ``stats()`` also carries
``last_published_at`` / ``last_rejected_at`` (epoch seconds, 0.0 = never) and
``last_rejection`` (the refusal text that used to go only to stderr).
🪤 Stamp those ONLY where an event really moved. A timestamp advanced on a tick
would make any freshness check pass forever — the same hole in a new shape.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from adk.harnesses.rooms import RoomError, RoomRegistry, default_registry


def spool_dir() -> Path:
    return Path(
        os.environ.get("AITHER_AEON_SPOOL", Path.home() / ".aither" / "aeon-spool")
    )


class SpoolTailer:
    """Tails every ``*.jsonl`` in the spool directory and publishes into rooms."""

    def __init__(
        self,
        registry: Optional[RoomRegistry] = None,
        directory: Optional[Path] = None,
        interval: float = 0.5,
        replay_existing: bool = False,
    ) -> None:
        self.registry = registry or default_registry()
        self.dir = directory or spool_dir()
        self.interval = interval
        #: file path -> byte offset already consumed
        self._offsets: Dict[str, int] = {}
        self._replay_existing = replay_existing
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.published = 0
        self.rejected = 0
        #: Epoch seconds of the last event that really reached a room, 0.0 = never.
        #: Advanced at the publish site, never on a tick — see the module docstring.
        self.last_published_at = 0.0
        #: Epoch seconds of the last refusal, 0.0 = never.
        self.last_rejected_at = 0.0
        #: The reason for that refusal, verbatim (a RoomError text, or the parse
        #: failure). Surfaced rather than only printed: stderr on a daemon thread is
        #: not somewhere an operator or a checker looks.
        self.last_rejection = ""

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="aeon-spool-tailer", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception as exc:  # noqa: BLE001
                # The tailer is a background thread: if it dies, hook events stop
                # arriving and NOTHING says so — the room just goes quiet, which
                # reads as "no agents are working". So it reports and keeps going.
                sys.stderr.write(f"[aeon-spool] drain failed: {exc}\n")
            self._stop.wait(self.interval)

    # ── draining ────────────────────────────────────────────────────────────

    def drain_once(self) -> int:
        """Read every new line in the spool. Returns how many events published."""
        if not self.dir.is_dir():
            return 0
        published = 0
        for path in sorted(self.dir.glob("*.jsonl")):
            published += self._drain_file(path)
        return published

    def _drain_file(self, path: Path) -> int:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return 0

        if key not in self._offsets:
            # First sight of this file: start at the end unless explicitly replaying,
            # so a daemon restart does not dump history into the live room.
            self._offsets[key] = 0 if self._replay_existing else size

        offset = self._offsets[key]
        if size < offset:
            # Truncated or rotated underneath us — restart from the beginning rather
            # than seeking past the end and going permanently silent.
            offset = 0
        if size == offset:
            return 0

        published = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                for line in handle:
                    if not line.endswith("\n"):
                        # Partial line: a hook is mid-write. Leave the offset before
                        # it so the whole line is read on the next pass.
                        break
                    offset += len(line.encode("utf-8"))
                    if self._publish(line):
                        published += 1
        except OSError as exc:
            sys.stderr.write(f"[aeon-spool] could not read {path}: {exc}\n")
            return published

        self._offsets[key] = offset
        return published

    def _reject(self, reason: str, log: str = "") -> bool:
        """Count, TIMESTAMP and NAME one refusal. Always returns False.

        Every rejection site funnels through here so none can grow the counter
        without also saying WHEN and WHY: a bare count of 11 from yesterday reads
        exactly like 11 from this minute.
        """
        self.rejected += 1
        self.last_rejected_at = time.time()
        # Verbatim, with no prefix: a checker and the /health reader want the
        # producer's own words, and the log line adds its own tag below.
        self.last_rejection = reason
        sys.stderr.write(f"[aeon-spool] {log or reason}\n")
        return False

    def _publish(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return False
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return self._reject("malformed spool line", "dropped a malformed spool line")
        if not isinstance(event, dict):
            # Previously counted in total silence, which is the failure mode this
            # module's docstring complains about, one level down.
            return self._reject("spool line was not a JSON object")
        try:
            room = self.registry.get_or_create(str(event.get("room") or "main"))
            room.publish(event)
        except RoomError as exc:
            # A refused event is a PRODUCER bug and must be visible. Counting it and
            # naming the reason is the difference between "the hook is wrong" and
            # "the room is mysteriously empty".
            return self._reject(str(exc), f"rejected event: {exc}")
        self.published += 1
        self.last_published_at = time.time()
        return True

    def stats(self) -> Dict[str, object]:
        return {
            "dir": str(self.dir),
            "files": len(self._offsets),
            "published": self.published,
            "rejected": self.rejected,
            # checked_at moves on every read; these two move only on real work, so
            # (checked_at - last_published_at) is the producer's staleness.
            "last_published_at": self.last_published_at,
            "last_rejected_at": self.last_rejected_at,
            "last_rejection": self.last_rejection,
            "running": self._thread is not None and self._thread.is_alive(),
            "checked_at": time.time(),
        }


_tailer: Optional[SpoolTailer] = None
_tailer_lock = threading.Lock()


def default_tailer() -> SpoolTailer:
    global _tailer
    with _tailer_lock:
        if _tailer is None:
            _tailer = SpoolTailer()
        return _tailer
