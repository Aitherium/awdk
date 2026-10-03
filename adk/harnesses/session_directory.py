"""Unified session directory merging daemon-owned and discovered tab sessions.

This module presents a unified view of all Claude Code sessions (both daemon-
owned via HarnessSession and discovered from interactive tabs) to clients. It
derives session status from transcript tails and assigns steering capability
based on origin.

All I/O (file reading, process probing) runs off the event loop. A server
calls `SessionDirectory.start_refresher()` once: a background thread then owns
every rebuild and `list_sessions_sync` returns the last snapshot without doing
any transcript I/O, so no caller ever waits on a rebuild. Without a refresher
(tests, one-shot CLI use) the directory rebuilds inline behind a short TTL.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from adk.harnesses.discovery import DiscoveredSession, cwd_basename, discover_live_sessions

logger = logging.getLogger(__name__)

#: Cache TTL: how long to hold session directory state before re-probing
#: (inline mode only -- with a refresher running, the snapshot is always served).
CACHE_TTL_SECONDS = 2.0

#: Background refresher cadence. Each wait is jittered by +/-20 % so several
#: daemons on one host do not hit the disk in lockstep.
REFRESH_INTERVAL_SECONDS = 2.0

#: Bytes of any ONE transcript the usage scan reads per refresh tick. A 130 MB
#: transcript therefore catches up in ~16 ticks in the background instead of
#: being read in one go on somebody's request.
USAGE_BYTES_PER_TICK = 8 * 1024 * 1024

#: How long a caller that finds NO snapshot yet waits for the first (cheap) one
#: before answering with an empty, stale list.
FIRST_SNAPSHOT_WAIT_SECONDS = 0.75

#: A snapshot older than this many refresh intervals is reported as stale.
STALE_AFTER_INTERVALS = 5

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
        # Only lines naming a tool block can matter; skipping the rest unparsed
        # keeps a busy session's 256 KB tail from being JSON-decoded every tick.
        if not line or ('"tool_use"' not in line and '"tool_result"' not in line):
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
    #: None while a background usage scan has not yet caught up with the file:
    #: a partial sum would read as a real (too small) spend.
    tokens_spent: Optional[int] = 0


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

#: A JSONL line carrying a plain-string `content` somewhere (a typed prompt).
_STRING_CONTENT = re.compile(rb'"content":\s*"')

#: The entry-level `gitBranch` field, read without parsing the line.
_GIT_BRANCH = re.compile(rb'"gitBranch":\s*"([^"\\]*)"')


#: Statuses worth SAYING on a surface the owner watches. "working" and "idle"
#: are the ordinary states and add noise; these two mean the session is stuck
#: on a human.
NEEDS_OWNER = {
    "waiting-input": "waiting for you",
    "waiting-permission": "waiting for approval",
    "blocked?": "maybe blocked",
}


def merge_registry_status(registry_status: str, transcript_status: str) -> str:
    """Combine Claude Code's own status with the one read from the transcript.

    Claude Code writes ``status`` into ``~/.claude/sessions/<pid>.json`` itself
    ("busy" | "idle" | "waiting"), so it outranks the transcript inference:

    - ``busy`` -> ``working``
    - ``waiting`` -> ``waiting-permission`` (it is showing the human a prompt)
    - ``idle`` -> the transcript's ``waiting-input`` / ``blocked?`` when it says
      so (both are finer-grained idle states), else ``idle``
    - anything else (older Claude Code, no field) -> the transcript value
    """
    reg = (registry_status or "").strip().lower()
    if reg == "busy":
        return "working"
    if reg == "waiting":
        return "waiting-permission"
    if reg == "idle":
        if transcript_status in ("waiting-input", "blocked?"):
            return transcript_status
        return "idle"
    return transcript_status


def session_display_title(session_id: str, cwd: str, transcript_path: str = "",
                          claude_name: str = "", status: str = "",
                          prompt_scan: bool = True) -> str:
    """`<repo>#<8 hex> - <what the human last asked for>`.

    The repo alone collided across every parallel tab in one checkout, which is
    what made the room say "<repo> says:" and every /sessions/unified row look
    identical. The topic half comes from the transcript's last HUMAN prompt --
    Claude Code sends machine turns (system reminders, hook feedback, task
    notifications) down the same channel, and those are filtered by
    `session_topic`, so an absent topic degrades to the bare name and is never
    guessed. ``prompt_scan=False`` uses only an already-cached prompt (no
    file read), for a first listing that must not walk a cold transcript.
    """
    from adk.harnesses.transcript_bridge import session_topic

    # Claude Code's own name for the session is `<repo> <branch> <HH:MM>` --
    # it already carries the BRANCH (peers work on their own) and a start time
    # the owner can match to a tab, so it beats anything derived here. The
    # `#<id8>` form stays for a session that has no such name.
    repo = cwd_basename(cwd)
    short = (session_id or "")[:8]
    name = " ".join(str(claude_name or "").split())
    if not name:
        name = f"{repo}#{short}" if repo and short else (repo or short or "session")
    if not transcript_path:
        prompt = ""
    elif prompt_scan:
        prompt = _last_human_prompt(transcript_path, session_topic)
    else:
        with _PROMPT_CACHE_LOCK:
            hit = _PROMPT_CACHE.get(transcript_path)
        prompt = hit[1] if hit else ""
    topic = session_topic(prompt) if prompt else ""
    need = NEEDS_OWNER.get(status or "")
    parts = [name]
    if need:
        parts.append(need)
    if topic:
        parts.append(topic)
    return " - ".join(parts)


def cached_last_prompt(transcript_path: str, max_chars: int = 400) -> str:
    """The last human prompt already found for ``transcript_path``, or "".

    Reads NO file: the background refresh fills the cache while it names the
    session, and a request path must never walk a cold transcript. Whitespace
    is folded and the text clipped to ``max_chars`` -- a row carries a line,
    not the whole prompt.
    """
    if not transcript_path:
        return ""
    with _PROMPT_CACHE_LOCK:
        hit = _PROMPT_CACHE.get(transcript_path)
    text = " ".join(str(hit[1] if hit else "").split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


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
        try:
            # One stat, not exists() + stat(): on Windows each is a file open,
            # and this runs for every session on every refresh tick.
            size = path.stat().st_size
        except (FileNotFoundError, NotADirectoryError):
            return ""
        with _PROMPT_CACHE_LOCK:
            cached = _PROMPT_CACHE.get(transcript_path)
        # Warm: read only what was appended since the last look. A file that
        # SHRANK (rotated, replaced) invalidates -- it is not the same stream.
        # UNCHANGED is the common case on a 2 s poll and must read nothing at
        # all: computing `size - cached[0]` and falling through to the deep
        # bound made the cache inert (measured: warm was 1x cold).
        if cached and cached[0] == size:
            return cached[1]
        if cached and 0 < cached[0] < size:
            limit = size - cached[0]
        else:
            limit = min(size, _PROMPT_SCAN_BYTES)
        found = ""
        with open(path, "rb") as fh:
            # Each window's complete lines are parsed exactly ONCE; only the
            # partial first line is carried into the next (earlier) window.
            # Re-decoding the whole accumulated buffer per window made a cold
            # 12 MB scan quadratic -- hundreds of MB decoded per transcript.
            scanned, carry = 0, b""
            while scanned < limit:
                step = min(TRANSCRIPT_TAIL_SIZE, size - scanned)
                scanned += step
                fh.seek(size - scanned)
                lines = (fh.read(step) + carry).split(b"\n")
                if scanned < size:
                    carry, lines = lines[0], lines[1:]
                else:
                    carry = b""
                for raw in reversed(lines):
                    # Cheap byte filters first: a typed prompt is a user entry
                    # whose content is a plain STRING. Tool results (the bulk of
                    # the bytes) have list content and are skipped unparsed.
                    if b'"user"' not in raw or not _STRING_CONTENT.search(raw):
                        continue
                    try:
                        entry = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
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
    """(branch, tokens_spent) for a transcript, read to the end in one call.

    Claude Code writes one JSONL entry per content BLOCK and repeats the whole
    message's `usage` on each, so usage is counted once per `message.id` --
    summing per line over-counts a multi-block turn by the block count.
    """
    branch, tokens, _ = _scan_usage(transcript_path, max_bytes=None)
    return branch, tokens


def _scan_usage(transcript_path: str, max_bytes: Optional[int]) -> tuple[str, int, bool]:
    """Advance the usage scan of one transcript by at most ``max_bytes``.

    Returns ``(branch, tokens_so_far, caught_up)``. ``caught_up`` is True once
    every complete line up to EOF has been counted; until then ``tokens_so_far``
    is a PARTIAL sum and must not be shown as the session's spend. Transcripts
    here reach 130 MB, and reading one from byte 0 on a request path is what
    made a cold directory listing take most of a minute; a per-tick budget turns
    that into a background catch-up that no caller waits on.

    ``max_bytes=None`` reads everything available (the one-shot behaviour).
    A single line longer than the budget is still consumed whole -- otherwise
    the scan would stall on it forever.
    """
    if not transcript_path:
        return "", 0, True
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
    except OSError:
        return "", 0, True
    with _USAGE_CACHE_LOCK:
        cached = _USAGE_CACHE.get(transcript_path)
    offset, branch, tokens, last_id = 0, "", 0, ""
    if cached and cached[0] <= size:
        offset, branch, tokens, last_id = cached
        if offset == size:
            return branch, tokens, True
    remaining = size - offset
    budget = remaining if max_bytes is None else max(1, min(remaining, max_bytes))
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(budget)
            while len(chunk) < remaining and b"\n" not in chunk:
                more = fh.read(min(budget, remaining - len(chunk)))
                if not more:
                    break
                chunk += more
    except OSError:
        return branch, tokens, False
    read_to_eof = len(chunk) >= remaining
    # Only whole lines are consumed; a line still being written is re-read next time.
    end = chunk.rfind(b"\n")
    if end < 0:
        return branch, tokens, read_to_eof
    consumed = chunk[: end + 1]
    seen: set[str] = {last_id} if last_id else set()
    for raw in consumed.split(b"\n"):
        if b'"usage"' not in raw:
            # Most bytes are tool results with no usage: take the branch with a
            # regex instead of JSON-parsing megabytes per line. Claude Code
            # writes the top-level `gitBranch` before `message`, so the first
            # match is the entry's own field.
            m = _GIT_BRANCH.search(raw)
            if m and m.group(1).strip():
                branch = m.group(1).decode("utf-8", errors="replace").strip()
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
    return branch, tokens, read_to_eof


def _cached_usage(transcript_path: str) -> tuple[str, Optional[int]]:
    """What the usage scan already knows, with NO file I/O beyond one stat.

    Tokens are None unless the scan has caught up to the current file size.
    """
    if not transcript_path:
        return "", 0
    with _USAGE_CACHE_LOCK:
        cached = _USAGE_CACHE.get(transcript_path)
    if not cached:
        return "", None
    try:
        size = Path(transcript_path).stat().st_size
    except OSError:
        return cached[1], None
    # A trailing partial line is never consumed, so "caught up" is "within one
    # unfinished line of EOF"; the next refresh tick settles it exactly.
    return cached[1], (cached[2] if cached[0] == size else None)


#: transcript path -> (size, mtime_ns, result). A 2 s refresh over ~20 tabs
#: re-parsed ~5 MB of unchanged tails per tick; an unchanged file now costs one
#: stat. Only results that do not depend on the clock are kept: a pending tool
#: turns "working" into "blocked?" by AGE alone, with no byte written.
_STATUS_CACHE: dict[str, tuple[int, int, tuple[str, float, str]]] = {}
_STATUS_CACHE_LOCK = threading.Lock()
_CLOCK_DEPENDENT_STATUSES = ("working", "blocked?", "unknown")


#: transcript path -> (size, mtime_ns, tool_name, tool_started). A session with a
#: pending tool is the one case ``_STATUS_CACHE`` cannot hold (its status ages
#: from "working" to "blocked?" with no byte written), so it used to re-read and
#: re-parse a 256 KB tail every refresh tick for as long as the tool ran. What
#: the BYTES say -- which tool, since when -- is cached here instead, and only
#: the clock-dependent classification is recomputed per tick.
_PENDING_CACHE: dict[str, tuple[int, int, str, float]] = {}


def _classify_pending(tool_name: str, tool_started: float) -> tuple[str, float, str]:
    """Status of a session whose newest tool_use has no tool_result yet."""
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


def _bounded_put(cache: dict[str, Any], key: str, value: Any) -> None:
    cache[key] = value
    if len(cache) > 256:  # bounded: sessions come and go
        for stale in list(cache)[:64]:
            cache.pop(stale, None)


def _derive_status_from_transcript(transcript_path: str) -> tuple[str, float, str]:
    """`_derive_status_uncached`, skipped when the transcript has not changed."""
    try:
        st = Path(transcript_path).stat()
    except OSError:
        return _derive_status_uncached(transcript_path)
    key = (st.st_size, st.st_mtime_ns)
    with _STATUS_CACHE_LOCK:
        hit = _STATUS_CACHE.get(transcript_path)
        pending_hit = _PENDING_CACHE.get(transcript_path)
    if hit and (hit[0], hit[1]) == key:
        return hit[2]
    if pending_hit and (pending_hit[0], pending_hit[1]) == key:
        return _classify_pending(pending_hit[2], pending_hit[3])
    pending: list[tuple[str, float]] = []
    result = _derive_status_uncached(transcript_path, pending_out=pending)
    with _STATUS_CACHE_LOCK:
        if pending:
            _bounded_put(_PENDING_CACHE, transcript_path,
                         (key[0], key[1], pending[0][0], pending[0][1]))
        elif result[0] not in _CLOCK_DEPENDENT_STATUSES:
            _bounded_put(_STATUS_CACHE, transcript_path, (key[0], key[1], result))
    return result


def _derive_status_uncached(
    transcript_path: str, pending_out: Optional[list[tuple[str, float]]] = None,
) -> tuple[str, float, str]:
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
        try:
            file_size = path.stat().st_size  # one stat doubles as the existence check
        except (FileNotFoundError, NotADirectoryError):
            return "unknown", time.time(), "(transcript not found)"

        # Read the tail
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
            if pending_out is not None:
                pending_out.append((tool_name, tool_started))
            return _classify_pending(tool_name, tool_started)

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


@dataclass
class _Snapshot:
    """One background-built view of the directory.

    Daemon rows are kept by id and re-joined with the manager's CURRENT session
    list on every read, so a session spawned or stopped a moment ago is never
    missing or resurrected just because the snapshot is two seconds old.
    """

    generated_at: float
    daemon_rows: dict[str, UnifiedSession]
    discovered: list[UnifiedSession]
    #: False for the quick first pass (no usage scan, no deep prompt scan).
    complete: bool


#: How a builder obtains `tokens_spent`:
#: "full"   -- read the transcript to EOF now (inline mode; exact, can be slow)
#: "budget" -- advance by at most USAGE_BYTES_PER_TICK; None until caught up
#: "cached" -- no read at all; whatever the scan already knows, else None
_USAGE_MODES = ("full", "budget", "cached")


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
        # ── background mode (start_refresher) ──
        self.interval: float = REFRESH_INTERVAL_SECONDS
        self.usage_budget: int = USAGE_BYTES_PER_TICK
        self._snap_lock = threading.Lock()
        self._snapshot: Optional[_Snapshot] = None
        self._daemon_sessions_fn: Optional[Callable[[], list[dict[str, Any]]]] = None
        self._latest_daemon: list[dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._first = threading.Event()
        self.builds = 0
        self.last_build_ms = 0.0
        self.last_error = ""

    # ── row builders ────────────────────────────────────────────────────────

    def _daemon_row(
        self, info: dict[str, Any], usage_mode: str = "full", derive: bool = True,
    ) -> UnifiedSession:
        """One daemon HarnessSession.info() dict as a UnifiedSession.

        ``derive=False`` reads no file at all (a session the snapshot has not
        seen yet): status comes from the session's own state.
        """
        session_id = info.get("id", "")
        transcript = info.get("transcript", "")
        state = str(info.get("state") or "")
        status, activity_at, activity_summary = "idle", time.time(), ""
        if transcript and derive:
            status, activity_at, activity_summary = _derive_status_from_transcript(transcript)
        elif not derive and state:
            status = state
        # The session's OWN state outranks anything inferred from its transcript.
        # Measured 2026-09-19: a killed claude-tty (state=exited, exit 2) was listed
        # as `idle` with steer_capability `full`, because a transcript that simply
        # stopped growing reads as an idle tab -- so `tell` could address a corpse
        # and the desk could keep a body on stage for it.
        dead = state in ("exited", "failed")
        if dead:
            status = "exited"
        branch, tokens_spent = self._usage(transcript, usage_mode if derive else "cached")
        return UnifiedSession(
            id=session_id,
            title=info.get("title", ""),
            cwd=info.get("cwd", ""),
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

    def _usage(self, transcript: str, mode: str) -> tuple[str, Optional[int]]:
        if mode not in _USAGE_MODES:
            raise ValueError(f"unknown usage mode {mode!r}")
        if mode == "full":
            return _transcript_usage(transcript)
        if mode == "cached":
            return _cached_usage(transcript)
        branch, tokens, caught_up = _scan_usage(transcript, max_bytes=self.usage_budget)
        return branch, (tokens if caught_up else None)

    def _build_from_daemon(
        self, daemon_sessions: list[dict[str, Any]], usage_mode: str = "full",
    ) -> list[UnifiedSession]:
        """Convert daemon HarnessSession.info() dicts to UnifiedSession."""
        return [self._daemon_row(info, usage_mode) for info in daemon_sessions]

    def _build_from_discovered(
        self,
        discovered: list[DiscoveredSession],
        usage_mode: str = "full",
        prompt_scan: bool = True,
    ) -> list[UnifiedSession]:
        """Convert DiscoveredSession to UnifiedSession.

        ``prompt_scan=False`` names the session from the prompt cache only, so a
        first listing never walks megabytes back through a cold transcript.
        """
        out = []
        for disc in discovered:
            status, activity_at, activity_summary = "idle", time.time(), ""
            if disc.transcript_path:
                status, activity_at, activity_summary = _derive_status_from_transcript(
                    disc.transcript_path
                )
            status = merge_registry_status(disc.status, status)
            waiting_for = getattr(disc, "waiting_for", "")
            if status == "waiting-permission" and waiting_for:
                activity_summary = f"waiting: {waiting_for}"
            branch, tokens_spent = self._usage(disc.transcript_path, usage_mode)

            out.append(
                UnifiedSession(
                    id=disc.id,
                    # disc.name is Claude Code's OWN `<repo> <branch> <HH:MM>`;
                    # the helper adds what the session is doing and whether it
                    # is stuck on a human.
                    title=session_display_title(disc.id, disc.cwd, disc.transcript_path,
                                                claude_name=disc.name, status=status,
                                                prompt_scan=prompt_scan),
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

    @staticmethod
    def _combine(
        daemon_unified: list[UnifiedSession], discovered_unified: list[UnifiedSession],
    ) -> list[UnifiedSession]:
        """Merge, deduplicating by id (daemon takes precedence), newest first.

        A daemon-owned claude-tty session is ALSO discovered from Claude's own
        state files under the --session-id the daemon minted for it -- fold that
        row into the daemon row, or one Claude Code lists twice: once steerable,
        once not.
        """
        daemon_ids = {s.id for s in daemon_unified}
        daemon_ids |= {
            str((s.extras or {}).get("harness_session_id") or "") for s in daemon_unified
        } - {""}
        combined = daemon_unified + [d for d in discovered_unified if d.id not in daemon_ids]
        combined.sort(key=lambda s: s.last_activity_at, reverse=True)
        return combined

    # ── reads ───────────────────────────────────────────────────────────────

    @property
    def refreshing(self) -> bool:
        """True once a background refresher owns the rebuilds."""
        return self._thread is not None

    def list_sessions_sync(self, daemon_sessions: list[dict[str, Any]]) -> list[UnifiedSession]:
        """List all sessions (daemon + discovered).

        With a refresher running this never does transcript I/O: it re-joins the
        last snapshot with ``daemon_sessions`` and returns. Without one it
        rebuilds inline behind a CACHE_TTL_SECONDS cache.

        Args:
            daemon_sessions: Output of SessionManager.list_sessions()

        Returns:
            Unified list sorted by last activity time (newest first).
        """
        return self.list_with_meta(daemon_sessions)[0]

    def list_with_meta(
        self, daemon_sessions: list[dict[str, Any]],
    ) -> tuple[list[UnifiedSession], dict[str, Any]]:
        """The rows plus ``{"generated_at", "stale"}`` describing them.

        ``generated_at`` is when the rows were built (None: nothing built yet);
        ``stale`` is True when they are older than STALE_AFTER_INTERVALS refresh
        intervals or no snapshot exists yet.
        """
        if not self.refreshing:
            rows = self._list_inline(daemon_sessions)
            return rows, {"generated_at": self._cache_time or time.time(), "stale": False}
        self._latest_daemon = list(daemon_sessions)
        snap = self._snapshot
        if snap is None:
            # First caller after start: the refresher's first pass is the cheap
            # one (registry files + transcript tails), so a short wait usually
            # gets it. If discovery itself is slow, answer empty and stale
            # rather than hold the caller.
            self._first.wait(FIRST_SNAPSHOT_WAIT_SECONDS)
            snap = self._snapshot
        rows = self._join(snap, daemon_sessions)
        if snap is None:
            return rows, {"generated_at": None, "stale": True}
        age = time.time() - snap.generated_at
        stale = age > self.interval * STALE_AFTER_INTERVALS
        return rows, {"generated_at": snap.generated_at, "stale": stale}

    def _list_inline(self, daemon_sessions: list[dict[str, Any]]) -> list[UnifiedSession]:
        now = time.time()
        with self._lock:
            if self._cache is not None and (now - self._cache_time) < CACHE_TTL_SECONDS:
                return self._cache
            daemon_unified = self._build_from_daemon(daemon_sessions)
            discovered_unified = self._build_from_discovered(self._discover())
            self._cache = self._combine(daemon_unified, discovered_unified)
            self._cache_time = now
        return self._cache or []

    def _join(
        self, snap: Optional[_Snapshot], daemon_sessions: list[dict[str, Any]],
    ) -> list[UnifiedSession]:
        """Snapshot rows + the manager's CURRENT daemon sessions. No file I/O."""
        known = snap.daemon_rows if snap else {}
        daemon_rows: list[UnifiedSession] = []
        unseen = False
        for info in daemon_sessions:
            base = known.get(info.get("id", ""))
            if base is None:
                unseen = True
                daemon_rows.append(self._daemon_row(info, derive=False))
                continue
            dead = str(info.get("state") or "") in ("exited", "failed")
            daemon_rows.append(replace(
                base,
                title=info.get("title", ""),
                cwd=info.get("cwd", ""),
                extras=info,
                status="exited" if dead else base.status,
                steer_capability="none" if dead else base.steer_capability,
            ))
        if unseen and self.refreshing:
            self._wake.set()  # a new session: refresh now, not in 2 s
        return self._combine(daemon_rows, list(snap.discovered) if snap else [])

    # ── background refresher ────────────────────────────────────────────────

    def start_refresher(
        self,
        daemon_sessions_fn: Optional[Callable[[], list[dict[str, Any]]]] = None,
        interval: Optional[float] = None,
    ) -> None:
        """Hand every rebuild to a background thread. Idempotent.

        Args:
            daemon_sessions_fn: returns the manager's current session infos. When
                omitted the refresher uses whatever the last reader passed in.
            interval: seconds between refreshes (jittered +/-20 %).
        """
        with self._snap_lock:
            if self._thread is not None:
                return
            if interval is not None:
                self.interval = max(0.05, float(interval))
            self._daemon_sessions_fn = daemon_sessions_fn
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="session-directory-refresh", daemon=True,
            )
            self._thread.start()

    def stop_refresher(self, timeout: float = 2.0) -> None:
        """Stop the refresher; the directory falls back to inline rebuilds."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._snap_lock:
            self._thread = None
            self._snapshot = None
            self._first.clear()

    def _run(self) -> None:
        deep = False
        while not self._stop.is_set():
            try:
                self.refresh(deep=deep)
                deep = True
            except Exception as exc:  # noqa: BLE001 - the thread must outlive one bad pass
                # If this thread died the directory would freeze at its last
                # snapshot while `stale` climbed; logging keeps it visible.
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("session directory refresh failed: %s", self.last_error)
            finally:
                self._first.set()
            if self._stop.is_set():
                break
            if self._wake.wait(self.interval * random.uniform(0.8, 1.2)):
                self._wake.clear()

    def refresh(self, deep: bool = True) -> _Snapshot:
        """Build and publish one snapshot. Runs on the refresher thread.

        ``deep=False`` is the quick first pass: discovery and transcript tails
        only, with no usage scan and no deep prompt scan.
        """
        started = time.time()
        if self._daemon_sessions_fn is not None:
            infos = list(self._daemon_sessions_fn())
        else:
            infos = list(self._latest_daemon)
        usage_mode = "budget" if deep else "cached"
        daemon_rows = {r.id: r for r in self._build_from_daemon(infos, usage_mode=usage_mode)}
        discovered = self._build_from_discovered(
            self._discover(), usage_mode=usage_mode, prompt_scan=deep,
        )
        snap = _Snapshot(
            generated_at=time.time(), daemon_rows=daemon_rows,
            discovered=discovered, complete=deep,
        )
        with self._snap_lock:
            self._snapshot = snap
        self.builds += 1
        self.last_build_ms = (time.time() - started) * 1000.0
        self.last_error = ""
        return snap

    def stats(self) -> dict[str, Any]:
        """O(1) liveness of the refresher, for a health probe."""
        snap = self._snapshot
        rows = (list(snap.daemon_rows.values()) + snap.discovered) if snap else []
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "builds": self.builds,
            "last_build_ms": round(self.last_build_ms, 1),
            "generated_at": snap.generated_at if snap else None,
            "complete": bool(snap and snap.complete),
            "rows": len(rows),
            "usage_pending": sum(1 for r in rows if r.tokens_spent is None),
            "last_error": self.last_error,
        }


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
