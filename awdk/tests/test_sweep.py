"""GraphMemory.sweep() + promote() — the maintenance primitives.

Proves: stale unreinforced nodes past their tier TTL are archived (tombstone +
FORGET ledger entry + removed); reinforced nodes survive; PERMANENT is immune;
max_nodes archives the lowest-scored overflow; dry_run acts on nothing;
tombstones are REVERSIBLE via TombstoneStore.recover(); and promote() re-tiers/
re-roles a node in place (edges survive, UPDATE ledgered, sweep-immunity when
promoted to permanent).
"""

import time

import pytest

from adk.graph_memory import GraphMemory
from adk.unified_contract import MemoryRecord, Role, Tier


@pytest.fixture
def ugraph(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_UNIFIED_MEMORY", "on")
    return GraphMemory(db_path=tmp_path / "sweep.db", agent_name="t")


async def _store_aged(graph, content, tier, age_secs, now, reinforcement=0):
    """Store a record whose last reinforcement was ``age_secs`` ago."""
    return await graph.store(MemoryRecord(
        content=content, role=Role.FACT, tier=tier,
        last_reinforced=now - age_secs, reinforcement_count=reinforcement,
    ))


class TestSweep:
    @pytest.mark.asyncio
    async def test_stale_unreinforced_archived(self, ugraph):
        now = time.time()
        # ephemeral tier (TTL 900s), 2h since reinforcement → freshness ≈ 0.004
        stale = await _store_aged(
            ugraph, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now)
        fresh = await _store_aged(
            ugraph, "durable fact about the alpha widget", Tier.PERSISTENT, 60, now)
        stats = ugraph.sweep(now=now)
        assert stats["examined"] == 2
        assert stats["archived"] == 1
        assert stats["kept"] == 1
        assert stale.id in stats["would_archive"]
        assert await ugraph.get_node(stale.id) is None
        assert await ugraph.get_node(fresh.id) is not None
        # the archive is ledgered as FORGET
        assert ugraph._governed.ledger.stats()["by_type"].get("forget", 0) >= 1

    @pytest.mark.asyncio
    async def test_reinforced_node_survives(self, ugraph):
        now = time.time()
        # same staleness as an archived node, but heavily reinforced
        kept = await _store_aged(
            ugraph, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now,
            reinforcement=8)
        stats = ugraph.sweep(now=now)
        assert stats["archived"] == 0
        assert await ugraph.get_node(kept.id) is not None

    @pytest.mark.asyncio
    async def test_permanent_immune(self, ugraph):
        now = time.time()
        perm = await _store_aged(
            ugraph, "identity core truth of the agent", Tier.PERMANENT,
            10 * 365 * 86400, now)
        stats = ugraph.sweep(now=now)
        assert stats["skipped_permanent"] == 1
        assert stats["archived"] == 0
        assert await ugraph.get_node(perm.id) is not None

    @pytest.mark.asyncio
    async def test_max_nodes_archives_lowest_scored_overflow(self, ugraph):
        now = time.time()
        nodes = []
        for i in range(1, 5):  # 1..4 days old — all within the 7-day TTL
            n = await _store_aged(
                ugraph, f"persistent fact number {i} about topic{i}",
                Tier.PERSISTENT, i * 86400, now)
            nodes.append(n)
        stats = ugraph.sweep(now=now, max_nodes=2)
        assert stats["archived"] == 2
        # the two OLDEST (lowest freshness) were archived; the youngest survive
        assert await ugraph.get_node(nodes[0].id) is not None
        assert await ugraph.get_node(nodes[1].id) is not None
        assert await ugraph.get_node(nodes[2].id) is None
        assert await ugraph.get_node(nodes[3].id) is None

    @pytest.mark.asyncio
    async def test_max_nodes_never_archives_permanent(self, ugraph):
        now = time.time()
        perm = await _store_aged(
            ugraph, "identity core truth of the agent", Tier.PERMANENT,
            365 * 86400, now)
        for i in range(1, 4):
            await _store_aged(
                ugraph, f"persistent fact number {i} about topic{i}",
                Tier.PERSISTENT, i * 3600, now)
        stats = ugraph.sweep(now=now, max_nodes=1)
        assert perm.id not in stats["would_archive"]
        assert await ugraph.get_node(perm.id) is not None

    @pytest.mark.asyncio
    async def test_dry_run_acts_on_nothing(self, ugraph):
        now = time.time()
        stale = await _store_aged(
            ugraph, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now)
        stats = ugraph.sweep(now=now, dry_run=True)
        assert stale.id in stats["would_archive"]
        assert stats["archived"] == 0
        assert stats["tombstones"] == {}
        # nothing was deleted, nothing was ledgered
        assert await ugraph.get_node(stale.id) is not None
        assert ugraph._governed.ledger.stats()["by_type"].get("forget", 0) == 0

    @pytest.mark.asyncio
    async def test_recover_restores_archived_node(self, ugraph):
        now = time.time()
        stale = await _store_aged(
            ugraph, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now)
        stats = ugraph.sweep(now=now)
        tomb_id = stats["tombstones"][stale.id]
        # REVERSIBLE: the tombstone snapshot round-trips the node
        snap = ugraph._governed.tombstones.recover(tomb_id)
        assert snap is not None
        assert snap["id"] == stale.id
        assert snap["content"] == "scratch note about the beta gizmo"
        # and the snapshot is sufficient to re-store the memory
        restored = await ugraph.add_node(
            label=snap["label"], node_type=snap["node_type"],
            content=snap["content"], tier=snap["tier"], role=snap["role"],
        )
        assert (await ugraph.get_node(restored.id)) is not None

    @pytest.mark.asyncio
    async def test_sweep_flag_off_still_reversible(self, tmp_path, monkeypatch):
        """Flag-off sweep works and remains reversible (lazy stores beside db)."""
        monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
        db = tmp_path / "sweep_off.db"
        g = GraphMemory(db_path=db, agent_name="t")
        assert g._governed is None
        now = time.time()
        stale = await _store_aged(
            g, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now)
        stats = g.sweep(now=now)
        assert stats["archived"] == 1
        tomb_id = stats["tombstones"][stale.id]
        from adk.graph_rag.governance import GovernanceArtifacts, TombstoneStore
        arts = GovernanceArtifacts.beside(str(db))
        assert arts.tombstone_path.exists()
        snap = TombstoneStore(arts.tombstone_path, persist=True).recover(tomb_id)
        assert snap is not None and snap["id"] == stale.id


class TestPromote:
    @pytest.mark.asyncio
    async def test_promote_retiers_in_place(self, ugraph):
        node = await ugraph.store(MemoryRecord(
            content="durable fact about the alpha widget",
            role=Role.FACT, tier=Tier.PERSISTENT,
        ))
        other = await ugraph.add_node("Neighbour", content="a related neighbour node")
        await ugraph.add_edge(node.id, other.id, "related", weight=0.9)

        assert ugraph.promote(node.id, tier="permanent", role="identity") is True
        fetched = await ugraph.get_node(node.id)
        assert fetched.tier == Tier.PERMANENT.value
        assert fetched.role == Role.IDENTITY.value
        # metadata mirror updated too
        assert fetched.metadata.get("tier") == Tier.PERMANENT.value
        assert fetched.metadata.get("role") == Role.IDENTITY.value
        # IN PLACE: same id, edges survive (no delete+re-store)
        neighbours = await ugraph.get_neighbors(node.id)
        assert any(n.id == other.id for n in neighbours)

    @pytest.mark.asyncio
    async def test_promote_tier_only_keeps_role(self, ugraph):
        node = await ugraph.store(MemoryRecord(
            content="a decision we made about the deploy",
            role=Role.DECISION, tier=Tier.SESSION,
        ))
        assert ugraph.promote(node.id, tier="trace") is True
        fetched = await ugraph.get_node(node.id)
        assert fetched.tier == Tier.TRACE.value
        assert fetched.role == Role.DECISION.value  # untouched

    @pytest.mark.asyncio
    async def test_promotion_protects_from_sweep(self, ugraph):
        now = time.time()
        stale = await _store_aged(
            ugraph, "scratch note about the beta gizmo", Tier.EPHEMERAL, 7200, now)
        assert ugraph.promote(stale.id, tier="permanent") is True
        stats = ugraph.sweep(now=now)
        assert stats["archived"] == 0
        assert stats["skipped_permanent"] == 1
        assert await ugraph.get_node(stale.id) is not None

    @pytest.mark.asyncio
    async def test_promote_ledgers_update(self, ugraph):
        node = await ugraph.store(MemoryRecord(
            content="durable fact about the alpha widget", role=Role.FACT,
        ))
        ugraph.promote(node.id, tier="relational")
        assert ugraph._governed.ledger.stats()["by_type"].get("update", 0) >= 1

    @pytest.mark.asyncio
    async def test_promote_rejects_bad_input(self, ugraph):
        node = await ugraph.store(MemoryRecord(
            content="durable fact about the alpha widget", role=Role.FACT,
        ))
        assert ugraph.promote("nonexistent_id", tier="permanent") is False
        assert ugraph.promote(node.id) is False  # no-op args
        assert ugraph.promote(node.id, tier="bogus_tier") is False
        assert ugraph.promote(node.id, role="bogus_role") is False
        # node untouched
        fetched = await ugraph.get_node(node.id)
        assert fetched.tier == Tier.PERSISTENT.value
