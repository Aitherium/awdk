"""Round-1 review regressions for adk.world / adk.world_adapters (each failed before its fix).

p1  one observation read as a GENERALIZED model at confidence 1.0; the planner paid no
    uncertainty for it.
p8  RECALLED returned the MOST RECENT outcome: one anomaly after five consistent
    observations became the prediction and the goal read UNREACHABLE.
p5  FactoredLearner (one parent per slot) was confidently wrong on conjunctive
    preconditions: 44/495 held-out answers wrong at confidence >= 0.99 after a
    1000-step walk; the agent planned "close" from (0,0) at cost 1.032.
"""
from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest

from tests._world_envs import ACTIONS, SCOPE, KeyDoorGrid, free_cells, open_store

from adk.world import GENERALIZED, OK, RECALLED, WorldModelAgent
from adk.world_adapters import FactoredLearner


@pytest.fixture()
def store(tmp_path: Path):
    st = open_store(tmp_path / "m.db")
    yield st
    st.close()


def test_p1_one_observation_is_not_a_confident_generalization(store):
    env = KeyDoorGrid()
    ag = WorldModelAgent(store, SCOPE, env, horizon=6)
    ag.act("up")                                   # (0,0) -> (0,1): delta {pos.y: 1}
    env.x, env.y = 0, 3
    ag.sense()
    p = ag.predict("up")
    assert p.source == GENERALIZED and p.support == 1
    assert p.confidence == pytest.approx(0.5)      # 1 * n/(n+1), the backend's rule
    plan = ag.plan({"pos.y": "1"})
    # the planner now pays for the doubt: 1 + 4 * (1 - 0.5)
    assert plan.verdict == OK and plan.expected_cost == pytest.approx(3.0)


def test_p1_two_agreeing_observations_do_generalize_with_shrunk_confidence(store):
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid())
    ag.act("pickup")                               # (0,0): nothing happens
    ag.act("up")
    ag.act("pickup")                               # (0,1): nothing happens again
    p = ag.predict("pickup", {"pos.x": "4", "pos.y": "0", "key": "home", "door": "closed"})
    assert p.source == GENERALIZED and p.support == 2 and p.note == ""
    assert p.confidence == pytest.approx(2 / 3)


def test_p8_recalled_is_the_majority_not_the_latest(store):
    env = KeyDoorGrid()
    ag = WorldModelAgent(store, SCOPE, env)
    for _ in range(5):
        ag.act("up")
        ag.act("down")
    env.teleport_at, env.teleport_seed = env.t, 0
    ag.act("up")                                   # the sixth time the world teleports
    env.x, env.y = 0, 0
    ag.sense()
    p = ag.predict("up")
    assert p.source == RECALLED and p.support == 6
    assert p.delta == {"world.grid.pos.y": "1"}
    assert p.confidence == pytest.approx(5 / 6)
    plan = ag.plan({"pos.x": "0", "pos.y": "1"})
    assert plan.verdict == OK and plan.actions == ("up",)


def _slots(e: KeyDoorGrid) -> dict:
    return {"pos.x": str(e.x), "pos.y": str(e.y), "key": e.key, "door": e.door}


def _all_states():
    for (x, y), key, door in itertools.product(free_cells(), ["home", "held"],
                                               ["open", "closed"]):
        if (x, y) == (2, 2) and door == "closed":
            continue
        yield x, y, key, door


def _held_out(walk: int):
    rng = random.Random(0)
    e, learner, seen = KeyDoorGrid(), FactoredLearner(), set()
    for _ in range(walk):
        a = rng.choice(ACTIONS)
        b = _slots(e)
        e.step(a)
        learner.observe(b, a, _slots(e))
        seen.add((tuple(sorted(b.items())), a))
    answered = right = confident_wrong = 0
    held = 0
    for s in _all_states():
        for a in ACTIONS:
            e = KeyDoorGrid()
            e.x, e.y, e.key, e.door = s
            b = _slots(e)
            if (tuple(sorted(b.items())), a) in seen:
                continue
            e.step(a)
            aft = _slots(e)
            held += 1
            p = learner.predict(b, a)
            if p is None:
                continue
            answered += 1
            truth = {k: aft.get(k) for k in set(b) | set(aft) if b.get(k) != aft.get(k)}
            if p.delta == truth:
                right += 1
            elif p.confidence >= 0.99:
                confident_wrong += 1
    return held, answered, right, confident_wrong


@pytest.mark.parametrize("walk", [200, 1000, 3000])
def test_p5_factored_learner_is_never_confidently_wrong_on_held_out_pairs(walk):
    held, answered, right, confident_wrong = _held_out(walk)
    print(f"\nwalk={walk}: held-out {held}, answered {answered}, right {right}, "
          f"wrong at confidence>=0.99: {confident_wrong}")
    assert confident_wrong == 0
    # ...and it still answers: abstaining everywhere would pass the line above
    assert answered >= 0.9 * held and right >= 0.75 * answered


def test_p5_a_conjunctive_precondition_needs_a_parent_set():
    """close closes the door only NEXT to it AND while open: (x, y, door), not (door)."""
    learner = FactoredLearner()
    e = KeyDoorGrid()
    e.x, e.y, e.key, e.door = 1, 2, "held", "open"
    for _ in range(3):                              # adjacent: closes
        b = _slots(e)
        e.step("close")
        learner.observe(b, "close", _slots(e))
        e.door = "open"
    for x, y in [(0, 2), (0, 2), (4, 2), (4, 2), (1, 0), (1, 0)]:   # not adjacent: nothing
        e.x, e.y, e.door = x, y, "open"
        b = _slots(e)
        e.step("close")
        learner.observe(b, "close", _slots(e))
    learner.train_step()
    best = learner._parents[("close", "door")][0][0]
    assert isinstance(best, tuple) and {"pos.x", "pos.y"} <= set(best)
    near = learner.predict({"pos.x": "1", "pos.y": "2", "key": "held", "door": "open"},
                           "close")
    assert near.delta == {"door": "closed"}
    unseen = learner.predict({"pos.x": "3", "pos.y": "2", "key": "held", "door": "open"},
                             "close")
    assert unseen is None or unseen.confidence <= FactoredLearner.EXTRAPOLATED_CAP


def test_p5_the_agent_no_longer_plans_close_from_afar_at_full_confidence(store):
    env = KeyDoorGrid()
    ag = WorldModelAgent(store, SCOPE, env, learner=FactoredLearner())
    rng = random.Random(0)
    for _ in range(1000):
        ag.act(rng.choice(ACTIONS))
    env.x, env.y, env.key, env.door = 0, 0, "home", "open"
    ag.sense()
    p = ag.predict("close")
    assert p.delta == {} or p.confidence <= FactoredLearner.EXTRAPOLATED_CAP
    plan = ag.plan({"door": "closed"})
    if plan.verdict == OK and plan.actions == ("close",):
        assert plan.expected_cost >= 1.0 + 4.0 * (1.0 - FactoredLearner.EXTRAPOLATED_CAP)
