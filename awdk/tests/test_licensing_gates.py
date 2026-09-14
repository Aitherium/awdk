"""Tests for fail-closed per-call entitlement gates in adk.

Verifies that:
1. Effort gates: COMMUNITY tier cannot escalate to reasoning (effort 7+) on metered gateway
2. Cron gates: only BUILDER+ can create cron routines
3. Swarm gates: only PROFESSIONAL+ can dispatch to swarm (already implemented)
4. Channels gates: only STARTER+ can use channel adapters (already implemented)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from pathlib import Path

import pytest

import adk.licensing as lic
from adk.agent import AitherAgent
from adk.licensing import (
    Entitlements, LicenseError, License, Tier,
    get_license_manager, reset_license_manager,
)
from adk.routines import RoutineStore


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts from a pristine environment."""
    for var in (
        "AITHER_TENANT_SLUG", "AITHER_LICENSE_KEY", "AITHER_LICENSE_FILE",
        "AITHER_LICENSE_ENFORCE", "AITHER_LICENSE_PUBLIC_KEY",
        "AITHER_CODE_LOCATOR", "AITHER_CODEGRAPH_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_license_manager()
    yield
    reset_license_manager()


# ── effort gate tests ────────────────────────────────────────────────────────

class TestEffortGate:
    """Fail-closed enforcement of reasoning effort (7-10) per call."""

    async def _make_agent_with_license(self, tier: Tier) -> AitherAgent:
        """Create an agent with a specific license tier and metered gateway flag."""
        agent = AitherAgent("test-agent", builtin_tools=False)
        # Inject a license at the tier level
        agent._license = lic.LicenseManager(
            License(tier=tier, entitlements=Entitlements.for_tier(tier))
        )
        # Mark as metered so effort gates apply
        agent._metered_gateway = True
        return agent

    @pytest.mark.asyncio
    async def test_community_tier_denies_effort_7_to_10(self):
        """COMMUNITY-tier agent is DENIED effort 7-10 calls (fail-closed)."""
        agent = await self._make_agent_with_license(Tier.COMMUNITY)
        # Effort 7+ should be rejected outright, not silently capped
        with pytest.raises(LicenseError) as exc:
            # This will hit the gate and raise before any LLM call
            await agent.chat("test", effort=7)
        assert "Reasoning effort 7" in str(exc.value)
        assert "paid tier" in str(exc.value)

    @pytest.mark.asyncio
    async def test_community_tier_allows_effort_3_and_below(self):
        """COMMUNITY-tier agent is ALLOWED effort <= 3 (within free tier cap)."""
        agent = await self._make_agent_with_license(Tier.COMMUNITY)
        # Effort 3 and below should not raise (though chat will fail w/o LLM, that's ok)
        # We're testing that the license gate doesn't block it
        try:
            # This might fail on LLM unavailability, but NOT on license
            await agent.chat("test", effort=3)
        except LicenseError as e:
            if "Reasoning effort" in str(e):
                # Should not reach here for effort 3
                pytest.fail(f"COMMUNITY tier should allow effort 3, but got: {e}")
            # Other LicenseErrors (e.g., channels) are ok; we only care about effort
            pass
        except Exception:
            # LLM/setup failures are expected in test env; license gate passed
            pass

    @pytest.mark.asyncio
    async def test_professional_tier_allows_effort_7_to_10(self):
        """PROFESSIONAL-tier agent is ALLOWED effort 7-10."""
        agent = await self._make_agent_with_license(Tier.PROFESSIONAL)
        # Should not raise LicenseError for high effort
        try:
            await agent.chat("test", effort=9)
        except LicenseError as e:
            if "Reasoning effort" in str(e):
                pytest.fail(f"PROFESSIONAL tier should allow effort 9, but got: {e}")
            pass
        except Exception:
            # LLM/setup failures are expected
            pass

    @pytest.mark.asyncio
    async def test_starter_tier_denies_effort_above_cap(self):
        """STARTER tier caps at effort 3; effort 7+ is denied."""
        agent = await self._make_agent_with_license(Tier.STARTER)
        # STARTER is rank 1, so max_effort = 3
        with pytest.raises(LicenseError) as exc:
            await agent.chat("test", effort=8)
        assert "Reasoning effort 8" in str(exc.value)

    @pytest.mark.asyncio
    async def test_internal_tier_allows_all_effort(self):
        """INTERNAL tier allows effort 7-10 (unrestricted)."""
        agent = await self._make_agent_with_license(Tier.INTERNAL)
        # max_effort for INTERNAL is 10
        try:
            await agent.chat("test", effort=10)
        except LicenseError as e:
            if "Reasoning effort" in str(e):
                pytest.fail(f"INTERNAL tier should allow effort 10, but got: {e}")
            pass
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_effort_gate_only_on_metered_gateway(self):
        """Effort gate only applies when _metered_gateway is True."""
        agent = await self._make_agent_with_license(Tier.COMMUNITY)
        agent._metered_gateway = False  # self-hosted, not metered
        # Should not raise on effort 9 when not on metered gateway
        try:
            await agent.chat("test", effort=9)
        except LicenseError as e:
            if "Reasoning effort" in str(e):
                pytest.fail(f"Effort gate should NOT apply off metered gateway, got: {e}")
            pass
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_effort_gate_disabled_when_enforcement_off(self, monkeypatch):
        """When AITHER_LICENSE_ENFORCE=0, effort gate is disabled."""
        monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "0")
        reset_license_manager()
        agent = await self._make_agent_with_license(Tier.COMMUNITY)
        agent._metered_gateway = True
        # Even with enforcement disabled, we reset the manager, so it should allow
        agent._license = lic.LicenseManager(
            License(tier=Tier.COMMUNITY, entitlements=Entitlements.for_tier(Tier.COMMUNITY))
        )
        # Should not raise because enforcement is disabled
        try:
            await agent.chat("test", effort=10)
        except LicenseError as e:
            if "Reasoning effort" in str(e):
                pytest.fail(f"Effort gate should be disabled, but got: {e}")
            pass
        except Exception:
            pass


# ── cron gate tests ──────────────────────────────────────────────────────────

class TestCronGate:
    """Fail-closed enforcement of cron routines per BUILDER+ tier."""

    @pytest.fixture
    def tmp_store(self, tmp_path):
        """Temporary routine store for testing."""
        return RoutineStore("test-agent", path=tmp_path / "routines.json")

    def test_community_tier_denies_cron_creation(self, tmp_store):
        """COMMUNITY-tier store DENIES .create() — fail-closed."""
        # Ensure we're on COMMUNITY tier
        reset_license_manager()
        lm = get_license_manager()
        assert lm.license.tier is Tier.COMMUNITY
        assert lm.can_use_cron() is False

        # Trying to create a routine should raise LicenseError
        with pytest.raises(LicenseError) as exc:
            tmp_store.create("daily-check", "0 9 * * *", "summarize the day")
        assert "Cron" in str(exc.value) or "cron" in str(exc.value)
        assert "requires" in str(exc.value).lower()

    def test_builder_tier_allows_cron_creation(self, tmp_store):
        """BUILDER-tier store ALLOWS .create() — positive assertion."""
        # Manually set a BUILDER-tier license
        builder_lic = License(
            tier=Tier.BUILDER,
            entitlements=Entitlements.for_tier(Tier.BUILDER),
        )
        lic.get_license_manager().license = builder_lic

        # Should succeed
        routine = tmp_store.create("daily-check", "0 9 * * *", "summarize")
        assert routine.name == "daily-check"
        assert routine.cron == "0 9 * * *"

    def test_professional_tier_allows_cron(self, tmp_store):
        """PROFESSIONAL tier also allows cron (rank >= BUILDER)."""
        pro_lic = License(
            tier=Tier.PROFESSIONAL,
            entitlements=Entitlements.for_tier(Tier.PROFESSIONAL),
        )
        lic.get_license_manager().license = pro_lic

        routine = tmp_store.create("nightly", "0 23 * * *", "cleanup")
        assert routine.name == "nightly"

    def test_starter_tier_denies_cron(self, tmp_store):
        """STARTER tier (rank 1) does NOT have cron; only BUILDER+ (rank 2+)."""
        starter_lic = License(
            tier=Tier.STARTER,
            entitlements=Entitlements.for_tier(Tier.STARTER),
        )
        lic.get_license_manager().license = starter_lic
        assert starter_lic.entitlements.cron is False

        with pytest.raises(LicenseError):
            tmp_store.create("job", "0 9 * * *", "work")

    def test_internal_tier_allows_cron(self, tmp_store):
        """INTERNAL tier is unrestricted."""
        internal_lic = License(
            tier=Tier.INTERNAL,
            entitlements=Entitlements.for_tier(Tier.INTERNAL),
        )
        lic.get_license_manager().license = internal_lic

        routine = tmp_store.create("admin-task", "*/5 * * * *", "health check")
        assert routine.name == "admin-task"

    def test_cron_gate_disabled_when_enforcement_off(self, tmp_store, monkeypatch):
        """When AITHER_LICENSE_ENFORCE=0, cron gate is disabled."""
        monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "0")
        reset_license_manager()
        # Even with COMMUNITY tier, enforcement disabled should allow cron
        routine = tmp_store.create("test", "0 9 * * *", "work")
        assert routine.name == "test"

    def test_system_routine_bypasses_license_gate(self, tmp_store):
        """System routines (_system=True) bypass the cron license gate."""
        reset_license_manager()
        # On COMMUNITY tier
        assert get_license_manager().can_use_cron() is False

        # System routines should not raise
        routine = tmp_store.create(
            "memory-maintenance", "*/10 * * * *", "upkeep",
            _system=True
        )
        assert routine.name == "memory-maintenance"

    def test_cron_gate_with_missing_license(self, tmp_store):
        """If licensing module unavailable, gate handles gracefully."""
        # This is defensive; in practice licensing is always available
        reset_license_manager()
        lm = get_license_manager()
        assert lm.license.tier is Tier.COMMUNITY
        assert lm.can_use_cron() is False

        with pytest.raises(LicenseError):
            tmp_store.create("test", "0 9 * * *", "work")


# ── existing gate verification ───────────────────────────────────────────────

class TestSwarmGateAlreadyImplemented:
    """Verify swarm() already has a fail-closed gate."""

    async def _make_agent_with_license(self, tier: Tier) -> AitherAgent:
        agent = AitherAgent("test-agent", builtin_tools=False)
        agent._license = lic.LicenseManager(
            License(tier=tier, entitlements=Entitlements.for_tier(tier))
        )
        return agent

    @pytest.mark.asyncio
    async def test_community_tier_swarm_returns_error(self):
        """COMMUNITY tier swarm() returns error dict, not raises."""
        agent = await self._make_agent_with_license(Tier.COMMUNITY)
        result = await agent.swarm("build something")
        # Should return error dict, not raise
        assert isinstance(result, dict)
        assert result.get("status") == "failed"
        assert "Professional" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_professional_tier_swarm_attempt(self):
        """PROFESSIONAL tier swarm() does not block on license."""
        agent = await self._make_agent_with_license(Tier.PROFESSIONAL)
        # Will likely fail on connection/genesis not running, but not on license
        result = await agent.swarm("build something")
        assert isinstance(result, dict)
        # Should be a different error (connection), not a license error
        if result.get("status") == "failed":
            error = result.get("error", "")
            assert "Professional" not in error  # no license gate


class TestChannelsGateAlreadyImplemented:
    """Verify channels already have a fail-closed gate."""

    def test_community_tier_channels_raises(self):
        """Creating a channel adapter on COMMUNITY tier raises LicenseError."""
        reset_license_manager()
        lm = get_license_manager()
        assert lm.license.tier is Tier.COMMUNITY
        assert lm.can_use_channels() is False

        from adk.channels import WebhookAdapter
        async def noop(p, c, u, t):
            return "ok"

        # Should raise LicenseError in __init__
        with pytest.raises(LicenseError) as exc:
            WebhookAdapter(token="test", on_message=noop)
        assert "Channel" in str(exc.value) or "channel" in str(exc.value)

    def test_starter_tier_channels_allowed(self):
        """STARTER tier (and above) can use channels."""
        starter_lic = License(
            tier=Tier.STARTER,
            entitlements=Entitlements.for_tier(Tier.STARTER),
        )
        lic.get_license_manager().license = starter_lic
        assert starter_lic.entitlements.channels is True

        from adk.channels import WebhookAdapter
        async def noop(p, c, u, t):
            return "ok"

        # Should not raise
        adapter = WebhookAdapter(token="test", on_message=noop)
        assert adapter.platform == "webhook"
