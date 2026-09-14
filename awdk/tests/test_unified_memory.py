"""Tests for the self-maintaining unified memory (AITHER_UNIFIED_MEMORY).

Proves: (1) flag-off behaviour is unchanged (no governance, plain search),
(2) flag-on store + recall_with_activation ranks a CORRECTION above a stale
FACT, and (3) an EPHEMERAL record decays out relative to a PERSISTENT one.
"""

import time

import pytest

from adk.graph_memory import GraphMemory, unified_memory_enabled, unified_memory_mode
from adk.unified_contract import MemoryRecord, Role, Tier


# ─── feature flag ────────────────────────────────────────────────────────────

class TestFeatureFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
        assert unified_memory_mode() == "off"
        assert unified_memory_enabled() is False

    def test_on_and_shadow(self, monkeypatch):
        monkeypatch.setenv("AITHER_UNIFIED_MEMORY", "on")
        assert unified_memory_mode() == "on"
        assert unified_memory_enabled() is True
        monkeypatch.setenv("AITHER_UNIFIED_MEMORY", "shadow")
        assert unified_memory_mode() == "shadow"
        assert unified_memory_enabled() is True

    def test_off_governance_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
        g = GraphMemory(db_path=tmp_path / "off.db", agent_name="t")
        assert g._governed is None
        assert g._unified_mode == "off"

    def test_on_governance_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITHER_UNIFIED_MEMORY", "on")
        g = GraphMemory(db_path=tmp_path / "on.db", agent_name="t")
        assert g._governed is not None


# ─── flag-off: legacy path unchanged ─────────────────────────────────────────

class TestFlagOffUnchanged:
    @pytest.mark.asyncio
    async def test_add_node_and_search_still_work(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
        g = GraphMemory(db_path=tmp_path / "legacy.db", agent_name="t")
        await g.add_node("Memory", content="Graph-based memory system with embeddings")
        res = await g.search("memory graph", limit=5)
        assert len(res) >= 1
        # no governance side effects
        assert g._governed is None

    @pytest.mark.asyncio
    async def test_new_columns_present_but_neutral(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AITHER_UNIFIED_MEMORY", raising=False)
        g = GraphMemory(db_path=tmp_path / "cols.db", agent_name="t")
        node = await g.add_node("Fact", content="just a plain statement of record")
        fetched = await g.get_node(node.id)
        assert fetched is not None
        # role/confidence/tier are populated with sane defaults
        assert fetched.role in Role.__members__.values() or fetched.role == "fact"
        assert 0.0 <= fetched.confidence <= 1.0
        assert fetched.tier


# ─── flag-on: authority recall ───────────────────────────────────────────────

@pytest.fixture
def ugraph(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_UNIFIED_MEMORY", "on")
    return GraphMemory(db_path=tmp_path / "unified.db", agent_name="t")


class TestUnifiedRecall:
    @pytest.mark.asyncio
    async def test_correction_ranks_above_stale_fact(self, ugraph):
        now = time.time()
        # a stale FACT (last reinforced 40 days ago) + a fresh CORRECTION
        await ugraph.store(MemoryRecord(
            content="The deploy port is 8080 for the widget service",
            role=Role.FACT, tier=Tier.PERSISTENT, confidence=0.7,
            last_reinforced=now - 40 * 86400, stale=True,
        ))
        await ugraph.store(MemoryRecord(
            content="Correction the deploy port for the widget service is 9090",
            role=Role.CORRECTION, tier=Tier.PERSISTENT, confidence=0.8,
        ))
        # include_stale so both are returned and we can assert the ORDER
        res = await ugraph.recall_with_activation(
            "deploy port widget service", limit=5, include_stale=True, now=now,
        )
        assert len(res) >= 2
        assert res[0].role == Role.CORRECTION.value
        # the correction carries its authority label
        assert "CORRECTION" in (res[0].metadata.get("_authority_labels") or [])
        # the stale fact is labelled STALE
        stale_node = next(n for n in res if n.role == Role.FACT.value)
        assert "STALE" in (stale_node.metadata.get("_authority_labels") or [])

    @pytest.mark.asyncio
    async def test_stale_fact_filtered_by_default(self, ugraph):
        now = time.time()
        await ugraph.store(MemoryRecord(
            content="The deploy port is 8080 for the widget service",
            role=Role.FACT, tier=Tier.PERSISTENT, confidence=0.7,
            last_reinforced=now - 40 * 86400, stale=True,
        ))
        await ugraph.store(MemoryRecord(
            content="Correction the deploy port for the widget service is 9090",
            role=Role.CORRECTION, tier=Tier.PERSISTENT, confidence=0.8,
        ))
        res = await ugraph.recall_with_activation(
            "deploy port widget service", limit=5, now=now,
        )
        roles = [n.role for n in res]
        assert Role.CORRECTION.value in roles
        assert Role.FACT.value not in roles  # stale → dropped

    @pytest.mark.asyncio
    async def test_ephemeral_decays_below_persistent(self, ugraph):
        now = time.time()
        await ugraph.store(MemoryRecord(
            content="persistent durable note about the alpha widget",
            role=Role.FACT, tier=Tier.PERSISTENT, confidence=0.7,
        ))
        await ugraph.store(MemoryRecord(
            content="scratch ephemeral note about the alpha widget",
            role=Role.FACT, tier=Tier.EPHEMERAL, confidence=0.7,
        ))
        # recall 2 hours later: the ephemeral memory (15-min half-life) has decayed
        future = now + 2 * 3600
        res = await ugraph.recall_with_activation(
            "alpha widget note", limit=5, now=future,
        )
        tiers = [n.tier for n in res]
        assert Tier.PERSISTENT.value in tiers
        # the persistent record outranks the ephemeral one
        if Tier.EPHEMERAL.value in tiers:
            assert tiers.index(Tier.PERSISTENT.value) < tiers.index(Tier.EPHEMERAL.value)

    @pytest.mark.asyncio
    async def test_store_ledgers_mutation(self, ugraph):
        await ugraph.store(MemoryRecord(content="a fresh durable fact", role=Role.FACT))
        stats = ugraph._governed.ledger.stats()
        assert stats["by_type"].get("store", 0) >= 1

    @pytest.mark.asyncio
    async def test_supersede_marks_old_and_links(self, ugraph):
        old = await ugraph.store(MemoryRecord(
            content="the build uses webpack for bundling", role=Role.FACT,
        ))
        new = await ugraph.supersede(
            old.id, "Correction the build uses vite for bundling now",
            reason="migrated bundler",
        )
        refreshed_old = await ugraph.get_node(old.id)
        assert refreshed_old.metadata.get("superseded_by") == new.id
        assert refreshed_old.metadata.get("stale") is True
        # SUPERSEDE is recorded in the ledger
        assert ugraph._governed.ledger.stats()["by_type"].get("supersede", 0) >= 1

    @pytest.mark.asyncio
    async def test_supersede_cascade_decays_related_neighbour(self, ugraph):
        # Superseding a fact must DECAY a RELATED neighbour's recall weight — not just write
        # cosmetic hint metadata. Regression guard: the cascade previously wrote temporal_consistency_hint
        # (never read by scoring), leaving the neighbour's effective_confidence unchanged.
        a = await ugraph.store(MemoryRecord(content="the build uses webpack for bundling", role=Role.FACT))
        b = await ugraph.store(MemoryRecord(content="the asset pipeline depends on the bundler configuration", role=Role.FACT))
        await ugraph.add_edge(a.id, b.id, "related", weight=1.0)
        before = await ugraph.get_node(b.id)
        assert float((before.metadata or {}).get("temporal_consistency", 1.0)) == pytest.approx(1.0)
        new = await ugraph.supersede(a.id, "Correction the build uses vite for bundling now", reason="migrated bundler")
        after = await ugraph.get_node(b.id)
        tc = float((after.metadata or {}).get("temporal_consistency", 1.0))
        # the field recall scoring CONSUMES (effective_confidence = confidence * temporal_consistency) actually dropped
        assert tc < 1.0, f"cascade did not bite the neighbour's temporal_consistency (got {tc})"
        assert (after.metadata or {}).get("cascade_from") == new.id
