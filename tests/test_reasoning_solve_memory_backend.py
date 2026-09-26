"""TypedMemoryBackend: the h30 ``MemoryBackend`` contract over adk typed memory.

* round-trip, typed records and scope keys, no network (Spirit + fleet sync off,
  sockets blocked);
* merge semantics identical to h30's File backend: the h30 skill-library and
  PRISM persistence tests ported and run against BOTH backends, plus one
  differential op sequence;
* semantic memory: refuted-hypothesis fingerprints survive a restart (store level
  and through ``solve()``);
* procedural memory: run A saves a skill, run B (a new backend on the same store)
  loads and installs it; another domain does not see it.

``SOLVE_MEMORY_BREAK=1`` makes ``update`` forget the stored value and skips
loading refutations; the meta-test below proves the merge, cross-run and restart
tests then fail.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import sqlite3
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve import Budget, LoopConfig, solve  # noqa: E402
from adk.reasoning.solve._vendor.interfaces import FileMemoryBackend  # noqa: E402
from adk.reasoning.solve._vendor.memory import (  # noqa: E402
    Episodic,
    HypothesisStore,
    SkillLibrary,
    top_level_defs,
)
from adk.reasoning.solve._vendor.prism import Prism  # noqa: E402
from adk.reasoning.solve.envs.toy import Counter1D  # noqa: E402
from adk.reasoning.solve.hypotheses import (  # noqa: E402
    load_hypotheses,
    load_refuted,
    save_hypotheses,
)
from adk.reasoning.solve.memory import TypedMemoryBackend  # noqa: E402


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setenv("AITHER_SPIRIT_BRIDGE", "false")
    monkeypatch.setenv("AITHER_FLEET_SYNC", "false")
    monkeypatch.delenv("AITHER_CLOUD_MODE", raising=False)
    attempts = []

    def refuse(*a, **k):
        attempts.append(a[1:2] or a)
        raise OSError("network blocked by test")

    async def refuse_async(*a, **k):
        refuse(*a, **k)

    # every network path adk.memory has (httpx), plus DNS and stdlib dialing.
    # socket.connect itself stays: asyncio's self-pipe on Windows is a loopback pair.
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_async)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


@pytest.fixture
def typed_factory(tmp_path, no_network):
    """``make(**scope)`` -> a NEW backend (a fresh Memory + TypedMemory, as after a
    process restart) over one SQLite file."""
    from adk.memory import Memory
    from adk.typed_memory import TypedMemory

    db = str(tmp_path / "solve.db")
    made = []

    def make(**scope):
        be = TypedMemoryBackend(TypedMemory(Memory(db_path=db, agent_name="solve")), **scope)
        made.append(be)
        return be

    make.db = db
    yield make
    for be in made:
        be.close()
    assert no_network == [], "a typed-memory call touched the network: %r" % no_network


@pytest.fixture(params=["file", "typed"])
def backend_factory(request, tmp_path):
    """Both backends behind one ``make()`` so the h30 tests run against each."""
    if request.param == "file":
        d = str(tmp_path / "file")
        return lambda: FileMemoryBackend(d)
    tf = request.getfixturevalue("typed_factory")
    return lambda: tf(domain="d", game="g", run="r")


# ---------------------------------------------------------------- round trip
def test_round_trip_is_json_typed_and_scoped(typed_factory):
    be = typed_factory(domain="arc", game="ls20", run="r1")
    assert be.get("procedural", "skills") is None
    assert be.get("procedural", "skills", {"d": 1}) == {"d": 1}
    value = {"a": [1, 2, {"b": None}], "s": "x/y", "f": 1.5, "t": True}
    be.put("procedural", "skills", value)
    got = be.get("procedural", "skills")
    assert got == value
    got["a"].append(99)  # a returned value is a copy
    assert be.get("procedural", "skills") == value
    be.put("procedural", "skills", None)  # a stored null is a value, not "missing"
    assert be.get("procedural", "skills", "default") is None
    assert be.update("notes", "k", lambda old: (old or 0) + 1) == 1
    assert be.update("notes", "k", lambda old: (old or 0) + 1) == 2
    with pytest.raises(TypeError):
        be.put("notes", "k", {"bad": object()})
    assert be.get("notes", "k") == 2

    rows = {
        r[0]: (r[1], r[2], json.loads(r[3]))
        for r in sqlite3.connect(typed_factory.db).execute(
            "SELECT key, value, category, metadata FROM kv_store"
        )
    }
    skills = rows["solve/arc/*/*/procedural/skills"]
    assert skills[1] == "procedure" and skills[2]["role"] == "procedure"
    assert skills[2]["tier"] == "persistent" and skills[2]["reinforcement_count"] == 1
    assert skills[2]["solve_scope"]["level"] == "domain"
    notes = rows["solve/arc/ls20/r1/notes/k"]  # unscoped namespaces are per run
    assert json.loads(notes[0]) == 2 and notes[1] == "fact"
    assert be.record_id("hypotheses", "store") == "solve/arc/ls20/*/hypotheses/store"


def test_scopes_isolate_runs_games_and_domains(typed_factory):
    a = typed_factory(domain="arc", game="g1", run="A")
    a.put("procedural", "skills", {"s": 1})
    a.put("hypotheses", "store", {"h": 1})
    a.put("scratch", "k", "a-only")
    same_game = typed_factory(domain="arc", game="g1", run="B")
    other_game = typed_factory(domain="arc", game="g2", run="B")
    other_domain = typed_factory(domain="toy", game="g1", run="A")
    assert same_game.get("procedural", "skills") == {"s": 1}
    assert same_game.get("hypotheses", "store") == {"h": 1}
    assert same_game.get("scratch", "k") is None
    assert other_game.get("procedural", "skills") == {"s": 1}
    assert other_game.get("hypotheses", "store") is None
    assert other_domain.get("procedural", "skills") is None
    with pytest.raises(ValueError):
        typed_factory(scopes={"procedural": "galaxy"})


def test_refuses_a_store_that_would_sync_over_the_network(tmp_path, monkeypatch):
    from adk.memory import Memory
    from adk.typed_memory import TypedMemory

    monkeypatch.setenv("AITHER_SPIRIT_BRIDGE", "true")
    monkeypatch.setenv("AITHER_FLEET_SYNC", "false")
    tm = TypedMemory(Memory(db_path=str(tmp_path / "n.db")))
    with pytest.raises(ValueError, match="AITHER_SPIRIT_BRIDGE"):
        TypedMemoryBackend(tm)
    TypedMemoryBackend(tm, allow_network=True).close()
    with pytest.raises(TypeError):
        TypedMemoryBackend(object())


# ---------------------------------------------------------------- File-backend parity (h30 tests)
CODE = '''
def find_player(f):
    """Locate the colour-3 player."""
    ys, xs = np.nonzero(f == 3)
    return int(ys[0]), int(xs[0])
'''


def _frame(pr, pc, n=16):
    f = np.zeros((n, n), np.int8)
    f[pr, pc] = 3
    return f


def test_skill_library_persists_reloads_and_counts_cross_game_reuse(backend_factory):
    lib = SkillLibrary(backend_factory())
    defs = top_level_defs(CODE)["find_player"]
    assert (
        lib.record_success("find_player", defs["source"], defs["doc"], defs["sig"], "gameA") is True
    )
    lib.save()
    assert backend_factory().get("procedural", "skills")["find_player"]["created_game"] == "gameA"

    lib2 = SkillLibrary(backend_factory())
    assert "find_player" in lib2.loaded_at_start
    ns = {"np": np}

    def define(src, filename):
        exec(compile(src, filename, "exec"), ns)  # noqa: S102 - test namespace
        return None

    assert lib2.install(define) == ["find_player"]
    assert ns["find_player"](_frame(2, 7)) == (2, 7)
    assert lib2.record_use("find_player", "gameB", ok=True) is True
    assert lib2.record_use("find_player", "gameA", ok=True) is False
    lib2.save()
    s = SkillLibrary(backend_factory()).skills["find_player"]
    assert s["reused_in"] == ["gameB"] and s["reuse_successes"] == 1 and s["uses"] == 2


def test_skill_library_merges_concurrent_writers(backend_factory):
    a, b = SkillLibrary(backend_factory()), SkillLibrary(backend_factory())
    a.record_success("fa", "def fa():\n    return 1\n", "", "fa()", "g1")
    a.save()
    b.record_success("fb", "def fb():\n    return 2\n", "", "fb()", "g2")
    b.save()  # must not drop a's skill
    assert set(SkillLibrary(backend_factory()).skills) == {"fa", "fb"}


def test_prism_scores_persist_so_a_later_game_starts_with_the_best(backend_factory):
    p = Prism(backend_factory())
    p.record("goal_first", actions=40, levels=2, verified=1)
    p.record("rule_first", actions=100, levels=0, verified=1)
    p.save()
    later = Prism(backend_factory())
    assert later.active.id == "goal_first"
    assert later.score("goal_first") > later.score("rule_first")
    later.record("goal_first", actions=10, levels=1, verified=0)
    later.save()
    data = backend_factory().get("procedural", "prism")
    assert data["goal_first"]["levels"] == 3 and data["goal_first"]["actions"] == 50


def test_same_op_sequence_gives_the_same_results_as_the_file_backend(tmp_path, typed_factory):
    fb, tb = FileMemoryBackend(str(tmp_path / "f")), typed_factory(domain="d", game="g", run="r")

    def ops(be):
        out = [be.get("procedural", "skills", "dflt"), be.get("x", "y")]
        be.put("procedural", "skills", {"a": 1})
        out.append(be.update("procedural", "skills", lambda d: dict(d or {}, b=2)))
        out.append(be.update("procedural", "fresh", lambda d: ["was", d]))
        be.put("x", "y", [1, "two", None])
        out.append(be.update("x", "y", lambda d: d + [len(d)]))
        be.put("x", "z", None)
        out += [be.get("x", "z", "dflt"), be.get("procedural", "skills"), be.get("x", "y")]
        return out

    assert ops(tb) == ops(fb)


# ---------------------------------------------------------------- semantic memory across a restart
def _moves(ep):
    d = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
    r, c = 5, 5
    for a in (4, 4, 2, 3, 1, 1):
        before = _frame(r, c)
        r, c = r + d[a][0], c + d[a][1]
        ep.add(0, (a, -1, -1), before, _frame(r, c))


def predict_wrong(frame, action):
    return frame.copy()  # "nothing ever changes"


def test_refuted_fingerprints_survive_a_restart(typed_factory):
    src = inspect.getsource(predict_wrong)
    ep = Episodic()
    _moves(ep)
    run_a = HypothesisStore()
    assert run_a.propose("predict_wrong", predict_wrong, src, "predict", ep)["status"] == "refuted"
    saved = save_hypotheses(run_a, typed_factory(domain="d", game="g", run="A"), "A")
    (rec,) = saved["refuted"].values()
    assert rec["source"] == src and rec["reason"].startswith("at t#0") and rec["episode"] == "A"

    # restart: a new process state, a new store, and a history too short to contradict it
    renamed = src.replace("predict_wrong", "still_wrong")
    control = HypothesisStore()
    assert (
        control.propose("still_wrong", predict_wrong, renamed, "predict", Episodic())["status"]
        != "refuted"
    )
    run_b = HypothesisStore()
    assert load_refuted(run_b, typed_factory(domain="d", game="g", run="B")) == 1
    rep = run_b.propose("still_wrong", predict_wrong, renamed, "predict", Episodic())
    assert rep["status"] == "refuted" and "predict_wrong" in rep["reason"]
    # a different game of the domain does not inherit this game's refutations
    assert load_refuted(HypothesisStore(), typed_factory(domain="d", game="other", run="B")) == 0


def test_refutation_is_never_undone_by_a_later_verified_save(typed_factory):
    be = typed_factory(domain="d", game="g", run="A")
    ep = Episodic()
    _moves(ep)
    src = inspect.getsource(predict_wrong)
    bad = HypothesisStore()
    bad.propose("predict_wrong", predict_wrong, src, "predict", ep)
    save_hypotheses(bad, be)
    ok = HypothesisStore()  # the same code admitted on an empty history elsewhere
    ok.propose("predict_wrong", predict_wrong, src, "predict", Episodic())
    assert ok.active
    merged = save_hypotheses(ok, be)
    assert len(merged["refuted"]) == 1 and merged["verified"] == {}
    assert load_hypotheses(be) == merged


RUN_A = """Act, then write down a rule.
```python
def wrong(state, action):
    return state.copy()
act(1)
act(1)
print(hypothesize(wrong, "predict"))
```"""

RUN_B = """Try the old idea first.
```python
def wrong_again(state, action):
    return state.copy()
print(hypothesize(wrong_again, "predict"))
```"""


class _Scripted:
    def __init__(self, reply):
        self.name = self.model = "scripted"
        self.reply = reply

    async def generate(self, messages, **kw):
        from adk.core.model import ModelResponse

        return ModelResponse(
            text=self.reply,
            model="scripted",
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 5},
        )


def _solve(reply, memory):
    cfg = LoopConfig(sase=False, prism=False, budget=Budget(max_llm_calls=1, max_actions=2))
    return asyncio.run(
        solve(Counter1D(target=50, levels=1), _Scripted(reply), config=cfg, memory=memory)
    )


def test_solve_persists_refutations_and_a_restarted_run_refuses_them(typed_factory):
    a = _solve(RUN_A, typed_factory(domain="toy", game="c1d", run="A"))
    assert [h.status for h in a.hypotheses if h.name == "wrong"] == ["refuted"]

    b = _solve(RUN_B, typed_factory(domain="toy", game="c1d", run="B"))
    assert b.stats["hyps_refuted_loaded"] == 1
    assert [h for h in b.hypotheses if h.name == "wrong_again"] == []

    cold = _solve(RUN_B, typed_factory(domain="toy", game="fresh", run="C"))  # control
    assert [h.status for h in cold.hypotheses if h.name == "wrong_again"] == ["pending"]


# ---------------------------------------------------------------- procedural memory across runs
def test_run_b_loads_and_installs_the_skill_run_a_saved(typed_factory):
    lib_a = SkillLibrary(typed_factory(domain="arc", game="g1", run="A"))
    defs = top_level_defs(CODE)["find_player"]
    lib_a.record_success("find_player", defs["source"], defs["doc"], defs["sig"], "g1")
    lib_a.save()
    Prism(typed_factory(domain="arc", game="g1", run="A")).save()

    lib_b = SkillLibrary(typed_factory(domain="arc", game="g2", run="B"))
    assert lib_b.loaded_at_start == {"find_player"}
    ns = {"np": np}
    assert lib_b.install(lambda s, fn: exec(compile(s, fn, "exec"), ns)) == ["find_player"]  # noqa: S102
    assert ns["find_player"](_frame(3, 4)) == (3, 4)
    assert lib_b.record_use("find_player", "g2", ok=True) is True  # cross-run, cross-game reuse
    lib_b.save()
    s = SkillLibrary(typed_factory(domain="arc", game="g3", run="C")).skills["find_player"]
    assert s["reused_in"] == ["g2"] and s["reuse_successes"] == 1
    assert SkillLibrary(typed_factory(domain="other", run="D")).skills == {}


# ---------------------------------------------------------------- the break arm
BROKEN_MUST_FAIL = (
    "test_round_trip_is_json_typed_and_scoped",
    "test_scopes_isolate_runs_games_and_domains",
    "test_skill_library_persists_reloads_and_counts_cross_game_reuse[typed]",
    "test_skill_library_merges_concurrent_writers[typed]",
    "test_prism_scores_persist_so_a_later_game_starts_with_the_best[typed]",
    "test_same_op_sequence_gives_the_same_results_as_the_file_backend",
    "test_refuted_fingerprints_survive_a_restart",
    "test_refutation_is_never_undone_by_a_later_verified_save",
    "test_solve_persists_refutations_and_a_restarted_run_refuses_them",
    "test_run_b_loads_and_installs_the_skill_run_a_saved",
)


@pytest.mark.skipif(os.environ.get("SOLVE_MEMORY_BREAK") == "1", reason="already the broken run")
def test_break_arm_fails_every_persistence_test():
    """With the backend made forgetful, every persistence test must fail (the
    [file] parity runs are the unaffected reference and must still pass)."""
    env = dict(os.environ, SOLVE_MEMORY_BREAK="1")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rfE",
            "-p",
            "no:cacheprovider",
            __file__,
            "-k",
            "not break_arm",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    failed = {
        line.split("::", 1)[1].split(" ")[0]
        for line in out.splitlines()
        if line.startswith("FAILED ") and "::" in line
    }
    assert failed == set(BROKEN_MUST_FAIL), out
