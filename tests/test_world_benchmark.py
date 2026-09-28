"""W6 benchmark: the key-door grid, end to end, with thresholds that can fail.

One exploratory episode, then: lookup fidelity on the TRAINING trajectory (a table
check, not prediction), held-out prediction and planning from a PARTIAL episode (the
generalization numbers), planning on the fully explored grid, NO_MODEL on a fresh
store, subgoals past the horizon, graded violation-of-expectation on paired seeds, and
the Reddit memory sessions driven through the agent -- with the key GIVEN (plumbing)
and without it (a pinned limit of the deterministic reconciler; the no-key benchmark
with a model lives in evals/llm_reconcile_eval.py). Measured numbers are printed
(run with -s) and asserted against thresholds set just under what was measured.
"""
from __future__ import annotations

import itertools
import random
import shutil
from pathlib import Path

import pytest

from tests._world_envs import ACTIONS, SCOPE, KeyDoorGrid, awm, free_cells, open_store

from adk.world import GENERALIZED, NO_MODEL, NONE, OK, PREDICTED, RECALLED, UNREACHABLE
from adk.world import MemoryAdapter
from adk.world import WorldModelAgent
from adk.world_adapters import FactoredLearner, train_from_transitions

HORIZON = 20
RESULTS: dict = {}


@pytest.fixture(scope="module")
def explored(tmp_path_factory):
    """ONE exploratory episode into a store; returns (db path, explored action list)."""
    d = tmp_path_factory.mktemp("bench")
    st = open_store(d / "explored.db")
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid())
    res = ag.explore(5000)
    RESULTS["explore_steps"] = res.steps
    RESULTS["explore_complete"] = res.complete
    RESULTS["transitions"] = len(st.transitions(SCOPE))
    st.close()
    assert res.complete
    return d / "explored.db", list(res.actions)


def _copy(db: Path, dst: Path) -> Path:
    shutil.copyfile(db, dst)
    return dst


def test_replay_accuracy_is_exact_after_one_episode(explored, tmp_path):
    """LOOKUP fidelity: the same actions from the same start on a deterministic grid.
    Every pair was in the training data, so 1.0 measures the table, not prediction
    (round-1 review). Prediction is test_held_out_prediction_after_a_partial_episode."""
    db, actions = explored
    st = open_store(_copy(db, tmp_path / "replay.db"), clock_start=10_000.0)
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid())
    hits = 0
    for a in actions:
        step = ag.act(a)
        ok = step.prediction.source == RECALLED and step.prediction.delta == step.transition.delta
        hits += ok
    acc = hits / len(actions)
    RESULTS["replay_lookup_fidelity"] = acc
    print(f"\nreplay lookup fidelity (training data) {hits}/{len(actions)} = {acc:.3f}")
    assert acc == 1.0
    st.close()


def _goals(n: int = 20):
    """Seeded goals: a cell, half of them also asking for the key held or the door open."""
    out = []
    cells = [c for c in free_cells() if c != (2, 2)]
    for seed in range(n):
        rng = random.Random(seed)
        x, y = rng.choice(cells)
        g = {"pos.x": str(x), "pos.y": str(y)}
        extra = rng.choice([None, ("key", "held"), ("door", "open")])
        if extra:
            g[extra[0]] = extra[1]
        out.append((seed, g))
    return out


def test_next_episode_planning_success(explored, tmp_path):
    db, _ = explored
    st = open_store(_copy(db, tmp_path / "plan.db"), clock_start=10_000.0)
    wins = 0
    lengths = []
    for seed, goal in _goals(20):
        env = KeyDoorGrid()
        ag = WorldModelAgent(st, SCOPE, env, horizon=HORIZON, seed=seed)
        r = ag.run(goal, budget=40)
        wins += r.reached
        lengths.append(len(r.steps))
        assert all(s.prediction.source == RECALLED for s in r.steps)
    rate = wins / 20
    RESULTS["plan_success"] = rate
    print(f"\nplanning success {wins}/20 = {rate:.2f}; steps per goal {lengths}")
    assert rate >= 0.9
    st.close()


PARTIAL_BUDGET = 300
#: Held-out calibration bars, set from the measurement in the test's docstring with
#: headroom. A learner stamping one confidence on every answer fails both.
HELD_OUT_MAX_OVERCONFIDENCE = 0.10
HELD_OUT_MIN_SEPARATION = 0.05


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _calibration(judged, buckets=10):
    """(ECE, overconfidence) over 10 confidence buckets, n-weighted.

    ECE = mean |accuracy - confidence|; overconfidence counts only buckets whose stated
    confidence EXCEEDS their accuracy -- the failure that makes a plan trust a wrong
    step. The learner is measured UNDER-confident (ECE 0.274 is almost all that), which
    is safe; overconfidence is what is bounded.
    """
    if not judged:
        return 0.0, 0.0
    rows = [[] for _ in range(buckets)]
    for c, ok in judged:
        rows[min(int(c * buckets), buckets - 1)].append((c, ok))
    ece = over = 0.0
    for r in rows:
        if not r:
            continue
        acc = _mean([1.0 if ok else 0.0 for _c, ok in r])
        conf = _mean([c for c, _ok in r])
        ece += len(r) * abs(acc - conf)
        over += len(r) * max(0.0, conf - acc)
    return ece / len(judged), over / len(judged)


def _grid_slots(x, y, key, door):
    return {"world.grid.pos.x": str(x), "world.grid.pos.y": str(y), "world.grid.key": key,
            "world.grid.door": door}


def test_held_out_prediction_after_a_partial_episode(tmp_path):
    """Generalization: explore 300 steps (incomplete), then predict every reachable
    (state, action) pair the table has NEVER seen and score it against the grid.
    Measured 2026-09-27: 394 held-out pairs, 394 answered, 304 right (0.772), none
    wrong at confidence >= 0.99; ECE 0.274, overconfidence 0.041, mean confidence
    0.656 right vs 0.324 wrong. Mutants (review 3): every answer stamped 0.98 ->
    overconfidence 0.208 (fails); learner never answering -> accuracy 0.571 (fails)."""
    st = open_store(tmp_path / "partial.db")
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid(), learner=FactoredLearner())
    res = ag.explore(PARTIAL_BUDGET)
    assert not res.complete
    held = answered = right = confident_wrong = 0
    judged: list = []  # (confidence, was_right) per answered pair
    for (x, y), key, door in itertools.product(free_cells(), ["home", "held"],
                                               ["open", "closed"]):
        if (x, y) == (2, 2) and door == "closed":
            continue
        sl = _grid_slots(x, y, key, door)
        digest = awm.state_digest(sl)
        for a in ACTIONS:
            if ag._table.has(digest, a):
                continue
            held += 1
            p = ag.predict(a, sl)
            env = KeyDoorGrid()
            env.x, env.y, env.key, env.door = x, y, key, door
            env.step(a)
            after = _grid_slots(env.x, env.y, env.key, env.door)
            truth = {k: v for k, v in after.items() if sl[k] != v}
            if p.source == NONE:
                continue
            answered += 1
            ok = p.delta == truth
            judged.append((float(p.confidence), ok))
            if ok:
                right += 1
            elif p.confidence >= 0.99:
                confident_wrong += 1
    acc = right / max(1, answered)
    ece, over = _calibration(judged)
    conf_right = _mean([c for c, ok in judged if ok])
    conf_wrong = _mean([c for c, ok in judged if not ok])
    RESULTS["held_out"] = (held, answered, right, confident_wrong, round(ece, 3),
                           round(over, 3))
    print(f"\nheld-out after {PARTIAL_BUDGET} steps: {held} pairs, {answered} answered, "
          f"{right} right ({acc:.3f}), wrong at confidence>=0.99: {confident_wrong}, "
          f"ECE {ece:.3f}, overconfidence {over:.3f}, "
          f"mean confidence right {conf_right:.3f} wrong {conf_wrong:.3f}")
    assert held >= 300 and answered == held
    assert acc >= 0.70
    assert confident_wrong == 0
    # `confident_wrong` alone cannot fail here: no held-out answer reaches 0.99
    # (review 3). Calibration is judged instead: stated confidence must track
    # accuracy without exceeding it, and must separate right answers from wrong ones.
    assert over <= HELD_OUT_MAX_OVERCONFIDENCE, (
        f"overconfidence {over:.3f} > {HELD_OUT_MAX_OVERCONFIDENCE}")
    assert conf_right - conf_wrong >= HELD_OUT_MIN_SEPARATION, (conf_right, conf_wrong)
    st.close()


def test_planning_from_a_partial_episode(tmp_path):
    """Goals the explored table may not cover. Measured 2026-09-27 (budget 60 per
    goal): 14/20 table-only, 15/20 with the factored learner, 1 executed step planned
    on a PREDICTED answer. A learner that never answers scores 14/20 and 0 steps."""
    src = tmp_path / "partial.db"
    st = open_store(src)
    WorldModelAgent(st, SCOPE, KeyDoorGrid()).explore(PARTIAL_BUDGET)
    st.close()
    wins = {"table": 0, "learner": 0}
    predicted_steps = 0
    for mode in wins:
        for seed, goal in _goals(20):
            st = open_store(_copy(src, tmp_path / f"{mode}{seed}.db"), clock_start=10_000.0)
            learner = None
            if mode == "learner":
                learner = FactoredLearner()
                train_from_transitions(st, learner, scope=SCOPE, prefix="world.grid")
            ag = WorldModelAgent(st, SCOPE, KeyDoorGrid(), horizon=HORIZON, seed=seed,
                                 learner=learner)
            r = ag.run(goal, budget=60)
            wins[mode] += r.reached
            if mode == "learner":
                predicted_steps += sum(1 for s in r.steps
                                       if s.prediction is not None
                                       and s.prediction.source == PREDICTED)
            st.close()
    RESULTS["partial_planning"] = wins
    print(f"\nplanning from a {PARTIAL_BUDGET}-step episode: table {wins['table']}/20, "
          f"learner {wins['learner']}/20, steps executed on a PREDICTED answer: "
          f"{predicted_steps}")
    assert wins["table"] >= 12 and wins["learner"] >= 12
    # Review 3: `>=` held with a learner that never answered (14 vs 14). The learner
    # must be USED (steps executed on its answers) and must win at least one goal more.
    assert predicted_steps > 0
    assert wins["learner"] > wins["table"]


def test_fresh_store_is_no_model(tmp_path):
    st = open_store(tmp_path / "fresh.db")
    ag = WorldModelAgent(st, SCOPE, KeyDoorGrid(), horizon=HORIZON)
    ag.sense()
    verdicts = {ag.plan(g).verdict for _, g in _goals(20)}
    RESULTS["fresh_verdicts"] = sorted(verdicts)
    assert verdicts == {NO_MODEL}
    assert ag.predict("up").source == NONE
    st.close()


def test_subgoals_solve_beyond_the_horizon(explored, tmp_path):
    db, _ = explored
    st = open_store(_copy(db, tmp_path / "sub.db"), clock_start=10_000.0)
    goal = {"pos.x": "4", "pos.y": "4"}
    subs = [{"key": "held"}, {"door": "open"}]
    env = KeyDoorGrid()
    ag = WorldModelAgent(st, SCOPE, env, horizon=6)
    ag.sense()
    flat = ag.plan(goal)
    chained = ag.plan(goal, subgoals=subs)
    RESULTS["subgoals"] = (flat.verdict, chained.verdict, len(chained.actions))
    print(f"\nhorizon 6: flat {flat.verdict}, with subgoals {chained.verdict} "
          f"({len(chained.actions)} actions)")
    assert flat.verdict == UNREACHABLE
    assert chained.verdict == OK and len(chained.actions) > 6
    r = ag.run(goal, budget=40, subgoals=subs)
    assert r.reached and (env.x, env.y) == (4, 4)
    st.close()


def _oracle_surprise(predicted, observed, before) -> float:
    """Independent re-derivation of the graded score: the share of touched slots whose
    predicted NEXT value differs from the observed one. Deliberately not awm's function,
    so a mutant of it cannot move the oracle too."""
    keys = set(predicted) | set(observed)
    if not keys:
        return 0.0
    want, got = dict(before), dict(before)
    for tgt, delta in ((want, predicted), (got, observed)):
        for k, v in delta.items():
            if v is None:
                tgt.pop(k, None)
            else:
                tgt[k] = v
    return sum(1 for k in keys if want.get(k) != got.get(k)) / len(keys)


def test_voe_teleport_is_more_surprising_on_every_paired_seed(explored, tmp_path):
    db, _ = explored
    pairs = []
    graded = []
    for seed in range(10):
        goal = _goals(10)[seed][1]
        assert goal != {"pos.x": "0", "pos.y": "0"}   # every run takes at least one step
        scores = []
        for perturbed in (False, True):
            st = open_store(_copy(db, tmp_path / f"voe{seed}{int(perturbed)}.db"),
                            clock_start=10_000.0)
            # the teleport rides on the FIRST executed step, so even a one-step goal sees it
            env = KeyDoorGrid(teleport_at=0 if perturbed else None, teleport_seed=seed)
            ag = WorldModelAgent(st, SCOPE, env, horizon=HORIZON, seed=seed)
            r = ag.run(goal, budget=40)
            scored = [s.surprise for s in r.steps if s.surprise is not None]
            assert len(scored) == len(r.steps)       # every step had an expectation
            scores.append(sum(scored))
            if perturbed:
                first = r.steps[0]
                graded.append((first.surprise, _oracle_surprise(
                    first.prediction.delta, first.transition.delta,
                    _grid_slots(0, 0, "home", "closed"))))
            st.close()
        pairs.append(tuple(scores))
    RESULTS["voe"] = pairs
    print("\nVoE (unperturbed, teleported) surprise per seed: "
          + ", ".join(f"({a:.2f}, {b:.2f})" for a, b in pairs))
    assert all(b > a for a, b in pairs)
    assert all(a == 0.0 for a, _ in pairs)
    # GRADED, not binary (round-1 review: a 0/1 mutant passed the two lines above):
    # each teleport step scores the oracle's share of wrong slots, and the seeds
    # include a partial miss strictly between 0 and 1.
    print("VoE teleport step (recorded, oracle): " + ", ".join(
        f"({a:.2f}, {b:.2f})" for a, b in graded))
    assert all(a == pytest.approx(b) for a, b in graded)
    assert any(0.0 < a < 1.0 for a, _ in graded)


def _session(db: Path, at: float):
    st = awm.MemoryStore(db)
    st._clock = lambda: at
    return st


def test_reddit_sessions_through_the_agent(tmp_path):
    """Session 1 dark, session 5 light, session 8 asks: ONE answer, dark in history.

    PLUMBING ONLY: the subject key ``user.ui_theme`` is GIVEN, so this proves the
    agent -> reconcile -> history path, not that anything found the key. Round-1
    review: chatter through the same adapter would overwrite the slot too (pinned in
    the next test). The no-key benchmark, where a model must derive the subject and
    ignore chatter, is evals/llm_reconcile_eval.py (scenario "reddit_nokey")."""
    db = tmp_path / "memory.db"
    user = awm.Scope("acme", "vansh", "chat")
    subject = "user.ui_theme"
    with _session(db, 1_000.0) as s1:
        ad = MemoryAdapter(s1, user, subject)
        ag = WorldModelAgent(s1, user, ad)
        st = ag.act("prefers dark mode")
        assert st.info["decision"]["action"] == "add" and st.surprise is None  # novel
    with _session(db, 5_000.0) as s5:
        ad = MemoryAdapter(s5, user, subject)
        ag = WorldModelAgent(s5, user, ad)
        st = ag.act("switched to light mode")
        assert st.info["decision"]["action"] == "update"
    with _session(db, 8_000.0) as s8:
        ag = WorldModelAgent(s8, user, MemoryAdapter(s8, user, subject))
        u = ag.understand()
        assert u.slots == {subject: "switched to light mode"}
        assert [h.value for h in s8.history(user, subject)] == [
            "prefers dark mode", "switched to light mode"]
        assert [m.value for m in s8.recall(user, as_of=3_000.0)] == ["prefers dark mode"]
        # the model learned what a statement DOES: saying dark again would switch back
        p = ag.predict("prefers dark mode")
        assert p.source == GENERALIZED and p.delta == {subject: "prefers dark mode"}
        assert len(s8.transitions(user)) == 2
    RESULTS["reddit"] = "key-given plumbing: one answer, history 2"


def test_reddit_with_the_key_given_chatter_is_written_as_an_update(tmp_path):
    """The pinned LIMIT that makes the test above easy: with the key handed over,
    the deterministic SlotReconciler records ANY sentence as an update of that slot.
    If this starts failing, the adapter learned to ignore chatter -- update both."""
    db = tmp_path / "memory.db"
    user = awm.Scope("acme", "vansh", "chat")
    subject = "user.ui_theme"
    with _session(db, 1_000.0) as s1:
        WorldModelAgent(s1, user, MemoryAdapter(s1, user, subject)).act("prefers dark mode")
    with _session(db, 5_000.0) as s5:
        st = WorldModelAgent(s5, user, MemoryAdapter(s5, user, subject)).act(
            "lol the standup ran long again today")
        assert st.info["decision"]["action"] == "update"
        assert s5.recall(user)[0].value == "lol the standup ran long again today"
    RESULTS["reddit_limit"] = "key given: chatter overwrites the slot (SlotReconciler)"
