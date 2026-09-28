"""W1: AwmWorldModelBackend -- the agent loop's world model on the awm transition table."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5

from adk import worldmodel as wmmod
from adk.worldmodel import redaction_salt, redaction_token
from adk.world import AwmWorldModelBackend, register_awm_backend
from adk.world_adapters import FactoredLearner, train_from_transitions
from adk.worldmodel import (
    STATE_DIMS,
    BuiltinWorldModel,
    clear_world_model_registry,
    get_world_model,
    registered_backend_name,
)

DIM = len(STATE_DIMS)
S0 = [0.1] * DIM
S1 = [0.2] * DIM
S_OTHER = [0.9] * DIM


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    for var in ("AITHER_AGENT_WM", "AITHER_AGENT_WM_BACKEND", "AITHER_AGENT_WM_AWM_DB",
                "AITHER_AGENT_WM_AWM_SCOPE", "AITHER_AGENT_WM_ALLOWED_ACTIONS",
                "AWM_AUTO_MIGRATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(tmp_path / "wm"))
    clear_world_model_registry()
    yield
    clear_world_model_registry()


def _backend(tmp_path: Path, **kw) -> AwmWorldModelBackend:
    b = AwmWorldModelBackend("tester", root=str(tmp_path / "wm"), **kw)
    b.load()
    assert b.degraded is None, b.degraded
    return b


def _db_text(path: Path) -> str:
    """Every text value in every table, for leak checks."""
    con = sqlite3.connect(str(path))
    try:
        out = []
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in con.execute(f"SELECT * FROM {name}"):
                out.extend(str(v) for v in row)
        return "\n".join(out)
    finally:
        con.close()


# -- selection --------------------------------------------------------------
def test_default_backend_is_unchanged(monkeypatch):
    """Unset = the pre-existing path (host autoload, else builtin) -- never awm."""
    monkeypatch.setenv("AITHER_AGENT_WM", "learn")
    wm = get_world_model("DefaultAgent")
    assert not isinstance(wm, AwmWorldModelBackend)
    assert registered_backend_name() != "awm"
    clear_world_model_registry()
    monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "")  # autoload disabled -> builtin
    wm = get_world_model("DefaultAgent2")
    assert type(wm) is BuiltinWorldModel
    assert registered_backend_name() is None


def test_env_selects_the_awm_backend(monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_WM", "learn")
    monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "awm")
    wm = get_world_model("SelectAgent")
    assert isinstance(wm, AwmWorldModelBackend)
    assert registered_backend_name() == "awm"
    assert wm.stats()["backend"] == "awm" and wm.degraded is None
    wm.close()


def test_off_mode_still_creates_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "awm")
    assert get_world_model("OffAgent") is None
    assert not (tmp_path / "wm").exists()


# -- record / advise ----------------------------------------------------------
def test_record_then_advise_ranks_by_tabular_success(tmp_path):
    b = _backend(tmp_path, allowed_actions=["kb_search", "kb_chat"])
    for _ in range(3):
        b.record(S0, "kb_search", S1, ok=True)
    b.record(S0, "kb_chat", S1, ok=False)
    b.record(S0, "kb_chat", S1, ok=True)
    adv = b.advise(S0, ["kb_chat", "kb_search", "never_seen"])
    # an untried tool sits at the neutral prior: after a sure success, level with
    # (and after, by index) a coin-flip tool
    assert adv["order"] == ["kb_search", "kb_chat", "never_seen"]
    assert adv["unknown"] == ["never_seen"]
    assert adv["scores"] == {"kb_search": 1.0, "kb_chat": 0.5}
    assert adv["sources"] == {"kb_search": "RECALLED", "kb_chat": "RECALLED"}
    assert adv["confidence"]["kb_search"] == pytest.approx(3 / 4)
    assert "never_seen" not in adv["scores"]
    b.close()


def test_advise_abstains_when_nothing_is_known(tmp_path):
    b = _backend(tmp_path)
    assert b.advise(S0, ["a", "b"]) is None
    b.record(S0, "a", S1, ok=True)
    assert b.advise(S0, ["zzz"]) is None
    assert b.advise(S0, []) is None
    assert b.advise("not a vector", ["a"]) is None
    b.close()


def test_unseen_state_answers_generalized_discounted(tmp_path):
    b = _backend(tmp_path, allowed_actions=["a"])
    b.record(S0, "a", S1, ok=True)
    # ONE observation elsewhere is not a generalization: abstain for this tool
    assert b.advise(S_OTHER, ["a"]) is None
    b.record(S0, "a", S1, ok=True)
    adv = b.advise(S_OTHER, ["a"])
    assert adv["sources"] == {"a": "GENERALIZED"}
    assert adv["confidence"]["a"] == pytest.approx(0.5 * 2 / 3)
    b.close()


def test_advise_never_steers_a_known_failure_ahead_of_an_untried_tool(tmp_path):
    """Round-1 review p3: one failure in a distant state ranked 'flaky' and left
    'fresh' out of the order, so MODE_STEER ran the failing tool first."""
    b = _backend(tmp_path, allowed_actions=["flaky", "fresh"])
    far, here = [0.9] * 8, [0.1] * 8
    b.record(far, "flaky", far, ok=False)
    assert b.advise(here, ["fresh", "flaky"]) is None          # knows too little
    b.record(far, "flaky", far, ok=False)
    adv = b.advise(here, ["flaky", "fresh"])
    assert adv["order"] == ["fresh", "flaky"]                  # untried before failing
    assert adv["unknown"] == ["fresh"] and adv["scores"] == {"flaky": 0.0}
    b.close()


def test_redaction_is_keyed_per_install_and_resists_a_dictionary(tmp_path):
    """Round-1 review p4: unsalted sha256[:12] of 'acme_payroll_export' was reversed."""
    secret_tool = "acme_payroll_export"
    b = _backend(tmp_path)
    b.record(S0, secret_tool, S1, ok=True)
    b.close()
    text = _db_text(b.db_path)
    dictionary = ["read_file", "write_file", "bash", secret_tool, "web_search"]
    unsalted = {"redacted:" + hashlib.sha256(w.encode()).hexdigest()[:12] for w in dictionary}
    assert not any(tok in text for tok in unsalted)
    salt_file = tmp_path / "wm" / ".redaction_salt"
    assert salt_file.is_file() and len(salt_file.read_bytes()) == 32
    # stable within the install (a reopened backend maps the name to the same row)...
    again = _backend(tmp_path)
    assert again.advise(S0, [secret_tool])["sources"] == {secret_tool: "RECALLED"}
    again.close()
    # ...and different across installs
    other = tmp_path / "other"
    mine = redaction_token(secret_tool, redaction_salt(str(tmp_path / "wm")))
    assert redaction_token(secret_tool, redaction_salt(str(other))) != mine


def test_stats_reports_transitions_and_surprise(tmp_path):
    b = _backend(tmp_path)
    b.record(S0, "a", S1, ok=True)
    b.record(S0, "a", S1, ok=True)    # predicted RECALLED, matches -> surprise 0
    b.record(S0, "a", S1, ok=False)   # predicted ok, got failure -> surprise > 0
    st = b.stats()
    assert st["transitions"] == 3 and st["n"] == 3 and st["actions"] == 1
    assert st["surprise"]["novel"] == 1 and st["surprise"]["count"] == 2
    assert st["surprise"]["max"] > 0.0
    assert st["degraded"] is None
    b.close()


def test_persists_across_instances(tmp_path):
    b = _backend(tmp_path, allowed_actions=["a"])
    b.record(S0, "a", S1, ok=True)
    b.save()
    b.close()
    again = _backend(tmp_path, allowed_actions=["a"])
    assert again.stats()["transitions"] == 1
    assert again.advise(S0, ["a"])["sources"] == {"a": "RECALLED"}
    ckpt = json.loads((tmp_path / "wm" / f"{again.agent_id}.awm.json").read_text())
    assert ckpt["backend"] == "awm" and ckpt["n"] == 1
    again.close()


# -- redaction (the SAME rule the federation export uses) ---------------------
def test_persisted_action_text_is_redacted(tmp_path):
    leak = "read_file:/clients/Acme/2026-merger-terms.docx"
    b = _backend(tmp_path, allowed_actions=["kb_search"])
    b.record(S0, leak, S1, ok=True)
    b.record(S0, "kb_search", S1, ok=True)
    b.close()
    text = _db_text(b.db_path)
    for bad in ("Acme", "merger", "/clients/", "read_file"):
        assert bad not in text
    assert "kb_search" in text
    token = redaction_token(leak, redaction_salt(str(tmp_path / "wm")))
    assert token in text
    # the unsalted digest a dictionary attack would compute is NOT what landed
    assert "redacted:" + hashlib.sha256(leak.encode()).hexdigest()[:12] not in text
    # ...and advise still maps the ORIGINAL candidate name back.
    again = _backend(tmp_path, allowed_actions=["kb_search"])
    assert set(again.advise(S0, [leak, "kb_search"])["order"]) == {leak, "kb_search"}
    again.close()


def test_no_whitelist_hashes_every_action(tmp_path):
    b = _backend(tmp_path)
    b.record(S0, "kb_search", S1, ok=True)
    b.close()
    assert "kb_search" not in _db_text(b.db_path)


def test_out_of_bounds_vectors_are_refused(tmp_path):
    b = _backend(tmp_path, allowed_actions=["a"])
    b.record([0.0] * DIM, "a", [42.0] * DIM, ok=True)
    b.record([-3.5] * DIM, "a", [0.5] * DIM, ok=True)
    b.record([0.1] * (DIM - 1), "a", S1, ok=True)
    assert b.stats()["transitions"] == 0 and b.stats()["rejected"] == 3
    b.close()


def test_the_backend_survives_the_agent_loop_call_shape(tmp_path):
    """observe(context) -> record(prev, tool, after, ok) -> bootstrap -> advise, as agent.py."""
    b = _backend(tmp_path, allowed_actions=["kb_search", "kb_chat"])
    prev = b.observe({"tools": 2, "success": 1.0})
    after = b.observe({"tools": 3, "success": 1.0, "errors": 0.0})
    for tool in ("kb_search", "kb_chat [error]"):
        b.record(prev, tool, after, ok="[" not in tool)
    assert b.bootstrap() == "cold"
    adv = b.advise(prev, ["kb_chat [error]", "kb_search"])
    assert adv["order"][0] == "kb_search"
    b.close()


# -- compat: an older awm file is never migrated from here --------------------
_V1 = """
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
  key TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'fact',
  created REAL NOT NULL, updated REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
  meta TEXT, UNIQUE(scope, key));
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (1);
INSERT INTO memories(scope,key,value,created,updated) VALUES ('acme:*:*','k','v',1,1);
"""


def test_compat_file_degrades_honestly_and_is_not_touched(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    con = sqlite3.connect(str(db))
    con.executescript(_V1)
    con.commit()
    con.close()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    monkeypatch.setenv("AWM_AUTO_MIGRATE", "1")  # even with the opt-in set
    b = AwmWorldModelBackend("tester", root=str(tmp_path / "wm"), db=db)
    b.record(S0, "a", S1, ok=True)
    assert b.advise(S0, ["a"]) is None
    st = b.stats()
    assert st["degraded"] and "awm migrate" in st["degraded"] and "v1" in st["degraded"]
    assert st["transitions"] == 0
    b.close()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert list(tmp_path.glob("old.db*")) == [db]  # no backup: nothing migrated


def test_register_awm_backend_is_the_registry_hook():
    register_awm_backend()
    assert registered_backend_name() == "awm"
    assert wmmod._registry_factory is not None


def test_offline_training_rebuilds_states_from_the_backend(tmp_path):
    b = _backend(tmp_path, allowed_actions=["a", "b"])
    b.record(S0, "a", S1, ok=True)
    b.record(S1, "b", S0, ok=False)
    learner = FactoredLearner()
    rep = train_from_transitions(b, learner)
    assert rep["fed"] == 2 and rep["skipped"] == {"digest_mismatch": 0, "unresolved": 0}
    assert learner.n == 2
    b.close()
