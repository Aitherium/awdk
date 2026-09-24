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
    #: Git branch the session is on, from the transcript's own `gitBranch` field
    #: (Claude Code stamps it on every entry). "" when the transcript names none.
    branch: str = ""
    #: Tokens this session has SPENT: input + cache-creation + output summed over
    #: every assistant message in the transcript. Cache READS are excluded on
    #: purpose -- they re-count the same context every turn and would make a long
    #: session look 20x more expensive than it was.
    tokens_spent: int = 0


#: How far back a COLD read looks for the last human prompt. One tail window is
#: not enough: measured 2026-09-19, a 19.5 MB transcript's newest prompt that
#: named anything sat 2.58 MB from EOF, because a long turn writes megabytes of
#: tool traffic between human turns.
_PROMPT_SCAN_BYTES = 12_000_000

#: transcript path -> (size_at_scan, prompt). The directory re-derives every
#: session on a 2 s TTL, so a deep scan per poll would be tens of MB/s of disk
#: for a string that changes once a turn. After the cold read only the appended
#: bytes are read, and the last found prompt stands until a newer one appears.
_PROMPT_CACHE: dict[str, tuple[int, str]] = {}
_PROMPT_CACHE_LOCK = threading.Lock()


#: Statuses worth SAYING on a surface the owner watches. "working" and "idle"
#: are the ordinary states and add noise; these two mean the session is stuck
#: on a human.
NEEDS_OWNER = {"waiting-input": "waiting for you", "blocked?": "maybe blocked"}


def session_display_title(session_id: str, cwd: str, transcript_path: str = "",
                          claude_name: str = "", status: str = "") -> str:
    """`<repo>#<8 hex> - <what the human last asked for>`.

    The repo alone collided across every parallel tab in one checkout, which is
    what made the room say "<repo> says:" and every /sessions/unified row look
    identical. The topic half comes from the transcript's last HUMAN prompt --
    Claude Code sends machine turns (system reminders, hook feedback, task
    notifications) down the same channel, and those are filtered by
    `session_topic`, so an absent topic degrades to the bare name and is never
    guessed.
    """
    from adk.harnesses.transcript_bridge import session_topic

    # Claude Code's own name for the session is `<repo> <branch> <HH:MM>` --
    # it already carries the BRANCH (peers work on their own) and a start time
    # the owner can match to a tab, so it beats anything derived here. The
    # `#<id8>` form stays for a session that has no such name.
    repo = Path(cwd).name if cwd else ""
    short = (session_id or "")[:8]
    name = " ".join(str(claude_name or "").split())
    if not name:
        name = f"{repo}#{short}" if repo and short else (repo or short or "session")
    prompt = _last_human_prompt(transcript_path, session_topic) if transcript_path else ""
    topic = session_topic(prompt) if prompt else ""
    need = NEEDS_OWNER.get(status or "")
    parts = [name]
    if need:
        parts.append(need)
    if topic:
        parts.append(topic)
    return " - ".join(parts)


def _last_human_prompt(transcript_path: str, topic_of=None) -> str:
    """The newest human prompt that NAMES something, scanning backwards.

    A tool RESULT is also a user entry, so the discriminator is the content
    SHAPE (a plain string) and not the role -- the same trap the bridge and
    hook_common.py both document. Transcripts here reach 18 MB and the last
    human turn is routinely far outside one tail window, so this walks backward
    in bounded chunks (at most _PROMPT_SCAN_BYTES) and stops at the first
    prompt `topic_of` accepts -- filler ("continue") and machine turns are
    rejected there, not here.
    """
    cached = None
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with _PROMPT_CACHE_LOCK:
            cached = _PROMPT_CACHE.get(transcript_path)
        # Warm: read only what was appended since the last look. A file that
        # SHRANK (rotated, replaced) invalidates -- it is not the same stream.
        # UNCHANGED is the common case on a 2 s poll and must read nothing at
        # all: computing `size - cached[0]` and falling through to the deep
        # bound made the cache inert (measured: warm was 1x cold).
        if cached and cached[0] == size:
            return cached[1]
        limit = min(size, size - cached[0]) if cached and 0 < cached[0] < size else min(size, _PROMPT_SCAN_BYTES)
        found = ""
        with open(path, "rb") as fh:
            scanned, buf = 0, b""
            while scanned < limit:
                step = min(TRANSCRIPT_TAIL_SIZE, size - scanned)
                scanned += step
                fh.seek(size - scanned)
                buf = fh.read(step) + buf
                text = buf.decode("utf-8", errors="replace")
                lines = text.split("\n")
                # The first line of a mid-file window is almost always partial;
                # keep it in `buf` for the next, wider pass instead of parsing it.
                for line in reversed(lines[1:] if scanned < size else lines):
                    line = line.strip()
                    if not line or '"user"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") != "user":
                        continue
                    content = (entry.get("message") or {}).get("content")
                    if not isinstance(content, str) or not content.strip():
                        continue
                    if topic_of is None or topic_of(content):
                        found = content
                        break
                if found:
                    break
    except OSError:
        return (cached[1] if cached else "")
    if not found and cached:
        # Nothing new named anything: the session is still on the same work.
        found = cached[1]
    with _PROMPT_CACHE_LOCK:
        _PROMPT_CACHE[transcript_path] = (size, found)
        if len(_PROMPT_CACHE) > 256:  # bounded: sessions come and go
            for stale in list(_PROMPT_CACHE)[:64]:
                _PROMPT_CACHE.pop(stale, None)
    return found


#: transcript path -> (bytes_scanned, branch, tokens_spent, last_message_id).
#: The first look reads the whole file once; every later poll reads only the
#: appended bytes, so a 2 s poll over a 20 MB transcript costs nothing.
_USAGE_CACHE: dict[str, tuple[int, str, int, str]] = {}
_USAGE_CACHE_LOCK = threading.Lock()


def _transcript_usage(transcript_path: str) -> tuple[str, int]:
    """(branch, tokens_spent) for a transcript, read incrementally.

    Claude Code writes one JSONL entry per content BLOCK and repeats the whole
    message's `usage` on each, so usage is counted once per `message.id` --
    summing per line over-counts a multi-block turn by the block count.
    """
    if not transcript_path:
        return "", 0
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return "", 0
    with _USAGE_CACHE_LOCK:
        cached = _USAGE_CACHE.get(transcript_path)
    offset, branch, tokens, last_id = 0, "", 0, ""
    if cached and cached[0] <= size:
        offset, branch, tokens, last_id = cached
        if offset == size:
            return branch, tokens
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return branch, tokens
    # Only whole lines are consumed; a line still being written is re-read next time.
    end = chunk.rfind(b"\n")
    if end < 0:
        return branch, tokens
    consumed = chunk[: end + 1]
    seen: set[str] = {last_id} if last_id else set()
    for raw in consumed.split(b"\n"):
        if b'"gitBranch"' not in raw and b'"usage"' not in raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        gb = obj.get("gitBranch")
        if isinstance(gb, str) and gb.strip():
            branch = gb.strip()
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        mid = str(msg.get("id") or "")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
            last_id = mid
        for key in ("input_tokens", "cache_creation_input_tokens", "output_tokens"):
            val = usage.get(key)
            if isinstance(val, int) and val > 0:
                tokens += val
    with _USAGE_CACHE_LOCK:
        _USAGE_CACHE[transcript_path] = (offset + len(consumed), branch, tokens, last_id)
        if len(_USAGE_CACHE) > 256:
            for stale in list(_USAGE_CACHE)[:64]:
                _USAGE_CACHE.pop(stale, None)
    return branch, tokens


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
            # The session's OWN state outranks anything inferred from its transcript.
            # Measured 2026-09-19: a killed claude-tty (state=exited, exit 2) was listed
            # as `idle` with steer_capability `full`, because a transcript that simply
            # stopped growing reads as an idle tab -- so `tell` could address a corpse
            # and the desk could keep a body on stage for it.
            dead = str(info.get("state") or "") in ("exited", "failed")
            if dead:
                status = "exited"
            branch, tokens_spent = _transcript_usage(transcript)

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
                    steer_capability="none" if dead else "full",
                    extras=info,
                    branch=branch,
                    tokens_spent=tokens_spent,
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
            branch, tokens_spent = _transcript_usage(disc.transcript_path)

            out.append(
                UnifiedSession(
                    id=disc.id,
                    # disc.name is Claude Code's OWN `<repo> <branch> <HH:MM>`;
                    # the helper adds what the session is doing and whether it
                    # is stuck on a human.
                    title=session_display_title(disc.id, disc.cwd, disc.transcript_path,
                                                claude_name=disc.name, status=status),
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
                    branch=branch,
                    tokens_spent=tokens_spent,
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

            # Merge, deduplicating by id (daemon takes precedence). A daemon-owned
            # claude-tty session is ALSO discovered from Claude's own state files under
            # the --session-id the daemon minted for it -- fold that row into the daemon
            # row, or one Claude Code lists twice: once steerable, once not.
            daemon_ids = {s.id for s in daemon_unified}
            daemon_ids |= {
                str((s.extras or {}).get("harness_session_id") or "") for s in daemon_unified
            } - {""}
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
