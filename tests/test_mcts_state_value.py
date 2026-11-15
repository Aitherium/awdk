"""The solver's imagination search accepts a learned state value (Terminal-RSI critic).

Pinned:
* off by default -- ``ImaginedEnv`` without ``value`` scores exactly the goal, as before;
* a real goal hit outranks the critic (it is scaled by ``VALUE_WEIGHT``);
* where the goal gives no signal, the critic guides the leaf value;
* a critic that throws contributes nothing;
* ``clone`` keeps the critic, so simulated rollouts use it too.
"""

from __future__ import annotations

from adk.reasoning.solve import _mcts
from adk.reasoning.solve._mcts import VALUE_WEIGHT, ImaginedEnv


class _Model:
    observed: dict = {}


def _env(state, goal=None, value=None):
    return ImaginedEnv(state, _Model(), [(1, -1, -1)], goal, value)


def test_default_is_unchanged():
    assert _env(3, goal=lambda s: 0.0).evaluate() == 0.0
    assert _env(3, goal=lambda s: 1.0).evaluate() == 1.0
    assert _env(3).evaluate() == 0.0


def test_goal_outranks_critic_and_critic_fills_the_gap():
    assert _env(3, goal=lambda s: 1.0, value=lambda s: 0.9).evaluate() == 1.0
    v = _env(3, goal=lambda s: 0.0, value=lambda s: 0.8).evaluate()
    assert abs(v - VALUE_WEIGHT * 0.8) < 1e-9
    assert _env(7, value=lambda s: s / 10).evaluate() > _env(2, value=lambda s: s / 10).evaluate()


def test_throwing_critic_and_clone():
    def boom(s):
        raise RuntimeError("critic down")

    assert _env(3, goal=lambda s: 0.0, value=boom).evaluate() == 0.0
    c = _env(5, value=lambda s: 1.0).clone()
    assert c.value is not None and c.evaluate() == VALUE_WEIGHT


def test_plan_threads_value_into_mcts(monkeypatch):
    seen = {}
    real = _mcts._mcts_plan

    def spy(*a, **kw):
        seen["value"] = kw.get("value")
        return real(*a, **kw)

    monkeypatch.setattr(_mcts, "_mcts_plan", spy)
    critic = lambda s: 0.3  # noqa: E731
    _mcts.plan(0, [], lambda s: 0.0, [1, 2], method="mcts", value=critic, time_s=0.2,
               max_depth=3)
    assert seen.get("value") is critic
