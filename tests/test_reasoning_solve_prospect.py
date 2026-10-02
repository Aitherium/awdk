"""The Prospector as a PRISM auto strategy (``adk.reasoning.solve._prospect``).

GridWalk5 through the vendored core: ``LoopConfig(prospect=True)`` registers the
``prospect`` strategy, PRISM can select it, its turns act through ``loop.step`` with
source ``prospect`` and are scored like any other arm; off, nothing changes.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve import LoopConfig  # noqa: E402
from adk.reasoning.solve._run import build_core_loop  # noqa: E402
from adk.reasoning.solve._vendor.prism import BY_ID  # noqa: E402
from adk.reasoning.solve.envs.toy import GridWalk5  # noqa: E402
from adk.reasoning.solve.memory import InMemory  # noqa: E402


def _loop(**cfg):
    env = GridWalk5(levels=2)
    loop = build_core_loop(env, None, LoopConfig(**cfg), memory=InMemory(), episode_id="g")
    loop.obs = env.observe()
    return env, loop


def test_prospect_is_a_prism_arm_that_acts_and_is_scored():
    env, loop = _loop(prospect=True)
    assert "prospect" in loop.prism.allowed and BY_ID["prospect"].kind == "auto"
    loop.prism.active = BY_ID["prospect"]
    loop._auto_turn(BY_ID["prospect"], set(), 0)
    s = loop.summary()
    assert s["actions_prospect"] > 0 and s.get("actions_explore", 0) == 0
    assert s["prospect"]["actions"] == s["actions_prospect"]
    assert loop.prism.game_stats["prospect"]["actions"] == s["actions_prospect"]
    # it explores new cells (measured 7 in 25 actions: it exhausts each cell's untried
    # actions before walking on -- breadth per state, the cost seen on ARC)
    assert len(loop.seen_keys) >= 5


def test_untried_rare_kinds_outrank_common_ones():
    _env, loop = _loop(prospect=True)
    pr = loop.prospector
    pr.kind_freq = {("A", 1): 9, ("A", 2): 0}
    pr.arm_outcomes = {("A", 1): ["0"] * 9}
    pr.outcome_freq = {"0": 9}
    assert pr.priority(("A", 2)) > pr.priority(("A", 1))
    assert pr.novelty(("A", 2)) == 3.0 and pr.novelty(("A", 1)) < 0.5


def test_prospect_can_be_named_in_strategies():
    _env, loop = _loop(prospect=True, strategies=("rule_first", "prospect"))
    assert loop.prism.allowed == ["rule_first", "prospect"]


def test_off_by_default():
    _env, loop = _loop()
    assert "prospect" not in loop.prism.allowed
    assert not hasattr(loop, "prospector") and "prospect" not in loop.summary()


def test_auto_is_restored_after_a_prospect_turn():
    _env, loop = _loop(prospect=True)
    assert "auto" not in vars(loop)
    loop._auto_turn(BY_ID["prospect"], set(), 0)
    assert "auto" not in vars(loop)  # back to the core's own explorer
    marker = lambda n, source="explore": {"actions": 0, "changed": 0}  # noqa: E731
    loop.auto = marker  # another installer's wrapper must survive a prospect turn
    loop._auto_turn(BY_ID["prospect"], set(), 0)
    assert loop.auto is marker


class _GridWalk5Int64(GridWalk5):
    """Same game, but the state keeps a non-int8 dtype (as ``envs/debug.py`` does)."""

    def _state(self):
        return super()._state().astype(np.int64)


def test_non_int8_state_keys_live_and_recorded_states_alike():
    # history stores int8 casts, loop.obs.state keeps the env's dtype; with no
    # state_key hook the two hashed differently and the frontier never shrank
    env = _GridWalk5Int64(levels=2)
    loop = build_core_loop(env, None, LoopConfig(prospect=True), memory=InMemory(), episode_id="g")
    loop.obs = env.observe()
    assert loop.obs.state.dtype == np.int64
    pr = loop.prospector
    for _ in range(3):
        loop._auto_turn(BY_ID["prospect"], set(), 0)
    assert pr.tried and set(pr.tried) <= set(pr.cands)
    assert pr.stats["walked"] > 0
    # no (state, action) is pressed twice while an untried one remains
    for s, tried in pr.tried.items():
        assert tried <= {a for a, _k in pr.cands[s]}
    assert len(pr.cands) >= 10
