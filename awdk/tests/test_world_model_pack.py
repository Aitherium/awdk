"""Integration test for world_model pack with in-process engine.

Tests the wm_* tools and learn-safely explore loop using AITHER_OFFLINE=1
(in-process MLPWorldModel). Does NOT make network calls.

The cursor world is deterministic: same action always produces same result.
The tabular learning engine MUST drive surprise down after enough observations.
If it doesn't, the loop is broken => test fails.
"""

from __future__ import annotations

import os

from tests._optional_world_model import requires_world_model_engine

# Force offline mode before importing tools
os.environ["AITHER_OFFLINE"] = "1"

import pytest  # noqa: E402




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

def test_cursor_world_adapter() -> None:
    """Test CursorWorldAdapter conforms to EnvironmentAdapter contract."""
    from adk.packs.world_model.safe_explore import CursorWorldAdapter

    adapter = CursorWorldAdapter(grid_size=12)

    # Check domain
    assert adapter.domain == "sandbox"

    # Check observe returns a string
    obs = adapter.observe()
    assert isinstance(obs, str)

    # Check actions returns a sequence
    actions = adapter.actions()
    assert len(actions) == 7
    assert all(isinstance(a, int) for a in actions)

    # Check step works
    obs1, reward, done, info = adapter.step(0)
    assert isinstance(obs1, str)
    assert isinstance(reward, float)
    assert isinstance(done, bool)
    assert isinstance(info, dict)
    assert not done  # Cursor world never terminates


def test_cursor_world_deterministic() -> None:
    """Test that CursorWorldAdapter is deterministic."""
    from adk.packs.world_model.safe_explore import CursorWorldAdapter

    # Create two instances with same grid size
    adapter1 = CursorWorldAdapter(grid_size=12)
    adapter2 = CursorWorldAdapter(grid_size=12)

    # Same initial states
    obs1_init = adapter1.observe()
    obs2_init = adapter2.observe()
    # Both start at center but might be the same string
    assert isinstance(obs1_init, str)
    assert isinstance(obs2_init, str)

    # Apply same action sequence
    for action in [0, 1, 2, 3, 0, 0]:
        obs1, _, _, _ = adapter1.step(action)
        obs2, _, _, _ = adapter2.step(action)
        assert obs1 == obs2, "Determinism broken"


@requires_world_model_engine
def test_wm_observe_offline() -> None:
    """Test wm_observe in offline mode (in-process engine)."""
    from adk.packs.world_model import tools as wm_tools

    result = wm_tools.wm_observe(
        obs="state_0",
        action="move_right",
        next_obs="state_1",
        reward=1.0,
        done=False,
        domain="sandbox",
    )

    assert result["ok"] is True
    assert "transitions_buffered" in result
    assert result["transitions_buffered"] >= 1


@requires_world_model_engine
def test_wm_surprise_offline() -> None:
    """Test wm_surprise in offline mode (in-process engine)."""
    from adk.packs.world_model import tools as wm_tools

    # First observe some transitions so surprise can score them
    wm_tools.wm_observe(
        obs="state_0",
        action="move_right",
        next_obs="state_1",
        domain="sandbox",
    )
    wm_tools.wm_observe(
        obs="state_1",
        action="move_right",
        next_obs="state_2",
        domain="sandbox",
    )

    # Score them
    result = wm_tools.wm_surprise(
        items=[
            {"id": "trans_0", "obs": "state_0", "action": "move_right",
             "next_obs": "state_1"},
            {"id": "trans_unseen", "obs": "unknown_state", "action": "unknown",
             "next_obs": "unknown_next"},
        ],
        domain="sandbox",
    )

    assert result["ok"] is True
    surprises = result.get("surprises", {})
    assert "trans_0" in surprises
    # Seen transition: 0.0 (exact match) or 1.0 (wrong); never None
    assert surprises["trans_0"] is not None
    # Unseen: None
    assert surprises.get("trans_unseen") is None


@requires_world_model_engine
def test_wm_status_offline() -> None:
    """Test wm_status in offline mode (in-process engine)."""
    from adk.packs.world_model import tools as wm_tools

    result = wm_tools.wm_status()

    assert result["ok"] is True
    assert "mode" in result
    assert "transition_count" in result
    # Mode should be tabular initially (or hybrid/neural after many transitions)
    assert result["mode"] in ("tabular", "hybrid", "neural")


@requires_world_model_engine
def test_explore_with_cursor_world() -> None:
    """Test explore() loop with CursorWorldAdapter.

    Runs 10 episodes, each with 30-step budget.
    Checks that:
      - transitions are recorded (> 100 total across episodes)
      - budget is respected (no episode runs >30 steps)
      - surprise trend shows learning (mean over last 10 < mean over first 10)
    """
    from adk.packs.world_model import tools as wm_tools
    from adk.packs.world_model.safe_explore import CursorWorldAdapter, explore

    total_steps = 0
    total_transitions = 0
    all_mean_surprises = []

    for episode in range(10):
        adapter = CursorWorldAdapter(grid_size=12)
        result = explore(
            adapter=adapter,
            budget=30,
            epsilon=0.3,
            wm_observe_fn=wm_tools.wm_observe,
        )

        assert result["budget_exhausted"] or result["steps"] <= 30
        assert result["transitions_recorded"] >= 0
        assert result.get("degraded", False) is False, (
            "explore() returned degraded=True (adapter validation failed)"
        )

        total_steps += result["steps"]
        total_transitions += result["transitions_recorded"]

        # Log surprise means if available
        if result["mean_surprise_start"] is not None:
            all_mean_surprises.append(result["mean_surprise_start"])
        if result["mean_surprise_end"] is not None:
            all_mean_surprises.append(result["mean_surprise_end"])

    # Assertion: enough transitions recorded
    assert total_steps > 0, "No steps taken"
    assert total_transitions > 100, (
        f"Expected >100 transitions, got {total_transitions} over "
        f"{total_steps} steps"
    )

    # Assertion: budget respected
    assert total_steps <= 10 * 30, "Budget exceeded"

    # Assertion: surprise trend. NO silent skip — 10 episodes x 30 steps over a
    # shared engine MUST produce surprise data; its absence means the loop is
    # not wiring transitions into the engine, which is exactly the failure this
    # test exists to catch (the slice-2 probe passed vacuously on all-None and
    # shipped a broken adapter; never again).
    assert len(all_mean_surprises) >= 2, (
        f"explore() produced only {len(all_mean_surprises)} surprise mean(s) over "
        f"10 episodes — the loop is not feeding the engine, or surprise is "
        f"always None; that is a broken loop, not missing data"
    )
    mean_init = sum(all_mean_surprises[:len(all_mean_surprises) // 2]) / max(
        1, len(all_mean_surprises) // 2
    )
    mean_final = sum(all_mean_surprises[len(all_mean_surprises) // 2:]) / max(
        1, len(all_mean_surprises) - len(all_mean_surprises) // 2
    )
    # Deterministic world + shared engine across episodes: surprise must
    # genuinely DECREASE, unless it was already ~perfect from the start.
    assert mean_final < mean_init or mean_final <= 0.05, (
        f"No learning: init={mean_init:.4f}, final={mean_final:.4f} — the cursor "
        f"world is deterministic; revisited transitions must stop surprising"
    )


def test_pack_registration() -> None:
    """Test that the world-model pack can be registered.

    This is a smoke test: just check that the pack's register() function
    can be called and returns a count.
    """
    from adk.packs import world_model

    class MockRegistry:
        def __init__(self):
            self.tools = []

        def register(self, fn):
            self.tools.append(fn)

    registry = MockRegistry()
    count = world_model.register(registry)

    assert count >= 0, f"register() should return count >= 0, got {count}"
    # 4 tools: wm_observe, wm_surprise, wm_status, env_enroll
    tool_names = [
        fn.__name__ if hasattr(fn, "__name__") else str(fn)
        for fn in registry.tools
    ]
    assert count == 4, (
        f"Expected 4 tools registered, got {count}. "
        f"Registered: {tool_names}"
    )


def test_tool_readiness_sandbox_proven_param() -> None:
    """Test that check_tool_readiness_adk accepts require_sandbox_proven param."""
    from adk.tool_readiness import check_tool_readiness_adk

    # Should not raise on the new param
    report = check_tool_readiness_adk(
        "some_tool",
        require_sandbox_proven=False,
    )
    assert isinstance(report.broken, bool)

    # With require_sandbox_proven=True, should report broken (no proof yet)
    report = check_tool_readiness_adk(
        "some_tool",
        require_sandbox_proven=True,
    )
    assert report.broken is True
    assert "sandbox-proven" in report.reason.lower()
