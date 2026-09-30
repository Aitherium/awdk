"""adk.storage_tools: suggest_deletion + the awstorage live-ids contract.

* awstorage present (a FAKE module; the brick's suggest API is not ours to build):
  the call reaches ``awstorage.suggest`` with ``suggested_by`` = the agent's OWN name,
  never a model argument, and the brick's verdict is returned verbatim;
* awstorage absent, or present without ``suggest`` -> an honest error, filed False;
* bad input never reaches the brick;
* the scratch dir is registered live in env + file while the agent lives, removed
  after, and only THIS process's lines are removed;
* a GENERIC leaf (scratchpad/tmp/...) is registered by FULL PATH, never bare;
* ids and agent names are validated; dead-process and TTL-expired lines are pruned;
  the file is written under a CROSS-PROCESS lock (N processes, no lost line).
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from adk import storage_tools as st


class _Tools:
    def __init__(self):
        self.fns = {}

    def register(self, fn, name=None, description="", **_):
        self.fns[name or fn.__name__] = fn


class _Agent:
    def __init__(self, name="demiurge", scratch=None):
        self.name = name
        self._tools = _Tools()
        if scratch is not None:
            self.scratch_dir = scratch


@pytest.fixture
def fake_awstorage(monkeypatch):
    calls = []
    mod = types.ModuleType("awstorage")

    def suggest(path, *, reason, suggested_by, action="quarantine", evidence=None,
                ttl_days=7.0, catalog=None):
        calls.append(dict(path=path, reason=reason, suggested_by=suggested_by,
                          action=action))
        return {"id": "s-1", "status": "pending-card", "why": "needs owner",
                "class": "build-temp", "size": 10, "checks": []}

    mod.suggest = suggest
    monkeypatch.setitem(sys.modules, "awstorage", mod)
    return calls


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    monkeypatch.setenv(st.LIVE_IDS_FILE_ENV, str(tmp_path / "live-ids.txt"))
    monkeypatch.delenv(st.LIVE_IDS_ENV, raising=False)
    monkeypatch.delenv(st.SCRATCH_ENV, raising=False)
    return tmp_path / "live-ids.txt"


def test_suggest_binds_the_agents_own_name(fake_awstorage, live_env):
    agent = _Agent("demiurge")
    assert st.register_storage_tools(agent) == 1
    tool = agent._tools.fns["suggest_deletion"]
    out = json.loads(tool("/tmp/build", "stale build output"))
    assert out["status"] == "pending-card" and out["id"] == "s-1"
    assert fake_awstorage == [{"path": "/tmp/build", "reason": "stale build output",
                               "suggested_by": "agent:demiurge", "action": "quarantine"}]


def test_the_model_cannot_choose_suggested_by(fake_awstorage, live_env):
    tool = st.make_suggest_deletion("demiurge")
    with pytest.raises(TypeError):
        tool("/x", "r", suggested_by="owner")  # not a parameter at all


@pytest.mark.parametrize("args", [("", "r"), ("/x", ""), ("/x", "r", "shred")])
def test_bad_input_never_reaches_the_brick(fake_awstorage, args):
    out = json.loads(st.make_suggest_deletion("a")(*args))
    assert "error" in out and fake_awstorage == []


def test_absent_awstorage_is_an_honest_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "awstorage", None)  # import -> ImportError
    out = json.loads(st.make_suggest_deletion("a")("/x", "r"))
    assert out["filed"] is False and "awstorage not installed" in out["error"]


def test_old_awstorage_without_suggest_is_an_honest_error(monkeypatch):
    mod = types.ModuleType("awstorage")
    mod.__version__ = "0.3.1"
    monkeypatch.setitem(sys.modules, "awstorage", mod)
    out = json.loads(st.make_suggest_deletion("a")("/x", "r"))
    assert out["filed"] is False and "no suggest()" in out["error"]


def test_brick_exception_is_returned_not_raised(monkeypatch):
    mod = types.ModuleType("awstorage")

    def boom(*a, **k):
        raise RuntimeError("catalog locked")

    mod.suggest = boom
    monkeypatch.setitem(sys.modules, "awstorage", mod)
    out = json.loads(st.make_suggest_deletion("a")("/x", "r"))
    assert out["filed"] is False and "catalog locked" in out["error"]


def test_generic_leaf_is_registered_by_full_path_plus_the_session_parent(tmp_path):
    scratch = tmp_path / "0a1b-sess" / "scratchpad"
    ids = st.scratch_live_ids(str(scratch))
    assert ids == [st.path_id(scratch), "0a1b-sess"]
    assert "scratchpad" not in ids                     # never the bare generic name
    for leaf in ("tmp", "TEMP", "work", "cache", "scratch"):
        got = st.scratch_live_ids(str(tmp_path / "tmp" / leaf))
        assert got == [st.path_id(tmp_path / "tmp" / leaf)]   # generic parent skipped too
    assert st.scratch_live_ids(str(tmp_path / "sess-42")) == ["sess-42"]


def test_agent_scratch_is_live_while_the_agent_lives(fake_awstorage, live_env, tmp_path):
    agent = _Agent("demiurge", scratch=str(tmp_path / "sess-9f" / "scratchpad"))
    st.register_storage_tools(agent)
    assert "sess-9f" in os.environ[st.LIVE_IDS_ENV].split(",")
    assert "scratchpad" not in os.environ[st.LIVE_IDS_ENV]   # path ids stay out of the env
    text = live_env.read_text(encoding="utf-8")
    assert "sess-9f" in text and f"pid={os.getpid()}" in text
    assert st.path_id(tmp_path / "sess-9f" / "scratchpad") in text
    # the awstorage reader contract: one id per line, `#` comments
    ids = {ln.split("#", 1)[0].strip() for ln in text.splitlines()}
    assert "sess-9f" in ids
    del agent
    gc.collect()
    assert "sess-9f" not in os.environ.get(st.LIVE_IDS_ENV, "")
    assert "sess-9f" not in live_env.read_text(encoding="utf-8")


def test_unregister_keeps_a_live_peers_line(live_env):
    peer = f"sess-x  # pid={os.getppid()} ts={int(time.time())} agent=peer\n"  # alive
    live_env.write_text(peer, encoding="utf-8")
    with st.live_session("/t/sess-x"):
        assert live_env.read_text(encoding="utf-8").count("sess-x") == 2
    assert live_env.read_text(encoding="utf-8") == peer


def test_env_scratch_is_used_when_the_agent_has_none(fake_awstorage, live_env, monkeypatch):
    monkeypatch.setenv(st.SCRATCH_ENV, "/t/sess-env")
    agent = _Agent("a")
    st.register_storage_tools(agent)
    assert "sess-env" in os.environ[st.LIVE_IDS_ENV]
    del agent
    gc.collect()


def test_real_awstorage_reader_sees_the_registration(live_env, tmp_path):
    sweep = pytest.importorskip("awstorage.sweep")
    with st.live_session("/t/sess-reader"):
        got = sweep.load_live_ids((), str(live_env), env={})
    assert "sess-reader" in got
    if not hasattr(sweep, "live_path_hit"):
        pytest.skip("awstorage < 0.4.1 has no path live ids")
    scratch = tmp_path / "sess-p" / "scratchpad"
    with st.live_session(str(scratch)):
        got = sweep.load_live_ids((), str(live_env), env={})
    assert sweep.live_path_hit(str(scratch), got)
    assert not sweep.live_path_hit(str(tmp_path / "other" / "scratchpad"), got)


# -- validation, pruning, cross-process lock ----------------------------------------------

@pytest.mark.parametrize("bad", ["a#b", "x\ny", "", "relative/path", "a b", "C:" + "x" * 300])
def test_invalid_ids_are_refused(live_env, bad):
    assert st.register_live_ids([bad]) == []
    assert not live_env.exists() or bad not in live_env.read_text(encoding="utf-8")


def test_an_invalid_agent_name_never_reaches_the_line(live_env):
    st.register_live_ids(["sess-ok"], owner="evil\n# pid=1")
    text = live_env.read_text(encoding="utf-8")
    assert text.count("\n") == 1 and "agent=invalid" in text
    st.unregister_live_ids(["sess-ok"])


def test_suggester_label_is_in_the_bricks_alphabet():
    assert st.suggester_label("demiurge") == "agent:demiurge"
    assert st.suggester_label("bad name;rm -rf") == "agent:bad-name-rm--rf"
    assert st.suggester_label("") == "agent:unknown"


def test_dead_and_expired_lines_are_pruned_manual_lines_kept(live_env, monkeypatch):
    now = int(time.time())
    live_env.write_text(
        f"ghost  # pid=999999 ts={now} agent=gone\n"               # dead process
        f"stale  # pid={os.getppid()} ts={now - 10**7} agent=old\n"   # past the TTL
        f"alive  # pid={os.getppid()} ts={now} agent=peer\n"
        "manual-entry\n", encoding="utf-8")
    monkeypatch.setattr(st, "_pid_alive", lambda pid: pid != 999999)
    st.register_live_ids(["sess-new"])
    text = live_env.read_text(encoding="utf-8")
    assert "ghost" not in text and "stale" not in text
    assert "alive" in text and "manual-entry" in text and "sess-new" in text
    st.unregister_live_ids(["sess-new"])


def test_a_held_lock_is_waited_for_then_reported(live_env, monkeypatch, caplog):
    monkeypatch.setattr(st, "LOCK_TIMEOUT_S", 0.2)
    live_env.parent.mkdir(parents=True, exist_ok=True)
    lock = live_env.with_name(live_env.name + ".lock")
    lock.write_text("", encoding="utf-8")
    with caplog.at_level("WARNING", logger="adk.storage_tools"):
        st.register_live_ids(["sess-locked"])
    assert not live_env.exists()                       # never wrote without the lock
    assert any("not updated" in r.getMessage() for r in caplog.records)
    old = time.time() - 3600
    os.utime(lock, (old, old))                         # a crashed holder: taken over
    st.register_live_ids(["sess-locked"])
    assert "sess-locked" in live_env.read_text(encoding="utf-8")
    st.unregister_live_ids(["sess-locked"])


def test_concurrent_processes_lose_no_line(live_env):
    root = Path(st.__file__).resolve().parents[1]
    code = ("import sys, time; from adk import storage_tools as st; "
            "st._pid_alive = lambda pid: True; "
            "st.register_live_ids([f'sess-{sys.argv[1]}-{i}' for i in range(5)]); "
            "time.sleep(0.5)")
    env = {**os.environ, "PYTHONPATH": str(root), st.LIVE_IDS_FILE_ENV: str(live_env)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(n)], env=env)
             for n in range(6)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    ids = {ln.split("#", 1)[0].strip()
           for ln in live_env.read_text(encoding="utf-8").splitlines()}
    assert {f"sess-{n}-{i}" for n in range(6) for i in range(5)} <= ids


def test_storage_category_is_accepted_and_opt_in():
    from adk import builtin_tools as bt

    assert bt.categories_from_env("storage") == ["storage"]
    assert "storage" not in bt.TOOL_CATEGORIES  # never registered by "all categories"
