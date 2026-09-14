"""Metering tests — BYO/local backend exemption for community tier."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from adk.metering import (
    AgentMeter,
    QuotaAction,
    QuotaConfig,
    get_meter,
    is_metered_backend,
)


@pytest.fixture(autouse=True)
def _clear_meter_singletons():
    """Isolate the get_meter() singleton cache between tests.

    ``get_meter`` caches AgentMeter instances by name in a module global, so a
    meter created (and its _backend_type mutated) by one test would otherwise
    leak into the next under cross-file ordering — making assertions on a fresh
    meter's default state flaky. Clear it before and after every test.
    """
    import adk.metering as _m
    _m._meters.clear()
    yield
    _m._meters.clear()


@pytest.fixture
def temp_db(tmp_path):
    """Temporary database path for each test."""
    db_path = tmp_path / "test.db"
    yield db_path


def test_gateway_backend_applies_monthly_cap(temp_db):
    """Gateway backend (Aitherium cloud) respects monthly token limit."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(monthly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "gateway"
    assert meter._quota.monthly_limit == 1000

    # Record 700 tokens — should be under soft limit (80%)
    meter.record_usage(tokens=700, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.ALLOW

    # Record 150 more (total 850, at 85%) — should be WARN
    meter.record_usage(tokens=150, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.WARN

    # Record 200 more (total 1050, > 1000) — should hit hard limit
    meter.record_usage(tokens=200, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.HARD_LIMIT


def test_byo_backend_no_cap_anthropic(temp_db):
    """BYO Anthropic API key never capped, even in community tier."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=1000),  # cap set by tier
        db_path=temp_db,
    )
    meter._backend_type = "anthropic"
    # In real agent, this would be set to 0; we override for test
    meter._quota.monthly_limit = 0

    # Record 10M tokens — should still be allowed (no cap)
    for _ in range(100):
        meter.record_usage(tokens=100_000, model="claude-opus")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_byo_backend_no_cap_openai(temp_db):
    """BYO OpenAI API key never capped."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "openai"
    meter._quota.monthly_limit = 0

    for _ in range(50):
        meter.record_usage(tokens=50_000, model="gpt-4o")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_local_ollama_backend_no_cap(temp_db):
    """Local Ollama inference never capped."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "ollama"
    meter._quota.monthly_limit = 0

    for _ in range(200):
        meter.record_usage(tokens=10_000, model="llama2")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_local_vllm_backend_no_cap(temp_db):
    """Local vLLM inference never capped."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "vllm"
    meter._quota.monthly_limit = 0

    for _ in range(200):
        meter.record_usage(tokens=10_000, model="qwen-32b")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_gateway_warns_at_80_percent(temp_db, caplog):
    """Gateway backend emits one warning at 80% of monthly cap."""
    import logging
    caplog.set_level(logging.WARNING)

    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(monthly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "gateway"

    # Record 799 tokens (79.9%) — no warn yet
    meter.record_usage(tokens=799, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.ALLOW
    assert "80%" not in caplog.text

    # Record 10 more (809 = 80.9%) — should warn
    meter.record_usage(tokens=10, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.WARN
    assert "80%" in caplog.text
    assert "https://aitherium.com/buy" in caplog.text

    # Record 200 more (1009 = 100.9%) — hard limit, but no double-warn
    meter.record_usage(tokens=200, model="aither-orchestrator")
    assert meter.can_spend() == QuotaAction.HARD_LIMIT
    # Check that warning was emitted exactly once
    warnings = [r for r in caplog.records if "80%" in r.message]
    assert len(warnings) == 1


def test_byo_backend_no_80_percent_warning(temp_db, caplog):
    """BYO backends (no cap) never warn about usage."""
    import logging
    caplog.set_level(logging.WARNING)

    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=10_000_000),
        db_path=temp_db,
    )
    meter._backend_type = "anthropic"
    meter._quota.monthly_limit = 0

    # Record huge usage with no cap
    for _ in range(1000):
        meter.record_usage(tokens=10_000, model="claude-opus")
    meter.can_spend()

    # No warning should be in logs
    assert "80%" not in caplog.text


def test_quota_config_zero_is_unlimited(temp_db):
    """Quota limit of 0 means unlimited (no enforcement)."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(daily_limit=0, monthly_limit=0),  # 0 = unlimited
        db_path=temp_db,
    )
    meter._backend_type = "gateway"

    for _ in range(100):
        meter.record_usage(tokens=100_000, model="aither-orchestrator")
    # With limit=0, no HARD_LIMIT possible
    assert meter.can_spend() == QuotaAction.ALLOW


def test_is_metered_backend_only_gateway():
    """Only the metered gateway is a metered backend; everything else fail-opens."""
    assert is_metered_backend("gateway") is True
    assert is_metered_backend("GATEWAY") is True  # case-insensitive
    assert is_metered_backend(" gateway ") is True  # whitespace-tolerant
    for byo in ("anthropic", "openai", "deepseek", "groq", "together",
                "ollama", "vllm", "llamacpp", "lmstudio",
                "unknown", "custom", "", None):  # type: ignore[arg-type]
        assert is_metered_backend(byo) is False


def test_unknown_backend_never_capped_even_with_limits(temp_db):
    """A non-gateway backend fail-opens even if a quota carries hard limits.

    This is the core BYO-default invariant: a stray cap (community default, a
    product super-cap, a custom endpoint the allowlist didn't recognize) can
    never brick a self-hosted agent — enforcement requires gateway provenance.
    """
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(monthly_limit=1000, daily_limit=1000, hourly_limit=1000),
        db_path=temp_db,
    )
    meter._backend_type = "unknown"  # the agent didn't recognize the provider

    # Blow past every limit — still ALLOW because the backend isn't metered.
    for _ in range(100):
        meter.record_usage(tokens=10_000, model="my-custom-endpoint")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_custom_byo_backend_never_capped(temp_db):
    """A custom OpenAI-compatible endpoint (not on any allowlist) is uncapped."""
    meter = AgentMeter(
        agent_name="test",
        quota=QuotaConfig(monthly_limit=500),
        db_path=temp_db,
    )
    meter._backend_type = "openrouter"  # not in any historical allowlist
    for _ in range(50):
        meter.record_usage(tokens=50_000, model="some/model")
    assert meter.can_spend() == QuotaAction.ALLOW


def test_default_daily_limit_is_unlimited():
    """The default QuotaConfig must NOT carry a latent daily cap (was 100k)."""
    assert QuotaConfig().daily_limit == 0
    assert QuotaConfig().monthly_limit == 0
    assert QuotaConfig().hourly_limit == 0


def test_meter_tracks_backend_type():
    """AgentMeter stores backend type for provenance tracking."""
    meter = get_meter("test_agent", quota=QuotaConfig(monthly_limit=100_000))
    assert hasattr(meter, "_backend_type")
    assert meter._backend_type == "unknown"

    meter._backend_type = "gateway"
    assert meter._backend_type == "gateway"

    meter._backend_type = "anthropic"
    assert meter._backend_type == "anthropic"
