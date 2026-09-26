"""h30 memory components (a)-(d): bounded working memory, episodic store,
hypotheses-as-code under replay, and the on-disk skill library.

Parity port of h30 ``agent/tests/test_h30_memory.py`` at c076671233: the same tests, run
against the vendored core (only the import paths changed).
"""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve._vendor.interfaces import FileMemoryBackend  # noqa: E402
from adk.reasoning.solve._vendor.memory import (  # noqa: E402
    Episodic,
    HypothesisStore,
    SkillLibrary,
    TurnGist,
    WorkingMemory,
    called_names,
    est_tokens,
    top_level_defs,
)


# ---------------------------------------------------------------- helpers
def _frame(pr: int, pc: int, n: int = 16) -> np.ndarray:
    f = np.zeros((n, n), np.int8)
    f[pr, pc] = 3
    return f


def _moves(ep: Episodic, start=(5, 5), acts=(4, 4, 2, 3, 1, 1), level: int = 0):
    """Colour-3 dot moving by one cell: 1 up, 2 down, 3 left, 4 right."""
    d = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
    r, c = start
    for a in acts:
        before = _frame(r, c)
        r, c = r + d[a][0], c + d[a][1]
        ep.add(level, (a, -1, -1), before, _frame(r, c))
    return r, c


def predict_move(frame, action):
    d = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}
    if action[0] not in d:
        return None
    ys, xs = np.nonzero(frame == 3)
    out = frame.copy()
    out[ys[0], xs[0]] = 0
    out[ys[0] + d[action[0]][0], xs[0] + d[action[0]][1]] = 3
    return out


def predict_wrong(frame, action):
    return frame.copy()  # "nothing ever changes"


# ---------------------------------------------------------------- (a) working memory
def test_working_memory_evicts_whole_turns_and_never_cuts_a_message():
    wm = WorkingMemory(budget_tokens=200, summary_lines=3)
    originals = []
    for t in range(1, 11):
        u, a = "user %d " % t + "u" * 150, "assistant %d " % t + "a" * 150
        originals.append((u, a))
        wm.add_turn(u, a, TurnGist(turn=t, acts=t, text="did %d" % t))
    msgs = wm.messages()
    # alternation and exact content: every kept message equals its original
    assert [m["role"] for m in msgs] == ["user", "assistant"] * (len(msgs) // 2)
    kept = [(msgs[i]["content"], msgs[i + 1]["content"]) for i in range(0, len(msgs), 2)]
    assert all(pair in originals for pair in kept)
    assert kept[-1] == originals[-1], "the newest turn is always kept"
    assert wm.tokens() <= 200 or len(kept) == 1
    assert wm.evicted == 10 - len(kept)
    # rolling summary: bounded lines + folded totals, nothing lost from the counts
    assert len(wm.summary) <= 3
    text = wm.summary_text()
    folded_acts = wm.folded.acts + sum(g.acts for g in wm.summary)
    assert folded_acts == sum(range(1, 10 - len(kept) + 1))  # turns 1..evicted
    assert "older turns" in text


def test_working_memory_keeps_an_oversized_newest_turn_whole():
    wm = WorkingMemory(budget_tokens=10)
    big = "x" * 5000
    wm.add_turn(big, big, TurnGist(turn=1))
    assert wm.messages()[0]["content"] == big and len(wm.pairs) == 1
    assert est_tokens(big) > 10


def test_evict_to_frees_room_on_demand():
    wm = WorkingMemory(budget_tokens=10_000)
    for t in range(5):
        wm.add_turn("u" * 350, "a" * 350, TurnGist(turn=t))
    n = wm.evict_to(250)
    assert n == 4 and len(wm.pairs) == 1 and len(wm.summary) == 4


# ---------------------------------------------------------------- (b) episodic
def test_episodic_stores_every_transition_and_is_queryable():
    ep = Episodic()
    _moves(ep)
    ep.add(0, (6, 3, 4), _frame(4, 5), _frame(4, 5))  # an inert click
    assert len(ep) == 7
    assert [t.i for t in ep.where(action=4)] == [0, 1]
    assert len(ep.where(changed=False)) == 1
    assert ep.where(action=(6, 3, 4))[0].changed == 0
    assert ep.last(2)[-1].action == (6, 3, 4)
    assert "A6: 1 tried, 0 changed" in ep.summary()
    assert ep[0].before.dtype == np.int8


# ---------------------------------------------------------------- (c) semantic
def test_replay_check_verifies_a_correct_rule_and_refutes_a_wrong_one():
    ep = Episodic()
    _moves(ep)
    hs = HypothesisStore(min_support=3)
    good = hs.replay_check(predict_move, ep)
    assert good["ok"] and good["passes"] and good["claimed"] == 6 and good["wrong"] == 0
    bad = hs.replay_check(predict_wrong, ep)
    assert not bad["ok"] and bad["wrong"] == 6
    assert bad["first_fail"]["t"] == 0 and bad["first_fail"]["cells_wrong"] == 2


def test_hypothesis_kept_while_it_replays_then_refuted_by_new_evidence():
    ep = Episodic()
    r, c = _moves(ep)
    hs = HypothesisStore(min_support=3)
    src = "def predict_move(frame, action):\n    return None\n"
    rep = hs.propose("predict_move", predict_move, src, "predict", ep)
    assert rep["status"] == "verified" and "predict_move" in hs.verified()
    # new level, same rules: still verified, support grows (carries across levels)
    _moves(ep, start=(8, 8), acts=(1, 4), level=1)
    assert hs.recheck(ep) == []
    assert hs.active["predict_move"].support == 8
    # a wall: action 4 no longer moves the dot -> refuted with the transition named
    ep.add(1, (4, -1, -1), _frame(7, 9), _frame(7, 9))
    assert hs.recheck(ep) == ["predict_move"]
    assert "predict_move" not in hs.active
    assert hs.refuted[-1].refuted_at_transition == len(ep) - 1
    d = hs.digest()
    assert "REFUTED (do not retry) predict_move" in d


def test_refuted_hypothesis_is_not_retried_under_a_new_name():
    ep = Episodic()
    _moves(ep)
    hs = HypothesisStore()
    src_a = "def nothing(frame, action):\n    return frame.copy()\n"
    src_b = "def still_nothing(f, a):\n    '''doc differs'''\n    return f.copy()\n"
    assert hs.propose("nothing", predict_wrong, src_a, "predict", ep)["status"] == "refuted"
    rep = hs.propose(
        "still_nothing",
        predict_wrong,
        src_b.replace("f, a", "frame, action").replace("f.copy", "frame.copy"),
        "predict",
        ep,
    )
    assert rep["status"] == "refuted" and "already-refuted" in rep["reason"]


def test_rule_with_too_little_support_waits_and_abstaining_is_allowed():
    ep = Episodic()
    _moves(ep, acts=(4, 4))
    hs = HypothesisStore(min_support=3)

    def only_right(frame, action):
        return predict_move(frame, action) if action[0] == 4 else None

    rep = hs.propose("only_right", only_right, "", "predict", ep)
    assert rep["status"] == "unsupported"
    _moves(ep, start=(1, 1), acts=(4, 2))
    hs.recheck(ep)
    assert hs.active["only_right"].status == "verified"  # 3 claims, 1 abstention


def test_goal_hypothesis_consistent_then_verified_by_a_win():
    ep = Episodic()
    _moves(ep, acts=(4, 4))

    def goal(frame):
        return bool(frame[5, 9] == 3)

    hs = HypothesisStore()
    rep = hs.propose("goal", goal, "", "goal", ep)
    assert rep["status"] == "consistent"
    win = _frame(5, 9)
    ep.add(0, (4, -1, -1), _frame(5, 8), np.zeros((16, 16), np.int8), level_up=True, win=win)
    hs.recheck(ep)
    assert hs.active["goal"].status == "verified" and hs.active["goal"].positives == 1

    def bad_goal(frame):
        return True

    assert hs.propose("bad_goal", bad_goal, "", "goal", ep)["status"] == "refuted"


def test_raising_rule_is_refuted_not_a_crash():
    ep = Episodic()
    _moves(ep)

    def boom(frame, action):
        raise KeyError("x")

    hs = HypothesisStore()
    rep = hs.propose("boom", boom, "", "predict", ep)
    assert rep["status"] == "refuted" and "KeyError" in rep["reason"]


# ---------------------------------------------------------------- (d) procedural
CODE = '''
def find_player(f):
    """Locate the colour-3 cell."""
    ys, xs = np.nonzero(f == 3)
    return int(ys[0]), int(xs[0])

def unused(f):
    return 0

r, c = find_player(frame)
'''


def test_top_level_defs_and_called_names():
    defs = top_level_defs(CODE)
    assert set(defs) == {"find_player", "unused"}
    assert defs["find_player"]["doc"] == "Locate the colour-3 cell."
    assert defs["find_player"]["sig"] == "find_player(f)"
    assert defs["find_player"]["source"].startswith("def find_player")
    assert called_names(CODE) >= {"find_player", "int"} and "unused" not in called_names(CODE)


def test_skill_library_persists_reloads_and_counts_cross_game_reuse(tmp_path):
    d = str(tmp_path / "skills")
    lib = SkillLibrary(FileMemoryBackend(d))
    defs = top_level_defs(CODE)["find_player"]
    assert (
        lib.record_success("find_player", defs["source"], defs["doc"], defs["sig"], "gameA") is True
    )
    lib.save()
    on_disk = json.loads((tmp_path / "skills" / "procedural.json").read_text(encoding="utf-8"))
    assert on_disk["skills"]["find_player"]["created_game"] == "gameA"

    # a later game in the same run loads it and installs it into a namespace
    lib2 = SkillLibrary(FileMemoryBackend(d))
    assert "find_player" in lib2.loaded_at_start
    ns = {"np": np}

    def define(src, filename):
        exec(compile(src, filename, "exec"), ns)  # noqa: S102 - test namespace
        return None

    assert lib2.install(define) == ["find_player"]
    assert ns["find_player"](_frame(2, 7)) == (2, 7)
    assert lib2.record_use("find_player", "gameB", ok=True) is True  # cross-game
    assert lib2.record_use("find_player", "gameA", ok=True) is False  # same game
    lib2.save()
    lib3 = SkillLibrary(FileMemoryBackend(d))
    s = lib3.skills["find_player"]
    assert s["reused_in"] == ["gameB"] and s["reuse_successes"] == 1 and s["uses"] == 2
    assert "find_player(f)" in lib3.digest()


def test_skill_library_merges_concurrent_writers(tmp_path):
    d = str(tmp_path)
    a, b = SkillLibrary(FileMemoryBackend(d)), SkillLibrary(FileMemoryBackend(d))
    a.record_success("fa", "def fa():\n    return 1\n", "", "fa()", "g1")
    a.save()
    b.record_success("fb", "def fb():\n    return 2\n", "", "fb()", "g2")
    b.save()  # must not drop a's skill
    assert set(SkillLibrary(FileMemoryBackend(d)).skills) == {"fa", "fb"}


def test_skill_redefinition_bumps_the_version(tmp_path):
    lib = SkillLibrary(FileMemoryBackend(str(tmp_path)))
    lib.record_success("f", "def f():\n    return 1\n", "", "f()", "g")
    lib.record_success("f", "def f():\n    return 2\n", "", "f()", "g")
    assert lib.skills["f"]["version"] == 2 and "return 2" in lib.skills["f"]["source"]
    assert lib.skills["f"]["successes"] == 2


def test_stub_hypotheses_are_rejected_as_vacuous():
    ep = Episodic()
    _moves(ep)
    hs = HypothesisStore()

    def stub(frame, action):
        pass

    def goal_stub(frame):
        pass

    assert hs.propose("stub", stub, "", "predict", ep)["status"] == "refuted"
    assert "vacuous" in hs.refuted[-1].reason
    assert hs.propose("goal_stub", goal_stub, "", "goal", ep)["status"] == "refuted"
    assert "stub" not in hs.active and "goal_stub" not in hs.active
