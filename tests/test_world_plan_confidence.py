"""A plan on one observation is executable but must say it is a guess.

Review finding (2026-09-27): one 'up' from (0,0) generalized to every cell and the
planner returned OK, indistinguishable from a plan on RECALLED dynamics. OK means
executable; ``Plan.speculative`` is how a caller tells a guess from knowledge.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adk.world import SPECULATIVE_BELOW, WorldModelAgent  # noqa: E402
from tests._world_envs import SCOPE, KeyDoorGrid, open_store  # noqa: E402


def _agent(tmp_path):
    st = open_store(tmp_path / "g.db")
    env = KeyDoorGrid()
    return WorldModelAgent(st, SCOPE, env, horizon=6), env


def test_one_observation_plan_is_speculative(tmp_path):
    ag, env = _agent(tmp_path)
    ag.act("up")
    env.x, env.y = 0, 3
    ag.sense()
    plan = ag.plan({"pos.y": "1"})
    assert plan.verdict == "OK" and plan.actions
    assert plan.confidence < SPECULATIVE_BELOW
    assert plan.speculative is True
    assert plan.to_dict()["speculative"] is True


def test_recalled_plan_is_not_speculative(tmp_path):
    ag, env = _agent(tmp_path)
    for _ in range(5):
        env.x, env.y = 0, 0
        ag.sense()
        ag.act("up")
    env.x, env.y = 0, 0
    ag.sense()
    plan = ag.plan({"pos.y": "1"})
    assert plan.verdict == "OK" and plan.actions == ("up",)
    assert plan.confidence >= SPECULATIVE_BELOW
    assert plan.speculative is False
