"""W3: the learned tail -- a factored model generalizes where the table cannot."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests._world_envs import SCOPE, KeyDoorGrid, awm, open_store

from adk import world as worldmod
from adk.world import GENERALIZED, NONE, PREDICTED, WorldModelAgent
from adk.world_adapters import (
    ACTION_WIDTH,
    STATE_WIDTH,
    AwmStateAdapter,
    AwpredictLearner,
    ChainLearner,
    FactoredLearner,
    make_learner,
    train_from_transitions,
)

AWPREDICT_SRC = Path("C:/AitherOS-Fresh/AitherOS/packages/awpredict")


class FlipWorld:
    """a in {0,1}; c a counter. flip toggles a; bump increments c. c is irrelevant to flip."""

    domain = "toy"

    def __init__(self, a: int = 0, c: int = 0) -> None:
        self.a, self.c = a, c

    def observe(self, env_state=None):
        return {"a": str(self.a), "c": str(self.c)}

    def actions(self):
        return ["flip", "bump"]

    def step(self, action):
        if action == "flip":
            self.a = 1 - self.a
        else:
            self.c += 1
        return self.observe(), 0.0, False, {}


@pytest.fixture()
def store(tmp_path: Path):
    st = open_store(tmp_path / "m.db")
    yield st
    st.close()


def _train_toy(store, learner):
    env = FlipWorld()
    ag = WorldModelAgent(store, SCOPE, env, learner=learner)
    for a in ["flip", "bump"] * 4:          # visits (a, c) for c = 0..4 only
        ag.act(a)
    return ag


def test_factored_predicts_an_unseen_full_state_the_table_cannot(store):
    learner = FactoredLearner()
    ag = _train_toy(store, learner)
    unseen = {"world.toy.a": "1", "world.toy.c": "9"}            # c=9 never observed
    digest = awm.state_digest(unseen)
    # The table: this exact state never happened -> no exact answer at all.
    assert store.transitions(SCOPE, state=digest, action="flip") == []
    assert ag._table.recalled(digest, "flip") is None
    tabular = WorldModelAgent(store, SCOPE, FlipWorld())       # no learner
    assert tabular.predict("flip", unseen).source == GENERALIZED  # state-blind at best
    assert tabular.predict("flip", unseen).confidence < 1.0
    p = ag.predict("flip", unseen)
    assert p.source == PREDICTED and p.delta == {"world.toy.a": "0"}
    assert p.confidence == 1.0 and p.engine == "factored-stdlib"
    # ...and the world agrees when the step is actually taken there.
    env = FlipWorld(a=1, c=9)
    live = WorldModelAgent(store, SCOPE, env, learner=learner)
    st = live.act("flip")
    assert st.prediction.source == PREDICTED and st.surprise == 0.0
    assert st.transition.delta == {"world.toy.a": "0"}


def test_factored_abstains_instead_of_guessing():
    learner = FactoredLearner()
    learner.observe({"a": "0"}, "flip", {"a": "1"})
    # one observation per parent value: leave-one-out accuracy 0, so no parent qualifies
    assert learner.predict({"a": "0"}, "flip") is None
    assert learner.predict({"a": "0"}, "never") is None
    learner.observe({"a": "0"}, "flip", {"a": "1"})
    assert learner.predict({"a": "0"}, "flip").delta == {"a": "1"}
    assert learner.predict({"a": "1"}, "flip") is None      # a=1 never seen with flip


def test_a_unique_id_slot_is_never_chosen_as_the_cause():
    learner = FactoredLearner()
    for i in range(6):
        a = str(i % 2)
        learner.observe({"a": a, "id": f"u{i}"}, "flip", {"a": str(1 - int(a)), "id": f"u{i}"})
    learner.train_step()
    ranked = learner._parents[("flip", "a")]
    assert ranked[0] == ("a", 1.0)
    assert dict(ranked)["id"] == 0.0


def test_the_learner_is_consulted_only_on_a_table_miss(store):
    learner = FactoredLearner()
    ag = _train_toy(store, learner)
    calls = []
    orig = learner.predict

    def spy(slots, action):
        calls.append(action)
        return orig(slots, action)

    learner.predict = spy
    seen = {"world.toy.a": "0", "world.toy.c": "0"}
    assert ag.predict("flip", seen).source == "RECALLED"
    assert calls == []


def test_state_adapter_is_fixed_width_deterministic_and_decodes_by_nearest():
    ad = AwmStateAdapter()
    s1 = {"x": "1", "y": "2"}
    s2 = {"x": "3", "y": "2"}
    v1 = ad.to_vector(s1)
    assert len(v1) == STATE_WIDTH == 256 and v1 == ad.to_vector(dict(s1))
    assert len(ad.action_vector("up")) == ACTION_WIDTH == 32
    assert sum(x * x for x in v1) == pytest.approx(1.0)
    best, sim = ad.decode(v1, [s2, s1])
    assert best == s1 and sim == pytest.approx(1.0)
    ws = awm.WorldState(digest=awm.state_digest(s1), slots=s1, scope=str(SCOPE), prefix=None,
                        as_of=None, ts=0.0)
    assert ad.to_vector(ws) == v1
    with pytest.raises(ValueError):
        ad.decode([0.0] * 3, [s1])


def test_train_from_transitions_rebuilds_states_with_as_of(store):
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid())
    for a in ["up", "up", "right", "down", "down", "left", "pickup", "up"]:
        ag.act(a)
    learner = FactoredLearner()
    rep = train_from_transitions(ag, learner, epochs=2)
    assert rep["transitions"] == 8 and rep["fed"] == 8
    assert rep["skipped"] == {"digest_mismatch": 0, "unresolved": 0}
    assert learner.n == 8 and rep["train_steps"] == 2  # observed once; epochs re-train
    rep2 = train_from_transitions(store, FactoredLearner(), scope=SCOPE, prefix="world.grid")
    assert rep2["fed"] == 8


def test_a_state_that_cannot_be_rebuilt_is_skipped_not_trained_on(store):
    # A transition over slots that were never written as memories cannot be rebuilt
    # by as_of: its digest will not match and it must be counted, not learned.
    slots = {"world.grid.pos.x": "7"}
    ws = awm.WorldState(digest=awm.state_digest(slots), slots=slots, scope=str(SCOPE),
                        prefix="world.grid", as_of=None, ts=store._clock())
    after = awm.WorldState(digest=awm.state_digest({"world.grid.pos.x": "8"}),
                           slots={"world.grid.pos.x": "8"}, scope=str(SCOPE),
                           prefix="world.grid", as_of=None, ts=store._clock())
    store.observe_transition(SCOPE, ws, "right", after)
    learner = FactoredLearner()
    rep = train_from_transitions(store, learner, scope=SCOPE, prefix="world.grid")
    assert rep["fed"] == 0 and rep["skipped"]["digest_mismatch"] == 1
    assert learner.n == 0


def test_without_awpredict_the_stdlib_learner_is_used_and_reported(monkeypatch):
    monkeypatch.setitem(sys.modules, "awpredict", None)
    monkeypatch.setitem(sys.modules, "awpredict.core", None)
    monkeypatch.setitem(sys.modules, "awpredict.core.mlp", None)
    monkeypatch.setattr(worldmod, "TELEMETRY", {"degraded": [], "counters": {}})
    import adk.world_adapters as wa
    monkeypatch.setattr(wa, "TELEMETRY", worldmod.TELEMETRY)
    learner = make_learner("auto")
    assert isinstance(learner, FactoredLearner)
    assert any(d.startswith("awpredict:") for d in worldmod.TELEMETRY["degraded"])


@pytest.mark.skipif(not AWPREDICT_SRC.is_dir(), reason="awpredict source not on this machine")
def test_awpredict_engine_chains_in_front_and_stays_labelled(monkeypatch, store):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the source tree is read-only
    monkeypatch.syspath_prepend(str(AWPREDICT_SRC))
    for mod in [m for m in sys.modules if m == "awpredict" or m.startswith("awpredict.")]:
        monkeypatch.delitem(sys.modules, mod)
    try:
        import awpredict.core.mlp  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"awpredict not importable here: {exc}")
    learner = make_learner("auto")
    assert isinstance(learner, ChainLearner)
    assert isinstance(learner.learners[0], AwpredictLearner)
    ag = _train_toy(store, learner)
    p = ag.predict("flip", {"world.toy.a": "1", "world.toy.c": "9"})
    # The MLP engine in tabular mode only knows exact states: an unseen one falls
    # through to the factored learner, and the label says which engine answered.
    assert p.source == PREDICTED and p.engine == "factored-stdlib"
    assert p.delta == {"world.toy.a": "0"}


def test_no_learner_means_none_for_a_never_taken_action(store):
    ag = WorldModelAgent(store, SCOPE, FlipWorld())
    assert ag.predict("flip", {"a": "0", "c": "0"}).source == NONE
    assert os.environ.get("AWM_AUTO_MIGRATE") in (None, "", "0")


def test_a_named_learner_is_warm_started_from_the_stores_history(store):
    # B2: an agent REOPENED on a store with history must not start with an empty
    # learned tail. The first agent had no learner at all; the second names one.
    _train_toy(store, None)
    unseen = {"world.toy.a": "1", "world.toy.c": "9"}
    cold = WorldModelAgent(store, SCOPE, FlipWorld(), learner=FactoredLearner())
    assert cold.learner.n == 0 and cold.learner_training is None
    assert cold.predict("flip", unseen).source != PREDICTED
    warm = WorldModelAgent(store, SCOPE, FlipWorld(), learner="factored")
    rep = warm.learner_training
    assert rep["transitions"] == 8 and rep["fed"] == 8
    assert rep["skipped"] == {"digest_mismatch": 0, "unresolved": 0}
    assert warm.learner.n == 8
    p = warm.predict("flip", unseen)
    assert p.source == PREDICTED and p.delta == {"world.toy.a": "0"}


def test_an_unknown_learner_name_is_refused(store):
    with pytest.raises(ValueError, match="learner="):
        WorldModelAgent(store, SCOPE, FlipWorld(), learner="mlp")
