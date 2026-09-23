"""MCTS must optimise the DISCOUNTED RETURN, not an averaged-and-clamped blend.

The rollout value used to be ``clamp01(0.4 * total_reward / steps + 0.6 * eval)``
and in-tree edge rewards were dropped on replay, so a sparse +1 was diluted by
the step count and then saturated at 1.0. A 0 -> 1000 reward sweep did not move
the chosen action.

These tests pin the fixed contract:

* a reward three steps down one branch wins, and the backed-up value SCALES
  with the reward instead of pinning at 1.0;
* a reward on the very first edge counts (it used to be discarded on replay);
* with ``discount < 1`` the nearer of two equal rewards wins;
* :class:`ObservedTransitionModel` can answer "shortest known path to a
  rewarding edge" and "shortest known path to an untried action".
"""

from __future__ import annotations

import asyncio
import random
from typing import Dict, List, Tuple

import pytest
from adk.reasoning.mcts import MCTSConfig, ObservedTransitionModel, UnifiedMCTS

Edge = Tuple[str, float, bool]


class GraphEnv:
    """Deterministic graph env: ``edges[(state, action)] -> (next, reward, done)``.

    Any action without an edge is a self-loop with zero reward. ``evaluate`` is
    flat on purpose, so the ONLY signal is the reward.
    """

    def __init__(self, edges: Dict[Tuple[str, str], Edge], actions: List[str],
                 state: str = "root") -> None:
        self.edges = edges
        self.actions = actions
        self.state = state
        self.done = False

    def get_state_hash(self) -> int:
        return hash(self.state)

    def get_actions(self) -> List[str]:
        return [] if self.done else list(self.actions)

    def step(self, action: str) -> Tuple[str, float, bool]:
        nxt, reward, done = self.edges.get((self.state, action), (self.state, 0.0, False))
        self.state = nxt
        self.done = done
        return nxt, reward, done

    def evaluate(self) -> float:
        return 0.5

    def clone(self) -> "GraphEnv":
        env = GraphEnv(self.edges, self.actions, self.state)
        env.done = self.done
        return env


def _two_branch(reward: float) -> Dict[Tuple[str, str], Edge]:
    """Branch A reaches ``reward`` after 3 steps; branch B never pays."""
    edges: Dict[Tuple[str, str], Edge] = {
        ("root", "A"): ("a1", 0.0, False),
        ("root", "B"): ("b1", 0.0, False),
    }
    for act in ("A", "B"):
        edges[("a1", act)] = ("a2", 0.0, False)
        edges[("a2", act)] = ("a3", reward, True)
        edges[("b1", act)] = ("b2", 0.0, False)
        edges[("b2", act)] = ("b3", 0.0, True)
    return edges


def _search(edges: Dict[Tuple[str, str], Edge], actions: List[str], **cfg_kw):
    random.seed(1234)
    cfg = MCTSConfig(iterations=300, simulation_depth=8, time_limit_ms=60_000.0, **cfg_kw)
    return asyncio.run(UnifiedMCTS(cfg).search(GraphEnv(edges, actions)))


def _root_q(result, action: str) -> float:
    for child in result.root.children:
        if child.action == action:
            return child.avg_value
    raise AssertionError(f"root has no child for {action!r}")


def test_delayed_reward_wins_and_value_scales_with_reward():
    """Reward indifference in miniature: sweep the reward, the value must follow."""
    values = []
    for reward in (1.0, 10.0, 100.0):
        result = _search(_two_branch(reward), ["A", "B"])
        assert result.best_action == "A", (reward, result.best_action)
        assert _root_q(result, "A") > _root_q(result, "B"), reward
        values.append(result.best_value)
    # Averaged + clamped, this read 1.0, 1.0, 1.0. A return scales.
    assert values[0] < values[1] < values[2], values
    assert values[2] > 10.0 * values[0] * 0.5, values


def test_first_edge_reward_is_not_discarded():
    """In-tree edge rewards used to be dropped when the path was replayed."""
    edges: Dict[Tuple[str, str], Edge] = {
        ("root", "A"): ("a1", 5.0, True),
        ("root", "B"): ("b1", 0.0, True),
    }
    result = _search(edges, ["A", "B"])
    assert result.best_action == "A"
    assert _root_q(result, "A") >= 5.0


def test_discount_prefers_the_nearer_of_two_equal_rewards():
    edges: Dict[Tuple[str, str], Edge] = {
        ("root", "A"): ("a1", 0.0, False),
        ("root", "B"): ("b1", 1.0, True),
    }
    for act in ("A", "B"):
        edges[("a1", act)] = ("a2", 0.0, False)
        edges[("a2", act)] = ("a3", 1.0, True)
    result = _search(edges, ["A", "B"], discount=0.9)
    assert result.best_action == "B"
    assert _root_q(result, "B") > _root_q(result, "A")


def test_zero_reward_value_is_the_discounted_bootstrap():
    """No reward anywhere: the root value is just the discounted evaluate()."""
    result = _search(_two_branch(0.0), ["A", "B"])
    assert result.best_value == pytest.approx(0.5 * 0.97 ** 3, abs=0.2)


# -- ObservedTransitionModel: "reach a known state" helpers -----------------


def _chain_model() -> ObservedTransitionModel:
    tm = ObservedTransitionModel()
    tm.record(0, "R", 1, 0.1, False)
    tm.record(1, "R", 2, 0.1, False)
    tm.record(2, "U", 3, 1.0, True)    # observed level-up
    tm.record(0, "L", 0, -0.1, False)  # known no-op
    tm.record(1, "L", 0, 0.1, False)   # a way back
    return tm


def test_path_to_reward_is_the_shortest_known_path():
    tm = _chain_model()
    assert tm.path_to_reward(0) == ["R", "R", "U"]
    assert tm.path_to_reward(2) == ["U"]
    assert tm.path_to_reward(3) is None           # nothing recorded past the win


def test_path_to_reward_threshold_excludes_shaping_rewards():
    tm = _chain_model()
    assert tm.path_to_reward(0, min_reward=0.0) == ["R"]
    assert tm.path_to_reward(0, min_reward=0.5) == ["R", "R", "U"]


def test_path_to_frontier_finds_the_nearest_untried_action():
    tm = _chain_model()
    # State 0 has tried L and R; U is untried at 0 itself.
    assert tm.path_to_frontier(0, ["L", "R", "U"]) == ["U"]
    # With only L/R available: 0 and 1 are exhausted, 2 has not tried L.
    assert tm.path_to_frontier(0, ["L", "R"]) == ["R", "R", "L"]


def test_path_to_frontier_none_when_everything_is_tried():
    tm = ObservedTransitionModel()
    tm.record(0, "A", 0, 0.0, False)
    assert tm.path_to_frontier(0, ["A"]) is None
