"""Durable, offline-first SQLite spool for provenance graph updates.

Stores GraphUpdate objects locally in a queue, retries on drain, and provides a
query mirror so disconnected operators can inspect recorded data without network.

Design constraints:
  - NEVER fails or blocks the agent run when the database is inaccessible, full, or
    locked (enqueue() returns -1 and logs a warning, never raises).
  - Thread-safe under Python's free-threaded build and under concurrent access from
    multiple processes (RLock guards the connection, WAL mode handles file lock races).
  - Offline-first: all methods work when the platform is unreachable.
  - Rows are never deleted until they are marked 'sent' AND old enough to purge.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.selfgraph.schema import (
    GraphUpdate,
    ProvEdge,
    ProvNode,
    ProvNodeType,
)

logger = logging.getLogger("adk.selfgraph.spool")


def _get_spool_path() -> Path:
    """Resolve the spool database path from env or default location."""
    override = os.getenv("AITHER_SELFGRAPH_DB", "")
    if override:
        return Path(override)
    return Path.home() / ".aither" / "selfgraph" / "spool.db"


@dataclass
class SpoolEntry:
    """A queued graph update awaiting publication."""

    rowid: int
    update_json: str
    run_id: str
    agent_id: str
    tenant_id: str
    created_at: float
    attempts: int
    last_error: str
    status: str  # 'pending', 'sent', 'failed'

    def to_update(self) -> Optional[GraphUpdate]:
        """Deserialize the JSON back to a GraphUpdate. Returns None on parse error."""
        try:
            data = json.loads(self.update_json)
            return GraphUpdate.from_dict(data)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Failed to deserialize update rowid %d: %s", self.rowid, e)
            return None


class Spool:
    """Thread-safe SQLite spool for GraphUpdate objects.

    All writes and reads are guarded by an RLock to ensure safety under Python's
    free-threaded build (where a file lock alone does not serialize threads in one
    process). WAL mode handles concurrent access from other processes.
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        max_attempts: int = 8,
    ) -> None:
        """Initialize the spool.

        Args:
            db_path: Path to the SQLite database. Defaults to ~/.aither/selfgraph/spool.db.
                     Can be overridden via AITHER_SELFGRAPH_DB env var.
            max_attempts: Max retries before a failed entry stops being retried (it stays
                         in the DB for manual inspection, but pending() skips it).
        """
        self._db_path = Path(db_path or _get_spool_path())
        self._max_attempts = max_attempts
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """Create the database and schema if they don't exist."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            with self._lock:
                conn = sqlite3.connect(
                    str(self._db_path),
                    timeout=30.0,
                    check_same_thread=False,  # RLock serializes access across threads
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")  # balance durability & perf
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS spool (
                        rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                        update_json TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT DEFAULT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'sent', 'failed'))
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_status_attempts ON spool "
                    "(status, attempts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_run_id ON spool (run_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_created_at ON spool (created_at)"
                )
                conn.commit()
                self._conn = conn
        except Exception as e:
            logger.warning("Failed to initialize spool database: %s", e)

    def _ensure_connection(self) -> Optional[sqlite3.Connection]:
        """Ensure the database connection is open. Returns None on failure."""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.DatabaseError:
                self._conn.close()
                self._conn = None

        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                timeout=30.0,
                check_same_thread=False,  # RLock serializes access across threads
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            return self._conn
        except Exception as e:
            logger.warning("Failed to connect to spool database: %s", e)
            self._conn = None
            return None

    def enqueue(self, update: GraphUpdate) -> int:
        """Enqueue a GraphUpdate for publication.

        MUST NOT raise exceptions or block the agent run, even on disk full,
        database lock, or other I/O errors. Logs a warning and returns -1 on
        failure; returns the rowid on success.

        Args:
            update: The GraphUpdate to enqueue.

        Returns:
            The rowid (>= 1) if enqueued successfully, or -1 on any error.
        """
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    logger.warning("Spool database unavailable, skipping update")
                    return -1

                update_json = json.dumps(update.to_dict())
                cursor = conn.execute(
                    """
                    INSERT INTO spool
                    (update_json, run_id, agent_id, tenant_id, created_at, attempts, status)
                    VALUES (?, ?, ?, ?, ?, 0, 'pending')
                    """,
                    (
                        update_json,
                        update.run_id or "",
                        update.agent_id or "",
                        update.tenant_id or "",
                        time.time(),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except sqlite3.OperationalError as e:
            if "disk I/O error" in str(e) or "disk is full" in str(e):
                logger.warning("Disk full or I/O error enqueueing update: %s", e)
            else:
                logger.warning("Database locked or unavailable: %s", e)
            return -1
        except Exception as e:
            logger.warning("Failed to enqueue update: %s", e)
            return -1

    def pending(self, limit: int = 100) -> List[SpoolEntry]:
        """Fetch pending updates (status='pending' and attempts < max_attempts).

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of SpoolEntry objects, oldest first. Empty list on any error.
        """
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    return []

                cursor = conn.execute(
                    """
                    SELECT rowid, update_json, run_id, agent_id, tenant_id,
                           created_at, attempts, last_error, status
                    FROM spool
                    -- max_attempts is a budget of RETRIES, so an entry is still
                    -- retryable when it has used exactly that many; it is
                    -- exhausted only once it EXCEEDS them. Using '<' here retired
                    -- entries one attempt early.
                    WHERE status = 'pending' AND attempts <= ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (self._max_attempts, limit),
                )
                rows = cursor.fetchall()
                return [
                    SpoolEntry(
                        rowid=row[0],
                        update_json=row[1],
                        run_id=row[2],
                        agent_id=row[3],
                        tenant_id=row[4],
                        created_at=row[5],
                        attempts=row[6],
                        last_error=row[7] or "",
                        status=row[8],
                    )
                    for row in rows
                ]
        except Exception as e:
            logger.warning("Failed to fetch pending entries: %s", e)
            return []

    def mark_sent(self, rowids: List[int]) -> None:
        """Mark a batch of rowids as 'sent'.

        Args:
            rowids: List of rowids to mark sent.
        """
        if not rowids:
            return
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    logger.warning("Cannot mark sent: spool unavailable")
                    return

                placeholders = ",".join("?" * len(rowids))
                conn.execute(
                    f"""
                    UPDATE spool SET status = 'sent' WHERE rowid IN ({placeholders})
                    """,
                    rowids,
                )
                conn.commit()
                logger.debug("Marked %d entries as sent", len(rowids))
        except Exception as e:
            logger.warning("Failed to mark entries sent: %s", e)

    def mark_failed(self, rowid: int, error: str) -> None:
        """Mark an entry as failed and increment its attempt count.

        If attempts >= max_attempts, status becomes 'failed' so the entry stops
        being retried. The row is never deleted, allowing manual inspection and
        replay.

        Args:
            rowid: The rowid to mark failed.
            error: Human-readable error message (truncated to 500 chars).
        """
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    logger.warning("Cannot mark failed: spool unavailable")
                    return

                error_trunc = error[:500] if error else ""
                cursor = conn.execute(
                    "SELECT attempts FROM spool WHERE rowid = ?",
                    (rowid,),
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("Cannot mark failed: rowid %d not found", rowid)
                    return

                # An entry that has already exhausted its budget is not
                # attempted again, so it must not be COUNTED again. Without this
                # the budget is not a bound: a redundant mark (two workers that
                # both read the same page of pending() before either marked it,
                # or a caller retrying a stale entry) pushes attempts past
                # max_attempts forever, and re-logs "will not retry" every time.
                # Measured with max_attempts=3: eight marks produced attempts=8
                # and five warnings, in a SINGLE thread — so this is a bound
                # that never held, not a race. It surfaced as an intermittent
                # thread-safety failure because concurrency is what generates
                # redundant marks, which is why it read as a flaky test.
                if row[0] > self._max_attempts:
                    logger.debug(
                        "rowid %d already exhausted its retry budget "
                        "(attempts=%d); not counting a redundant mark",
                        rowid, row[0])
                    return

                attempts = row[0] + 1
                # Matches pending()'s filter: exhausted only once attempts EXCEED
                # the budget, so the two can never disagree about which rows are
                # still retryable.
                new_status = "failed" if attempts > self._max_attempts else "pending"
                conn.execute(
                    """
                    UPDATE spool SET attempts = ?, last_error = ?, status = ?
                    WHERE rowid = ?
                    """,
                    (attempts, error_trunc, new_status, rowid),
                )
                conn.commit()
                if new_status == "failed":
                    logger.warning(
                        "Entry rowid %d failed after %d attempts; will not retry",
                        rowid,
                        attempts,
                    )
        except Exception as e:
            logger.warning("Failed to mark entry %d as failed: %s", rowid, e)

    def requeue_failed(self) -> int:
        """Operator escape hatch: move all 'failed' entries back to 'pending'.

        Resets attempts to 0 so they can be retried. Use with caution — this
        should only be invoked after the underlying issue has been fixed.

        Returns:
            Number of entries moved from 'failed' to 'pending'.
        """
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    logger.warning("Cannot requeue: spool unavailable")
                    return 0

                cursor = conn.execute(
                    "SELECT COUNT(*) FROM spool WHERE status = 'failed'"
                )
                count = cursor.fetchone()[0]
                if count > 0:
                    conn.execute(
                        "UPDATE spool SET status = 'pending', attempts = 0 "
                        "WHERE status = 'failed'"
                    )
                    conn.commit()
                    logger.info("Requeued %d failed entries", count)
                return count
        except Exception as e:
            logger.warning("Failed to requeue failed entries: %s", e)
            return 0

    def purge_sent(self, older_than_s: float = 604800) -> int:
        """Delete sent entries older than a threshold.

        This is the ONLY place where rows are removed from the spool. Rows are
        kept indefinitely until explicitly purged, allowing historical inspection.

        Args:
            older_than_s: Age in seconds; default 7 days (604800). A NEGATIVE
                value would place the cutoff in the future and delete everything
                sent, which is the opposite of what a caller asking to keep
                recent rows means — so it deletes nothing instead.

        Returns:
            Number of rows deleted.
        """
        if older_than_s < 0:
            logger.warning(
                "purge_sent called with a negative age (%s); refusing to purge",
                older_than_s,
            )
            return 0
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    logger.warning("Cannot purge: spool unavailable")
                    return 0

                cutoff = time.time() - older_than_s
                cursor = conn.execute(
                    # '<=' not '<': with older_than_s=0 the cutoff IS now, and a
                    # row written in the same clock tick would otherwise survive
                    # a "purge everything sent" request.
                    "DELETE FROM spool WHERE status = 'sent' AND created_at <= ?",
                    (cutoff,),
                )
                conn.commit()
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.debug("Purged %d old sent entries", deleted)
                return deleted
        except Exception as e:
            logger.warning("Failed to purge old entries: %s", e)
            return 0

    def stats(self) -> Dict[str, Any]:
        """Get spool statistics.

        Returns:
            A dict with keys: pending, sent, failed, oldest_pending_age_s, db_bytes.
            All numeric values are 0 on any error.
        """
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    return {
                        "pending": 0,
                        "sent": 0,
                        "failed": 0,
                        "oldest_pending_age_s": 0.0,
                        "db_bytes": 0,
                    }

                cursor = conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                        MIN(CASE WHEN status = 'pending' THEN created_at ELSE NULL END)
                    FROM spool
                    """
                )
                row = cursor.fetchone()
                pending = row[0] or 0
                sent = row[1] or 0
                failed = row[2] or 0
                oldest_pending_ts = row[3]

                oldest_age_s = 0.0
                if oldest_pending_ts is not None:
                    oldest_age_s = time.time() - oldest_pending_ts

                # Database size
                db_bytes = 0
                if self._db_path.exists():
                    db_bytes = self._db_path.stat().st_size
                    # WAL mode also creates -wal and -shm files
                    wal_path = self._db_path.with_suffix(self._db_path.suffix + "-wal")
                    shm_path = self._db_path.with_suffix(self._db_path.suffix + "-shm")
                    if wal_path.exists():
                        db_bytes += wal_path.stat().st_size
                    if shm_path.exists():
                        db_bytes += shm_path.stat().st_size

                return {
                    "pending": pending,
                    "sent": sent,
                    "failed": failed,
                    "oldest_pending_age_s": oldest_age_s,
                    "db_bytes": db_bytes,
                }
        except Exception as e:
            logger.warning("Failed to get spool stats: %s", e)
            return {
                "pending": 0,
                "sent": 0,
                "failed": 0,
                "oldest_pending_age_s": 0.0,
                "db_bytes": 0,
            }

    def local_nodes(
        self,
        node_type: Optional[ProvNodeType] = None,
        text: str = "",
        limit: int = 50,
    ) -> List[ProvNode]:
        """Query the local spool for nodes.

        Returns nodes from all spooled updates (pending and sent), filtered by
        type and text. Useful for disconnected inspection: ``aiter graph nodes``.

        Args:
            node_type: Filter by node type; None means all types.
            text: Substring search on node names (case-insensitive).
            limit: Maximum number of nodes to return.

        Returns:
            List of ProvNode objects, newest first (by created_at).
        """
        results: Dict[str, ProvNode] = {}  # {id -> node}, keyed to dedupe
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    return []

                # Fetch all updates (both pending and sent)
                cursor = conn.execute(
                    "SELECT update_json FROM spool ORDER BY created_at DESC"
                )
                for (json_str,) in cursor.fetchall():
                    try:
                        update = GraphUpdate.from_dict(json.loads(json_str))
                        for node in update.nodes:
                            # Filter by type
                            if node_type and node.node_type != node_type:
                                continue
                            # Filter by text
                            if text and text.lower() not in node.name.lower():
                                continue
                            # Dedupe by id (newest wins because we iterate DESC)
                            if node.id not in results:
                                results[node.id] = node
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue

                # Sort by created_at DESC and truncate
                sorted_nodes = sorted(
                    results.values(),
                    key=lambda n: n.created_at,
                    reverse=True,
                )
                return sorted_nodes[:limit]
        except Exception as e:
            logger.warning("Failed to query local nodes: %s", e)
            return []

    def local_edges(
        self,
        source_id: str = "",
        target_id: str = "",
        limit: int = 50,
    ) -> List[ProvEdge]:
        """Query the local spool for edges.

        Returns edges from all spooled updates (pending and sent), filtered by
        endpoint ids. Useful for disconnected inspection: ``aiter graph edges``.

        Args:
            source_id: Filter by source_id; empty means any source.
            target_id: Filter by target_id; empty means any target.
            limit: Maximum number of edges to return.

        Returns:
            List of ProvEdge objects, newest first (by created_at).
        """
        results: Dict[str, ProvEdge] = {}  # {id -> edge}, keyed to dedupe
        try:
            with self._lock:
                conn = self._ensure_connection()
                if conn is None:
                    return []

                # Fetch all updates (both pending and sent)
                cursor = conn.execute(
                    "SELECT update_json FROM spool ORDER BY created_at DESC"
                )
                for (json_str,) in cursor.fetchall():
                    try:
                        update = GraphUpdate.from_dict(json.loads(json_str))
                        for edge in update.edges:
                            # Filter by source
                            if source_id and edge.source_id != source_id:
                                continue
                            # Filter by target
                            if target_id and edge.target_id != target_id:
                                continue
                            # Dedupe by id (newest wins because we iterate DESC)
                            if edge.id not in results:
                                results[edge.id] = edge
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue

                # Sort by created_at DESC and truncate
                sorted_edges = sorted(
                    results.values(),
                    key=lambda e: e.created_at,
                    reverse=True,
                )
                return sorted_edges[:limit]
        except Exception as e:
            logger.warning("Failed to query local edges: %s", e)
            return []

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception as e:
                    logger.warning("Error closing spool connection: %s", e)
                finally:
                    self._conn = None

    def __enter__(self) -> Spool:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
