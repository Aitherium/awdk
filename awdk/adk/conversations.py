"""Persistent Conversation Store — JSON file-based conversation storage.

Lightweight port of AitherOS ConversationStore. Stores conversations as
JSON files in ~/.aither/conversations/. Survives restarts.

Enhanced with session repair:
  - 7-phase message history validation
  - Auto-recovery from corrupted/truncated sessions
  - Role alternation enforcement
  - Orphan tool-result detection and cleanup
  - Timestamp monotonicity checks
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.conversations")

# Optional selfgraph integration (no-op if unavailable or disabled)
try:
    from adk.selfgraph import current_run, ProvNodeType, make_node_id
    _SELFGRAPH_AVAILABLE = True
except ImportError:
    _SELFGRAPH_AVAILABLE = False

_LRU_MAX = 50  # Max conversations in LRU cache


# ─────────────────────────────────────────────────────────────────────────────
# Session Repair (7-phase validation)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RepairReport:
    """Report from session repair."""
    session_id: str
    phases_run: int = 0
    issues_found: int = 0
    issues_fixed: int = 0
    messages_removed: int = 0
    messages_reordered: int = 0
    backup_created: bool = False
    details: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.issues_found == 0


@dataclass
class Conversation:
    """A conversation session."""
    session_id: str
    agent_name: str
    messages: list[dict] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict = field(default_factory=dict)


class ConversationStore:
    """Persistent conversation storage using JSON files.

    Each session is stored as a separate JSON file for easy inspection
    and backup. An LRU cache keeps hot sessions in memory.

    Optional cloud sync:
        After each save, if a sync_hook is registered, it is called to push
        the session to a remote portal/gateway. The sync hook is async and
        runs in the background (fire-and-forget).
    """

    def __init__(self, data_dir: str | Path | None = None):
        if data_dir is None:
            data_dir = Path.home() / ".aither" / "conversations"
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: OrderedDict[str, Conversation] = OrderedDict()
        self._sync_hook = None  # Optional async callable for cloud sync
        self._watermark_file = self._dir.parent / "pull_watermark.json"  # ~/.aither/pull_watermark.json

    def _path(self, session_id: str) -> Path:
        # Sanitize session_id for filesystem
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
        return self._dir / f"{safe_id}.json"

    def _load(self, session_id: str) -> Conversation | None:
        """Load a conversation from disk."""
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Conversation(
                session_id=data.get("session_id", session_id),
                agent_name=data.get("agent_name", ""),
                messages=data.get("messages", []),
                created_at=data.get("created_at", 0.0),
                updated_at=data.get("updated_at", 0.0),
                metadata=data.get("metadata", {}),
            )
        except Exception as e:
            logger.warning("Failed to load conversation %s: %s", session_id, e)
            return None

    def set_sync_hook(self, hook) -> None:
        """
        Register an async callable to be invoked after each save.

        The hook will be called with:
            await hook(session_id, agent_name, messages, created_at, updated_at, metadata)

        If the hook raises an exception, it is logged but does NOT block save.
        """
        self._sync_hook = hook

    def _record_conversation_selfgraph(self, conv: Conversation) -> None:
        """Record conversation as a SOURCE node to an active run's graph (best-effort).

        Creates a SOURCE node with uri=conversation://<session_id> so claims can
        later cite the conversation they came from. Does NOT record message
        content (graph is durable and replicated; conversations may not be).
        """
        if not _SELFGRAPH_AVAILABLE:
            return

        try:
            recorder = current_run()
            if not recorder:
                return  # No active run to record to

            # Create SOURCE node for the conversation (uri, not content)
            uri = f"conversation://{conv.session_id}"
            source_node = recorder.source(
                uri,
                title=f"Agent: {conv.agent_name}" if conv.agent_name else "",
            )
            logger.debug("recorded conversation source %s to graph", uri)
        except Exception:  # noqa: BLE001 — selfgraph recording is best-effort
            pass

    def _save(self, conv: Conversation) -> None:
        """Save a conversation to disk and trigger optional sync hook."""
        path = self._path(conv.session_id)
        try:
            data = {
                "session_id": conv.session_id,
                "agent_name": conv.agent_name,
                "messages": conv.messages,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
                "metadata": conv.metadata,
            }
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error("Failed to save conversation %s: %s", conv.session_id, e)

        # Record to selfgraph if a run is active (best-effort)
        self._record_conversation_selfgraph(conv)

        # Fire-and-forget async sync hook (if registered)
        if self._sync_hook:
            try:
                # Use asyncio.create_task if running in an event loop
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._sync_hook(
                            conv.session_id,
                            conv.agent_name,
                            conv.messages,
                            conv.created_at,
                            conv.updated_at,
                            conv.metadata,
                        )
                    )
                except RuntimeError:
                    # No event loop running — can't call async hook
                    logger.debug(
                        "No event loop to invoke sync hook for session %s — skipping",
                        conv.session_id
                    )
            except Exception as e:
                logger.debug(
                    "Sync hook for session %s raised exception (non-fatal): %s",
                    conv.session_id, e
                )

    def _touch_cache(self, session_id: str, conv: Conversation) -> None:
        """Add/move to front of LRU cache."""
        self._cache.pop(session_id, None)
        self._cache[session_id] = conv
        # Evict oldest if over limit
        while len(self._cache) > _LRU_MAX:
            self._cache.popitem(last=False)

    async def get_or_create(self, session_id: str, agent_name: str = "") -> Conversation:
        """Get an existing conversation or create a new one."""
        # Check cache first
        if session_id in self._cache:
            conv = self._cache[session_id]
            self._touch_cache(session_id, conv)
            return conv

        # Try disk
        conv = self._load(session_id)
        if conv is not None:
            self._touch_cache(session_id, conv)
            return conv

        # Create new
        now = time.time()
        conv = Conversation(
            session_id=session_id,
            agent_name=agent_name,
            created_at=now,
            updated_at=now,
        )
        self._touch_cache(session_id, conv)
        self._save(conv)
        return conv

    async def append_message(
        self, session_id: str, role: str, content: str, agent_name: str = ""
    ) -> None:
        """Append a message to a conversation."""
        conv = await self.get_or_create(session_id, agent_name)
        conv.messages.append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
        })
        conv.updated_at = time.time()
        self._save(conv)

    async def get_recent(self, session_id: str, n: int = 20) -> list[dict]:
        """Get the N most recent messages from a conversation."""
        conv = await self.get_or_create(session_id)
        return conv.messages[-n:]

    async def list_sessions(self, agent_name: str | None = None) -> list[dict]:
        """List all conversation sessions, optionally filtered by agent."""
        sessions = []
        for path in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if agent_name and data.get("agent_name") != agent_name:
                    continue
                sessions.append({
                    "session_id": data.get("session_id", path.stem),
                    "agent_name": data.get("agent_name", ""),
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at", 0.0),
                    "updated_at": data.get("updated_at", 0.0),
                })
            except Exception:
                continue
        return sessions

    async def delete_session(self, session_id: str) -> bool:
        """Delete a conversation session."""
        self._cache.pop(session_id, None)
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    async def bulk_delete_sessions(self, session_ids: list[str]) -> int:
        """Delete multiple conversation sessions. Returns count deleted."""
        deleted = 0
        for sid in session_ids:
            if await self.delete_session(sid):
                deleted += 1
        return deleted

    async def load_full_history(self, session_id: str) -> list[dict]:
        """Load the complete message history for a session.

        Unlike get_recent() which returns the last N messages, this returns
        ALL messages. Used when switching sessions to restore full context.
        """
        conv = await self.get_or_create(session_id)
        return list(conv.messages)

    # ─────────────────────────────────────────────────────────────────────
    # SESSION REPAIR (7-phase validation)
    # ─────────────────────────────────────────────────────────────────────

    async def repair_session(self, session_id: str, auto_fix: bool = True) -> RepairReport:
        """Run 7-phase session repair on a conversation.

        Phases:
          1. Schema validation — ensure required fields exist
          2. Role validation — check role values are valid
          3. Content validation — check for empty/null content
          4. Timestamp monotonicity — ensure timestamps are non-decreasing
          5. Role alternation — detect back-to-back same-role messages
          6. Orphan tool results — find tool results without preceding assistant
          7. Integrity check — verify message count and compute hash

        Args:
            session_id: The session to repair.
            auto_fix: If True, automatically fix issues. If False, report only.

        Returns:
            RepairReport with details of what was found/fixed.
        """
        report = RepairReport(session_id=session_id)
        conv = await self.get_or_create(session_id)

        if not conv.messages:
            report.phases_run = 7
            return report

        # Create backup before repair
        if auto_fix:
            backup_path = self._path(session_id).with_suffix(".json.bak")
            try:
                path = self._path(session_id)
                if path.exists():
                    shutil.copy2(path, backup_path)
                    report.backup_created = True
            except Exception as e:
                logger.warning("Failed to create backup for %s: %s", session_id, e)

        messages = conv.messages
        valid_roles = {"system", "user", "assistant", "tool", "function"}

        # Phase 1: Schema validation
        report.phases_run = 1
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                report.issues_found += 1
                report.details.append(f"P1: Message {i} is not a dict")
                if auto_fix:
                    messages[i] = {"role": "system", "content": str(msg), "timestamp": time.time()}
                    report.issues_fixed += 1
            elif "role" not in msg or "content" not in msg:
                report.issues_found += 1
                report.details.append(f"P1: Message {i} missing role or content")
                if auto_fix:
                    msg.setdefault("role", "system")
                    msg.setdefault("content", "")
                    report.issues_fixed += 1

        # Phase 2: Role validation
        report.phases_run = 2
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in valid_roles:
                report.issues_found += 1
                report.details.append(f"P2: Message {i} has invalid role '{role}'")
                if auto_fix:
                    msg["role"] = "system"
                    report.issues_fixed += 1

        # Phase 3: Content validation
        report.phases_run = 3
        to_remove = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if content is None or (isinstance(content, str) and not content.strip()):
                # Empty tool results are OK, empty user/assistant messages are not
                if msg.get("role") not in ("tool", "function"):
                    report.issues_found += 1
                    report.details.append(f"P3: Message {i} ({msg.get('role')}) has empty content")
                    if auto_fix:
                        to_remove.append(i)
                        report.issues_fixed += 1

        if auto_fix and to_remove:
            for i in reversed(to_remove):
                messages.pop(i)
                report.messages_removed += 1

        # Phase 4: Timestamp monotonicity
        report.phases_run = 4
        last_ts = 0.0
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            ts = msg.get("timestamp", 0.0)
            if isinstance(ts, (int, float)) and ts < last_ts:
                report.issues_found += 1
                report.details.append(
                    f"P4: Message {i} timestamp {ts} < previous {last_ts}"
                )
                if auto_fix:
                    msg["timestamp"] = last_ts + 0.001
                    report.issues_fixed += 1
                    report.messages_reordered += 1
            if isinstance(ts, (int, float)):
                last_ts = max(last_ts, ts)

        # Phase 5: Role alternation (detect loops)
        report.phases_run = 5
        consecutive_same = 0
        last_role = None
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == last_role and role in ("user", "assistant"):
                consecutive_same += 1
                if consecutive_same >= 3:
                    report.issues_found += 1
                    report.details.append(
                        f"P5: {consecutive_same + 1} consecutive '{role}' messages at index {i}"
                    )
            else:
                consecutive_same = 0
            last_role = role

        # Phase 6: Orphan tool results
        report.phases_run = 6
        orphan_indices = []
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "tool":
                # Tool result should follow an assistant message with tool_calls
                if i == 0:
                    report.issues_found += 1
                    report.details.append(f"P6: Orphan tool result at index {i} (first message)")
                    orphan_indices.append(i)
                else:
                    prev = messages[i - 1] if i > 0 else None
                    if prev and isinstance(prev, dict):
                        prev_role = prev.get("role")
                        if prev_role not in ("assistant", "tool"):
                            report.issues_found += 1
                            report.details.append(
                                f"P6: Orphan tool result at index {i} "
                                f"(preceded by '{prev_role}')"
                            )
                            orphan_indices.append(i)

        if auto_fix and orphan_indices:
            for i in reversed(orphan_indices):
                messages.pop(i)
                report.messages_removed += 1
                report.issues_fixed += 1

        # Phase 7: Integrity check
        report.phases_run = 7
        content_hash = hashlib.sha256(
            json.dumps(messages, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        conv.metadata["last_repair"] = {
            "timestamp": time.time(),
            "message_count": len(messages),
            "content_hash": content_hash,
            "issues_found": report.issues_found,
            "issues_fixed": report.issues_fixed,
        }

        # Save repaired conversation
        if auto_fix and report.issues_fixed > 0:
            conv.messages = messages
            conv.updated_at = time.time()
            self._save(conv)
            logger.info(
                "Repaired session %s: %d issues found, %d fixed, %d messages removed",
                session_id, report.issues_found, report.issues_fixed, report.messages_removed,
            )

        return report

    async def validate_session(self, session_id: str) -> RepairReport:
        """Validate a session without making changes (dry run)."""
        return await self.repair_session(session_id, auto_fix=False)

    async def repair_all(self, auto_fix: bool = True) -> list[RepairReport]:
        """Repair all sessions on disk."""
        reports = []
        for path in self._dir.glob("*.json"):
            if path.suffix == ".bak":
                continue
            session_id = path.stem
            report = await self.repair_session(session_id, auto_fix=auto_fix)
            if not report.clean:
                reports.append(report)
        return reports

    # ─────────────────────────────────────────────────────────────────────
    # PULL WATERMARK MANAGEMENT (session sync)
    # ─────────────────────────────────────────────────────────────────────

    async def get_pull_watermark(self) -> float:
        """
        Get the last pull watermark (max remote updated_at we've seen).

        Returns:
            Unix timestamp, or 0.0 if no watermark has been set
        """
        if not self._watermark_file.exists():
            return 0.0

        try:
            data = json.loads(self._watermark_file.read_text(encoding="utf-8"))
            return data.get("last_updated_at", 0.0)
        except Exception as e:
            logger.warning("Failed to read pull watermark: %s", e)
            return 0.0

    async def set_pull_watermark(self, ts: float) -> None:
        """
        Set the pull watermark (monotonic — only increases).

        Args:
            ts: Unix timestamp to set as watermark
        """
        # Ensure monotonic — don't go backwards
        current = await self.get_pull_watermark()
        if ts < current:
            logger.warning(
                "Watermark regression detected (current=%.1f, new=%.1f) — keeping current",
                current, ts
            )
            return

        try:
            data = {
                "last_updated_at": ts,
                "pulled_at": time.time(),
                "count": await self._count_sessions(),
            }
            # Atomic write via temp file + rename (Windows-safe)
            temp_file = self._watermark_file.with_suffix(".json.tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(self._watermark_file)
            logger.debug("Updated pull watermark to %.1f", ts)
        except Exception as e:
            logger.warning("Failed to write pull watermark: %s", e)

    async def _count_sessions(self) -> int:
        """Count total sessions on disk."""
        try:
            return len(list(self._dir.glob("*.json")))
        except Exception:
            return 0

    async def merge_remote_session(self, session_id: str, remote: dict) -> None:
        """
        Atomically merge a remote session into the store using last-writer-wins.

        Replaces the entire local session with the remote version if remote is
        deemed newer (by merge_remote_sessions() caller logic).

        Args:
            session_id: Session ID
            remote: Remote session dict with keys: session_id, agent_name, messages,
                   created_at, updated_at, metadata

        Raises:
            ValueError: If remote session shape is invalid
            Exception: On save/storage failures
        """
        # Validate remote session shape
        if not isinstance(remote.get("messages"), list):
            raise ValueError(
                f"Remote session {session_id} has invalid messages shape "
                f"(expected list, got {type(remote.get('messages')).__name__})"
            )

        # Create or overwrite with remote
        conv = Conversation(
            session_id=session_id,
            agent_name=remote.get("agent_name", ""),
            messages=remote.get("messages", []),
            created_at=remote.get("created_at", time.time()),
            updated_at=remote.get("updated_at", time.time()),
            metadata=remote.get("metadata", {}),
        )

        # Update cache
        self._touch_cache(session_id, conv)

        # Save to disk
        self._save(conv)
        logger.debug("Merged remote session %s", session_id)


_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    """Get the global conversation store singleton."""
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store


def start_selfgraph_drainer() -> Optional[Any]:
    """Start the background drainer for spooled graph updates.

    Call this in long-lived processes (servers, daemons) to periodically
    publish graph updates from the spool to the platform. Short-lived CLI
    invocations should NOT call this.

    Returns:
        BackgroundDrainer instance (started), or None if selfgraph unavailable.
        Caller can call .stop() to gracefully shut down, though it runs daemonized.

    Example::

        # In a server's startup code:
        drainer = start_selfgraph_drainer()
        # ... server runs ...
        # On shutdown (optional, daemon will exit with process):
        if drainer:
            drainer.stop()
    """
    if not _SELFGRAPH_AVAILABLE:
        return None

    try:
        from adk.selfgraph import BackgroundDrainer, Spool

        spool = Spool()
        drainer = BackgroundDrainer(spool, poll_interval=30.0)
        drainer.start()
        logger.info("started background graph drainer")
        return drainer
    except Exception as e:  # noqa: BLE001 — drainer startup is best-effort
        logger.warning("failed to start background drainer: %s", e)
        return None
