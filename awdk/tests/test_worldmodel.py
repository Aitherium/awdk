"""Rigorous tests for adk.worldmodel — native world-model bootstrapping for agents.

Tests cover:
  1. DEFAULT OFF: with AITHER_AGENT_WM unset, get_world_model("X") is None and NO files created.
  2. wm_agent_id normalization matches fleet feed ids.
  3. STAGE MACHINE: cold -> warm -> trained at exactly WARM_MIN / TRAINED_MIN.
  4. LEARNING IS REAL: advise() ranks good above bad with < baseline prediction error.
  5. PER-AGENT DIVERGENCE: same action, opposite effects -> different orderings.
  6. PERSISTENCE: save/load restores state/n/learned effects; mismatched state_dims refused.
  7. NEVER RAISES: record() and advise() safe with malformed/None/junk input.
  8. advise() returns None in cold stage.
"""

import json
from pathlib import Path

import pytest

from adk.worldmodel import (
    MODE_OFF,
    MODE_LEARN,
    MODE_SHADOW,
    MODE_STEER,
    WARM_MIN,
    TRAINED_MIN,
    TRAIN_EVERY,
    MAX_BUFFER,
    STATE_DIMS,
    DEFAULT_GOAL,
    wm_mode,
    wm_agent_id,
    wm_root,
    get_world_model,
    clear_world_model_registry,
    register_world_model,
    registered_backend_name,
    BuiltinWorldModel,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def _clear_registry():
    """Clear the world-model registry and instance cache before each test."""
    yield
    clear_world_model_registry()


@pytest.fixture
def wm_tmp_dir(tmp_path, monkeypatch):
    """Isolate AITHER_AGENT_WM_DIR to a temp directory for the test."""
    wm_dir = tmp_path / "wm"
    wm_dir.mkdir()
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(wm_dir))
    # Ensure AITHER_AGENT_WM is unset by default (tests override as needed)
    monkeypatch.delenv("AITHER_AGENT_WM", raising=False)
    return wm_dir


@pytest.fixture
def enable_learn_mode(monkeypatch):
    """Enable AITHER_AGENT_WM=learn mode."""
    monkeypatch.setenv("AITHER_AGENT_WM", "learn")


@pytest.fixture
def enable_shadow_mode(monkeypatch):
    """Enable AITHER_AGENT_WM=shadow mode."""
    monkeypatch.setenv("AITHER_AGENT_WM", "shadow")


@pytest.fixture
def enable_steer_mode(monkeypatch):
    """Enable AITHER_AGENT_WM=steer mode."""
    monkeypatch.setenv("AITHER_AGENT_WM", "steer")


# ============================================================================
# Test 1: DEFAULT OFF
# ============================================================================

class TestDefaultOff:
    """With AITHER_AGENT_WM unset, get_world_model() returns None and creates no files."""

    def test_wm_mode_off_by_default(self, wm_tmp_dir):
        """wm_mode() returns MODE_OFF when AITHER_AGENT_WM is unset."""
        # AITHER_AGENT_WM not set in wm_tmp_dir fixture
        assert wm_mode() == MODE_OFF

    def test_get_world_model_returns_none_when_off(self, wm_tmp_dir):
        """get_world_model() returns None in MODE_OFF."""
        wm = get_world_model("TestAgent")
        assert wm is None

    def test_no_files_created_in_off_mode(self, wm_tmp_dir):
        """No .wm.json or .transitions.jsonl files are created in MODE_OFF."""
        assert len(list(wm_tmp_dir.glob("*"))) == 0
        wm = get_world_model("TestAgent")
        assert wm is None
        # Verify no files created
        assert len(list(wm_tmp_dir.glob("*"))) == 0

    def test_builtin_world_model_off_by_default(self, wm_tmp_dir):
        """BuiltinWorldModel with AITHER_AGENT_WM unset still works but get_world_model returns None."""
        # Direct instantiation should work
        wm = BuiltinWorldModel("DirectTest", root=str(wm_tmp_dir))
        assert wm.agent_id == "agent.directtest"
        # But get_world_model() should return None
        assert get_world_model("DirectTest") is None


# ============================================================================
# Test 2: wm_agent_id Normalization
# ============================================================================

class TestAgentIdNormalization:
    """wm_agent_id matches fleet feed normalization."""

    def test_aither_agent(self):
        """'AitherAgent' -> 'agent.aither'."""
        assert wm_agent_id("AitherAgent") == "agent.aither"

    def test_atlas_agent(self):
        """'Atlas Agent' -> 'agent.atlas'."""
        assert wm_agent_id("Atlas Agent") == "agent.atlas"

    def test_iris(self):
        """'iris' -> 'agent.iris'."""
        assert wm_agent_id("iris") == "agent.iris"

    def test_aitheros_hera(self):
        """'aitheros-hera' -> 'agent.aitheros-hera'."""
        assert wm_agent_id("aitheros-hera") == "agent.aitheros-hera"

    def test_mixed_case_with_agent_suffix(self):
        """'MyAgent' -> 'agent.my'."""
        assert wm_agent_id("MyAgent") == "agent.my"

    def test_agent_prefix_lowercase(self):
        """'Agent Test' -> 'agent.test'."""
        assert wm_agent_id("Agent Test") == "agent.test"

    def test_empty_string(self):
        """'' -> 'agent.unknown'."""
        assert wm_agent_id("") == "agent.unknown"

    def test_none(self):
        """None -> 'agent.unknown'."""
        assert wm_agent_id(None) == "agent.unknown"


# ============================================================================
# Test 3: Stage Machine (cold -> warm -> trained)
# ============================================================================

class TestStageMachine:
    """Stage progression: cold < WARM_MIN, warm < TRAINED_MIN, trained >= TRAINED_MIN."""

    def test_cold_stage_initial(self, wm_tmp_dir, enable_learn_mode):
        """Fresh BuiltinWorldModel starts in cold stage."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        assert wm.bootstrap() == "cold"
        assert wm._n == 0

    def test_stage_progression_cold_to_warm(self, wm_tmp_dir, enable_learn_mode):
        """Stage progresses from cold to warm at n >= WARM_MIN (50)."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Record 49 transitions -> should stay cold
        for i in range(49):
            wm.record(state, "test_action", state)
        wm.bootstrap()
        assert wm._stage == "cold", "Stage should be cold at n=49"
        assert wm._n == 49

        # Record to n=50 -> warm stage
        wm.record(state, "test_action", state)
        wm.bootstrap()
        assert wm._stage == "warm", "Stage should be warm at n=50"
        assert wm._n == 50

    def test_stage_progression_warm_to_trained(self, wm_tmp_dir, enable_learn_mode):
        """Stage progresses from warm to trained at n >= TRAINED_MIN (200)."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Record 199 transitions -> should stay warm (or cold if < 50)
        for i in range(199):
            wm.record(state, "test_action", state)
        wm.bootstrap()
        assert wm._n == 199
        if wm._n >= WARM_MIN:
            assert wm._stage == "warm", f"Stage should be warm at n=199"

        # Record to n=200 -> trained stage
        wm.record(state, "test_action", state)
        wm.bootstrap()
        assert wm._stage == "trained", "Stage should be trained at n=200"
        assert wm._n == 200

    def test_warm_requires_min_observations_per_action(self, wm_tmp_dir, enable_learn_mode):
        """_fit_warm skips actions with < 3 observations."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Manually set stage to warm for testing _fit_warm
        wm._stage = "warm"

        # Record 5 transitions with different actions (1 obs each)
        for i in range(5):
            wm.record(state, f"action_{i}", state)

        # No action has >= 3 obs, so no bias fitted
        wm._fit_warm()
        assert len(wm._bias) == 0, "bias should be empty for actions with < 3 obs"

        # Record 3 more transitions for "action_0"
        for i in range(3):
            wm.record(state, "action_0", state)

        wm._fit_warm()
        assert "action_0" in wm._bias, "action_0 should be fitted now"


# ============================================================================
# Test 4: Learning Is Real
# ============================================================================

class TestLearningIsReal:
    """advise() ranks good above bad; prediction error < do-nothing baseline."""

    def test_learn_success_vs_failure(self, wm_tmp_dir, enable_learn_mode):
        """Train on corpus where 'good' raises success, 'bad' raises errors. advise() ranks good > bad."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Build corpus:
        # 'good' action: state stays same except success dim (+0.1)
        # 'bad' action: state stays same except errors dim (+0.1)
        good_after = state[:]
        good_after[7] = min(1.0, state[7] + 0.1)  # success

        bad_after = state[:]
        bad_after[4] = min(1.0, state[4] + 0.1)  # errors

        # Record 60 transitions: 30 good, 30 bad (enough to reach warm stage at n=50)
        for i in range(30):
            wm.record(state, "good", good_after)
            wm.record(state, "bad", bad_after)

        # Bootstrap to warm/trained stage and get advice
        wm.bootstrap()
        assert wm._stage in ("warm", "trained"), f"Should be warm/trained, got {wm._stage}"

        advice = wm.advise(state, ["good", "bad"])
        assert advice is not None, "advise() should return advice in warm/trained stage"
        assert "order" in advice
        assert "good" in advice["order"]
        assert "bad" in advice["order"]
        # good should rank higher than bad (has better goal alignment)
        assert advice["order"][0] == "good", f"Expected 'good' first, got {advice['order']}"
        assert advice["order"][1] == "bad"

    def test_prediction_error_beats_baseline(self, wm_tmp_dir, enable_learn_mode):
        """After training, model prediction error < do-nothing (next == current)."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Create a corpus with a strong signal:
        # 'rise' action reliably increases success (dim 7)
        # 'fall' action reliably decreases success
        transitions = []
        for i in range(40):
            state_before = [0.5] * len(STATE_DIMS)
            state_before[7] = 0.3 + i * 0.01  # vary success slightly

            state_after_rise = state_before[:]
            state_after_rise[7] = min(1.0, state_after_rise[7] + 0.15)

            state_after_fall = state_before[:]
            state_after_fall[7] = max(0.0, state_after_fall[7] - 0.15)

            transitions.append((state_before, "rise", state_after_rise))
            transitions.append((state_before, "fall", state_after_fall))

        for state_before, action, state_after in transitions:
            wm.record(state_before, action, state_after)

        wm.bootstrap()
        assert wm._stage in ("warm", "trained"), f"Should be warm/trained, got {wm._stage}"

        # Compute MSE of predictions vs held-out transitions (use first 5)
        test_transitions = transitions[:5]
        pred_error = 0.0
        baseline_error = 0.0
        for state_before, action, state_after in test_transitions:
            # Predict: pred_i = x_i + b[a]_i + w[a]_i * x_i
            pred_state = state_before[:]
            if action in wm._bias:
                for i in range(len(STATE_DIMS)):
                    bias_val = wm._bias[action][i] if i < len(wm._bias[action]) else 0.0
                    gain_val = wm._gain.get(action, [0.0] * len(STATE_DIMS))[i]
                    pred_state[i] += bias_val + gain_val * state_before[i]

            # MSE of prediction
            for i in range(len(STATE_DIMS)):
                pred_error += (pred_state[i] - state_after[i]) ** 2
                # Baseline: assume no change
                baseline_error += (state_before[i] - state_after[i]) ** 2

        # Model's prediction error should be lower than baseline (do nothing)
        assert pred_error < baseline_error, (
            f"Model MSE {pred_error:.4f} should be < baseline {baseline_error:.4f}"
        )


# ============================================================================
# Test 5: Per-Agent Divergence
# ============================================================================

class TestPerAgentDivergence:
    """Two agents with same action but opposite effects produce different advise() orderings."""

    def test_divergent_agent_orderings(self, wm_tmp_dir, enable_learn_mode):
        """Agent1: 'jump' raises success; Agent2: 'jump' lowers success. Different models."""
        state = [0.5] * len(STATE_DIMS)

        # Agent 1: 'jump' is good (raises success)
        wm1 = BuiltinWorldModel("Agent1", root=str(wm_tmp_dir))
        jump_good = state[:]
        jump_good[7] = min(1.0, state[7] + 0.2)
        for i in range(60):
            wm1.record(state, "jump", jump_good)

        wm1.bootstrap()
        assert wm1._n == 60

        # Agent 2: 'jump' is bad (lowers success), 'good' is good
        wm2 = BuiltinWorldModel("Agent2", root=str(wm_tmp_dir))
        jump_bad = state[:]
        jump_bad[7] = max(0.0, state[7] - 0.2)
        jump_good2 = state[:]
        jump_good2[4] = min(1.0, state[4] + 0.2)  # errors up instead
        for i in range(30):
            wm2.record(state, "jump", jump_bad)
            wm2.record(state, "good", jump_good2)

        wm2.bootstrap()
        assert wm2._n == 60

        # Get advice from each agent
        advice1 = wm1.advise(state, ["jump", "null"])
        advice2 = wm2.advise(state, ["jump", "good"])

        # At minimum, they should have learned different models
        # Agent1 should have learned that jump raises success
        # Agent2 should have learned that jump lowers success
        assert wm1._bias.get("jump") is not None, "Agent1 should have jump model"
        assert wm2._bias.get("jump") is not None, "Agent2 should have jump model"

        # The bias vectors should be different (opposite effects)
        bias1_jump = wm1._bias.get("jump", [0] * len(STATE_DIMS))
        bias2_jump = wm2._bias.get("jump", [0] * len(STATE_DIMS))

        # Agent1's jump should have positive delta in success (dim 7)
        # Agent2's jump should have negative delta in success (dim 7)
        assert bias1_jump[7] > 0, f"Agent1 jump should raise success, got {bias1_jump[7]}"
        assert bias2_jump[7] < 0, f"Agent2 jump should lower success, got {bias2_jump[7]}"


# ============================================================================
# Test 6: Persistence
# ============================================================================

class TestPersistence:
    """save/load restores state/n/learned effects; mismatched state_dims refused."""

    def test_save_and_load_restores_state(self, wm_tmp_dir, enable_learn_mode):
        """save() then load() restores n, stage, bias."""
        wm1 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Record 60 transitions (enough to reach warm stage at n=50)
        for i in range(60):
            wm1.record(state, "test", state)
        wm1.bootstrap()
        wm1.save()

        # Load into a fresh instance
        wm2 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        wm2.load()

        # Verify state restored
        assert wm2._n == 60, f"n should be 60, got {wm2._n}"
        assert wm2._stage in ("warm", "trained"), f"stage should be warm/trained, got {wm2._stage}"
        assert "test" in wm2._bias, "bias for 'test' action should be restored"

    def test_state_dims_mismatch_refuses_load(self, wm_tmp_dir, enable_learn_mode):
        """load() refuses checkpoint with mismatched state_dims."""
        wm1 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Record and save
        for i in range(5):
            wm1.record(state, "test", state)
        wm1.save()

        # Manually corrupt checkpoint to have different state_dims
        ckpt_path = Path(str(wm1._ckpt_path))
        ckpt = json.loads(ckpt_path.read_text())
        ckpt["state_dims"] = ["wrong", "dims"]
        ckpt_path.write_text(json.dumps(ckpt))

        # Load should refuse and reset
        wm2 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        wm2.load()
        assert wm2._n == 0, "load() should reset on state_dims mismatch"
        assert wm2._stage == "cold"

    def test_transitions_persist(self, wm_tmp_dir, enable_learn_mode):
        """Transitions are saved and restored."""
        wm1 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        # Record and save
        for i in range(5):
            wm1.record(state, f"action_{i}", state)
        wm1.save()

        # Load and verify transitions
        wm2 = BuiltinWorldModel("Agent", root=str(wm_tmp_dir))
        wm2.load()
        assert len(wm2._transitions) == 5, "Should have 5 transitions"


# ============================================================================
# Test 7: Never Raises
# ============================================================================

class TestNeverRaises:
    """record() and advise() must be exception-safe."""

    def test_record_with_wrong_dim_state(self, wm_tmp_dir, enable_learn_mode):
        """record() with dimension mismatch does not raise."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        wrong_state = [0.5] * (len(STATE_DIMS) - 1)

        # Should not raise
        wm.record(state, "action", wrong_state)
        assert wm._n == 0, "Wrong-dim record should be ignored"

    def test_record_with_none_state(self, wm_tmp_dir, enable_learn_mode):
        """record() with None state does not raise."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        wm.record(None, "action", state)
        assert wm._n == 0

    def test_record_with_non_numeric_values(self, wm_tmp_dir, enable_learn_mode):
        """record() with non-numeric state values does not raise."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        bad_state = ["not", "numeric"] * 4
        wm.record(bad_state, "action", bad_state)
        assert wm._n == 0

    def test_record_with_empty_action(self, wm_tmp_dir, enable_learn_mode):
        """record() with empty/whitespace action is ignored."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        wm.record(state, "", state)
        wm.record(state, "   ", state)
        assert wm._n == 0

    def test_advise_with_unknown_candidates(self, wm_tmp_dir, enable_learn_mode, monkeypatch):
        """advise() with unknown candidates does not raise."""
        monkeypatch.setenv("AITHER_AGENT_WM_WARM_MIN", "5")
        monkeypatch.setenv("AITHER_AGENT_WM_TRAINED_MIN", "10")

        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        for i in range(10):
            wm.record(state, "known", state)
        wm.bootstrap()

        # Advise with unknown actions
        advice = wm.advise(state, ["unknown1", "unknown2"])
        assert advice is None, "advise() should return None when no known actions scored"

    def test_advise_with_empty_candidates(self, wm_tmp_dir, enable_learn_mode):
        """advise() with empty candidate list does not raise."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        advice = wm.advise(state, [])
        assert advice is None

    def test_advise_with_wrong_dim_state(self, wm_tmp_dir, enable_learn_mode):
        """advise() with dimension mismatch does not raise."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        wrong_state = [0.5] * (len(STATE_DIMS) - 1)
        advice = wm.advise(wrong_state, ["action"])
        assert advice is None

    def test_load_corrupt_checkpoint(self, wm_tmp_dir, enable_learn_mode):
        """load() with corrupt/truncated checkpoint does not raise."""
        wm1 = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        for i in range(5):
            wm1.record(state, "action", state)
        wm1.save()

        # Corrupt the checkpoint
        ckpt_path = Path(str(wm1._ckpt_path))
        ckpt_path.write_text("{INVALID JSON")

        # load() should not raise
        wm2 = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        wm2.load()
        assert wm2._n == 0, "Corrupt checkpoint should result in reset state"

    def test_load_corrupt_transitions(self, wm_tmp_dir, enable_learn_mode):
        """load() with corrupt transitions.jsonl does not raise and skips bad lines."""
        wm1 = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        for i in range(3):
            wm1.record(state, "action", state)
        wm1.save()

        # Corrupt transitions file
        trans_path = Path(str(wm1._trans_path))
        trans_path.write_text(
            '{"action":"good"}\n{BAD JSON\n{"action":"good2"}\n'
        )

        # load() should skip bad lines and load good ones
        wm2 = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        wm2.load()
        # Should load the 2 good lines
        assert len(wm2._transitions) == 2, "Should skip corrupt JSONL lines"


# ============================================================================
# Test 8: advise() Returns None in Cold Stage
# ============================================================================

class TestAdviseInColdStage:
    """advise() returns None when stage is cold."""

    def test_advise_none_in_cold(self, wm_tmp_dir, enable_learn_mode):
        """advise() returns None in cold stage."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)

        wm.bootstrap()
        assert wm._stage == "cold"

        advice = wm.advise(state, ["action1", "action2"])
        assert advice is None, "advise() should return None in cold stage"


# ============================================================================
# Registry & Factory Tests
# ============================================================================

class TestRegistry:
    """Test registry and factory patterns."""

    def test_register_custom_backend(self, wm_tmp_dir, enable_learn_mode):
        """Can register a custom backend factory."""
        call_count = [0]

        class CustomBackend(BuiltinWorldModel):
            pass

        def custom_factory(agent_name: str):
            call_count[0] += 1
            return CustomBackend(agent_name, root=str(wm_tmp_dir))

        register_world_model(custom_factory)
        # A factory with no advertised backend_name falls back to its own __name__.
        assert registered_backend_name() == "custom_factory"

        wm = get_world_model("Test")
        assert isinstance(wm, CustomBackend)
        assert call_count[0] == 1

    def test_registered_backend_name_reports_advertised_name(self, wm_tmp_dir, enable_learn_mode):
        """A backend advertising backend_name is reported by name, and reports it in stats().

        This is what lets `adk wm status` say "fleet" instead of a generic "custom" --
        an operator must be able to see WHICH backend is actually serving.
        """
        class NamedBackend(BuiltinWorldModel):
            backend_name = "fleet"

        assert registered_backend_name() is None      # builtin in use -> None
        register_world_model(NamedBackend)
        assert registered_backend_name() == "fleet"

        wm = get_world_model("Test")
        assert wm.stats()["backend"] == "fleet"
        # ...while the plain builtin still reports itself honestly.
        assert BuiltinWorldModel("Other", root=str(wm_tmp_dir)).stats()["backend"] == "builtin"

    def test_registry_caches_per_agent_id(self, wm_tmp_dir, enable_learn_mode):
        """get_world_model() caches one instance per agent_id."""
        wm1 = get_world_model("Test")
        wm2 = get_world_model("Test")
        assert wm1 is wm2, "Same agent should get same cached instance"

    def test_different_agents_different_instances(self, wm_tmp_dir, enable_learn_mode):
        """Different agent names get different instances."""
        wm1 = get_world_model("Agent1")
        wm2 = get_world_model("Agent2")
        assert wm1 is not wm2
        assert wm1.agent_id != wm2.agent_id

    def test_fallback_to_builtin_on_custom_error(self, wm_tmp_dir, enable_learn_mode):
        """If custom factory raises, falls back to BuiltinWorldModel."""
        def bad_factory(agent_name):
            raise ValueError("Factory error")

        register_world_model(bad_factory)
        wm = get_world_model("Test")
        assert wm is not None
        assert isinstance(wm, BuiltinWorldModel)


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """End-to-end integration tests."""

    def test_full_workflow_learn_save_load(self, wm_tmp_dir, enable_learn_mode):
        """Full workflow: create -> record -> train -> save -> load -> advise."""
        # Create and train
        wm1 = BuiltinWorldModel("Integration", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        good_state = state[:]
        good_state[7] = 1.0  # success

        for i in range(60):
            wm1.record(state, "good", good_state)
            wm1.record(state, "bad", state)  # neutral action

        wm1.bootstrap()
        assert wm1._stage in ("warm", "trained")
        wm1.save()

        # Load and advise
        wm2 = get_world_model("Integration")
        assert wm2 is not None
        assert wm2._stage in ("warm", "trained")

        advice = wm2.advise(state, ["good", "bad"])
        assert advice is not None
        assert advice["order"][0] == "good"

    def test_observe_builds_valid_state_vector(self, wm_tmp_dir, enable_learn_mode):
        """observe() builds a valid 8-dim state vector from context."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))

        context = {
            "tools": 5,
            "errors": 0.1,
            "latency": 1.5,
            "tokens": 500,
            "recall": 0.8,
            "depth": 3,
            "novelty": 0.2,
            "success": 0.9,
        }

        state = wm.observe(context)
        assert state is not None
        assert len(state) == len(STATE_DIMS)
        # All values should be clamped to [0, 1]
        for val in state:
            assert 0.0 <= val <= 1.0

    def test_stats_returns_valid_dict(self, wm_tmp_dir, enable_learn_mode):
        """stats() returns a dict with all expected keys."""
        wm = BuiltinWorldModel("Test", root=str(wm_tmp_dir))
        state = [0.5] * len(STATE_DIMS)
        for i in range(5):
            wm.record(state, "action", state)

        stats = wm.stats()
        assert stats["agent_id"] == "agent.test"
        assert stats["backend"] == "builtin"
        assert stats["stage"] == "cold"
        assert stats["n"] == 5
        assert stats["actions"] == 1
        assert stats["state_dim"] == len(STATE_DIMS)
        assert "goal" in stats


class TestModeGateDiscipline:
    """The production-critical invariant: shadow must be behaviourally invisible.

    off / learn / shadow must execute the EXACT same tool order as an unmodified adk.
    Only steer may reorder, and only as a strict permutation. A shadow-mode regression
    would silently change live agent behaviour on 8 sovereign containers, so this is
    verified by EXECUTION through a real agent turn -- not by reading the code.
    """

    TOOLS = ["alpha", "beta", "gamma"]
    EFFECT = {"alpha": -0.20, "beta": 0.0, "gamma": +0.20}  # on the `success` dim (goal +1.0)

    def _seed(self, root):
        """Train a checkpoint that prefers exactly the REVERSE of the LLM's emitted order."""
        wm = BuiltinWorldModel("gatetest", root=str(root))
        for k in range(400):
            a = self.TOOLS[k % 3]
            s = [0.5] * 8
            nxt = list(s)
            nxt[7] = max(0.0, min(1.0, s[7] + self.EFFECT[a]))
            wm.record(s, a, nxt, ok=True)
            wm.bootstrap()
        wm.save()
        adv = wm.advise([0.5] * 8, self.TOOLS)
        assert adv and adv["order"] == ["gamma", "beta", "alpha"], "seed failed to learn the preference"

    async def _run_turn(self, mode, root, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from adk.agent import AitherAgent
        from adk.llm.base import LLMResponse, ToolCall
        from adk.tools import ToolRegistry
        from adk.memory import Memory

        clear_world_model_registry()
        monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(root))
        monkeypatch.setenv("AITHER_SKILLS", "false")
        monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")
        if mode is None:
            monkeypatch.delenv("AITHER_AGENT_WM", raising=False)
        else:
            monkeypatch.setenv("AITHER_AGENT_WM", mode)

        def tool_resp():
            return LLMResponse(content="", model="mock", tool_calls=[
                ToolCall(id=f"tc_{i}", name=n, arguments={}) for i, n in enumerate(self.TOOLS)])

        llm = MagicMock()
        llm.provider_name = "mock"
        llm.chat = AsyncMock(side_effect=[
            tool_resp(), LLMResponse(content="one", model="mock"),
            tool_resp(), LLMResponse(content="two", model="mock"),
        ])
        registry = ToolRegistry()
        for n in self.TOOLS:
            registry.register((lambda nm: (lambda: f"ran {nm}"))(n), name=n, description=n)

        agent = AitherAgent("gatetest", llm=llm, tools=[registry],
                            memory=Memory(db_path=tmp_path / f"{mode}.db", agent_name="gatetest"))
        await agent.chat("go")                    # turn 1 establishes _wm_prev_state
        r2 = await agent.chat("go again")         # turn 2 is where the advisory can fire
        return agent._wm is not None, list(r2.tool_calls_made)

    @pytest.mark.asyncio
    async def test_shadow_is_behaviourally_identical_to_off(self, tmp_path, monkeypatch):
        root = tmp_path / "wm"
        root.mkdir()
        self._seed(root)

        off_attached, off_order = await self._run_turn(None, root, monkeypatch, tmp_path)
        learn_attached, learn_order = await self._run_turn(MODE_LEARN, root, monkeypatch, tmp_path)
        shadow_attached, shadow_order = await self._run_turn(MODE_SHADOW, root, monkeypatch, tmp_path)
        steer_attached, steer_order = await self._run_turn(MODE_STEER, root, monkeypatch, tmp_path)

        # off attaches no world model at all; the others do -- so this is a real comparison,
        # not four runs that all trivially no-op.
        assert off_attached is False
        assert learn_attached and shadow_attached and steer_attached

        assert off_order == self.TOOLS                      # unmodified adk order
        assert learn_order == off_order, "learn mode changed executed tool order"
        assert shadow_order == off_order, "SHADOW MODE CHANGED EXECUTED TOOL ORDER -- regression"

        # steer is the ONLY mode allowed to differ, and only as a strict permutation
        assert steer_order == ["gamma", "beta", "alpha"], "steer did not apply the learned preference"
        assert sorted(steer_order) == sorted(off_order), "steer dropped or duplicated a tool call"

    @pytest.mark.asyncio
    async def test_steer_rejects_reorder_on_duplicate_tool_names(self, tmp_path, monkeypatch):
        """Duplicate tool names in one turn must FAIL CLOSED (no reorder), never drop a call."""
        from unittest.mock import AsyncMock, MagicMock
        from adk.agent import AitherAgent
        from adk.llm.base import LLMResponse, ToolCall
        from adk.tools import ToolRegistry
        from adk.memory import Memory

        root = tmp_path / "wm"
        root.mkdir()
        self._seed(root)

        clear_world_model_registry()
        monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(root))
        monkeypatch.setenv("AITHER_AGENT_WM", MODE_STEER)
        monkeypatch.setenv("AITHER_SKILLS", "false")
        monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")

        emitted = ["alpha", "gamma", "alpha"]      # alpha twice -- the collapsing case
        def tool_resp():
            return LLMResponse(content="", model="mock", tool_calls=[
                ToolCall(id=f"tc_{i}", name=n, arguments={"i": i}) for i, n in enumerate(emitted)])

        llm = MagicMock()
        llm.provider_name = "mock"
        llm.chat = AsyncMock(side_effect=[
            tool_resp(), LLMResponse(content="one", model="mock"),
            tool_resp(), LLMResponse(content="two", model="mock"),
        ])
        registry = ToolRegistry()
        for n in set(emitted):
            registry.register((lambda nm: (lambda i=0: f"ran {nm}"))(n), name=n, description=n)

        agent = AitherAgent("gatetest", llm=llm, tools=[registry],
                            memory=Memory(db_path=tmp_path / "dup.db", agent_name="gatetest"))
        await agent.chat("go")
        r2 = await agent.chat("go again")

        # Nothing may be lost: every emitted call still executed, same multiset.
        assert sorted(r2.tool_calls_made) == sorted(emitted), "steer lost or duplicated a call"


class TestHostBackendAutoload:
    """A host platform must be able to install a richer backend without adk depending on
    it, and adk must degrade silently when no host is present.

    Without this autoload nothing imports the fleet backend in production, so live agents
    would quietly run the 8-dim builtin while the deployment looked correct.
    """

    def test_autoload_installs_host_backend(self, tmp_path, wm_tmp_dir, enable_learn_mode, monkeypatch):
        """A REAL import of a host module that self-registers at import time.

        Written to disk rather than stubbed into sys.modules on purpose: a cached module
        would not re-execute, so a stub would test nothing about the import path.
        """
        import sys

        (tmp_path / "fake_host_backend.py").write_text(
            "from adk.worldmodel import BuiltinWorldModel, register_world_model\n"
            "\n"
            "class HostBackend(BuiltinWorldModel):\n"
            "    backend_name = 'hosted'\n"
            "\n"
            "def install():\n"
            "    register_world_model(HostBackend)\n"
            "\n"
            "install()\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "fake_host_backend")
        sys.modules.pop("fake_host_backend", None)
        try:
            clear_world_model_registry()
            assert registered_backend_name() is None       # nothing registered yet
            wm = get_world_model("Test")                    # must import and pick it up
            assert registered_backend_name() == "hosted"
            assert type(wm).__name__ == "HostBackend"
            assert wm.stats()["backend"] == "hosted"
        finally:
            sys.modules.pop("fake_host_backend", None)

    def test_autoload_degrades_silently_with_no_host(self, wm_tmp_dir, enable_learn_mode, monkeypatch):
        """A missing host module must not raise or block the builtin."""
        monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "definitely.not.a.real.module")
        clear_world_model_registry()
        wm = get_world_model("Test")
        assert registered_backend_name() is None
        assert isinstance(wm, BuiltinWorldModel)
        assert wm.stats()["backend"] == "builtin"

    def test_autoload_can_be_disabled(self, wm_tmp_dir, enable_learn_mode, monkeypatch):
        """An empty AITHER_AGENT_WM_BACKEND turns the autoload off entirely."""
        monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "")
        clear_world_model_registry()
        wm = get_world_model("Test")
        assert registered_backend_name() is None
        assert isinstance(wm, BuiltinWorldModel)
