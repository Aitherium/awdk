"""W2: WorldModelAgent -- understand / predict / plan / step over an adapter, on awm."""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._world_envs import SCOPE, Dial, KeyDoorGrid, awm, open_store

from adk.world import (
    GENERALIZED,
    NO_MODEL,
    NONE,
    OK,
    PREDICTED,
    RECALLED,
    UNREACHABLE,
    EnvironmentAdapter,
    MemoryAdapter,
    WorldModelAgent,
)
from adk.world_adapters import FactoredLearner
from adk.world_code import CodeWorld


@pytest.fixture()
def store(tmp_path: Path):
    st = open_store(tmp_path / "m.db")
    yield st
    st.close()


def _explored(store, env=None, **kw) -> WorldModelAgent:
    env = env or KeyDoorGrid()
    ag = WorldModelAgent(store, SCOPE, env, **kw)
    res = ag.explore(3000)
    assert res.complete
    return ag


def test_grid_and_dial_conform_to_the_adapter_protocol():
    assert isinstance(KeyDoorGrid(), EnvironmentAdapter)
    assert isinstance(Dial(), EnvironmentAdapter)


def test_fresh_store_plans_no_model(store):
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid())
    ag.sense()
    p = ag.plan({"pos.x": "1"})
    assert (p.verdict, p.actions) == (NO_MODEL, ())
    st = ag.step({"pos.x": "1"})
    assert st.executed is False and st.plan.verdict == NO_MODEL
    assert store.transitions(SCOPE) == []  # refusing to plan executed nothing


def test_step_executes_only_the_first_action_and_records_it(store):
    _explored(store)
    env = KeyDoorGrid()
    ag2 = WorldModelAgent(store, SCOPE, env)
    ag2.sense()
    n0 = len(store.transitions(SCOPE))
    plan = ag2.plan({"pos.x": "0", "pos.y": "3"})
    assert plan.verdict == OK and plan.actions == ("up", "up", "up")
    st = ag2.step({"pos.x": "0", "pos.y": "3"})
    assert st.executed and st.action == "up"
    assert (env.x, env.y) == (0, 1)                  # ONE move, not three
    assert len(store.transitions(SCOPE)) == n0 + 1
    assert st.prediction.source == RECALLED and st.surprise == 0.0
    assert st.transition.delta == {"world.grid.pos.y": "1"}


def test_predict_provenance_is_never_merged(store):
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid())
    assert ag.predict("up").source == NONE
    ag.act("up")                                     # (0,0) -> (0,1)
    assert ag.predict("up", {"pos.x": "0", "pos.y": "0", "key": "home",
                             "door": "closed"}).source == RECALLED
    # never seen from (0,1); ONE observation of "up" is not a generalization
    # (round-1 review p1): state-blind, confidence shrunk to 1 * 1/2
    p = ag.predict("up")
    assert p.source == GENERALIZED and p.confidence == 0.5 and "state-blind" in p.note
    ag.act("up")                                     # (0,1) -> (0,2): "up" now disagrees
    ag.act("down")
    ag.act("down")
    ag.act("down")                                   # (0,0) -> (0,0): blocked, empty delta
    unseen = {"pos.x": "3", "pos.y": "3", "key": "held", "door": "open"}
    p = ag.predict("down", unseen)
    assert p.source == GENERALIZED and p.confidence < 1.0 and "state-blind" in p.note


def test_learner_answers_below_consistent_generalization(store):
    learner = FactoredLearner()
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid(), learner=learner)
    for a in ("up", "down", "up", "down", "down"):  # down: y1->y0 twice, blocked once
        ag.act(a)
    unseen = {"pos.x": "3", "pos.y": "1", "key": "held", "door": "open"}
    p = ag.predict("down", unseen)                  # marginal disagrees -> learner
    assert p.source == PREDICTED and p.engine == "factored-stdlib"
    assert p.delta == {"world.grid.pos.y": "0"}
    # a parent value it never saw twice: the learner abstains, the marginal answers
    q = ag.predict("down", {"pos.x": "3", "pos.y": "3", "key": "held", "door": "open"})
    assert q.source == GENERALIZED and "state-blind" in q.note


def test_beam_is_optimal_and_subgoals_reach_past_the_horizon(store):
    _explored(store)
    far = {"pos.x": "4", "pos.y": "2"}
    ag2 = WorldModelAgent(store, SCOPE, KeyDoorGrid(), horizon=6)
    ag2.sense()
    assert ag2.plan(far).verdict == UNREACHABLE
    subs = [{"key": "held"}, {"door": "open"}]
    p = ag2.plan(far, subgoals=subs)
    assert p.verdict == OK and p.method == "beam" and p.segments == 3
    assert len(p.actions) == 12 and p.expected_cost == 12.0
    full = WorldModelAgent(store, SCOPE, KeyDoorGrid(), horizon=16)
    full.sense()
    q = full.plan(far)
    assert q.verdict == OK and len(q.actions) == 12   # the true shortest path


def test_cem_path_for_large_action_sets_is_seeded(store, monkeypatch):
    ag = WorldModelAgent(store, SCOPE, Dial(), horizon=3)
    assert ag.explore(1000).complete
    for name in ("random", "choice", "choices", "shuffle", "randint", "sample"):
        monkeypatch.setattr(random, name, _forbidden)
    a = ag.plan({"n": "7"}, state={"n": "0"})
    b = ag.plan({"n": "7"}, state={"n": "0"})
    assert a.verdict == OK and a.method == "cem"
    assert a == b                                            # same seed, same plan
    n = 0
    for act in a.actions:
        n = (n + int(act.split("_")[1])) % 10
    assert n == 7
    other = WorldModelAgent(store, SCOPE, Dial(), horizon=3, seed=99)
    assert other.plan({"n": "7"}, state={"n": "0"}).verdict == OK


def _forbidden(*a, **k):
    raise AssertionError("the global random generator was used")


def test_run_reaches_goals_and_reports_budget(store):
    _explored(store)
    env = KeyDoorGrid()
    ag2 = WorldModelAgent(store, SCOPE, env, horizon=20)
    r = ag2.run({"pos.x": "4", "pos.y": "0"}, budget=40)
    assert r.reached and r.verdict == OK and (env.x, env.y) == (4, 0)
    assert r.surprise_total == 0.0
    short = WorldModelAgent(store, SCOPE, KeyDoorGrid(), horizon=20).run(
        {"pos.x": "4", "pos.y": "4"}, budget=3)
    assert not short.reached and short.verdict == "BUDGET" and len(short.steps) == 3


def test_understand_anchors_symbols_and_flags_stale_ones(store):
    chunks = {
        "a": SimpleNamespace(name="load_config", source_path="/repo/pkg/config.py",
                             start_line=3, called_by=[], calls=[]),
        "b": SimpleNamespace(name="Vansh", source_path="/repo/pkg/people.py",
                             start_line=9, called_by=[], calls=[]),
    }
    code = CodeWorld("/repo", graph=SimpleNamespace(chunks=chunks))
    code.repo_root = "/repo"
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid(), code=code)
    ag.sense()
    assert ag.anchor("load_config") == "pkg/config.py"
    store.remember(SCOPE, "anchor.old_helper", "pkg/gone.py", kind="anchor")
    store.remember(SCOPE, "anchor.Vansh", "pkg/elsewhere.py", kind="anchor")
    store.resolve_entity(SCOPE, "Vansh")
    u = ag.understand()
    assert u.slots["world.grid.pos.x"] == "0"
    assert {e["canonical"]: e["anchor"] for e in u.entities} == {
        "Vansh": {"path": "pkg/people.py", "line": 9}}
    stale = {s["symbol"]: (s["reason"], s["now"]) for s in u.stale}
    assert stale == {"old_helper": ("missing", None), "Vansh": ("moved", "pkg/people.py")}
    assert {a["symbol"] for a in u.anchors} == {"load_config", "old_helper", "Vansh"}
    assert u.degraded == []


def test_understand_without_awgraph_says_so(store):
    ag = WorldModelAgent(store, SCOPE, KeyDoorGrid())
    ag.sense()
    store.remember(SCOPE, "anchor.x", "a.py", kind="anchor")
    u = ag.understand()
    assert "awgraph:unbound" in u.degraded
    assert u.anchors == [{"symbol": "x", "path": "a.py", "verified": False}] and u.stale == []


def test_step_reconciles_facts_the_adapter_reports(store):
    class Noting(KeyDoorGrid):
        def step(self, action):
            nxt, r, d, info = super().step(action)
            info["facts"] = [{"subject": "user.last_move", "fact": action}]
            return nxt, r, d, info

    ag = WorldModelAgent(store, SCOPE, Noting())
    ag.act("up")
    st = ag.act("right")
    assert [d.action for d in st.facts] == ["update"]
    assert [m.value for m in store.recall(SCOPE, query="last_move")] == ["right"]


def test_memory_adapter_actions_go_through_reconcile(store):
    ad = MemoryAdapter(store, SCOPE, "user.ui_theme")
    ag = WorldModelAgent(store, SCOPE, ad)
    ad.say("prefers dark mode")
    st = ag.step({"user.ui_theme": "prefers dark mode"})
    assert st.executed is False and st.plan.verdict == NO_MODEL  # never seen: no guess
    ag.act("prefers dark mode")
    ag.act("switched to light mode")
    assert ag.understand().slots == {"user.ui_theme": "switched to light mode"}
    assert [h.value for h in store.history(SCOPE, "user.ui_theme")] == [
        "prefers dark mode", "switched to light mode"]


def test_a_compat_file_is_refused_not_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE memories (id INTEGER PRIMARY KEY, scope TEXT, key TEXT,"
                      " value TEXT, kind TEXT DEFAULT 'fact', created REAL, updated REAL,"
                      " hits INTEGER DEFAULT 0, meta TEXT, UNIQUE(scope,key));"
                      "CREATE TABLE schema_meta (version INTEGER NOT NULL);"
                      "INSERT INTO schema_meta VALUES (1);")
    con.commit()
    con.close()
    raw = db.read_bytes()
    with pytest.raises(awm.NeedsMigration):
        WorldModelAgent(db, SCOPE, KeyDoorGrid())
    assert db.read_bytes() == raw
