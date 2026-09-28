"""Regressions for the fourth independent review (awdk side).

Each test failed before its fix (verified against the pre-fix tree).
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._world_envs import awm  # noqa: F401 -- skips the module without awm >= 0.5
from tests._world_envs import open_store

from adk import crystal as C
from adk import worldmodel as WM
from adk.commands import wm as wmcli
from adk.world import (
    GENERALIZED,
    INCOMPLETE,
    OK,
    PREDICTED,
    UNREACHABLE,
    AwmWorldModelBackend,
    WorldModelAgent,
)
from adk.world_code import COCHANGE, CodeWorld
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
                "AITHER_AGENT_WM_REDACTION_SALT", "AWM_AUTO_MIGRATE", "ADK_CRYSTAL_SCOPE",
                "ADK_CRYSTAL_DB", "ADK_CRYSTAL_NO_EMBED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_AGENT_WM_DIR", str(tmp_path / "wm"))
    clear_world_model_registry()
    yield
    clear_world_model_registry()


# -- F8: GENERALIZED needs more than one DISTINCT state -----------------------------
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


class _Oracle:
    name = "oracle"

    def __init__(self) -> None:
        self.asked = 0

    def predict(self, slots, action):
        self.asked += 1
        x = int(slots["world.ctr.x"])
        return {"world.ctr.x": str(x + 1 if action == "inc" else 0)}

    def observe(self, *a):
        pass


def test_agreement_seen_in_one_state_is_not_generalized(tmp_path):
    st = open_store(tmp_path / "c.db")
    learner = _Oracle()
    ag = WorldModelAgent(st, awm.Scope("acme", "t", "ctr"), _Counter(), learner=learner)
    for a in ("inc", "reset", "inc", "reset"):  # inc seen twice, both at x=0
        ag.act(a)
    asked = learner.asked
    p = ag.predict("inc", {"x": "5"})
    assert p.source == PREDICTED and p.delta == {"world.ctr.x": "6"}
    assert learner.asked > asked
    plan = ag.plan({"x": "6"}, state={"x": "5"})
    assert plan.verdict == OK and plan.actions == ("inc",) and plan.speculative
    # reset seen so far only at x=1 (twice); now also at x=2: two DISTINCT states agree
    ag.act("inc")
    ag.act("inc")
    ag.act("reset")  # reset now seen at x=1 and x=2, always -> 0
    q = ag.predict("reset", {"x": "9"})
    assert q.source == GENERALIZED and q.delta == {"world.ctr.x": "0"}
    st.close()


# -- F9: a pruned beam never says UNREACHABLE --------------------------------------
_DECOYS = [f"d{i}" for i in range(10)]
_P = "world.bits."


class _BitsOracle:
    name = "oracle"

    def predict(self, s, a):
        if a in _DECOYS:
            d = {_P + a: "1"}
        elif a == "zz_a":
            d = {_P + "k": str(min(4, int(s[_P + "k"]) + 1))}
        else:
            d = {_P + "g": "1"} if s[_P + "k"] == "4" else {}
        return SimpleNamespace(delta=d, confidence=1.0, engine="oracle")

    def observe(self, *a):
        pass


class _Bits:
    domain = "bits"

    def observe(self, e=None):
        return dict({d: "0" for d in _DECOYS}, k="0", g="0")

    def actions(self):
        return _DECOYS + ["zz_a", "zz_b"]

    def step(self, a):
        return None, 0.0, False, {}


def test_a_beam_that_pruned_the_path_says_incomplete_not_unreachable(tmp_path):
    st = open_store(tmp_path / "b.db")
    ag = WorldModelAgent(st, awm.Scope("acme", "t", "bits"), _Bits(), learner=_BitsOracle(),
                         horizon=5)
    ag.sense()
    p = ag.plan({"g": "1"})
    assert p.verdict == INCOMPLETE and p.verdict != UNREACHABLE
    assert "dropped" in p.note
    ag.beam_width = 100_000  # exhaustive: the path is found
    assert ag.plan({"g": "1"}).actions == ("zz_a",) * 4 + ("zz_b",)
    st.close()


# -- F7: CodeWorld(root=<subdir>) never predicts outside root ---------------------
def _git(top: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(top), *a], check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_codeworld_cochange_stays_inside_its_root(tmp_path):
    top = tmp_path / "repo"
    top.mkdir()
    _git(top, "init", "-q")
    _git(top, "config", "user.email", "t@t")
    _git(top, "config", "user.name", "t")
    for d in ("tenant_a", "tenant_b"):
        (top / d).mkdir()
    for i in range(2):
        (top / "tenant_a" / "app.py").write_text(f"x={i}\n")
        (top / "tenant_a" / "util.py").write_text(f"u={i}\n")
        (top / "tenant_b" / "secret_plan.py").write_text(f"y={i}\n")
        _git(top, "add", "-A")
        _git(top, "commit", "-qm", f"c{i}")
    cw = CodeWorld(str(top / "tenant_a"), localizer=None)
    p = cw.predict_cochange(str(top / "tenant_a" / "app.py"))
    assert p.source == COCHANGE
    names = [f for f, _ in p.files]
    assert names == ["tenant_a/util.py"]
    assert not any(f.startswith("tenant_b/") for f in names)


# -- F10: a short env salt is refused ----------------------------------------------
def test_a_short_env_salt_is_refused_and_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_AGENT_WM_REDACTION_SALT", "x")
    before = WM.SALT_TELEMETRY["env_rejected"]
    salt = redaction_salt(str(tmp_path / "saltroot"))
    assert salt != b"x" and len(salt) >= WM.SALT_MIN_BYTES
    assert WM.SALT_TELEMETRY["env_rejected"] == before + 1
    token = hmac.new(salt, b"acme_payroll_export", hashlib.sha256).hexdigest()[:12]
    assert token != hmac.new(b"x", b"acme_payroll_export", hashlib.sha256).hexdigest()[:12]
    monkeypatch.setenv("AITHER_AGENT_WM_REDACTION_SALT", "k" * 16)
    assert redaction_salt(str(tmp_path / "saltroot")) == b"k" * 16


# -- F16: ids are the host's ids; old checkpoints are read -------------------------
def _host_agent_id_of(agent_name):
    """lib/cognitive/agent_action_feed.py agent_id_of, verbatim."""
    slug = (agent_name or "unknown").lower().replace("agent", "").replace(" ", "-").strip("-")
    return "agent." + (slug or "unknown")


@pytest.mark.parametrize("name", ["AitherAgent", "Atlas Agent", "iris", "Reagent", "Magenta",
                                  "myagent", "Agents", "Management", "code_agent", "AGENT_X",
                                  "agent.aither", "SubAgentRunner", "Pageant", "", None])
def test_wm_agent_id_is_the_hosts_rule(name):
    assert wm_agent_id(name) == _host_agent_id_of(name)


def test_a_checkpoint_written_under_the_host_id_is_read_back(tmp_path):
    root = str(tmp_path / "wm")
    a = BuiltinWorldModel("Magenta", root=root)
    for _ in range(3):
        a.record([0.1] * DIM, "grep", [0.2] * DIM, ok=True)
    a.save()
    assert (Path(root) / "agent.ma.wm.json").exists()
    b = BuiltinWorldModel("Magenta", root=root)
    b.load()
    assert b.stats()["n"] == 3


# -- F17: a name with a space or an accent still binds a crystal --------------------
@pytest.mark.parametrize("name", ["Atlas Agent", "Émile", "bob"])
def test_crystal_binds_names_that_are_one_literal_segment(tmp_path, monkeypatch, name):
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "acme:bob:{agent}")
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "c.db"))
    c = C.crystal_from_env(name)
    assert c is not None
    assert str(c.store._scope) == f"acme:bob:{name}"
    c.store.close()


@pytest.mark.parametrize("name", ["*", "a:b", "x\ny"])
def test_crystal_still_refuses_wildcard_separator_and_control(tmp_path, monkeypatch, name):
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "acme:bob:{agent}")
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "c.db"))
    assert C.crystal_from_env(name) is None
    assert name in C.BIND_TELEMETRY["refused"]


# -- F18: `adk wm` reads the configured backend, from its table ----------------------
def test_wm_inspect_and_status_read_the_awm_backends_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_AGENT_WM_BACKEND", "awm")
    root = str(tmp_path / "wm")
    b = AwmWorldModelBackend("Atlas Agent", root=root, allowed_actions=["read_file", "grep"])
    b.load()
    for _ in range(5):
        b.record([0.1] * DIM, "read_file", [0.2] * DIM, ok=True)
    for _ in range(3):
        b.record([0.1] * DIM, "grep", [0.3] * DIM, ok=True)
    b.save()
    live = b.stats()
    b.close()
    assert live["n"] == 8 and live["actions"] == 2
    # a stale builtin checkpoint for the same agent must not shadow the awm one
    bi = BuiltinWorldModel("Atlas Agent", root=root)
    bi.record([0.1] * DIM, "old_tool", [0.2] * DIM, ok=True)
    bi.save()
    rc = wmcli.cmd_wm_inspect(SimpleNamespace(agent="agent.atlas"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Backend:            awm" in out
    assert "Known Actions:      2" in out
    assert "read_file" in out and "grep" in out and "old_tool" not in out
    assert wmcli.cmd_wm_status(SimpleNamespace()) == 0
    status = capsys.readouterr().out
    awm_row = [ln for ln in status.splitlines() if "agent.atlas" in ln and " awm " in ln]
    assert awm_row and awm_row[0].split()[3:5] == ["8", "2"]
