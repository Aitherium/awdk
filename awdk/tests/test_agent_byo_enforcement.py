"""Agent-level BYO/self-hosted enforcement tests.

Covers the two gates that live in ``AitherAgent`` (not in the metering layer):

  1. Token-cap inversion — only a metered ``gateway`` backend gets a monthly cap;
     BYO / local / unknown backends have every cap zeroed.
  2. Effort-clamp gating — the community effort ceiling (<=3) is applied ONLY on
     the metered gateway; self-hosted/BYO agents keep full reasoning effort.

These assert the behavior end-to-end with a fake router (no network), complementing
``test_metering_byo_exemption.py`` which covers the metering layer in isolation.
"""

from __future__ import annotations

import asyncio

import pytest

from adk.agent import AitherAgent
from adk.licensing import reset_license_manager
from adk.llm import LLMResponse


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Pristine, community-tier, self-contained environment for each test.

    - Clear all license env so the default resolves to COMMUNITY (the dev box
      sets AITHER_TENANT_SLUG=aitherium → INTERNAL, which would lift every cap).
    - Point data dirs at a tmp path so memory/graph never touch the real home.
    - Disable the heavy optional subsystems so construction stays fast + offline.
    - Reset the license + meter singletons so state never leaks between tests.
    """
    for var in (
        "AITHER_TENANT_SLUG", "AITHER_LICENSE_KEY", "AITHER_LICENSE_FILE",
        "AITHER_LICENSE_ENFORCE", "AITHER_LICENSE_PUBLIC_KEY",
        "AITHER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")
    monkeypatch.setenv("AITHER_SKILLS", "false")

    import adk.metering as _m
    _m._meters.clear()
    reset_license_manager()
    yield
    _m._meters.clear()
    reset_license_manager()


class _FakeRouter:
    """A minimal LLMRouter stand-in: records the effort it was called with."""

    def __init__(self, provider_name: str):
        self._provider_name = provider_name
        self.last_effort = "UNSET"

    @property
    def provider_name(self) -> str:
        return self._provider_name

    async def chat(self, messages, tools=None, effort=None, **kwargs):
        self.last_effort = effort
        return LLMResponse(content="done", model="fake", finish_reason="stop", tool_calls=[])

    # AitherAgent may call these defensively; make them no-ops.
    def switch_backend(self, *a, **k):
        pass

    def set_reasoning_backend(self, *a, **k):
        pass


def _make_agent(provider_name: str) -> tuple[AitherAgent, _FakeRouter]:
    fake = _FakeRouter(provider_name)
    agent = AitherAgent("aither", llm=fake, builtin_tools=False)
    # Null the optional subsystems so chat() exercises only the effort/cap path.
    agent._graph = None
    agent._typed = None
    agent._skills = None
    agent._auto_neurons = None
    agent._safety = None
    agent._context_mgr = None
    agent._events = None
    return agent, fake


# ── __init__-level: cap inversion ──────────────────────────────────────────

def test_byo_agent_uncapped_at_construction():
    """A BYO backend has every token cap zeroed and is flagged non-metered."""
    agent, _ = _make_agent("deepseek")
    assert agent._metered_gateway is False
    assert agent.meter._quota.monthly_limit == 0
    assert agent.meter._quota.daily_limit == 0
    assert agent.meter._quota.hourly_limit == 0


def test_unknown_custom_backend_uncapped_at_construction():
    """An unrecognized/custom OpenAI-compatible backend is treated as self-hosted."""
    agent, _ = _make_agent("openrouter")
    assert agent._metered_gateway is False
    assert agent.meter._quota.monthly_limit == 0
    assert agent.meter._quota.daily_limit == 0


def test_gateway_agent_gets_community_cap_at_construction():
    """The metered gateway DOES carry the community monthly cap (the upgrade pull)."""
    agent, _ = _make_agent("gateway")
    assert agent._metered_gateway is True
    assert agent.meter._quota.monthly_limit == 100_000


# ── chat()-level: effort-clamp gating ──────────────────────────────────────

def test_byo_agent_keeps_full_effort():
    """Self-hosted/BYO agents are NOT effort-clamped — effort 10 reaches the LLM."""
    agent, fake = _make_agent("deepseek")
    asyncio.run(agent.chat("hello there", effort=10))
    assert fake.last_effort == 10


def test_gateway_agent_effort_refused_above_community_ceiling():
    """On the metered gateway, community tier REFUSES effort above its cap.

    agent.chat() enforces fail-closed (raise, don't silently cap) — a silently
    clamped effort looked like the paid feature working. This test previously
    asserted the old clamp behavior and went red when enforcement hardened.
    """
    from adk.licensing import LicenseError
    agent, fake = _make_agent("gateway")
    with pytest.raises(LicenseError):
        asyncio.run(agent.chat("hello there", effort=10))
    assert fake.last_effort == "UNSET"  # the LLM was never reached


def test_gateway_agent_effort_within_ceiling_passes():
    """Effort at/below the community cap still reaches the LLM on the gateway."""
    agent, fake = _make_agent("gateway")
    asyncio.run(agent.chat("hello there", effort=3))
    assert fake.last_effort == 3
