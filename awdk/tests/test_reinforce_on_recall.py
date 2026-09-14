"""Reinforce-on-recall — recall_with_activation(reinforce=True).

Proves: (1) the default (reinforce=False) performs ZERO writes, so every
existing caller stays byte-identical; (2) opting in bumps
reinforcement_count/last_reinforced on each RETURNED node's metadata mirror and
the bump PERSISTS across a fresh GraphMemory reopen; (3) repeated recalls
accumulate; (4) only returned nodes are touched.
"""

import json
import sqlite3
import time

import pytest

from adk.graph_memory import GraphMemory


def _raw_metadata(db_path, node_id):
    """Read the node's metadata straight from SQLite (no GraphMemory caching)."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0] or "{}") if row else None


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "reinforce.db"


@pytest.fixture
def graph(db_path, monkeypatch):
    monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
    return GraphMemory(db_path=db_path, agent_name="t")


class TestReinforceOff:
    @pytest.mark.asyncio
    async def test_default_recall_writes_nothing(self, graph, db_path):
        node = await graph.add_node(
            "Widget", content="the widget service listens on port 8080"
        )
        before = _raw_metadata(db_path, node.id)
        res = await graph.recall_with_activation("widget service port", limit=5)
        assert len(res) >= 1
        after = _raw_metadata(db_path, node.id)
        assert after == before  # byte-identical — no reinforcement fields written
        assert "reinforcement_count" not in after
        assert "last_reinforced" not in after


class TestReinforceOn:
    @pytest.mark.asyncio
    async def test_counts_bump_and_persist_across_reopen(self, graph, db_path):
        node = await graph.add_node(
            "Widget", content="the widget service listens on port 8080"
        )
        now = time.time()
        res = await graph.recall_with_activation(
            "widget service port", limit=5, now=now, reinforce=True,
        )
        assert any(n.id == node.id for n in res)
        returned = next(n for n in res if n.id == node.id)
        # the RETURNED node already carries the bumped values
        assert returned.metadata.get("reinforcement_count") == 1
        assert returned.metadata.get("last_reinforced") == pytest.approx(now)

        # persisted: a FRESH GraphMemory on the same db sees the bump
        reopened = GraphMemory(db_path=db_path, agent_name="t")
        fetched = await reopened.get_node(node.id)
        assert fetched is not None
        assert fetched.metadata.get("reinforcement_count") == 1
        assert fetched.metadata.get("last_reinforced") == pytest.approx(now)

    @pytest.mark.asyncio
    async def test_repeat_recalls_accumulate(self, graph, db_path):
        node = await graph.add_node(
            "Widget", content="the widget service listens on port 8080"
        )
        for expected in (1, 2, 3):
            res = await graph.recall_with_activation(
                "widget service port", limit=5, reinforce=True,
            )
            assert any(n.id == node.id for n in res)
            md = _raw_metadata(db_path, node.id)
            assert md.get("reinforcement_count") == expected

    @pytest.mark.asyncio
    async def test_only_returned_nodes_bumped(self, graph, db_path):
        await graph.add_node(
            "Widget", content="the widget service listens on port 8080"
        )
        miss = await graph.add_node(
            "Yonder", content="entirely unrelated zebra quill material"
        )
        res = await graph.recall_with_activation(
            "widget service port", limit=1, reinforce=True,
        )
        returned_ids = {n.id for n in res}
        assert returned_ids  # something came back
        for nid in returned_ids:
            assert _raw_metadata(db_path, nid).get("reinforcement_count") == 1
        if miss.id not in returned_ids:
            assert "reinforcement_count" not in (_raw_metadata(db_path, miss.id) or {})

    @pytest.mark.asyncio
    async def test_reinforced_node_scores_fresher(self, graph):
        """The bump is LIVE: last_reinforced feeds MemoryRecord.freshness()."""
        node = await graph.add_node(
            "Widget", content="the widget service listens on port 8080"
        )
        now = time.time()
        await graph.recall_with_activation(
            "widget service port", limit=5, now=now, reinforce=True,
        )
        # freshness is computed from the metadata mirror the bump just wrote
        assert graph._freshness_of(node.id, now=now) == pytest.approx(1.0, abs=1e-6)
