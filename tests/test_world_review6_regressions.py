"""Regressions for the sixth independent review (awdk world side).

Each test failed before its fix (verified against the pre-fix tree).
"""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5
from tests._world_envs import SCOPE, KeyDoorGrid, open_store

from adk import world as W
from adk.commands import wm as wmcli
from adk.world import (
    NO_MODEL,
    NONE,
    OK,
    UNREACHABLE,
    AwmWorldModelBackend,
    WorldModelAgent,
)
from adk.worldmodel import clear_world_model_registry

S0 = [0.1] * 8


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("AITHER_AGENT_WM", "AITHER_AGENT_WM_BACKEND", "AITHER_AGENT_WM_AWM_DB",
                "AITHER_AGENT_WM_AWM_SCOPE", "AITHER_AGENT_WM_ALLOWED_ACTIONS",
                "AWM_AUTO_MIGRATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(tmp_path / "wm"))
    clear_world_model_registry()
    yield
    clear_world_model_registry()


class _Env:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.n = 0

    def observe(self, env_state=None):
        return {"n": str(self.n)}

    def actions(self):
        return ["tick", "inc"]

    def step(self, a):
        if a == "inc":
            self.n += 1
        return self.observe(), 0.0, False, {}


# -- F8: another prefix's no-ops entered this agent's marginal -----------------------
def test_foreign_noops_do_not_make_an_untried_action_generalized(tmp_path):
    st = open_store(tmp_path / "m.db")
    b = WorldModelAgent(st, SCOPE, _Env("b"))
    for a in ("tick", "inc", "tick", "inc", "tick"):
        b.act(a)
    ag = WorldModelAgent(st, SCOPE, _Env("a"))
    ag.sense()
    assert ag.predict("tick").source == NONE
    ag.act("inc")
    ag.act("inc")
    assert ag.plan({"n": "0"}).verdict == NO_MODEL
    st.close()


def test_own_noops_still_reload_into_the_table(tmp_path):
    st = open_store(tmp_path / "m.db")
    ag = WorldModelAgent(st, SCOPE, _Env("a"))
    ag.act("tick")                                 # a no-op from a state no delta reaches
    ag.act("inc")
    ag.act("tick")
    n_live = ag._table.n
    assert ag.reload() == n_live == 3
    st.close()


# -- F9: step() kept subgoal progress across an episode reset -------------------------
def test_step_rederives_subgoal_progress_after_a_reset(tmp_path):
    st = open_store(tmp_path / "x.db")
    WorldModelAgent(st, SCOPE, KeyDoorGrid()).explore(5000)
    goal = {"pos.x": "4", "pos.y": "4"}
    subs = [{"key": "held"}, {"door": "open"}]
    env = KeyDoorGrid()
    ag = WorldModelAgent(st, SCOPE, env, horizon=6)
    for _ in range(60):
        if env.door == "open":
            break
        assert ag.step(goal, subgoals=subs).executed
    ag.step(goal, subgoals=subs)                   # progress now past both subgoals
    env.x, env.y, env.key, env.door = 0, 0, "home", "closed"   # episode reset
    r = ag.step(goal, subgoals=subs)
    assert r.executed and r.plan.verdict == OK
    ag.reset_progress()
    assert ag._step_progress == {}
    st.close()


# -- F11: ONE observation of a stochastic action proved "UNREACHABLE" -----------------
class _Coin:
    domain = "coin"

    def __init__(self, seed: int) -> None:
        self.won = "no"
        self.rng = random.Random(seed)

    def observe(self, env_state=None):
        return {"won": self.won}

    def actions(self):
        return ["flip"]

    def step(self, a):
        if self.rng.random() < 0.5:
            self.won = "yes"
        return self.observe(), 0.0, False, {}


def _losing_seed() -> int:
    for seed in range(1000):
        if random.Random(seed).random() >= 0.5:
            return seed
    raise AssertionError("no losing seed")


def test_one_losing_flip_is_not_proof_the_goal_is_unreachable(tmp_path):
    st = open_store(tmp_path / "m.db")
    ag = WorldModelAgent(st, awm.Scope.parse("local:alice:coin"), _Coin(_losing_seed()))
    ag.act("flip")
    assert ag.predict("flip").support == 1
    p = ag.plan({"won": "yes"})
    assert p.verdict == NO_MODEL and p.verdict != UNREACHABLE
    assert "observed only once" in p.note
    st.close()


def test_a_world_shown_deterministic_still_proves_unreachable(tmp_path):
    st = open_store(tmp_path / "x.db")
    WorldModelAgent(st, SCOPE, KeyDoorGrid()).explore(5000)
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid(), horizon=6)
    ag.sense()
    assert ag._table.deterministic()
    assert ag.plan({"pos.x": "4", "pos.y": "2"}).verdict == UNREACHABLE
    st.close()


def test_disagreeing_repeats_mean_not_deterministic():
    t = W._Table()
    t.add("s", "a", {"x": "1"})
    t.add("s", "a", {"x": "1"})
    t.add("u", "a", {"x": "1"})
    t.add("u", "a", {"x": "1"})
    assert t.deterministic()
    t.add("v", "b", {})
    t.add("v", "b", {"y": "2"})
    assert not t.deterministic()


# -- F15: `adk wm reset` deleted the checkpoint and left the awm table ----------------
def test_reset_refuses_when_awm_is_unreadable_even_without_an_awm_checkpoint(
        tmp_path, capsys, monkeypatch):
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("agent.atlas", root=str(root), agent_id="agent.atlas",
                             allowed_actions="*")
    for _ in range(3):
        b.record(S0, "file_read", S0, ok=True)
    b.close()                                      # no save: no .awm.json checkpoint
    ckpt = root / "agent.atlas.wm.json"
    ckpt.write_text('{"version":1,"agent_id":"agent.atlas","backend":"builtin","n":0}')
    assert not (root / "agent.atlas.awm.json").exists()
    monkeypatch.setattr(W, "_awm", lambda: None)   # awm not importable
    rc = wmcli.cmd_wm_reset(SimpleNamespace(agent="agent.atlas", yes=True))
    assert rc == 1 and "refusing a partial reset" in capsys.readouterr().out
    assert ckpt.exists()
    monkeypatch.undo()
    left = AwmWorldModelBackend("agent.atlas", root=str(root), agent_id="agent.atlas")
    try:
        assert left.stats()["n"] == 3
    finally:
        left.close()
