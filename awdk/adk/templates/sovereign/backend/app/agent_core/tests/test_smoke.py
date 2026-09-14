"""Smoke + behaviour tests for agent_core."""

from __future__ import annotations

import asyncio
import tempfile
import time

import pytest

import agent_core as ac
from agent_core.bridge import _CircuitBreaker
from agent_core.tiers import (
    DECAY_RATES,
    MemoryTier,
    PROMOTION_CONFIDENCE,
    PROMOTION_SOURCES,
    PROMOTION_THRESHOLDS,
)


# ── Constants sanity ──────────────────────────────────────────────────────


def test_promotion_thresholds_monotonic():
    # working -> episodic -> semantic -> archival: 5,10,25,50
    assert PROMOTION_THRESHOLDS[MemoryTier.WORKING] == 5
    assert PROMOTION_THRESHOLDS[MemoryTier.EPISODIC] == 10
    assert PROMOTION_THRESHOLDS[MemoryTier.SEMANTIC] == 25
    assert PROMOTION_THRESHOLDS[MemoryTier.ARCHIVAL] == 50


def test_promotion_confidence_monotonic():
    assert PROMOTION_CONFIDENCE[MemoryTier.WORKING] == pytest.approx(0.90)
    assert PROMOTION_CONFIDENCE[MemoryTier.EPISODIC] == pytest.approx(0.95)
    assert PROMOTION_CONFIDENCE[MemoryTier.SEMANTIC] == pytest.approx(0.98)
    assert PROMOTION_CONFIDENCE[MemoryTier.ARCHIVAL] == pytest.approx(0.99)


def test_promotion_sources_monotonic():
    assert PROMOTION_SOURCES[MemoryTier.WORKING] == 1
    assert PROMOTION_SOURCES[MemoryTier.EPISODIC] == 2
    assert PROMOTION_SOURCES[MemoryTier.SEMANTIC] == 3
    assert PROMOTION_SOURCES[MemoryTier.ARCHIVAL] == 4


def test_decay_rates_descend():
    # Each higher tier decays slower than the previous one.
    order = [
        MemoryTier.WORKING, MemoryTier.EPISODIC,
        MemoryTier.SEMANTIC, MemoryTier.ARCHIVAL, MemoryTier.IDENTITY,
    ]
    rates = [DECAY_RATES[t] for t in order]
    assert all(a >= b for a, b in zip(rates, rates[1:]))
    assert DECAY_RATES[MemoryTier.IDENTITY] == 0


# ── LocalMemoryStore roundtrip ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_roundtrip_and_recall():
    db = tempfile.mkstemp(suffix=".db")[1]
    store = ac.LocalMemoryStore(db_path=db)
    await store.init()
    mid = await store.store(
        "tenantA", "Customer prefers concise weekly summaries on Fridays.",
        tier=MemoryTier.WORKING, source="conversation",
        confidence=0.8, document_citations=["doc-1"],
    )
    assert mid
    rows = await store.recall("tenantA", "weekly summaries", limit=5)
    assert len(rows) == 1
    assert rows[0].memory_id == mid
    assert rows[0].tier == MemoryTier.WORKING
    assert rows[0].confidence == pytest.approx(0.8)


# ── Citation-aware tombstone eviction ────────────────────────────────────


@pytest.mark.asyncio
async def test_block_recall_for_document_tombstones_citing_memories():
    db = tempfile.mkstemp(suffix=".db")[1]
    store = ac.LocalMemoryStore(db_path=db)
    await store.init()

    # Two memories cite doc-X; one cites only doc-Y.
    mx1 = await store.store("t1", "first memory citing X", confidence=0.7,
                            document_citations=["doc-X"])
    mx2 = await store.store("t1", "second memory citing X", confidence=0.7,
                            document_citations=["doc-X", "doc-Y"])
    my = await store.store("t1", "only cites Y", confidence=0.7,
                           document_citations=["doc-Y"])

    n = await store.block_recall_for_document("t1", "doc-X")
    assert n == 2

    # Default recall excludes blocked rows.
    visible = await store.recall("t1", "", limit=20)
    visible_ids = {r.memory_id for r in visible}
    assert my in visible_ids
    assert mx1 not in visible_ids
    assert mx2 not in visible_ids

    # include_blocked surfaces them again.
    all_rows = await store.recall("t1", "", limit=20, include_blocked=True)
    assert {r.memory_id for r in all_rows} >= {mx1, mx2, my}


# ── Circuit breaker ──────────────────────────────────────────────────────


def test_circuit_breaker_opens_after_threshold():
    cb = _CircuitBreaker(threshold=5, reset_after=60.0)
    for _ in range(4):
        cb.record_failure()
        assert not cb.is_open()
    cb.record_failure()
    assert cb.is_open()


def test_circuit_breaker_half_opens_after_reset():
    cb = _CircuitBreaker(threshold=2, reset_after=0.01)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open()
    time.sleep(0.05)
    # Should self-clear opened_at when probed.
    assert not cb.is_open()


def test_circuit_breaker_success_resets():
    cb = _CircuitBreaker(threshold=3, reset_after=60.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    # Only 2 failures since last success — should NOT be open yet.
    assert not cb.is_open()


# ── integration helpers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_and_recall_through_client():
    db = tempfile.mkstemp(suffix=".db")[1]
    store = ac.LocalMemoryStore(db_path=db)
    await store.init()
    client = ac.UnifiedMemoryClient(store=store, mode=ac.MemoryMode.LOCAL)
    await client.init()

    await ac.record_chat_turn(
        client, "t1", "conv1",
        user_message="Where do refunds get processed at our store?",
        assistant_response=(
            "Refunds are processed within fourteen calendar days from purchase "
            "as documented in our terms-and-conditions document."
        ),
        context_chunks=[{"doc_id": "doc-policy", "text": "refunds 14d"}],
    )

    block = await ac.build_recall_context(
        client, "t1", "refunds", limit=5, min_confidence=0.0,
    )
    assert "Relevant context" in block
    assert "(working" in block
    assert "Refunds" in block


@pytest.mark.asyncio
async def test_build_recall_context_is_safe_on_no_client():
    out = await ac.build_recall_context(None, "t1", "anything")
    assert out == ""


@pytest.mark.asyncio
async def test_record_chat_turn_skips_trivial():
    db = tempfile.mkstemp(suffix=".db")[1]
    store = ac.LocalMemoryStore(db_path=db)
    await store.init()
    client = ac.UnifiedMemoryClient(store=store, mode=ac.MemoryMode.LOCAL)
    await client.init()

    # Below min_chars threshold (80) — should be dropped silently.
    await ac.record_chat_turn(
        client, "t1", "conv1",
        user_message="hi",
        assistant_response="ok",
    )
    rows = await store.recall("t1", "", limit=10)
    assert rows == []


# ── citation-aware eviction via tombstone_document helper ────────────────


@pytest.mark.asyncio
async def test_tombstone_document_helper_evicts():
    db = tempfile.mkstemp(suffix=".db")[1]
    store = ac.LocalMemoryStore(db_path=db)
    await store.init()
    client = ac.UnifiedMemoryClient(store=store, mode=ac.MemoryMode.LOCAL)
    await client.init()

    await client.store_memory(
        "t1", "Refund window memory",
        tier=MemoryTier.WORKING, confidence=0.7,
        document_citations=["doc-policy"],
    )

    blocked = await ac.evict_cached_conversations_referencing(
        tenant_id="t1",
        document_id="doc-policy",
        memory_client=client,
        cache=None,
    )
    assert blocked["memories_blocked"] == 1

    # Memory should now be hidden from default recall.
    visible = await store.recall("t1", "", limit=20)
    assert visible == []
    all_rows = await store.recall("t1", "", limit=20, include_blocked=True)
    assert len(all_rows) == 1
