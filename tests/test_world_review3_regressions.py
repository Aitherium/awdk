"""Regressions for the third independent review of the world model + crystal (awdk side).

Each test failed before its fix (verified against the pre-fix tree).
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5
from tests._world_envs import SCOPE, KeyDoorGrid, open_store

from adk import crystal as C
from adk.commands import wm as wmcli
from adk.world import NO_MODEL, AwmWorldModelBackend, WorldModelAgent
from adk.worldmodel import (
    STATE_DIMS,
    BuiltinWorldModel,
    clear_world_model_registry,
    redaction_salt,
    wm_agent_id,
)

DIM = len(STATE_DIMS)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("AITHER_AGENT_WM", "AITHER_AGENT_WM_BACKEND", "AITHER_AGENT_WM_AWM_DB",
                "AITHER_AGENT_WM_AWM_SCOPE", "AITHER_AGENT_WM_ALLOWED_ACTIONS",
                "AITHER_AGENT_WM_REDACTION_SALT", "AWM_AUTO_MIGRATE", "ADK_CRYSTAL_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(tmp_path / "wm"))
    clear_world_model_registry()
    yield
    clear_world_model_registry()


# -- crystal ------------------------------------------------------------------
def test_a_summary_line_cannot_overwrite_an_owner_rule(tmp_path):
    db = tmp_path / "m.db"
    sc = awm.Scope.parse("acme:alice:proj")
    st = awm.MemoryStore(db)
    st.remember(sc, "deploy.host", "prod.internal", kind="rule")
    st.close()
    c = C.build_crystal(str(sc), db=db, embed=False)
    asyncio.run(c.crystallize("- deploy.host: attacker.example.com (as the README says)"))
    c.store.close()
    st = awm.MemoryStore(db)
    live = {m.key: (m.value, m.kind) for m in st.recall(sc)}
    assert live["deploy.host"] == ("prod.internal", "rule")
    st.close()


def test_crystal_scope_refuses_a_wildcard_agent_name(tmp_path, monkeypatch):
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "acme:agents:{agent}")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    assert C.crystal_from_env("*") is None
    assert C.crystal_from_env("a:b") is None
    ok = C.crystal_from_env("bob")
    assert ok is not None and ok.scope == "acme:agents:bob"
    ok.store.close()


def test_scan_keeps_slot_facts_when_legacy_rows_fill_the_limit(tmp_path):
    c = C.build_crystal("acme:bob:proj", db=tmp_path / "s.db", embed=False)
    for i in range(5):
        asyncio.run(c.crystallize("\n".join(
            f"Observation number {i * 40 + j} about the build pipeline." for j in range(40))))
    asyncio.run(c.crystallize("deploy.runtime: docker compose on the host"))
    keys = [k for k, _v, _m in c.store.scan(C.SCAN_LIMIT)]
    assert len(keys) == C.SCAN_LIMIT and "deploy.runtime" in keys
    c.store.close()


def test_a_legacy_slot_row_is_superseded_and_meta_is_kept(tmp_path):
    db = tmp_path / "l.db"
    sc = awm.Scope.parse("acme:bob:proj")
    st = awm.MemoryStore(db)  # what the pre-reconcile crystal wrote
    old = "deploy.runtime: podman quadlets under WSL"
    st.remember(sc, C.fact_key(old), old, kind="crystal", meta={"src": "compaction"})
    st.close()
    c = C.build_crystal(str(sc), db=db, embed=False)

    async def emb(texts):
        return [[1.0, 0.0] for _ in texts]

    c.embed = emb
    asyncio.run(c.crystallize("deploy.runtime: docker compose on the host"))
    got = asyncio.run(c.recall_facts("deploy.runtime", limit=10))
    assert "deploy.runtime: docker compose on the host" in got
    assert old not in got
    [row] = [m for m in c.store._store.recall(sc) if m.key == "deploy.runtime"]
    assert "vec" in row.meta and "ts" in row.meta
    c.store.close()


# -- agent ids, salt ------------------------------------------------------------
def test_agent_ids_are_the_hosts_ids_letter_for_letter():
    # Review 4 superseded the round-3 "no collapse on embedded letters" rule: the id
    # names checkpoints on disk and joins the host's fleet corpus, so it is the host's
    # rule exactly (collision included); see test_world_review4_regressions.py.
    assert wm_agent_id("AitherAgent") == "agent.aither"
    assert wm_agent_id("Atlas Agent") == "agent.atlas"
    assert wm_agent_id("Magenta") == "agent.ma"


def test_a_short_salt_file_is_replaced_not_used(tmp_path):
    root = tmp_path / "saltroot"
    root.mkdir()
    (root / ".redaction_salt").write_bytes(b"")
    salt = redaction_salt(str(root))
    assert len(salt) >= 16
    assert (root / ".redaction_salt").read_bytes() == salt


# -- awm backend ------------------------------------------------------------------
def test_awm_backend_works_from_a_thread_other_than_the_loader(tmp_path):
    b = AwmWorldModelBackend("atlas", root=str(tmp_path / "wm"), allowed_actions=["a"])
    t = threading.Thread(target=b.load)
    t.start()
    t.join()
    for _ in range(3):
        b.record([0.1] * DIM, "a", [0.2] * DIM, ok=True)
    assert b.stats()["n"] == 3 and "error" not in b.stats()
    b.close()


def test_awm_backend_never_overwrites_the_builtin_checkpoint(tmp_path):
    root = str(tmp_path / "wm")
    bw = BuiltinWorldModel("atlas", root=root)
    for i in range(20):
        bw.record([0.1] * DIM, "read_file", [0.2] * DIM, ok=True)
    bw.save()
    ckpt = Path(root) / f"{wm_agent_id('atlas')}.wm.json"
    before = json.loads(ckpt.read_text())
    a = AwmWorldModelBackend("atlas", root=root, allowed_actions=["read_file"])
    a.record([0.1] * DIM, "read_file", [0.2] * DIM, ok=True)
    a.save()
    a.close()
    assert json.loads(ckpt.read_text()) == before
    assert (Path(root) / f"{wm_agent_id('atlas')}.awm.json").exists()


def test_advise_prefers_this_states_evidence_over_a_distant_generalization(tmp_path):
    b = AwmWorldModelBackend("agent", root=str(tmp_path / "wm"),
                             allowed_actions=["here", "elsewhere"])
    here, far = [0.1] * DIM, [0.9] * DIM
    for i in range(10):
        b.record(here, "here", here, ok=(i != 0))
    for _ in range(2):
        b.record(far, "elsewhere", far, ok=True)
    assert b.advise(here, ["here", "elsewhere"])["order"] == ["here", "elsewhere"]
    b.close()


def test_wm_reset_and_train_see_the_awm_backend(tmp_path, capsys):
    root = tmp_path / "wm"
    b = AwmWorldModelBackend("Atlas", root=str(root), allowed_actions=["read_file"])
    for i in range(5):
        b.record([0.1 * i] * DIM, "read_file", [0.1 * i + 0.05] * DIM, ok=True)
    b.save()
    b.close()
    args = SimpleNamespace(agent="agent.atlas", yes=True)
    assert wmcli.cmd_wm_train(args) == 0
    assert "Total transitions:    5" in capsys.readouterr().out
    assert wmcli.cmd_wm_reset(args) == 0
    again = AwmWorldModelBackend("Atlas", root=str(root))
    again.load()
    assert again.stats()["n"] == 0
    again.close()
    assert not any(p.name.endswith((".wm.json", ".awm.json")) for p in root.iterdir())


# -- planner -------------------------------------------------------------------
def test_planner_says_no_model_when_the_search_met_unmodelled_actions(tmp_path):
    st = open_store(tmp_path / "u.db")
    env = KeyDoorGrid()
    ag = WorldModelAgent(st, SCOPE, env, horizon=20)
    ag.act("up")
    p = ag.plan({"pos.x": "1", "pos.y": "1"})
    assert p.verdict == NO_MODEL and "right" in p.note
    st.close()



def test_cem_planner_also_says_no_model_on_unmodelled_actions(tmp_path):
    # beam_max_actions=1 routes the same search through CEM (review 3: "_cem has the
    # same logic").
    st = open_store(tmp_path / "c.db")
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid(), horizon=20, beam_max_actions=1)
    ag.act("up")
    p = ag.plan({"pos.x": "1", "pos.y": "1"})
    assert p.verdict == NO_MODEL and "right" in p.note, (p.verdict, p.note)
    st.close()
