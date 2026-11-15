"""GraphMemory.sweep() + promote() — the maintenance primitives.

Proves: stale unreinforced nodes past their tier TTL are archived (tombstone +
FORGET ledger entry + removed); reinforced nodes survive; PERMANENT is immune;
max_nodes archives the lowest-scored overflow; dry_run acts on nothing;
tombstones are REVERSIBLE via TombstoneStore.recover(); and promote() re-tiers/
re-roles a node in place (edges survive, UPDATE ledgered, sweep-immunity when
promoted to permanent).
"""

import json
import os
import time

import pytest

from adk.decisions.store import (
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
)
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


# ── the decision store's hourly stale pass ──────────────────────────
#
# The queue filled with asks raised by sessions that then died: nothing will
# ever answer them and they pin themselves above every live ask. The store
# retires exactly those -- open, older than the threshold, raiser PROVABLY gone,
# no live waiter -- as ``expired`` with the declared default applied and the
# reason on the card. It must never retire on an unknown, and never a
# credential / blocked / blocker.

HOUR = 3600.0
DEAD_PID = 999_999


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """A private session registry, so no real running session is consulted."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("AITHER_CLAUDE_SESSIONS_DIR", str(directory))
    return directory


def _register(directory, session_id: str, pid: int) -> None:
    (directory / f"{session_id}.json").write_text(
        json.dumps({"sessionId": session_id, "pid": pid, "status": "idle"}),
        encoding="utf-8",
    )


def _dcard(store: DecisionStore, title: str, *, kind: str = "decision",
           age_h: float = 30.0, session_id: str = "sess-dead",
           session_pid: int = 0, waiters=(), dedupe: str = "") -> DecisionCard:
    common = dict(id="", title=title, kind=kind, dedupe_key=dedupe,
                  source=DecisionSource(session_id=session_id,
                                        session_pid=session_pid),
                  created_at=time.time() - age_h * HOUR,
                  waiters=list(waiters))
    if kind == "credential":
        return store.create(DecisionCard(
            **common, credential_format="custom",
            credential_description="need it", secret_name=f"SECRET_{title}",
        ))
    if kind in ("blocked", "blocker"):
        return store.create(DecisionCard(**common))
    return store.create(DecisionCard(
        **common, default_key="a",
        options=[DecisionOption(key="a", label="Accept", recommended=True),
                 DecisionOption(key="b", label="Decline")],
    ))


@pytest.fixture
def decisions(tmp_path):
    return DecisionStore(tmp_path / "cards")


class TestDecisionStaleSweep:
    def test_a_dead_sessions_card_retires_with_the_default_applied(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        card = _dcard(decisions, "orphan")
        retired = decisions.retire_stale()
        assert [c.id for c in retired] == [card.id]
        after = decisions.get(card.id)
        assert after.status == "expired"
        assert after.answer == "a"                      # the declared default
        assert after.answered_via == "session-gone"
        assert after.retire_reason and "gone" in after.retire_reason
        assert after.is_open is False

    def test_a_live_sessions_card_is_untouched(self, decisions, sessions):
        _register(sessions, "sess-live", os.getpid())
        card = _dcard(decisions, "still wanted", session_id="sess-live")
        assert decisions.retire_stale() == []
        assert decisions.get(card.id).status == "open"

    def test_an_unmeasurable_session_is_never_retired(self, decisions, sessions):
        # No registry record and no recorded pid: "not measured", never "gone".
        card = _dcard(decisions, "unknown raiser")
        assert decisions.retire_stale() == []
        assert decisions.get(card.id).status == "open"

    def test_protected_kinds_never_auto_retire(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        kept = [
            _dcard(decisions, "cred", kind="credential"),
            _dcard(decisions, "blocked", kind="blocked"),
            _dcard(decisions, "blocker", kind="blocker"),
        ]
        assert decisions.retire_stale() == []
        for card in kept:
            assert decisions.get(card.id).status == "open"

    def test_a_live_waiter_keeps_the_card_open(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        _register(sessions, "sess-live", os.getpid())
        card = _dcard(decisions, "two sessions", dedupe="wait-one",
                      waiters=[DecisionSource(session_id="sess-live")])
        assert decisions.retire_stale() == []
        assert decisions.get(card.id).status == "open"

    def test_a_card_younger_than_the_threshold_is_untouched(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        card = _dcard(decisions, "only two hours old", age_h=2)
        assert decisions.retire_stale() == []
        assert decisions.get(card.id).status == "open"

    def test_dry_run_reports_without_writing(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        card = _dcard(decisions, "would retire")
        would = decisions.retire_stale(dry_run=True)
        assert [c.id for c in would] == [card.id]
        assert would[0].retire_reason
        still = decisions.get(card.id)
        assert still.status == "open"
        assert still.retire_reason is None

    def test_an_already_closed_card_is_not_swept_again(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        card = _dcard(decisions, "answered before the sweep")
        decisions.answer(card.id, "a")
        assert decisions.retire_stale() == []
        assert decisions.get(card.id).status == "answered"

    def test_the_threshold_is_configurable(self, decisions, sessions, monkeypatch):
        _register(sessions, "sess-dead", DEAD_PID)
        monkeypatch.setenv("AITHER_DECISIONS_RETIRE_HOURS", "1")
        card = _dcard(decisions, "past an hour", age_h=2)
        assert [c.id for c in decisions.retire_stale()] == [card.id]
        assert "1h" in (decisions.get(card.id).retire_reason or "")

    def test_the_sweep_scales_to_the_whole_queue(self, decisions, sessions):
        _register(sessions, "sess-dead", DEAD_PID)
        cards = [_dcard(decisions, f"orphan {i}", dedupe=f"orphan-{i}") for i in range(5)]
        retired = decisions.retire_stale()
        assert {c.id for c in retired} == {c.id for c in cards}
        assert all(decisions.get(c.id).status == "expired" for c in cards)
