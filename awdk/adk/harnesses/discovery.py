"""Discover interactive Claude Code tab sessions this daemon does not own.

This module implements the Python port of Resume-ClaudeSessions.ps1's
Get-LiveClaudeSessions logic: enumerate live claude processes via pid + start
time verification, exclude SDK/API sessions, and map each to its transcript.

All functions are SYNCHRONOUS and THREAD-SAFE (process enumeration via psutil
can be expensive; threading protects against concurrent over-probing).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Regex to exclude SDK/API sessions (not terminal tabs). A headless `claude -p`
#: run reports kind="interactive" like a real session and is distinguishable only
#: here (entrypoint "sdk-cli"). Excluded as a DENYLIST so an unfamiliar entrypoint
#: is still captured: over-capturing costs a skipped resume, under-capturing loses work.
EXCLUDE_ENTRYPOINT_PATTERN = r"^(sdk|api)"

#: Ground truth liveness: process exists + procStart matches within 10 seconds.
#: (PowerShell: 100000000L ticks = 10 seconds.) The tolerance covers clock skew.
PROCSTART_TOLERANCE_SECONDS = 10.0


@dataclass
class DiscoveredSession:
    """A session discovered from Claude's own state files and transcripts."""

    id: str  # Session ID (UUID)
    cwd: str  # Working directory
    name: str  # Claude's name for the session
    pid: int  # Process ID
    entrypoint: str  # "cli", "sdk-cli", etc.
    kind: str  # "interactive", "oneshot", etc.
    status: str  # Claude's status string
    transcript_path: str  # Path to events.jsonl


def _encode_cwd(cwd: str) -> str:
    """Encode a path for use as a directory name.

    Replaces ':' and '\\' with '-', e.g., 'C:\\AitherOS-Fresh' -> 'C--AitherOS-Fresh'.
    """
    if not cwd:
        return ""
    # Replace backslashes and colons with dashes
    encoded = cwd.replace(":", "-").replace("\\", "-")
    # Also handle forward slashes on Linux/Unix
    encoded = encoded.replace("/", "-")
    return encoded


def _try_parse_int(value: Any) -> Optional[int]:
    """Try to parse a value as int, return None on failure."""
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        return None
    except (ValueError, TypeError):
        return None


def _windows_ticks_to_unix(ticks: int) -> float:
    """Convert Windows FILETIME (100ns intervals since 1601) to Unix timestamp.

    Windows FILETIME is 100-nanosecond intervals since January 1, 1601.
    Unix timestamp is seconds since January 1, 1970.
    The constant 116444736000000000 is 100-ns intervals from 1601 to 1970.
    """
    return (ticks - 116444736000000000) / 10000000


def _get_process_start_time(pid: int) -> Optional[float]:
    """Get a process's start time (Unix timestamp), or None if not available.

    This uses psutil if available, with fallback to direct Windows API if not.
    Returns None if the process doesn't exist or start time cannot be read.
    """
    try:
        import psutil

        try:
            proc = psutil.Process(pid)
            # .create_time() returns Unix timestamp
            return proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
    except ImportError:
        # Fallback: psutil not available. Try Windows-specific approach.
        if os.name == "nt":
            try:
                import subprocess

                result = subprocess.run(
                    ["wmic", "process", "where", f"ProcessId={pid}", "get", "CreationDate"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=2,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    for line in lines:
                        line = line.strip()
                        if line and line != "CreationDate":
                            # WMI format: '20260102143054.000000+000'
                            # Convert to Unix timestamp
                            try:
                                dt_str = line[:14]  # '20260102143054'
                                from datetime import datetime

                                dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
                                return dt.timestamp()
                            except (ValueError, IndexError) as exc:
                                logger.debug(f"Error parsing WMI date for pid {pid}: {exc}")
                                return None
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                logger.debug(f"Error probing process {pid} start time: {exc}")
                return None
        return None


def _process_is_alive(pid: int, procstart_claim: Optional[int]) -> bool:
    """Check if a process exists and its start time matches the claimed value.

    Returns True only if:
    1. Process with given pid exists
    2. procstart_claim is provided AND matches within PROCSTART_TOLERANCE_SECONDS
       (after converting from Windows FILETIME if necessary)
       OR procstart_claim is None (no claim to verify)

    This is the ground-truth liveness check from Resume-ClaudeSessions.ps1.
    procstart_claim is expected to be Windows FILETIME ticks (100-ns intervals).
    """
    start_time = _get_process_start_time(pid)
    if start_time is None:
        return False

    if procstart_claim is None:
        # No claim to verify; process exists, that's enough
        return True

    # Convert Windows ticks to Unix timestamp for comparison
    try:
        claim_unix = _windows_ticks_to_unix(procstart_claim)
    except (ValueError, OverflowError):
        # If conversion fails, claim is invalid
        logger.debug(f"Could not convert procstart_claim {procstart_claim} to Unix timestamp")
        return False

    # Verify start time matches (10s tolerance for clock skew)
    return abs(start_time - claim_unix) <= PROCSTART_TOLERANCE_SECONDS


def discover_live_sessions(
    sessions_dir: Optional[str] = None, exclude_entrypoint: Optional[str] = None
) -> list[DiscoveredSession]:
    """Discover interactive Claude Code sessions currently running.

    Reads Claude's own state files (~/.claude/sessions/<pid>.json) and verifies
    liveness via process existence and start-time match. Excludes SDK/API sessions.

    Args:
        sessions_dir: Directory containing Claude's state files.
                     Default: ~/.claude/sessions
        exclude_entrypoint: Regex pattern for entrypoints to exclude.
                           Default: '^(sdk|api)' (SDK/API sessions)

    Returns:
        List of DiscoveredSession objects representing live tab sessions.
        Returns empty list if sessions directory does not exist or no live sessions found.
    """
    if sessions_dir is None:
        sessions_dir = str(Path.home() / ".claude" / "sessions")

    if exclude_entrypoint is None:
        exclude_entrypoint = EXCLUDE_ENTRYPOINT_PATTERN

    sessions_path = Path(sessions_dir)
    if not sessions_path.exists():
        return []

    live_sessions: list[DiscoveredSession] = []
    exclude_re = re.compile(exclude_entrypoint)

    try:
        for state_file in sessions_path.glob("*.json"):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            session_id = state.get("sessionId")
            pid = _try_parse_int(state.get("pid"))
            cwd = state.get("cwd", "")
            name = state.get("name", "")
            entrypoint = state.get("entrypoint", "")
            kind = state.get("kind", "")
            status = state.get("status", "")

            if not session_id or pid is None:
                continue

            # Exclude SDK/API sessions
            if exclude_re.match(entrypoint):
                continue

            # Verify liveness: process exists + start time matches
            procstart_claim = _try_parse_int(state.get("procStart"))
            if not _process_is_alive(pid, procstart_claim):
                continue

            # Find transcript: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
            projects_root = Path.home() / ".claude" / "projects"
            transcript_path = None

            # Direct path check, never a recursive glob. The transcript lives at
            # exactly one known location, so this is a single stat.
            #
            # Measured 2026-08-09 on this box (19 live sessions): the previous
            # `glob("**/<encoded>/<id>.jsonl")` walked the ENTIRE projects tree —
            # 50,000 directory scans, ~9s per discovery pass. That is not merely
            # slow, it defeats the feature: SessionDirectory's cache TTL is 2s, so
            # the cache could NEVER hit, every 2s UI poll re-walked the tree, and
            # the cockpit would sit permanently ~9s stale while pinning a core.
            # A pattern with no wildcard before the filename has exactly one
            # possible answer; globbing for it asks the filesystem a question it
            # does not need to ask.
            if cwd:
                candidate = projects_root / _encode_cwd(cwd) / f"{session_id}.jsonl"
                if candidate.is_file():
                    transcript_path = str(candidate)

            if not transcript_path:
                # Fallback for a cwd whose encoding we did not reproduce exactly.
                # Scans the IMMEDIATE project dirs only (one stat each) rather
                # than recursively walking every session file under all of them.
                # Deterministic on collision: pick the most recently modified, so
                # a UUID reused across projects resolves to the live one instead
                # of whichever the filesystem happened to yield first.
                try:
                    hits = [
                        p for p in (
                            d / f"{session_id}.jsonl"
                            for d in projects_root.iterdir() if d.is_dir()
                        ) if p.is_file()
                    ]
                except OSError:
                    hits = []
                if hits:
                    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    transcript_path = str(hits[0])

            if transcript_path:
                live_sessions.append(
                    DiscoveredSession(
                        id=session_id,
                        cwd=cwd,
                        name=name,
                        pid=pid,
                        entrypoint=entrypoint,
                        kind=kind,
                        status=status,
                        transcript_path=transcript_path,
                    )
                )
    except OSError as exc:
        logger.warning(f"Error discovering sessions: {exc}")

    return live_sessions
