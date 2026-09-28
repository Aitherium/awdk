"""Regressions for the seventh independent review (awdk side).

Each test failed before its fix (verified by reverting the fixed module).
"""
from __future__ import annotations

import json
import shutil
from types import SimpleNamespace

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5

from adk import world as W
from adk.commands import wm as wmcli
from adk.world import AwmWorldModelBackend, states_scope_text
from adk.world_adapters import FactoredLearner
from adk.worldmodel import clear_world_model_registry

DIM = 8
PREV = [0.2] * DIM
AFTER = [0.3] * DIM


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


# -- advise: failures recorded the agent-loop way reach the bare tool name ----------
def test_failures_recorded_by_the_agent_loop_reach_advise(tmp_path):
    b = AwmWorldModelBackend("tester", root=str(tmp_path / "wm"))
    b.load()
    # exactly agent.py: a failed call is recorded as f"{tc.name}[denied]", ok=False
    for name in ["flaky[denied]"] * 9 + ["flaky"] + ["solid"] * 3:
        b.record(PREV, name, AFTER, ok="[" not in name)
    adv = b.advise(PREV, ["flaky", "solid", "untried"])   # agent.py asks bare names
    assert adv["scores"]["flaky"] == pytest.approx(0.1)
    assert adv["order"] == ["solid", "untried", "flaky"]
    assert b.stats()["actions"] == 2   # one action per TOOL, not per failure marker
    b.close()


def test_split_failure_marker():
    assert W.split_failure_marker("flaky[denied]") == ("flaky", True)
    assert W.split_failure_marker("kb_chat [error]") == ("kb_chat", True)
    assert W.split_failure_marker("solid") == ("solid", False)
    assert W.split_failure_marker("[x]") == ("[x]", False)   # no tool name: unchanged


# -- FactoredLearner: a slot never seen with the action is untested ----------------
def test_a_slot_never_seen_with_the_action_caps_confidence():
    lr = FactoredLearner()
    for _ in range(3):
        for a, b in (("closed", "open"), ("open", "closed")):
            lr.observe({"w.door": a}, "toggle", {"w.door": b})
    lr.train_step()
    assert lr.predict({"w.door": "closed"}, "toggle").confidence == 1.0
    p = lr.predict({"w.door": "closed", "w.lock": "locked"}, "toggle")
    assert p.confidence <= FactoredLearner.EXTRAPOLATED_CAP


# -- crystal: ancestor owner rules survive the crystal's own facts -----------------
def test_landed_owner_rule_is_not_crowded_out_by_crystallized_facts(tmp_path):
    from awm import MemoryStore, Scope

    from adk.crystal import SCAN_LIMIT, build_crystal

    db = tmp_path / "m.db"
    st = MemoryStore(db)
    st.remember(Scope.parse("acme:alice:*"), "no-force-push", "never force-push to develop",
                kind="rule", meta={"source": "memory-file", "name": "no-force-push"})
    proj = Scope.parse("acme:alice:proj")
    for i in range(SCAN_LIMIT + 40):
        st.remember(proj, f"f:{i:04d}", f"step {i} finished ok", kind="crystal")
    st.close()
    c = build_crystal("acme:alice:proj", db=db, embed=False)
    assert any(k == "no-force-push" for k, _v, _m in c.store.scan_landed(SCAN_LIMIT))


# -- adk wm reset: the configured root's table, never the checkpoint's old path ----
def test_wm_reset_in_a_copied_root_clears_the_configured_root(tmp_path, monkeypatch):
    ra, rb = tmp_path / "rA", tmp_path / "rB"
    b = AwmWorldModelBackend("Atlas", root=str(ra), allowed_actions=["read_file"])
    for i in range(5):
        b.record([0.1 * i] * DIM, "read_file", [0.1 * i + 0.05] * DIM, ok=True)
    b.save()
    b.close()
    assert json.loads((ra / "agent.atlas.awm.json").read_text())["db"].startswith(str(ra))
    shutil.copytree(ra, rb)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(rb))
    assert wmcli.cmd_wm_reset(SimpleNamespace(agent="agent.atlas", yes=True)) == 0
    for root, want in ((ra, 5), (rb, 0)):
        again = AwmWorldModelBackend("Atlas", root=str(root))
        again.load()
        assert again.stats()["n"] == want, root
        again.close()


def test_an_explicit_db_in_the_checkpoint_is_still_honoured(tmp_path, monkeypatch):
    elsewhere = tmp_path / "shared" / "world.db"
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("Atlas", root=str(root), db=str(elsewhere),
                             allowed_actions=["read_file"])
    b.record(PREV, "read_file", AFTER, ok=True)
    b.save()
    b.close()
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(root))
    assert wmcli._awm_backend("agent.atlas", json.loads(
        (root / "agent.atlas.awm.json").read_text())).db_path == elsewhere


# -- bookkeeping rows are not world state ------------------------------------------
def test_state_rows_are_not_visible_at_the_transition_scope(tmp_path):
    b = AwmWorldModelBackend("Atlas", root=str(tmp_path / "wm"), allowed_actions=["t"])
    b.load()
    for i in range(6):
        b.record([0.1 * i] * DIM, "t", [0.1 * i + 0.1] * DIM, ok=True)
    st = b._store
    assert [m for m in st.recall(b._scope, limit=10_000) if m.kind == W.STATE_KIND] == []
    assert st.surprise_stats(b._scope).unexplained == 0
    assert st.encode_state(b._scope, prefix=W.BACKEND_PREFIX).slots == {}
    t = st.transitions(b._scope)[0]
    assert b.slots_for_transition(t) is not None   # offline training still rebuilds states
    b.close()


def test_states_scope_is_never_visible_from_the_transition_scope():
    from awm import Scope
    from awm.scope import visible_scopes

    for text in ("local:agent.atlas:wm", "acme:agent.atlas:*", "agent.atlas:*:*"):
        vis = {str(s) for s in visible_scopes(Scope.parse(text))}
        st = states_scope_text(text)
        Scope.parse(st)
        assert st not in vis and st != text


# -- a degraded backend never overwrites its checkpoint ----------------------------
def test_degraded_save_keeps_the_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("Atlas", root=str(root), allowed_actions=["t"])
    for i in range(4):
        b.record([0.1 * i] * DIM, "t", [0.1 * i + 0.1] * DIM, ok=True)
    b.save()
    b.close()
    ck = root / "agent.atlas.awm.json"
    assert json.loads(ck.read_text())["n"] == 4
    monkeypatch.setattr(W, "_awm", lambda: None)   # awm not importable
    hid = AwmWorldModelBackend("Atlas", root=str(root))
    hid.load()
    assert hid.degraded
    hid.save()
    assert json.loads(ck.read_text())["n"] == 4
    assert W.TELEMETRY["counters"].get("backend.save_skipped", 0) >= 1


def test_status_keeps_the_last_known_n_when_the_table_cannot_be_read(tmp_path, monkeypatch,
                                                                     capsys):
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("Atlas", root=str(root), allowed_actions=["t"])
    for i in range(4):
        b.record([0.1 * i] * DIM, "t", [0.1 * i + 0.1] * DIM, ok=True)
    b.save()
    b.close()
    monkeypatch.setattr(W, "_awm", lambda: None)   # awm not importable
    assert wmcli.cmd_wm_status(SimpleNamespace()) == 0
    row = [ln for ln in capsys.readouterr().out.splitlines() if "agent.atlas" in ln][0]
    assert row.split()[1] == "awm!" and row.split()[3] == "4"
