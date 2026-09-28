"""Regressions for the fifth independent review (awdk side).

Each test failed before its fix (verified against the pre-fix tree).
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5
from tests._world_envs import open_store

from adk import crystal as C
from adk import world_adapters as WA
from adk.commands import wm as wmcli
from adk.world import (
    NO_MODEL,
    OK,
    SPECULATIVE_BELOW,
    AwmWorldModelBackend,
    WorldModelAgent,
)
from adk.worldmodel import clear_world_model_registry

S0 = [0.1] * 8


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("AITHER_AGENT_WM", "AITHER_AGENT_WM_BACKEND", "AITHER_AGENT_WM_AWM_DB",
                "AITHER_AGENT_WM_AWM_SCOPE", "AITHER_AGENT_WM_ALLOWED_ACTIONS",
                "AITHER_AGENT_WM_REDACTION_SALT", "AWM_AUTO_MIGRATE", "ADK_CRYSTAL_SCOPE",
                "ADK_CRYSTAL_DB", "ADK_CRYSTAL_NO_EMBED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(tmp_path / "wm"))
    clear_world_model_registry()
    yield
    clear_world_model_registry()


# -- AITHER_AGENT_WM_AWM_SCOPE pooled every agent into one scope ---------------------
def test_env_scope_is_per_agent_with_agent_placeholder(monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_WM_AWM_SCOPE", "acme:ops:{agent}")
    monkeypatch.setenv("AITHER_AGENT_WM_ALLOWED_ACTIONS", "*")
    atlas, iris = AwmWorldModelBackend("atlas"), AwmWorldModelBackend("iris")
    try:
        assert atlas.scope_text == "acme:ops:agent.atlas"
        assert iris.scope_text == "acme:ops:agent.iris"
        for _ in range(3):
            atlas.record(S0, "deploy_prod", S0, ok=False)
        iris.record(S0, "read_file", S0, ok=True)
        assert iris.stats()["n"] == 1
    finally:
        atlas.close()
        iris.close()


def test_env_scope_without_agent_placeholder_is_refused_not_pooled(monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_WM_AWM_SCOPE", "acme:ops:wm")
    monkeypatch.setenv("AITHER_AGENT_WM_ALLOWED_ACTIONS", "*")
    atlas, iris = AwmWorldModelBackend("atlas"), AwmWorldModelBackend("iris")
    try:
        for _ in range(3):
            atlas.record(S0, "deploy_prod", S0, ok=False)
        iris.record(S0, "read_file", S0, ok=True)
        assert "{agent}" in (iris.stats()["degraded"] or "")
        assert iris.advise(S0, ["deploy_prod"]) is None  # never atlas's outcomes
        atlas.save()
        ckpt = json.loads(atlas._ckpt_path.read_text(encoding="utf-8"))
        assert ckpt["scope"] is None  # a refused scope is never persisted
    finally:
        atlas.close()
        iris.close()


def test_reset_refuses_to_clear_a_scope_shared_with_other_agents(tmp_path, capsys):
    db = tmp_path / "wm" / "awm_world.db"
    root = str(tmp_path / "wm")
    atlas = AwmWorldModelBackend("atlas", root=root, db=db, scope="acme:ops:wm",
                                 allowed_actions="*")
    iris = AwmWorldModelBackend("iris", root=root, db=db, scope="acme:ops:wm",
                                allowed_actions="*")
    atlas.record(S0, "deploy_prod", S0, ok=False)
    iris.record(S0, "read_file", S0, ok=True)
    atlas.save()  # an older checkpoint that recorded the shared scope
    atlas.close()
    iris.close()
    rc = wmcli.cmd_wm_reset(SimpleNamespace(agent="agent.atlas", yes=True))
    assert rc == 1 and "shared" in capsys.readouterr().out
    left = AwmWorldModelBackend("iris", root=root, db=db, scope="acme:ops:wm")
    try:
        assert left.stats()["n"] == 2
    finally:
        left.close()


# -- state-blind marginals and guessed edges ---------------------------------------
class _Counter:
    domain = "ctr"

    def __init__(self) -> None:
        self.x = 0

    def observe(self, env_state=None):
        return {"x": str(self.x)}

    def actions(self):
        return ["inc", "reset"]

    def step(self, a):
        self.x = self.x + 1 if a == "inc" else 0
        return None, 0.0, False, {}


def test_a_state_blind_marginal_is_speculative_and_never_proves_unreachable(tmp_path):
    env = _Counter()
    ag = WorldModelAgent(open_store(tmp_path / "c.db"), awm.Scope("acme", "t", "ctr"), env)
    for _ in range(10):  # inc seen ten times, always at x=0
        ag.act("inc")
        ag.act("reset")
    p = ag.predict("inc", {"x": "5"})
    assert p.confidence < SPECULATIVE_BELOW and "state-blind" in p.note
    pl = ag.plan({"x": "1"}, state={"x": "5"})
    assert pl.verdict == OK and pl.speculative
    miss = ag.plan({"x": "6"}, state={"x": "5"})
    assert miss.verdict == NO_MODEL and "guessed" in miss.note


class _Lock:
    domain = "lock"

    def __init__(self) -> None:
        self.x, self.door = 0, "shut"

    def observe(self, env_state=None):
        return {"x": str(self.x), "door": self.door}

    def actions(self):
        return ["inc", "unlock"]

    def step(self, a):
        if a == "inc":
            self.x = min(3, self.x + 1)
        elif self.x == 3:
            self.door = "open"
        return None, 0.0, False, {}


def test_a_guessed_no_op_is_not_a_proof_of_unreachability(tmp_path):
    ag = WorldModelAgent(open_store(tmp_path / "l.db"), awm.Scope("acme", "t", "lock"),
                         _Lock(), learner=WA.FactoredLearner())
    for a in ("unlock", "inc", "inc", "inc"):
        ag.act(a)
    assert ag.predict("unlock").confidence < SPECULATIVE_BELOW
    assert ag.plan({"door": "open"}).verdict == NO_MODEL


# -- step() keeps subgoal progress --------------------------------------------------
_K = "world.ctr.x"


class _Line:
    domain = "ctr"

    def __init__(self) -> None:
        self.x = 2

    def observe(self, env_state=None):
        return {"x": str(self.x)}

    def actions(self):
        return ["inc", "dec"]

    def step(self, a):
        self.x = min(5, self.x + 1) if a == "inc" else max(0, self.x - 1)
        return None, 0.0, False, {}


class _Oracle:
    name = "oracle"

    def predict(self, s, a):
        x = int(s[_K])
        n = min(5, x + 1) if a == "inc" else max(0, x - 1)
        return SimpleNamespace(delta={_K: str(n)} if n != x else {}, confidence=1.0,
                               engine="oracle")

    def observe(self, *a):
        pass


def test_step_keeps_subgoal_progress_across_calls(tmp_path):
    env = _Line()
    ag = WorldModelAgent(open_store(tmp_path / "a.db"), awm.Scope("acme", "t", "ctr"), env,
                         learner=_Oracle())
    trace = []
    for _ in range(6):
        ag.step({"x": "1"}, subgoals=[{"x": "3"}])
        trace.append(env.x)
        if env.x == 1:
            break
    assert trace == [3, 2, 1]


# -- unexplained counts --------------------------------------------------------------
def test_backend_bookkeeping_rows_are_not_teleports(tmp_path):
    b = AwmWorldModelBackend("agent", root=str(tmp_path), db=tmp_path / "w.db",
                             allowed_actions="read_file")
    try:
        for i in range(5):
            b.record([i / 10] * 8, "read_file", [(i + 1) / 10] * 8, ok=True)
        s = b.stats()
        assert s["transitions"] == 5 and s["surprise"]["unexplained"] == 0
    finally:
        b.close()


class _FactCounter:
    domain = "ctr"

    def __init__(self) -> None:
        self.x = 0

    def observe(self, env_state=None):
        return {"x": str(self.x)}

    def actions(self):
        return ["inc"]

    def step(self, a):
        self.x += 1
        return None, 0.0, False, {"facts": [{"subject": "user.last_counter",
                                             "fact": f"counter is {self.x}"}]}


def test_agent_reconciled_facts_are_not_teleports(tmp_path):
    ag = WorldModelAgent(open_store(tmp_path / "c.db"), awm.Scope("acme", "t", "ctr"),
                         _FactCounter())
    for _ in range(4):
        ag.act("inc")
    assert ag.stats()["surprise"]["unexplained"] == 1  # only the initial sync


# -- train_from_transitions epochs ---------------------------------------------------
def test_epochs_do_not_turn_one_transition_into_confident_evidence(tmp_path):
    b = AwmWorldModelBackend("atlas", root=str(tmp_path), agent_id="agent.atlas",
                             allowed_actions=["grep"])
    try:
        b.load()
        b.record(S0, "grep", [0.9] * 8, ok=True)
        t = b._store.transitions(b._scope)[-1]
        before = b.slots_for_transition(t)
        for ep in (1, 2, 5):
            L = WA.make_learner("factored")
            rep = WA.train_from_transitions(b, L, epochs=ep)
            assert rep["fed"] == 1 and L.n == 1 and rep["train_steps"] == ep
            assert L.predict(before, t.action) is None
    finally:
        b.close()


# -- adk wm status: one source per row ----------------------------------------------
def test_status_stage_comes_from_the_live_table_not_a_stale_checkpoint(tmp_path, capsys):
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("atlas", root=str(root), agent_id="agent.atlas",
                             allowed_actions="*")
    try:
        for i in range(60):
            b.record([(i % 10) / 10] * 8, "grep" if i % 2 else "edit", S0, ok=True)
        b.save()
    finally:
        b.close()
    ck = b._ckpt_path
    data = json.loads(ck.read_text(encoding="utf-8"))
    data.update(n=0, stage="cold")  # what a degraded save writes
    ck.write_text(json.dumps(data), encoding="utf-8")
    assert wmcli.cmd_wm_status(SimpleNamespace()) == 0
    row = next(ln for ln in capsys.readouterr().out.splitlines() if "agent.atlas" in ln)
    live = AwmWorldModelBackend("atlas", root=str(root), agent_id="agent.atlas")
    try:
        stage = live.stats()["stage"]
    finally:
        live.close()
    assert stage != "cold" and row.split()[2] == stage and row.split()[3] == "60"


# -- crystal: a missing awgraph and a missing index are told apart -------------------
def test_crystal_names_why_awgraph_is_unavailable(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_awgraph(name, *a, **k):
        if name == "awgraph" or name.startswith("awgraph."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    idx = C.AwgraphIndex(str(tmp_path))
    monkeypatch.setattr(builtins, "__import__", no_awgraph)
    with pytest.raises(C.AwgraphUnavailable) as missing:
        asyncio.run(idx.query("x", 3))
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert "ModuleNotFoundError" in str(missing.value)

    pytest.importorskip("awgraph.cli")
    empty = tmp_path / "empty"
    empty.mkdir()
    cr = C.Crystal(scope="acme:bob:proj", store=None, graph=C.AwgraphIndex(str(empty)),
                   embed=None)
    asyncio.run(cr.recall_graph("deploy"))
    tokens = [d for d in cr.telemetry["degraded"] if d.startswith("awgraph:")]
    assert tokens and tokens[0].startswith("awgraph:no-index for "), tokens
    assert os.path.basename(str(empty)) in tokens[0]
