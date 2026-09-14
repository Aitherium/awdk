"""Tests for the offline-first SQLite spool for provenance updates."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from adk.selfgraph.schema import GraphUpdate, ProvEdge, ProvEdgeType, ProvNode, ProvNodeType
from adk.selfgraph.schema import make_node_id
from adk.selfgraph.spool import Spool, SpoolEntry


@pytest.fixture
def spool_db(tmp_path):
    """Create a Spool instance with a temporary database."""
    db_path = tmp_path / "test_spool.db"
    spool = Spool(db_path=db_path, max_attempts=3)
    yield spool
    spool.close()


def _make_test_update(run_id="run1", agent_id="agent1", tenant_id="t1"):
    """Create a minimal valid GraphUpdate for testing."""
    claim_node = ProvNode(
        id=make_node_id(ProvNodeType.CLAIM, "test claim", tenant_id),
        node_type=ProvNodeType.CLAIM,
        name="test claim",
        tenant_id=tenant_id,
        workspace_id="ws1",
        run_id=run_id,
        agent_id=agent_id,
    )
    source_node = ProvNode(
        id=make_node_id(ProvNodeType.SOURCE, "http://example.com", tenant_id),
        node_type=ProvNodeType.SOURCE,
        name="http://example.com",
        tenant_id=tenant_id,
        workspace_id="ws1",
        agent_id=agent_id,
    )
    edge = ProvEdge(
        source_id=claim_node.id,
        target_id=source_node.id,
        edge_type=ProvEdgeType.CITES,
        tenant_id=tenant_id,
        workspace_id="ws1",
        run_id=run_id,
        agent_id=agent_id,
    )
    return GraphUpdate(
        nodes=[claim_node, source_node],
        edges=[edge],
        run_id=run_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace_id="ws1",
    )


class TestSpoolEnqueueAndPending:
    """Test basic enqueue and pending operations."""

    def test_enqueue_returns_rowid(self, spool_db):
        """enqueue() should return a rowid >= 1 on success."""
        update = _make_test_update()
        rowid = spool_db.enqueue(update)
        assert rowid >= 1, "enqueue() should return a valid rowid"

    def test_pending_returns_enqueued(self, spool_db):
        """pending() should return entries that were enqueued."""
        update = _make_test_update()
        rowid = spool_db.enqueue(update)

        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Should have one pending entry"
        assert pending[0].rowid == rowid, "Rowid should match"
        assert pending[0].status == "pending", "Status should be pending"
        assert pending[0].update_json, "Should have JSON content"

    def test_pending_respects_limit(self, spool_db):
        """pending() should respect the limit parameter."""
        for i in range(5):
            update = _make_test_update(run_id=f"run{i}")
            spool_db.enqueue(update)

        pending_3 = spool_db.pending(limit=3)
        assert len(pending_3) == 3, "Should return at most 3 entries"

        pending_10 = spool_db.pending(limit=10)
        assert len(pending_10) == 5, "Should return all 5 when limit is 10"

    def test_pending_ordered_by_created_at(self, spool_db):
        """pending() should return entries oldest first."""
        rowids = []
        for i in range(3):
            update = _make_test_update(run_id=f"run{i}")
            rowid = spool_db.enqueue(update)
            rowids.append(rowid)
            # Small delay to ensure different timestamps
            time.sleep(0.01)

        pending = spool_db.pending(limit=10)
        assert len(pending) == 3, "Should have all 3 entries"
        returned_rowids = [e.rowid for e in pending]
        assert returned_rowids == rowids, "Should be ordered by creation time (oldest first)"

    def test_pending_excludes_sent(self, spool_db):
        """pending() should not return entries marked as 'sent'."""
        rowid1 = spool_db.enqueue(_make_test_update(run_id="run1"))
        rowid2 = spool_db.enqueue(_make_test_update(run_id="run2"))

        spool_db.mark_sent([rowid1])

        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Should have only 1 pending entry"
        assert pending[0].rowid == rowid2, "Should be the non-sent entry"

    def test_pending_excludes_over_max_attempts(self, spool_db):
        """pending() should exclude entries that have exceeded max_attempts."""
        rowid = spool_db.enqueue(_make_test_update())

        # Mark failed multiple times to exceed max_attempts
        for _ in range(4):  # max_attempts=3, so 4 fails it
            spool_db.mark_failed(rowid, "test error")

        pending = spool_db.pending(limit=10)
        assert len(pending) == 0, "Over-attempted entries should not be pending"

    def test_entry_to_update_deserializes(self, spool_db):
        """SpoolEntry.to_update() should deserialize back to GraphUpdate."""
        original = _make_test_update()
        rowid = spool_db.enqueue(original)

        pending = spool_db.pending(limit=1)
        assert len(pending) == 1

        entry = pending[0]
        update = entry.to_update()
        assert update is not None, "to_update() should deserialize successfully"
        assert update.run_id == original.run_id
        assert update.agent_id == original.agent_id
        assert len(update.nodes) == len(original.nodes)
        assert len(update.edges) == len(original.edges)


class TestMarkSentAndFailed:
    """Test mark_sent and mark_failed operations."""

    def test_mark_sent_changes_status(self, spool_db):
        """mark_sent() should change status to 'sent'."""
        rowid = spool_db.enqueue(_make_test_update())

        spool_db.mark_sent([rowid])

        pending = spool_db.pending(limit=10)
        assert len(pending) == 0, "Sent entry should not appear in pending"

    def test_mark_sent_batch(self, spool_db):
        """mark_sent() should handle multiple rowids."""
        rowids = []
        for i in range(3):
            rowid = spool_db.enqueue(_make_test_update(run_id=f"run{i}"))
            rowids.append(rowid)

        spool_db.mark_sent(rowids)

        pending = spool_db.pending(limit=10)
        assert len(pending) == 0, "All entries should be marked sent"

    def test_mark_failed_increments_attempts(self, spool_db):
        """mark_failed() should increment attempts and keep status pending."""
        rowid = spool_db.enqueue(_make_test_update())

        spool_db.mark_failed(rowid, "first error")

        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Entry should still be pending"
        assert pending[0].attempts == 1, "Attempts should be 1"
        assert pending[0].last_error == "first error", "Error should be recorded"

    def test_mark_failed_multiple_times(self, spool_db):
        """Repeated mark_failed() should keep incrementing attempts."""
        rowid = spool_db.enqueue(_make_test_update())

        for i in range(3):
            spool_db.mark_failed(rowid, f"error {i}")

        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Should still be pending before max_attempts"
        assert pending[0].attempts == 3, "Attempts should be 3"

    def test_mark_failed_exceeds_max_attempts(self, spool_db):
        """mark_failed() past max_attempts should set status to 'failed'."""
        rowid = spool_db.enqueue(_make_test_update())

        # spool_db has max_attempts=3; fail 4 times to exceed it
        for i in range(4):
            spool_db.mark_failed(rowid, f"error {i}")

        pending = spool_db.pending(limit=10)
        assert len(pending) == 0, "Over-attempted entry should not be pending"

    def test_failed_entry_still_in_database(self, spool_db):
        """Failed entries should never be deleted, only marked failed."""
        rowid = spool_db.enqueue(_make_test_update())

        # Fail it enough to mark it as failed
        for i in range(4):
            spool_db.mark_failed(rowid, f"error {i}")

        # The row should still exist in the database
        stats = spool_db.stats()
        # pending=0, sent=0, failed=1
        assert stats["failed"] == 1, "Entry should be in failed count"


class TestRequeueFailed:
    """Test requeue_failed() operation."""

    def test_requeue_failed_moves_back_to_pending(self, spool_db):
        """requeue_failed() should move failed entries back to pending."""
        rowid = spool_db.enqueue(_make_test_update())

        # Fail it enough to mark as failed
        for i in range(4):
            spool_db.mark_failed(rowid, f"error {i}")

        # Should not be pending now
        assert len(spool_db.pending(limit=10)) == 0

        # Requeue
        count = spool_db.requeue_failed()
        assert count == 1, "Should have requeued 1 entry"

        # Should be pending again
        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Should be pending again"
        assert pending[0].attempts == 0, "Attempts should be reset to 0"

    def test_requeue_failed_resets_attempts(self, spool_db):
        """requeue_failed() should reset attempts to 0."""
        rowid = spool_db.enqueue(_make_test_update())

        for i in range(4):
            spool_db.mark_failed(rowid, "error")

        spool_db.requeue_failed()

        pending = spool_db.pending(limit=10)
        assert pending[0].attempts == 0, "Attempts should be reset"


class TestPurgeSent:
    """Test purge_sent() operation."""

    def test_purge_sent_removes_sent_entries(self, spool_db):
        """purge_sent() should remove only entries marked as 'sent'."""
        rowid1 = spool_db.enqueue(_make_test_update(run_id="run1"))
        rowid2 = spool_db.enqueue(_make_test_update(run_id="run2"))

        spool_db.mark_sent([rowid1])

        # Purge with a very old threshold so both would be old
        count = spool_db.purge_sent(older_than_s=0)
        assert count == 1, "Should have deleted 1 sent entry"

        # rowid2 should still be pending
        pending = spool_db.pending(limit=10)
        assert len(pending) == 1, "Should have 1 pending entry left"
        assert pending[0].rowid == rowid2

    def test_purge_sent_respects_older_than(self, spool_db):
        """purge_sent() should respect the older_than_s threshold."""
        rowid1 = spool_db.enqueue(_make_test_update(run_id="run1"))
        spool_db.mark_sent([rowid1])

        # Purge with a threshold of 1 second in the future (don't delete anything)
        count = spool_db.purge_sent(older_than_s=-1)
        assert count == 0, "Should not delete entries older than -1s (future)"

        # Now purge with a threshold of 0 (delete immediately)
        count = spool_db.purge_sent(older_than_s=0)
        assert count == 1, "Should delete entries older than 0s (now)"

    def test_purge_sent_does_not_delete_pending_or_failed(self, spool_db):
        """purge_sent() should only delete 'sent' entries, not pending or failed."""
        rowid1 = spool_db.enqueue(_make_test_update(run_id="run1"))
        rowid2 = spool_db.enqueue(_make_test_update(run_id="run2"))
        rowid3 = spool_db.enqueue(_make_test_update(run_id="run3"))

        spool_db.mark_sent([rowid1])
        for _ in range(4):
            spool_db.mark_failed(rowid2, "error")

        stats_before = spool_db.stats()
        assert stats_before["pending"] == 1  # rowid3
        assert stats_before["sent"] == 1  # rowid1
        assert stats_before["failed"] == 1  # rowid2

        # Purge old sent entries
        count = spool_db.purge_sent(older_than_s=0)
        assert count == 1, "Should delete 1 sent entry"

        stats_after = spool_db.stats()
        assert stats_after["pending"] == 1, "Pending should be unchanged"
        assert stats_after["sent"] == 0, "Sent should be empty"
        assert stats_after["failed"] == 1, "Failed should be unchanged"


class TestLocalNodesAndEdges:
    """Test local_nodes() and local_edges() query methods."""

    def test_local_nodes_returns_nodes(self, spool_db):
        """local_nodes() should return nodes from spooled updates."""
        update = _make_test_update()
        spool_db.enqueue(update)

        nodes = spool_db.local_nodes()
        assert len(nodes) > 0, "Should have nodes"
        # Original update has 2 nodes (claim and source)
        assert len(nodes) >= 2, "Should have at least the 2 nodes from update"

    def test_local_nodes_filters_by_type(self, spool_db):
        """local_nodes() should filter by node_type."""
        update = _make_test_update()
        spool_db.enqueue(update)

        claims = spool_db.local_nodes(node_type=ProvNodeType.CLAIM)
        assert len(claims) >= 1, "Should have at least 1 claim"
        assert all(n.node_type == ProvNodeType.CLAIM for n in claims)

        sources = spool_db.local_nodes(node_type=ProvNodeType.SOURCE)
        assert len(sources) >= 1, "Should have at least 1 source"
        assert all(n.node_type == ProvNodeType.SOURCE for n in sources)

    def test_local_nodes_filters_by_text(self, spool_db):
        """local_nodes() should filter by text search on name."""
        update = _make_test_update()
        spool_db.enqueue(update)

        results = spool_db.local_nodes(text="example")
        assert len(results) >= 1, "Should find nodes with 'example' in name"
        assert all("example" in n.name.lower() for n in results)

    def test_local_nodes_respects_limit(self, spool_db):
        """local_nodes() should respect the limit parameter.

        The tenant is varied per update on purpose: node ids are derived from
        (type, name, tenant), so re-enqueueing the same claim under one tenant
        yields ONE node however many times it is spooled. local_nodes dedupes by
        id, which is correct — but it means a fixture that repeats identical
        content cannot exercise a limit at all.
        """
        for i in range(10):
            update = _make_test_update(run_id=f"run{i}", tenant_id=f"t{i}")
            spool_db.enqueue(update)

        nodes_5 = spool_db.local_nodes(limit=5)
        assert len(nodes_5) <= 5, "Should respect limit=5"

        nodes_100 = spool_db.local_nodes(limit=100)
        # 2 distinct nodes (claim + source) per tenant, 10 tenants.
        assert len(nodes_100) >= 10

    def test_local_nodes_dedupes_identical_ids(self, spool_db):
        """Re-spooling the same node id yields one node, not one per enqueue."""
        for i in range(5):
            spool_db.enqueue(_make_test_update(run_id=f"run{i}"))

        nodes = spool_db.local_nodes(limit=100)
        assert len(nodes) == 2, "claim + source, deduped across 5 identical updates"

    def test_local_edges_returns_edges(self, spool_db):
        """local_edges() should return edges from spooled updates."""
        update = _make_test_update()
        spool_db.enqueue(update)

        edges = spool_db.local_edges()
        assert len(edges) >= 1, "Should have at least 1 edge"

    def test_local_edges_filters_by_source_id(self, spool_db):
        """local_edges() should filter by source_id."""
        update = _make_test_update()
        spool_db.enqueue(update)

        # Get the claim node id to query edges from it
        nodes = spool_db.local_nodes(node_type=ProvNodeType.CLAIM)
        if nodes:
            claim_id = nodes[0].id
            edges = spool_db.local_edges(source_id=claim_id)
            # Should find at least the CITES edge
            assert all(e.source_id == claim_id for e in edges)

    def test_local_edges_filters_by_target_id(self, spool_db):
        """local_edges() should filter by target_id."""
        update = _make_test_update()
        spool_db.enqueue(update)

        # Get the source node id to query edges to it
        nodes = spool_db.local_nodes(node_type=ProvNodeType.SOURCE)
        if nodes:
            source_id = nodes[0].id
            edges = spool_db.local_edges(target_id=source_id)
            # Should find at least the CITES edge
            assert all(e.target_id == source_id for e in edges)

    def test_local_edges_respects_limit(self, spool_db):
        """local_edges() should respect the limit parameter."""
        for i in range(10):
            update = _make_test_update(run_id=f"run{i}")
            spool_db.enqueue(update)

        edges_5 = spool_db.local_edges(limit=5)
        assert len(edges_5) <= 5, "Should respect limit=5"


class TestEnqueueFailureHandling:
    """Test that enqueue() never fails, even on disk/db errors."""

    def test_enqueue_on_unopenable_db_returns_minus_one(self, tmp_path):
        """enqueue() returns -1 on I/O error and NEVER raises.

        This is the offline-first contract: a failed graph write must not break
        the agent run that produced it. The db path is a DIRECTORY here, which
        sqlite cannot open — a missing parent directory is not a usable failure
        because Spool creates its parents on purpose.
        """
        blocked = tmp_path / "spool.db"
        blocked.mkdir()  # a directory where the database file should be

        spool = Spool(db_path=blocked)
        rowid = spool.enqueue(_make_test_update())  # must not raise
        assert rowid == -1, "Should return -1 when the database cannot be opened"

        # And the other read paths degrade rather than explode.
        assert spool.pending(limit=10) == []
        assert spool.local_nodes(limit=10) == []
        spool.close()

    def test_enqueue_returns_minus_one_when_the_db_write_itself_raises(self, tmp_path):
        """The RAISING branch of enqueue, not just the no-connection branch.

        The test above exercises a spool whose connection could never be opened,
        so ``_ensure_connection`` returns None and enqueue takes an early exit —
        the ``except`` handler is never reached. Proven by mutation: making that
        handler ``raise`` instead of returning -1 left the suite green, because
        nothing drove a live connection into a failing write.

        This drives that path: a working spool whose INSERT then blows up. The
        offline-first contract is that a failed graph write can never break the
        agent run that produced it, so this must return -1 and not propagate.
        """
        spool = Spool(db_path=tmp_path / "ok.db")
        assert spool.enqueue(_make_test_update()) > 0, "spool must work before we break it"

        class _ExplodingConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("disk I/O error")

            def commit(self):
                raise sqlite3.OperationalError("disk I/O error")

        spool._conn = _ExplodingConn()

        rowid = spool.enqueue(_make_test_update(run_id="after-break"))
        assert rowid == -1, "a raising write must degrade to -1, never propagate"


class TestThreadSafety:
    """Test thread-safe concurrent access."""

    def test_concurrent_enqueue(self, spool_db):
        """Multiple threads enqueueing concurrently should all succeed."""
        results = []
        errors = []

        def enqueue_thread(thread_id):
            try:
                for i in range(5):
                    update = _make_test_update(run_id=f"thread{thread_id}_run{i}")
                    rowid = spool_db.enqueue(update)
                    if rowid >= 1:
                        results.append(rowid)
                    else:
                        errors.append(f"Failed to enqueue in thread {thread_id}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=enqueue_thread, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent enqueue errors: {errors}"
        assert len(results) == 15, "All 15 enqueues should succeed (3 threads * 5 each)"

    def test_concurrent_pending_and_mark(self, spool_db):
        """Multiple threads reading and updating should be thread-safe."""
        # Pre-enqueue some entries
        rowids = []
        for i in range(10):
            rowid = spool_db.enqueue(_make_test_update(run_id=f"run{i}"))
            rowids.append(rowid)

        errors = []

        def worker():
            try:
                for _ in range(5):
                    pending = spool_db.pending(limit=3)
                    for entry in pending:
                        spool_db.mark_failed(entry.rowid, "test error")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"

        # What is actually guaranteed under a racing interleaving: no errors, no
        # lost rows, and every attempt recorded. How MANY entries have exhausted
        # their retry budget depends on which thread saw which page of pending()
        # — asserting an exact count here passed only by luck and turned any
        # change to the retry arithmetic into a mystery failure.
        stats = spool_db.stats()
        assert stats["pending"] + stats["failed"] + stats["sent"] == len(rowids), (
            "rows were lost or duplicated under concurrent access"
        )
        assert stats["sent"] == 0, "nothing was successfully published in this test"

        # NOT "every entry was attempted". pending() is a HEAD query ordered by
        # created_at, so which rows the 15 iterations reach depends entirely on
        # the interleaving: three threads that read the same page all mark the
        # same rows, and the tail can legitimately go untouched. Asserting it
        # made this test fail intermittently (D-2121) while blaming
        # thread-safety for an ordinary property of a bounded work loop.
        #
        # What IS guaranteed is the retry BUDGET, and that used to be false: a
        # redundant mark on an exhausted entry kept incrementing, so attempts
        # ran past max_attempts without limit. Reproducible in one thread; see
        # test_budget_is_a_bound below.
        with sqlite3.connect(str(spool_db._db_path)) as conn:
            attempts = [row[0] for row in conn.execute("SELECT attempts FROM spool")]
        assert attempts, "no rows survived the concurrent run"
        assert all(a >= 0 for a in attempts)
        assert max(attempts) <= spool_db._max_attempts + 1, (
            f"attempts exceeded the retry budget under concurrency: {attempts} "
            f"(max_attempts={spool_db._max_attempts})")
        # Progress, or the loop did nothing and every assertion above is vacuous.
        assert any(a >= 1 for a in attempts), "no entry was attempted at all"

    def test_budget_is_a_bound(self, spool_db):
        """A redundant mark must not push an exhausted entry past its budget.

        Deterministic and single-threaded on purpose: this is the defect behind
        the intermittent failure in test_concurrent_pending_and_mark, and
        concurrency was only what GENERATED the redundant marks. Reproducing it
        with threads would inherit the flakiness of the thing it explains.
        """
        rowid = spool_db.enqueue(_make_test_update(run_id="bounded"))
        for _ in range(spool_db._max_attempts + 6):
            spool_db.mark_failed(rowid, "boom")
        with sqlite3.connect(str(spool_db._db_path)) as conn:
            attempts, status = conn.execute(
                "SELECT attempts, status FROM spool WHERE rowid = ?", (rowid,)
            ).fetchone()
        assert status == "failed"
        assert attempts == spool_db._max_attempts + 1, (
            f"attempts={attempts} — the retry budget is not a bound")
        assert spool_db.pending(limit=10) == [], "an exhausted entry is still pending"
