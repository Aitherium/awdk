"""Transcript bridge — every running Claude Code tab, on the spine, for free.

WHY THIS EXISTS INSTEAD OF A PER-TOOL-CALL HOOK
-----------------------------------------------
The obvious way to get a tab's tool calls into the room is a ``PostToolUse`` hook. It
was built, and then measured: **~224 ms per invocation on this box, of which bare
CPython startup alone is ~302 ms** — the script's own work is lost in the noise. Every
tool call, in every one of a dozen live sessions, would pay an interpreter spawn, and
hooks run SYNCHRONOUSLY inside the owner's session, so that is latency a human waits.

But Claude Code already writes every tool call, every result and every message to
``~/.claude/projects/<encoded-cwd>/<id>.jsonl``. Tailing what is already being written
costs the session exactly nothing and needs no cooperation from it — which is what
COCKPIT-DESIGN.md said in the first place. So the transcript is the primary producer
for tab sessions, and the hook spool is reserved for what a transcript cannot express:
turn boundaries with intent, steering acknowledgements, and team task events.

Zero cooperation also means it works on sessions that were already running before any
of this existed, which a hook can never do.

THREADS, NOT THE EVENT LOOP
---------------------------
Discovery probes processes and reads files. A blocking call inside a coroutine
is not "slow", it is an outage for every concurrent request for its full duration. This
runs on its own daemon thread, exactly like ``session_directory``'s own I/O.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from adk.harnesses.discovery import discover_live_sessions
from adk.harnesses.rooms import RoomError, RoomRegistry, default_registry

#: How often to re-discover sessions. Discovery walks state files and probes pids, so
#: it is deliberately slower than the tail loop.
DISCOVERY_INTERVAL = 10.0

#: How often to read new transcript bytes.
TAIL_INTERVAL = 0.75

#: Cap on any single rendered field so one enormous tool argument cannot dominate a
#: room event. The transcript keeps the full record; the room carries the signal.
MAX_FIELD = 300


def _clip(value: Any, limit: int = MAX_FIELD) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(tool_input: Any) -> Dict[str, str]:
    """The one or two fields that make a tool call READABLE in a room row."""
    if not isinstance(tool_input, dict):
        return {}
    out: Dict[str, str] = {}
    for key in ("file_path", "command", "pattern", "path", "url", "description", "prompt"):
        if tool_input.get(key):
            out[key] = _clip(tool_input[key])
            break
    return out


def events_from_entry(entry: Dict[str, Any], session_id: str, cwd: str) -> List[Dict[str, Any]]:
    """Map one Claude Code transcript entry onto zero or more AitherEvents.

    ``pillar`` is left absent on purpose — the room derives it from the event type, so
    this bridge never carries a second copy of the pillar vocabulary.
    """
    kind = entry.get("type")
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")

    actor = {
        "kind": "claude_code",
        "id": session_id,
        "name": Path(cwd).name or session_id,
    }
    base = {
        "v": 1,
        "room": "main",
        "session": session_id,
        "actor": actor,
        "tier": "host",
        "correlation_id": session_id,
    }
    out: List[Dict[str, Any]] = []

    # A human prompt arrives as a user entry whose content is a plain string. Tool
    # RESULTS also arrive as user entries (with a content list), which is why the
    # discriminator is the shape and not the role -- same trap hook_common.py documents.
    if kind == "user" and isinstance(content, str) and content.strip():
        out.append({**base, "type": "classify", "stage": "transcript.user",
                    "payload": {"prompt": _clip(content), "cwd": _clip(cwd)}})
        return out

    if kind != "assistant" or not isinstance(content, list):
        return out

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            payload = {"tool": _clip(block.get("name"), 80), "cwd": _clip(cwd)}
            payload.update(_tool_summary(block.get("input")))
            out.append({**base, "type": "tool_call", "stage": "transcript.assistant",
                        "payload": payload})
        elif btype == "thinking":
            text = block.get("thinking") or block.get("text") or ""
            if str(text).strip():
                out.append({**base, "type": "thinking", "stage": "transcript.assistant",
                            "payload": {"text": _clip(text)}})
        elif btype == "text":
            text = block.get("text") or ""
            if str(text).strip():
                out.append({**base, "type": "message", "stage": "transcript.assistant",
                            "payload": {"text": _clip(text)}})
    return out


class TranscriptBridge:
    """Tails discovered Claude Code transcripts and publishes into rooms."""

    def __init__(
        self,
        registry: Optional[RoomRegistry] = None,
        room: str = "main",
        replay_existing: bool = False,
        discover_fn=None,
    ) -> None:
        self.registry = registry or default_registry()
        self.room = room
        self._replay_existing = replay_existing
        self._discover = discover_fn or discover_live_sessions
        #: transcript path -> byte offset consumed
        self._offsets: Dict[str, int] = {}
        #: transcript path -> (session_id, cwd)
        self._known: Dict[str, tuple] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_discovery = 0.0
        self.published = 0
        self.rejected = 0
        self.sessions_seen = 0
        self.last_error = ""

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="aeon-transcript-bridge", daemon=True
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
                self.tick()
            except Exception as exc:  # noqa: BLE001
                # If this thread dies, tab sessions silently stop appearing and the
                # room reads as "nobody is working". Record and continue.
                self.last_error = str(exc)
                sys.stderr.write(f"[aeon-transcript] tick failed: {exc}\n")
            self._stop.wait(TAIL_INTERVAL)

    # ── work ────────────────────────────────────────────────────────────────

    def tick(self) -> int:
        now = time.time()
        if now - self._last_discovery >= DISCOVERY_INTERVAL:
            self._last_discovery = now
            self.refresh_sessions()
        published = 0
        for path, (session_id, cwd) in list(self._known.items()):
            published += self._drain(Path(path), session_id, cwd)
        return published

    def refresh_sessions(self) -> int:
        """Re-discover live tabs. Sessions that vanish keep their offsets so a
        restarted tab does not replay its history."""
        try:
            sessions: Iterable = self._discover()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"discovery failed: {exc}"
            sys.stderr.write(f"[aeon-transcript] {self.last_error}\n")
            return 0
        count = 0
        for session in sessions:
            path = getattr(session, "transcript_path", "") or ""
            if not path:
                continue
            self._known[path] = (getattr(session, "id", ""), getattr(session, "cwd", ""))
            count += 1
        self.sessions_seen = count
        return count

    def _drain(self, path: Path, session_id: str, cwd: str) -> int:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return 0

        if key not in self._offsets:
            # Start at the END of a transcript seen for the first time. Replaying a
            # long session's whole history into the room on daemon start would bury
            # live traffic under archaeology.
            self._offsets[key] = 0 if self._replay_existing else size

        offset = self._offsets[key]
        if size < offset:
            offset = 0  # rotated or truncated: re-read rather than go silent
        if size == offset:
            return 0

        published = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                for line in handle:
                    if not line.endswith("\n"):
                        break  # partial write in flight; pick it up next pass
                    offset += len(line.encode("utf-8"))
                    published += self._publish_line(line, session_id, cwd)
        except OSError as exc:
            self.last_error = f"read {path}: {exc}"
            return published

        self._offsets[key] = offset
        return published

    def _publish_line(self, line: str, session_id: str, cwd: str) -> int:
        line = line.strip()
        if not line:
            return 0
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return 0
        if not isinstance(entry, dict):
            return 0

        room = self.registry.get_or_create(self.room)
        published = 0
        for event in events_from_entry(entry, session_id, cwd):
            try:
                room.publish(event)
                published += 1
                self.published += 1
            except RoomError as exc:
                self.rejected += 1
                self.last_error = str(exc)
                sys.stderr.write(f"[aeon-transcript] rejected: {exc}\n")
        return published

    def stats(self) -> Dict[str, Any]:
        return {
            "sessions": self.sessions_seen,
            "transcripts": len(self._offsets),
            "published": self.published,
            "rejected": self.rejected,
            "running": self._thread is not None and self._thread.is_alive(),
            "last_error": self.last_error,
        }


_bridge: Optional[TranscriptBridge] = None
_bridge_lock = threading.Lock()


def default_bridge() -> TranscriptBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = TranscriptBridge()
        return _bridge
