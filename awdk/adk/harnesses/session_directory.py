"""Unified session directory merging daemon-owned and discovered tab sessions.

This module presents a unified view of all Claude Code sessions (both daemon-
owned via HarnessSession and discovered from interactive tabs) to clients. It
derives session status from transcript tails and assigns steering capability
based on origin.

All I/O (file reading, process probing) runs on threads to protect the async
event loop. Status derivation is cached with a short TTL to avoid re-walking
the filesystem on every poll.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from adk.harnesses.discovery import DiscoveredSession, discover_live_sessions

logger = logging.getLogger(__name__)

#: Cache TTL: how long to hold session directory state before re-probing.
CACHE_TTL_SECONDS = 2.0

#: Tail read size for transcript analysis. Most metadata lives in the last 256KB.
TRANSCRIPT_TAIL_SIZE = 262144

#: How long a tool_use may sit without a tool_result before the session is
#: reported as blocked rather than merely working.
#:
#: The transcript CANNOT tell these apart directly: a permission prompt is drawn
#: in the terminal and never written to the JSONL, so "tool running" and "waiting
#: for the human to approve a tool" have the identical on-disk shape — a tool_use
#: block with no matching tool_result. Age is the only available discriminator.
#: Most tools return in well under a minute, so a tool pending far longer is
#: overwhelmingly an unanswered prompt.
#:
#: This is why the status is named `blocked?` with a question mark and reported
#: through a summary that says which tool and for how long, instead of asserting
#: `waiting-permission`. Naming an inference as a fact is the defect this whole
#: cockpit exists to avoid: the operator is meant to look at the row and decide,
#: and a confident wrong label is worse than an honest uncertain one.
PENDING_TOOL_BLOCKED_SECONDS = 90.0

#: Upper bound on that inference. A tool_use pending for WEEKS is not a prompt
#: anyone is about to answer — it is an abandoned or crashed turn — and shouting
#: "blocked?" about it trains the operator to ignore the column that matters.
#: Caught by an existing test whose fixture carried a hard-coded July timestamp:
#: the rule cheerfully reported a month-old pending tool as needing approval now.
PENDING_TOOL_STALE_SECONDS = 86400.0  # 24h


def _pending_tool_use(lines: list[str]) -> tuple[Optional[str], float]:
    """Find a tool_use in the tail with no matching tool_result.

    Returns (tool_name, started_at) or (None, 0.0). Pairing is by tool_use_id,
    never by position: a turn can issue several tool calls at once and they
    complete out of order, so index-matching reports phantom pending calls.
    """
    from datetime import datetime

    pending: dict[str, tuple[str, float]] = {}
    satisfied: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        when = 0.0
        ts = obj.get("timestamp", "")
        if ts:
            try:
                when = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                when = 0.0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                pending[block["id"]] = (block.get("name", "tool"), when)
            elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                satisfied.add(block["tool_use_id"])
    for tool_id, (name, when) in pending.items():
        if tool_id not in satisfied:
            return name, when
    return None, 0.0


@dataclass
class UnifiedSession:
    """A session in the unified directory (daemon-owned or discovered tab)."""

    id: str
    title: str
    cwd: str
    harness: str
    harness_label: str
    origin: str  # "daemon" | "discovered"
    status: str  # "starting" | "ready" | "busy" | "idle" | "exited" | "failed"
    last_activity_at: float  # Unix timestamp of last activity
    last_activity_summary: str  # One line: the last user prompt or assistant action
    transcript_path: str
    pid: Optional[int] = None  # None for daemon-owned sessions without a real process
    steer_capability: str = "none"  # "full" | "turn-boundary" | "none"
    #: Extra fields from the original session (HarnessSession.info() fields or DiscoveredSession)
    extras: Optional[dict[str, Any]] = None


def _derive_status_from_transcript(transcript_path: str) -> tuple[str, float, str]:
    """Derive session status from transcript tail.

    Returns:
        (status, last_activity_at, last_activity_summary)

    Status derivation follows the transcript seam report:
    - "working": assistant generating or waiting for tool result
    - "waiting-input": assistant finished, awaiting human
    - "blocked?": a tool_use has had no tool_result for
      PENDING_TOOL_BLOCKED_SECONDS. Usually an unanswered permission prompt,
      possibly a genuinely long tool — the transcript cannot distinguish them,
      so the name carries the uncertainty rather than hiding it.
    - "idle": session has been idle (away_summary)
    - "exited": session stopped or timed out
    """
    try:
        path = Path(transcript_path)
        if not path.exists():
            return "unknown", time.time(), "(transcript not found)"

        # Read the tail
        file_size = path.stat().st_size
        tail_size = min(TRANSCRIPT_TAIL_SIZE, file_size)
        tail_text = ""
        if tail_size > 0:
            with open(path, "rb") as f:
                f.seek(file_size - tail_size)
                tail_text = f.read().decode("utf-8", errors="replace")

        # Parse lines in reverse order
        lines = tail_text.split("\n")
        last_activity_at = time.time()
        last_activity_summary = ""

        # A pending tool outranks every other signal: it is the one state where
        # the session may be waiting on the HUMAN, which is the whole reason the
        # owner opens the cockpit. Checked before the terminal-event scan because
        # the last line of a tool-blocked session is an ordinary assistant turn,
        # which would otherwise read as plain "working".
        tool_name, tool_started = _pending_tool_use(lines)
        if tool_name:
            waited = time.time() - tool_started if tool_started else 0.0
            if tool_started and waited >= PENDING_TOOL_STALE_SECONDS:
                return (
                    "idle",
                    tool_started,
                    f"{tool_name} pending since {int(waited // 3600)}h ago (abandoned turn)",
                )
            if tool_started and waited >= PENDING_TOOL_BLOCKED_SECONDS:
                mins = int(waited // 60)
                age = f"{mins}m" if mins else f"{int(waited)}s"
                return (
                    "blocked?",
                    tool_started,
                    f"{tool_name} pending {age} — may need approval",
                )
            return "working", tool_started or time.time(), f"running {tool_name}"

        # Look for terminal events
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "")
            timestamp_str = obj.get("timestamp", "")

            # Parse timestamp if available
            if timestamp_str:
                try:
                    # ISO-8601 format: 2026-07-12T15:14:38.922Z
                    from datetime import datetime

                    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    last_activity_at = dt.timestamp()
                except (ValueError, AttributeError) as exc:
                    logger.debug(f"Error parsing timestamp {timestamp_str}: {exc}")

            # Status derivation (simplified from seam report)
            if msg_type == "assistant":
                # Check stop_reason
                message = obj.get("message", {})
                stop_reason = message.get("stop_reason", "")
                if stop_reason in ("tool_use", "max_tokens"):
                    return "working", last_activity_at, "(generating)"
                # Otherwise assistant is done, likely waiting input

            elif msg_type == "system":
                subtype = obj.get("subtype", "")
                if subtype == "turn_duration":
                    # Turn completed
                    return "waiting-input", last_activity_at, "(awaiting input)"
                elif subtype == "away_summary":
                    content = obj.get("content", "")
                    if content:
                        last_activity_summary = content[:80]
                    return "idle", last_activity_at, last_activity_summary

            elif msg_type == "user":
                # User just sent input.
                #
                # `content` is a plain STRING for a typed prompt and a LIST of
                # blocks for a structured one. Handling only the list left ~a
                # third of live rows with an empty summary — and a row with no
                # context is a row the operator has to open a tab to understand,
                # which is the exact cost this cockpit exists to remove.
                message = obj.get("message", {})
                content = message.get("content", [])
                if isinstance(content, str):
                    last_activity_summary = content.strip()[:80]
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "text":
                            text = (c.get("text") or "").strip()
                            if text:
                                last_activity_summary = text[:80]
                                break
                        elif c.get("type") == "tool_result":
                            # A tool result is a user-role message too; naming the
                            # tool beats showing nothing.
                            last_activity_summary = last_activity_summary or "(tool result)"
                # Never return an empty summary: "" reads as "nothing to report",
                # when the truth is "this session is waiting for you".
                return (
                    "waiting-input",
                    last_activity_at,
                    last_activity_summary or "(awaiting input)",
                )

        # No terminal event found; check if there's an assistant message in flight
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") == "assistant":
                return "working", last_activity_at, "(generating)"

        return "idle", last_activity_at, "(no activity)"
    except Exception as exc:
        logger.warning(f"Error deriving status from {transcript_path}: {exc}")
        return "unknown", time.time(), f"(error: {exc})"


class SessionDirectory:
    """Unified view of all Claude sessions."""

    def __init__(self, discover_fn: Optional[Callable[[], list]] = None) -> None:
        """
        Args:
            discover_fn: how to find tab sessions this daemon does not own.
                Defaults to the real host probe. Injectable because without a
                seam a test reaches the REAL machine: two tests here asserted
                `len(unified) == 1` and got 20 on a box with 19 Claude windows
                open, while passing on CI where none are. Environment-dependent
                green is worse than red — it passes in the place that cannot
                see the bug and fails in the place that can.
        """
        self._lock = threading.RLock()
        self._cache: Optional[list[UnifiedSession]] = None
        self._cache_time: float = 0.0
        self._discover = discover_fn or discover_live_sessions

    def _build_from_daemon(self, daemon_sessions: list[dict[str, Any]]) -> list[UnifiedSession]:
        """Convert daemon HarnessSession.info() dicts to UnifiedSession."""
        out = []
        for info in daemon_sessions:
            session_id = info.get("id", "")
            transcript = info.get("transcript", "")
            cwd = info.get("cwd", "")
            title = info.get("title", "")

            status, activity_at, activity_summary = "idle", time.time(), ""
            if transcript:
                status, activity_at, activity_summary = _derive_status_from_transcript(
                    transcript
                )

            out.append(
                UnifiedSession(
                    id=session_id,
                    title=title,
                    cwd=cwd,
                    harness=info.get("harness", ""),
                    harness_label=info.get("harness_label", ""),
                    origin="daemon",
                    status=status,
                    last_activity_at=activity_at,
                    last_activity_summary=activity_summary,
                    transcript_path=transcript,
                    pid=None,
                    steer_capability="full",
                    extras=info,
                )
            )
        return out

    def _build_from_discovered(
        self, discovered: list[DiscoveredSession]
    ) -> list[UnifiedSession]:
        """Convert DiscoveredSession to UnifiedSession."""
        out = []
        for disc in discovered:
            status, activity_at, activity_summary = "idle", time.time(), ""
            if disc.transcript_path:
                status, activity_at, activity_summary = _derive_status_from_transcript(
                    disc.transcript_path
                )

            out.append(
                UnifiedSession(
                    id=disc.id,
                    title=disc.name,
                    cwd=disc.cwd,
                    harness="claude",  # Discovered sessions are always Claude
                    harness_label="Claude Code",
                    origin="discovered",
                    status=status,
                    last_activity_at=activity_at,
                    last_activity_summary=activity_summary,
                    transcript_path=disc.transcript_path,
                    pid=disc.pid,
                    steer_capability="turn-boundary",  # Can interrupt but not full control
                    extras=asdict(disc),
                )
            )
        return out

    def list_sessions_sync(self, daemon_sessions: list[dict[str, Any]]) -> list[UnifiedSession]:
        """List all sessions (daemon + discovered), with caching.

        Args:
            daemon_sessions: Output of SessionManager.list_sessions()

        Returns:
            Unified list sorted by last activity time (newest first).
        """
        now = time.time()
        with self._lock:
            # Return cached list if fresh
            if self._cache is not None and (now - self._cache_time) < CACHE_TTL_SECONDS:
                return self._cache

            # Rebuild cache: daemon sessions + discovered tab sessions
            daemon_unified = self._build_from_daemon(daemon_sessions)
            discovered = self._discover()
            discovered_unified = self._build_from_discovered(discovered)

            # Merge, deduplicating by id (daemon takes precedence)
            daemon_ids = {s.id for s in daemon_unified}
            combined = daemon_unified + [d for d in discovered_unified if d.id not in daemon_ids]

            # Sort by last activity (newest first)
            combined.sort(key=lambda s: s.last_activity_at, reverse=True)

            self._cache = combined
            self._cache_time = now

        return self._cache or []


#: Global singleton
_directory: Optional[SessionDirectory] = None
_directory_lock = threading.Lock()


def default_directory() -> SessionDirectory:
    """Get or create the global session directory."""
    global _directory
    with _directory_lock:
        if _directory is None:
            _directory = SessionDirectory()
        return _directory
