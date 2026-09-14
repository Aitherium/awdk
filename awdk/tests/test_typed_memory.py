"""Tests for adk.typed_memory — authority ranking, decay, supersession, constraints."""

from __future__ import annotations

import time

import pytest

from adk.memory import Memory
from adk.typed_memory import (
    Role,
    Tier,
    TypedMemory,
    freshness,
    infer_role,
    make_metadata,
    parse_constraint,
)


@pytest.fixture
def mem(tmp_path):
    return Memory(db_path=tmp_path / "typed.db", agent_name="test")


@pytest.fixture
def typed(mem):
    # disable the Spirit bridge so tests stay fully local
    mem._spirit_enabled = False
    return TypedMemory(mem)


# ── role inference ──────────────────────────────────────────────────────────


def test_infer_role_correction():
    assert infer_role("Actually that's wrong, use Postgres") == Role.CORRECTION


def test_infer_role_decision():
    assert infer_role("We decided to go with Stripe for payments") == Role.DECISION


def test_infer_role_preference():
    assert infer_role("I always prefer tabs over spaces") == Role.PREFERENCE


def test_infer_role_defaults_fact():
    assert infer_role("The sky is blue") == Role.FACT


def test_infer_role_from_category():
    assert infer_role("anything", {"category": "decision"}) == Role.DECISION


# ── constraint parsing ────────────────────────────────────────────────────────


def test_parse_constraint_avoid():
    assert parse_constraint("Never commit secrets").startswith("AVOID:")


def test_parse_constraint_require():
    assert parse_constraint("Always run the tests first").startswith("REQUIRE:")


def test_parse_constraint_prefer():
    assert parse_constraint("Prefer ruff over flake8").startswith("PREFER:")


# ── authority-ranked recall ───────────────────────────────────────────────────


async def test_correction_outranks_fact(typed):
    await typed.remember("Deploys go to the us-east-1 region", role=Role.FACT, confidence=0.7)
    await typed.remember("Deploys go to the eu-west-1 region", role=Role.CORRECTION, confidence=0.7)
    items = await typed.recall("region", limit=5)
    assert items, "expected recall results"
    # correction (authority 1.3) should rank above the plain fact (1.0)
    assert items[0].role == Role.CORRECTION
    assert "eu-west-1" in items[0].content
    assert "CORRECTION" in items[0].labels


async def test_recall_labels_new_record(typed):
    await typed.remember("A neutral fact about widgets", role=Role.FACT)
    items = await typed.recall("widgets", limit=3)
    assert items
    assert "NEW" in items[0].labels or "CONFIDENT" not in items[0].labels


# ── freshness decay ────────────────────────────────────────────────────────────


def test_freshness_decays_with_age():
    now = 1_000_000.0
    fresh_md = make_metadata("note", role=Role.TASK, now=now)          # SESSION tier, 1h half-life
    entry_fresh = _entry(fresh_md, "note", ts=now)
    entry_old = _entry({**fresh_md, "created_at": now - 3600, "last_reinforced": now - 3600}, "note", ts=now - 3600)
    f_new = freshness(entry_fresh, now=now)
    f_old = freshness(entry_old, now=now)
    assert f_new > f_old
    assert f_old == pytest.approx(0.5, abs=0.05)  # one half-life → ~0.5


def test_permanent_tier_never_decays():
    now = 2_000_000.0
    md = make_metadata("identity", role=Role.IDENTITY, now=now - 10 * 365 * 86400)
    assert md["tier"] == Tier.PERMANENT
    entry = _entry(md, "identity", ts=now - 10 * 365 * 86400)
    assert freshness(entry, now=now) == 1.0


# ── supersession + cascade ─────────────────────────────────────────────────────


async def test_supersede_marks_old_and_promotes_new(typed):
    old_id = await typed.remember("Use MySQL", role=Role.DECISION)
    new_id = await typed.supersede(old_id, "Actually use Postgres now")

    old = await typed.get(old_id)
    assert old.metadata["superseded_by"] == new_id
    assert old.metadata["stale"] is True

    # default recall hides the superseded record, surfaces the correction
    # (both share the token "use" so the substring search returns both)
    items = await typed.recall("use", limit=5)
    contents = [it.content for it in items]
    assert any("Postgres" in c for c in contents)
    assert not any(c == "Use MySQL" for c in contents)


async def test_supersede_cascade_decays_neighbour(typed):
    neighbour_id = await typed.remember("MySQL tuning notes", role=Role.FACT, confidence=0.8)
    old_id = await typed.remember(
        "Use MySQL", role=Role.DECISION, related_ids=[neighbour_id], confidence=0.8
    )
    await typed.supersede(old_id, "Use Postgres", cascade=True)

    neighbour = await typed.get(neighbour_id)
    # confidence decayed by ×0.7
    assert neighbour.metadata["confidence"] == pytest.approx(0.8 * 0.7, abs=1e-6)


async def test_include_stale_returns_superseded(typed):
    old_id = await typed.remember("Use MySQL", role=Role.DECISION)
    await typed.supersede(old_id, "Use Postgres")
    items = await typed.recall("MySQL", limit=10, include_stale=True)
    assert any("MySQL" in it.content for it in items)


# ── reinforcement ──────────────────────────────────────────────────────────────


async def test_reinforce_bumps_count_and_confidence(typed):
    rec_id = await typed.remember("Stable fact", role=Role.FACT, confidence=0.7)
    assert await typed.reinforce(rec_id)
    entry = await typed.get(rec_id)
    assert entry.metadata["reinforcement_count"] == 1
    assert entry.metadata["confidence"] == pytest.approx(0.75, abs=1e-6)


async def test_reinforce_unknown_id_returns_false(typed):
    assert await typed.reinforce("tm_doesnotexist") is False


# ── constraints block ─────────────────────────────────────────────────────────


async def test_constraints_block_surfaces_decisions(typed):
    await typed.remember("Always run ruff before committing", role=Role.DECISION)
    await typed.remember("Never push directly to main", role=Role.CORRECTION)
    await typed.remember("The cache lives in Redis", role=Role.FACT)
    block = await typed.constraints_block()
    assert "DECISIONS / CORRECTIONS" in block
    assert "REQUIRE:" in block or "AVOID:" in block
    assert "Redis" not in block  # plain facts are not constraints


async def test_superseded_decision_excluded_from_constraints(typed):
    dec_id = await typed.remember("Always deploy on Fridays", role=Role.DECISION)
    await typed.supersede(dec_id, "Never deploy on Fridays")
    block = await typed.constraints_block()
    assert "Always deploy on Fridays" not in block


async def test_context_block_empty_when_no_memories(typed):
    assert await typed.context_block("nothing stored yet") == ""


# ── persistence across wrapper instances (SQLite write-back) ───────────────────


async def test_typed_metadata_persists(mem):
    mem._spirit_enabled = False
    t1 = TypedMemory(mem)
    rec_id = await t1.remember("Persistent decision", role=Role.DECISION, confidence=0.9)
    # fresh wrapper over the same backing store
    t2 = TypedMemory(mem)
    entry = await t2.get(rec_id)
    assert entry is not None
    assert entry.metadata["role"] == Role.DECISION
    assert entry.metadata["confidence"] == 0.9


# ── helpers ────────────────────────────────────────────────────────────────────


def _entry(metadata: dict, value: str, ts: float):
    from adk.memory import MemoryEntry
    return MemoryEntry(key="k", value=value, category=metadata.get("role", "fact"),
                       timestamp=ts, metadata=metadata)
