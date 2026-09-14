"""env_enroll contract: enrollment proves learnable environments, refuses
unlearnable ones, and its proof registry is what unlocks
require_sandbox_proven — which was an always-False stub before this.
"""
from __future__ import annotations

import os
import random
import string
from typing import Any, Dict, Sequence, Tuple

os.environ["AITHER_OFFLINE"] = "1"

import pytest

from tests._optional_world_model import requires_world_model_engine




@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Re-assert offline mode AFTER conftest's env isolation strips it.

    conftest._isolate_env now delenv's AITHER_OFFLINE so no test INHERITS offline
    mode from another module (the leak that made TestSwarmCode take swarm_code's
    sovereign A2A path and fail the public-payload gate). These packs genuinely
    need it, and they read it at CALL time — not just at import — so they declare
    it here per-test instead of relying on a module-level side effect that also
    lands on everyone else. Module-level autouse fixtures run after conftest's,
    which is the ordering conftest's own docstring relies on.
    """
    monkeypatch.setenv("AITHER_OFFLINE", "1")

class ChaosAdapter:
    """An unlearnable world: every observation is fresh noise, so surprise can
    never fall — enrollment must NOT prove it."""

    domain = "sandbox"

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random()  # deliberately unseeded chaos

    def observe(self, env_state: Any = None) -> str:
        return "".join(self._rng.choices(string.ascii_letters, k=24))

    def actions(self) -> Sequence[int]:
        return [0, 1, 2, 3]

    def step(self, action: int) -> Tuple[str, float, bool, Dict[str, Any]]:
        return self.observe(), 0.0, False, {}


class BrokenAdapter:
    domain = "sandbox"

    def observe(self, env_state: Any = None) -> str:
        return "x"
    # no actions(), no step()


@pytest.fixture()
def proof_registry(tmp_path, monkeypatch):
    path = tmp_path / "proofs.json"
    monkeypatch.setenv("ADK_SANDBOX_PROOF_PATH", str(path))
    import importlib

    # The in-process world-model engine is a module global shared across the
    # whole pytest process. The chaos test deliberately feeds it noise, which
    # would leak into any later test that asserts a learning trend on the
    # same domain (the order-dependent-green class, quality gate 1k). Reload
    # tools around every enroll test so each starts — and leaves behind — a
    # FRESH engine.
    import adk.packs.world_model.env_enroll as ee
    import adk.packs.world_model.tools as wm_tools
    importlib.reload(wm_tools)
    importlib.reload(ee)
    yield ee, path
    importlib.reload(wm_tools)


@requires_world_model_engine
def test_enroll_proves_learnable_world(proof_registry):
    ee, path = proof_registry
    out = ee.env_enroll(
        "adk.packs.world_model.safe_explore:CursorWorldAdapter",
        adapter_kwargs={"grid_size": 12},
        episodes=10, budget=30)
    assert out["ok"], out
    assert out["transitions"] > ee.MIN_TRANSITIONS
    assert out["proven"] is True, (
        f"deterministic cursor world must prove: trailing mean "
        f"{out['trailing_mean_surprise']} > {ee.TRAILING_MEAN_MAX} means the "
        f"loop is not learning — the exact failure enrollment exists to catch")
    assert out["proof_persisted"] and path.exists()
    assert ee.is_sandbox_proven(out["name"]) is True


def test_enroll_refuses_chaos(proof_registry):
    ee, path = proof_registry
    out = ee.env_enroll(
        "tests.test_env_enroll:ChaosAdapter",
        episodes=6, budget=30)
    assert out["ok"], out
    assert out["proven"] is False, (
        "pure-noise world must NOT prove — if it does, the proof means nothing")
    assert ee.is_sandbox_proven(out["name"]) is False


def test_enroll_rejects_contract_violation(proof_registry):
    ee, path = proof_registry
    out = ee.env_enroll("tests.test_env_enroll:BrokenAdapter", episodes=2)
    assert out["ok"] is False and out.get("degraded") is True
    assert "missing" in out["error"]
    assert not path.exists(), "a degraded enrollment must write no proof"


@requires_world_model_engine
def test_readiness_gate_reads_registry(proof_registry):
    ee, path = proof_registry
    from adk.tool_readiness import check_tool_readiness_adk

    report = check_tool_readiness_adk("sandbox", require_sandbox_proven=True)
    assert report.broken is True, "unproven env must stay gated"

    out = ee.env_enroll(
        "adk.packs.world_model.safe_explore:CursorWorldAdapter",
        adapter_kwargs={"grid_size": 12}, episodes=10, budget=30)
    assert out["proven"]

    report = check_tool_readiness_adk("sandbox", require_sandbox_proven=True)
    assert report.broken is False, (
        "a real enrollment proof must unlock the gate — the pre-2026-08-01 "
        "stub returned False forever, which made the gate uncloseable")


def test_pack_registers_env_enroll():
    from adk.packs import world_model

    class MockRegistry:
        def __init__(self):
            self.tools = []

        def register(self, fn):
            self.tools.append(fn)

    reg = MockRegistry()
    count = world_model.register(reg)
    names = {getattr(f, "__name__", "") for f in reg.tools}
    assert count == 4 and "env_enroll" in names, (count, names)
