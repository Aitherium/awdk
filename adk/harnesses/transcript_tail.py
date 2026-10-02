"""A bounded slice of one session's transcript, in the harness event vocabulary.

WHY THIS EXISTS
---------------
``/sessions/{id}/events`` and ``/stream`` resolve through the daemon's own
session manager, so a Claude Code tab the daemon DISCOVERED (the owner's own
terminals -- usually every session on the box) had no readable body anywhere:
the cockpit listed it and showed an empty pane. Its transcript is already on
disk; this reads a slice of it for ``GET /sessions/{id}/transcript``.

ONE MAPPER. The entries are mapped by ``transcript_bridge.events_from_entry``,
the same function that feeds the room, and then renamed onto the event kinds a
daemon-owned session streams (``turn.started``, ``text.delta``,
``thinking.delta``, ``tool.call``) so a client renders both with one pane.

BOUNDED, ALWAYS. A transcript grows without limit (hundreds of MB on a
long-lived tab) and one line can be megabytes of tool output. A read consumes
at most ``limit`` bytes of file, parses no line over ``MAX_LINE_BYTES``, clips
every field to ``MAX_FIELD_CHARS`` and stops at ``MAX_EVENTS``; the caller pages
with the returned ``next`` cursor. The cursor is a BYTE OFFSET into the file, so
a reconnect resumes exactly where it stopped without the daemon keeping state.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.harnesses.transcript_bridge import events_from_entry

#: Bytes of file one read consumes by default (and the size of the first tail).
TAIL_BYTES = 256 * 1024

#: The most a caller may ask one read to consume.
MAX_BYTES = 1024 * 1024

#: A line longer than this is skipped, not parsed: it is a tool result or an
#: attachment, and loading it to drop it would be the unbounded read this avoids.
MAX_LINE_BYTES = 4 * 1024 * 1024

#: Events per response. One entry can map to several, so this is a soft stop
#: checked between lines.
MAX_EVENTS = 400

#: Per-field clip. Far above the room's 300 so a reply reads whole, far below
#: "a pasted file".
MAX_FIELD_CHARS = 4000

_KINDS = {
    "classify": "turn.started",
    "message": "text.delta",
    "thinking": "thinking.delta",
    "tool_call": "tool.call",
}


class TranscriptUnavailable(Exception):
    """The row names no transcript this daemon can read."""


def _epoch(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def harness_events(entry: Dict[str, Any], session_id: str, cwd: str,
                   seq: int) -> List[Dict[str, Any]]:
    """One transcript entry as harness events, numbered from ``seq``."""
    out: List[Dict[str, Any]] = []
    ts = _epoch(entry.get("timestamp"))
    for event in events_from_entry(entry, session_id, cwd, max_field=MAX_FIELD_CHARS):
        kind = _KINDS.get(str(event.get("type")))
        if kind is None:
            continue
        payload = event.get("payload") or {}
        row: Dict[str, Any] = {
            "seq": seq + len(out),
            "session_id": session_id,
            "ts": ts,
            "kind": kind,
            "text": "",
            "tool": "",
            "tool_use_id": "",
            "data": {},
            "turn": 0,
        }
        if kind == "turn.started":
            row["text"] = str(payload.get("prompt") or "")
        elif kind == "tool.call":
            row["tool"] = str(payload.get("tool") or "")
            row["data"] = {
                "input": {k: v for k, v in payload.items() if k not in ("tool", "cwd")},
            }
        else:
            row["text"] = str(payload.get("text") or "")
        out.append(row)
    return out


def read_transcript(path: str, session_id: str, cwd: str = "",
                    since: Optional[int] = None,
                    limit: int = TAIL_BYTES) -> Dict[str, Any]:
    """Events from ``path`` starting at byte ``since`` (None/negative = the tail).

    Returns ``events``, ``next`` (the cursor to pass as ``since``), ``size``,
    ``more`` (complete lines remain past ``next``) and ``truncated`` (older
    history before this slice was not sent). A cursor past the end of the file
    means it was rotated or truncated, and is answered with the tail.
    """
    target = Path(path)
    if target.suffix.lower() != ".jsonl" or not target.is_file():
        raise TranscriptUnavailable(f"no readable transcript for session {session_id!r}")
    limit = max(1, min(int(limit), MAX_BYTES))
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise TranscriptUnavailable(str(exc)) from exc

    tail = since is None or since < 0 or since > size
    start = max(0, size - limit) if tail else int(since)
    events: List[Dict[str, Any]] = []
    skipped = 0
    try:
        with target.open("rb") as handle:
            if tail and start > 0:
                # Landed mid-file: unless the byte before is a newline this is
                # the middle of a line, which is dropped rather than parsed.
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    handle.readline(MAX_LINE_BYTES)
            else:
                handle.seek(start)
            offset = handle.tell()
            first = offset
            while offset - first < limit and len(events) < MAX_EVENTS:
                line = handle.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    if len(line) <= MAX_LINE_BYTES:
                        break  # partial write in flight; the next read takes it
                    # Oversized: walk to its end without holding it.
                    dropped, complete = len(line), False
                    while True:
                        chunk = handle.readline(MAX_LINE_BYTES)
                        if not chunk:
                            break
                        dropped += len(chunk)
                        if chunk.endswith(b"\n"):
                            complete = True
                            break
                    if not complete:
                        break
                    offset += dropped
                    skipped += 1
                    continue
                line_start = offset
                offset += len(line)
                try:
                    entry = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    events.extend(harness_events(entry, session_id, cwd, line_start))
    except OSError as exc:
        raise TranscriptUnavailable(f"read {target.name}: {exc}") from exc

    return {
        "events": events,
        "next": offset,
        "size": size,
        "more": offset < size,
        "truncated": bool(tail and first > 0),
        "skipped_lines": skipped,
    }
