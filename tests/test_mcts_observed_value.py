"""The MCTS value_model seam must have a learned scorer that something wires in.

Before: ``MCTSConfig.value_model`` defaulted None and no caller in awdk passed
one, so the seam that replaced ``lib.cognitive.MCTSValueModel`` was dead.

Pinned here:

* :class:`ObservedValueModel` learns state values from a finished search tree
  and inverts the engine's ``Q = reward + discount * V`` backup correctly;
* an unseen state returns ``None`` so the engine falls back to its rollout;
* the engine actually CONSULTS the learned model past the depth gate;
* :class:`MctsPlanLoop` trains one per tool set and feeds it to the next search.
"""

from __future__ import annotations

import asyncio
import random
from typing import Dict, List, Tuple

from adk.reasoning.mcts import MCTSConfig, ObservedValueModel, UnifiedMCTS
from adk.reasoning.mcts.loop import MctsPlanLoop

Edge = Tuple[str, float, bool]


class ChainEnv:
    """root -A-> s1 -A-> s2 -A-> s3 (reward 1, done); B is a zero self-loop."""

    EDGES: Dict[Tuple[str, str], Edge] = {
        ("root", "A"): ("s1", 0.0, False),
        ("s1", "A"): ("s2", 0.0, False),
        ("s2", "A"): ("s3", 1.0, True),
    }

    def __init__(self, state: str = "root") -> None:
        self.state = state
        self.done = False

    def get_state_hash(self) -> int:
        return hash(self.state)

    def get_actions(self) -> List[str]:
        return [] if self.done else ["A", "B"]

    def step(self, action: str) -> Edge:
        nxt, reward, done = self.EDGES.get((self.state, action), (self.state, 0.0, False))
        self.state, self.done = nxt, done
        return nxt, reward, done

    def evaluate(self) -> float:
        return 0.0

    def clone(self) -> "ChainEnv":
        env = ChainEnv(self.state)
        env.done = self.done
        return env


def _search(**cfg_kw):
    random.seed(7)
    cfg = MCTSConfig(iterations=200, simulation_depth=6, time_limit_ms=60_000.0, **cfg_kw)
    return cfg, asyncio.run(UnifiedMCTS(cfg).search(ChainEnv()))


def _expected(result, state: str, gamma: float) -> float:
    """Visit-weighted V over EVERY node in ``state`` (B self-loops revisit states)."""
    num = den = 0.0
    stack = [result.root]
    while stack:
        n = stack.pop()
        stack.extend(n.children)
        if n.visits <= 0 or n.state_hash != hash(state):
            continue
        q = n.value_sum / n.visits
        v = q if n.parent is None else (q - n.reward) / gamma
        num += v * n.visits
        den += n.visits
    return num / den


def test_learns_root_value_from_the_search_tree():
    cfg, result = _search()
    vm = ObservedValueModel(min_visits=1)
    assert vm.learn_from_result(result, discount=cfg.discount) > 1
    assert abs(vm.estimate(hash("root")) - _expected(result, "root", cfg.discount)) < 1e-9
    # The only reward is on the s2 -A-> s3 edge: s2 is worth more than root.
    assert vm.estimate(hash("s2")) > vm.estimate(hash("root"))


def test_inverts_the_discounted_backup_for_child_states():
    cfg, result = _search(discount=0.5)
    vm = ObservedValueModel(min_visits=1)
    vm.learn_from_result(result, discount=cfg.discount)
    # Q(child) = reward + 0.5 * V(state)  =>  V(state) = (Q - reward) / 0.5
    for state in ("s1", "s2"):
        assert abs(vm.estimate(hash(state)) - _expected(result, state, 0.5)) < 1e-9


def test_unseen_state_has_no_opinion():
    vm = ObservedValueModel()
    assert vm.value(ChainEnv("never-seen")) is None
    vm.record(hash("s2"), 0.8)
    assert vm.value(ChainEnv("s2")) is None  # below min_visits=2
    vm.record(hash("s2"), 0.6)
    assert abs(vm.value(ChainEnv("s2")) - 0.7) < 1e-9


def test_engine_consults_the_learned_model_past_the_depth_gate():
    calls: List[str] = []

    class Spy(ObservedValueModel):
        def value(self, state):  # noqa: ANN001
            calls.append(state.state)
            return super().value(state)

    vm = Spy(min_visits=1)
    _search(value_model=vm, value_model_depth_threshold=0, max_llm_calls_per_search=0)
    assert calls, "value_model seam was never consulted"


def test_save_load_round_trip(tmp_path):
    vm = ObservedValueModel(min_visits=1)
    vm.record(hash("s1"), 0.25, weight=4)
    path = tmp_path / "values.jsonl"
    vm.save(path)
    other = ObservedValueModel(min_visits=1)
    assert other.load(path) == 1
    assert abs(other.estimate(hash("s1")) - 0.25) < 1e-9


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name

    async def __call__(self) -> str:
        return self.name


class _Agent:
    tools = [_Tool("search"), _Tool("read"), _Tool("write")]


def test_plan_loop_wires_a_learned_value_model():
    loop = MctsPlanLoop(config=MCTSConfig(iterations=40, time_limit_ms=60_000.0), execute=False)
    assert loop.learn_values
    asyncio.run(loop.run(_Agent(), "do the thing"))
    vm = loop.value_model_for(["search", "read", "write"])
    assert len(vm) > 0, "the first search did not train the value model"
    assert loop.config.value_model is None  # caller's config is not mutated


def test_plan_loop_respects_a_caller_value_model():
    mine = ObservedValueModel()
    loop = MctsPlanLoop(config=MCTSConfig(iterations=10, value_model=mine), execute=False)
    assert not loop.learn_values
