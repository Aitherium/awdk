"""Design slice 3: plan() inside the verified model, disagree(), and daydreaming.

* ``plan`` finds the OPTIMAL path from verified rules alone (zero env actions),
  on a walled maze where greedy fails, and through the loop's tool on GridWalk5;
  past the state cap it falls back to UnifiedMCTS over the imagined env.
* ``disagree`` picks the action that discriminates between surviving rules;
  ``find_disagreement`` walks the imagined graph to the nearest split.
* ``daydream`` never calls ``env.act`` (act raises during it), fills ``DREAM``
  and the notes, and an end-to-end ``solve`` wins by executing the dreamt plan.

``PLAN_SELFTEST_BREAK=1`` blinds the model: the plan/disagree tests must fail.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.core.model import ModelResponse  # noqa: E402
from adk.reasoning.solve import LoopConfig, solve  # noqa: E402
from adk.reasoning.solve._daydream import PlanningLoop  # noqa: E402
from adk.reasoning.solve._mcts import disagree, find_disagreement, plan  # noqa: E402
from adk.reasoning.solve._run import build_core_loop  # noqa: E402
from adk.reasoning.solve.envs.toy import GridWalk5  # noqa: E402
from adk.reasoning.solve.memory import InMemory  # noqa: E402

MOVES = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
ACTIONS = [(a, -1, -1) for a in (1, 2, 3, 4)]


def _dot(state):
    r, c = np.argwhere(state == 1)[0]
    return int(r), int(c)


def _board(r, c):
    s = np.zeros((5, 5), dtype=np.int8)
    s[r, c] = 1
    return s


def grid_rule(state, action):
    """The true GridWalk5 dynamics (walls at the border)."""
    r, c = _dot(state)
    dr, dc = MOVES.get(action[0], (0, 0))
    return _board(min(4, max(0, r + dr)), min(4, max(0, c + dc)))


def mirror_rule(state, action):
    """Agrees with grid_rule on actions 1-3; action 4 moves LEFT."""
    if action[0] == 4:
        return grid_rule(state, (3, -1, -1))
    return grid_rule(state, action)


def far_rule(state, action):
    """Agrees with grid_rule except action 4 from column >= 2 (it stays)."""
    if action[0] == 4 and _dot(state)[1] >= 2:
        return state.copy()
    return grid_rule(state, action)


def at_corner(state):
    return bool(state[4, 4] == 1)


class CountingGrid(GridWalk5):
    def __init__(self, levels: int = 2) -> None:
        super().__init__(levels=levels)
        self.calls = 0

    def act(self, action, source="model"):
        self.calls += 1
        return super().act(action, source=source)


# ------------------------------------------------------------------ pure planner
WALLS = {(0, 1), (1, 1), (2, 1), (3, 1), (1, 3), (2, 3), (3, 3), (4, 3)}


def maze_rule(state, action):
    r, c = _dot(state)
    dr, dc = MOVES[action[0]]
    nr, nc = r + dr, c + dc
    if not (0 <= nr < 5 and 0 <= nc < 5) or (nr, nc) in WALLS:
        return state.copy()
    return _board(nr, nc)


def _bfs_len(start, goal):
    q, seen = deque([(start, 0)]), {start}
    while q:
        p, d = q.popleft()
        if p == goal:
            return d
        for dr, dc in MOVES.values():
            n = (p[0] + dr, p[1] + dc)
            if 0 <= n[0] < 5 and 0 <= n[1] < 5 and n not in WALLS and n not in seen:
                seen.add(n)
                q.append((n, d + 1))
    return None


def test_plan_finds_the_optimal_maze_path_from_the_rules_alone():
    calls: List[Any] = []

    def rule(s, a):
        calls.append(a)
        return maze_rule(s, a)

    p = plan(_board(0, 0), [("maze_rule", rule)], at_corner, ACTIONS)
    assert p["found"] and p["method"] == "exact" and p["env_actions"] == 0
    assert (
        p["steps"] == _bfs_len((0, 0), (4, 4)) == 16
    )  # the serpentine, not the 8-step greedy line
    s = _board(0, 0)
    for a in p["path"]:
        s = maze_rule(s, a)
    assert at_corner(s) and calls  # the path is real, and it came from the rule


def test_plan_reports_why_when_the_rules_do_not_reach_the_goal():
    def half(s, a):  # knows only "down"
        return grid_rule(s, a) if a[0] == 2 else None

    p = plan(_board(0, 0), [("half", half)], at_corner, ACTIONS)
    assert not p["found"] and p["unknown_edges"] > 0 and "abstain" in p["reason"]
    assert plan(_board(0, 0), [("g", grid_rule)], None, ACTIONS)["reason"].startswith("no goal")


def test_plan_falls_back_to_unified_mcts_past_the_state_cap():
    random.seed(0)
    p = plan(_board(2, 3), [("g", grid_rule)], at_corner, ACTIONS, max_states=2, time_s=5.0)
    assert p["method"] == "mcts" and p["found"] and p["env_actions"] == 0
    s = _board(2, 3)
    for a in p["path"]:
        s = grid_rule(s, a)
    assert at_corner(s) and p["steps"] <= 6


# ------------------------------------------------------------------ disagreement
def test_disagree_picks_the_discriminating_action():
    d = disagree(_board(0, 0), [("grid_rule", grid_rule), ("mirror_rule", mirror_rule)], ACTIONS)
    assert d["best"] == (4, -1, -1)
    top = d["ranked"][0]
    assert top["entropy"] == 1.0 and top["outcomes"] == 2
    assert all(r["entropy"] == 0 for r in d["ranked"][1:])

    def stay(s, a):
        return s.copy()

    # mid-board, three rules: action 4 splits them three ways, the others two ways
    d3 = disagree(_board(2, 2), [("g", grid_rule), ("m", mirror_rule), ("s", stay)], ACTIONS)
    assert d3["best"] == (4, -1, -1) and d3["ranked"][0]["outcomes"] == 3


def test_find_disagreement_walks_to_the_nearest_split():
    hyps = [("grid_rule", grid_rule), ("far_rule", far_rule)]
    assert disagree(_board(0, 0), hyps, ACTIONS)["best"] is None  # they agree here
    f = find_disagreement(_board(0, 0), [("grid_rule", grid_rule)], hyps, ACTIONS)
    assert f is not None and f["action"] == (4, -1, -1)
    assert f["depth"] == 2 and f["path"] == [(4, -1, -1), (4, -1, -1)]


# ------------------------------------------------------------------ inside the loop
def _loop(env, moves=(2, 4, 1, 3), **cfg):
    loop = build_core_loop(env, None, LoopConfig(**cfg), memory=InMemory(), episode_id="plan")
    loop.obs = env.observe()
    for a in moves:
        loop.step((a, -1, -1), "explore")
    return loop


def test_loop_plan_tool_finds_the_optimal_path_with_zero_env_actions():
    env = CountingGrid(levels=2)
    loop = _loop(env)
    assert isinstance(loop, PlanningLoop)
    ns = loop.sandbox.ns
    assert ns["hypothesize"](grid_rule, "predict")["status"] == "verified"
    assert ns["hypothesize"](at_corner, "goal")["status"] == "consistent"
    n = env.calls
    p = ns["plan"]()
    assert env.calls == n == 4
    assert p["found"] and p["steps"] == 8 and p["predictors"] == ["grid_rule"]
    for a in p["path"]:
        t = loop.step(a, "model")
    assert t.level_up and env.level == 1 and env.calls == 12


def test_loop_disagree_tool_picks_the_discriminating_action():
    loop = _loop(CountingGrid(), moves=(2, 1, 2, 1))  # only up/down seen: both rules verified
    ns = loop.sandbox.ns
    assert ns["hypothesize"](grid_rule, "predict")["status"] == "verified"
    assert ns["hypothesize"](mirror_rule, "predict")["status"] == "verified"
    assert ns["disagree"]()["best"] == (4, -1, -1)


def test_daydream_never_calls_env_act(monkeypatch):
    env = CountingGrid()
    loop = _loop(env, moves=(2, 1, 2, 1, 3))
    ns = loop.sandbox.ns
    ns["hypothesize"](grid_rule, "predict")
    ns["hypothesize"](mirror_rule, "predict")
    ns["hypothesize"](at_corner, "goal")

    def forbidden(*_a, **_k):
        raise AssertionError("daydream called env.act")

    monkeypatch.setattr(env, "act", forbidden)
    d = loop.daydream("exploring")
    assert d is not None and d["env_actions"] == 0 and not d.get("timeout")
    assert d["plan"]["found"] and d["plan"]["steps"] == 8
    assert d["experiment"]["action"] == (4, -1, -1)
    assert ns["DREAM"] is d and any(n.startswith("DAYDREAM") for n in loop.notes)
    assert loop.daydream("exploring") is d  # same inputs: nothing recomputed
    assert loop.stats["daydreams"] == 1


# ------------------------------------------------------------------ end to end
TURN1 = """SITUATION: a dot on a 5x5 board.
ANALYSIS: probe each action once.
SYNTHESIS: a move rule and a corner goal.
EXECUTION:
```python
for a in (2, 4, 1, 3):
    act(a)
MOVES = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
def move(state, action):
    r, c = [int(v) for v in np.argwhere(state == 1)[0]]
    dr, dc = MOVES.get(action[0], (0, 0))
    out = np.zeros_like(state)
    out[min(4, max(0, r + dr)), min(4, max(0, c + dc))] = 1
    return out
def corner(state):
    return bool(state[4, 4] == 1)
print(hypothesize(move, "predict"), hypothesize(corner, "goal"))
```"""

TURN2 = """SITUATION: rules verified.
ANALYSIS: the daydream planned inside them.
SYNTHESIS: follow the plan.
EXECUTION:
```python
for a in DREAM["plan"]["path"]:
    act(*a)
```"""


class ScriptedModel:
    def __init__(self, replies: List[str]) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.calls: List[Any] = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls.append(list(messages))
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return ModelResponse(
            text=text,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 20},
        )

    async def stream(self, messages, **kw):  # pragma: no cover - unused
        raise NotImplementedError


def test_solve_wins_by_executing_the_daydreamt_plan():
    env = CountingGrid(levels=1)
    model = ScriptedModel([TURN1, TURN2])
    res = asyncio.run(solve(env, model, config=LoopConfig(prism=False)))
    assert res.won and res.finish_reason == "won", res
    assert res.actions == 12 and res.llm_calls == 2  # 4 probes + the 8-step optimal plan
    assert res.stats["daydreams"] == 1 and res.stats["dream_plans"] == 1
    dream = res.stats["dream_last"]
    assert dream["env_actions"] == 0 and dream["plan"]["steps"] == 8
    assert (
        "DAYDREAM" in model.calls[1][-1].content and "plan(goal=None" in model.calls[0][0].content
    )


def test_planning_off_is_the_plain_core():
    loop = build_core_loop(GridWalk5(), None, LoopConfig(planning=False), memory=InMemory())
    assert type(loop).__name__ == "ReasoningLoop" and "plan" not in loop.sandbox.ns
    plain = build_core_loop(GridWalk5(), None, LoopConfig(sase=False), memory=InMemory())
    assert "plan" not in plain.sandbox.ns and "PLANNING API" not in plain.system
