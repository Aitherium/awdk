"""Rules as code, verified by replay (the library the world-model service hosts; design
sections 3 and 7).

A rule is SOURCE: ``predict(frame, action, world) -> next frame | None`` where
``world`` is the binding the context supplies (the perceived colours and the goal
cells seen so far). ``-1`` in a predicted frame is "no claim on that cell". A goal is
``goal(frame, world) -> bool``. Verification is the awdk ``HypothesisStore`` replay
(``_vendor/memory.py``), unchanged: a rule is verified only when it made cell claims on
at least ``min_support`` transitions of this history and none was wrong.

The scripted arm's rules (hand-written, the plumbing isolate) live here as
:data:`PUSH_RULE` and :data:`PUSH_GOAL`; the local-model arm proposes its own source
through the same :func:`compile_rule` / :func:`verify` path.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..reasoning.solve._vendor.memory import (
    ActionArg,
    Episodic,
    HypothesisStore,
    score_prediction,
)
from .classroom._vendor import rules
from .classroom.env import level_frame
from .perceive import COLOUR_ROLES

__all__ = [
    "PUSH_RULE",
    "PUSH_GOAL",
    "compile_rule",
    "world_binding",
    "verify",
    "heldout_accuracy",
    "goal_cells",
]

PUSH_RULE = """
def predict(frame, action, world):
    c = world["colours"]
    P, B, W, F, T, BO = (c.get(k) for k in ("player", "box", "wall", "floor", "goal", "box_on"))
    dirs = {1: (0, -1), 2: (0, 1), 3: (-1, 0), 4: (1, 0)}
    a = int(action)
    if a not in dirs or P is None or W is None or F is None:
        return None
    ys, xs = np.nonzero(frame == P)
    if len(xs) != 1:
        return None
    x, y = int(xs[0]), int(ys[0])
    dx, dy = dirs[a]
    h, w = frame.shape
    targets = set(tuple(t) for t in world["targets"])
    out = frame.copy()
    nx, ny = x + dx, y + dy
    if not (0 <= nx < w and 0 <= ny < h):
        return out
    cell = int(frame[ny, nx])
    boxes = [v for v in (B, BO) if v is not None]
    if cell == W:
        return out
    if cell in boxes:
        bx, by = nx + dx, ny + dy
        if not (0 <= bx < w and 0 <= by < h) or int(frame[by, bx]) in [W] + boxes:
            return out
        if (bx, by) in targets:
            out[by, bx] = BO if BO is not None else -1
        else:
            out[by, bx] = B if B is not None else -1
    elif cell != F and cell != T:
        return None
    out[ny, nx] = P
    out[y, x] = T if (x, y) in targets and T is not None else (F if (x, y) not in targets else -1)
    return out
"""

PUSH_GOAL = """
def goal(frame, world):
    B = world["colours"].get("box")
    if B is None:
        return None
    return not bool((frame == B).any())
"""


def compile_rule(source: str, name: str) -> Callable[..., Any]:
    """Compile rule source into its function (``np`` is the only name provided)."""
    ns: Dict[str, Any] = {"np": np}
    exec(compile(source, "<rule %s>" % name, "exec"), ns)  # noqa: S102 - rules ARE code
    fns = [v for k, v in ns.items() if callable(v) and k in ("predict", "goal")]
    if len(fns) != 1:
        raise ValueError("rule %r must define exactly one of predict/goal" % name)
    return fns[0]


def goal_cells(frames: Sequence[np.ndarray], colours: Dict[str, Optional[int]]) -> List[List[int]]:
    """Every cell seen in the goal or box-on-goal colour, as ``[x, y]``."""
    want = [colours.get(k) for k in ("goal", "box_on") if colours.get(k) is not None]
    seen = set()
    for f in frames:
        for v in want:
            ys, xs = np.nonzero(np.asarray(f) == v)
            seen.update(zip((int(x) for x in xs), (int(y) for y in ys)))
    return [list(c) for c in sorted(seen)]


def world_binding(
    colours: Dict[str, Optional[int]], frames: Sequence[np.ndarray]
) -> Dict[str, Any]:
    cols = {k: colours.get(k) for k in COLOUR_ROLES}
    return {"colours": cols, "targets": goal_cells(frames, cols)}


def bind(fn: Callable[..., Any], world: Dict[str, Any]) -> Callable[..., Any]:
    return lambda frame, action: fn(np.asarray(frame), action, world)


def verify(
    source: str,
    name: str,
    world: Dict[str, Any],
    transitions: Sequence[Tuple[np.ndarray, int, np.ndarray]],
    level: int = 0,
    min_support: int = 2,
) -> Dict[str, Any]:
    """Replay-verify a predict rule over ``transitions`` with the vendored store."""
    fn = compile_rule(source, name)
    hist = Episodic()
    for b, a, af in transitions:
        hist.add(level, (int(a), -1, -1), b, af)
    store = HypothesisStore(min_support=min_support)
    rep = store.propose(name, bind(fn, world), source, "predict", hist, level=level)
    rep["transitions"] = len(hist)
    return rep


def heldout_accuracy(
    source: str,
    name: str,
    world: Dict[str, Any],
    level_spec: Dict[str, Any],
    palette: List[int],
    n: int = 200,
    seed: str = "heldout",
    max_states: int = 20000,
) -> Dict[str, Any]:
    """Score the rule on ``n`` held-out ``(state, action)`` pairs generated by the TRUE
    ``rules.py``, the states drawn uniformly from those reachable from the level start.
    ``wrong`` counts pairs with ANY wrong claimed cell; ``coverage`` is claimed cells over
    all cells."""
    fn = bind(compile_rule(source, name), world)
    prep = rules.prep(level_spec)
    rng = random.Random("%s:%s" % (seed, name))
    # held-out states: uniform over every state reachable from the level start (not a
    # random walk, which stays near the start and misses the corners a wrong rule
    # gets wrong)
    start = rules.initial_state(prep)
    seen = {start: None}
    frontier = [start]
    while frontier and len(seen) < max_states:
        nxt_frontier = []
        for st in frontier:
            for a in (1, 2, 3, 4):
                ns, status = rules.apply(prep, st, a)
                if ns not in seen and status == rules.SY_PLAY:
                    seen[ns] = None
                    nxt_frontier.append(ns)
        frontier = nxt_frontier
    states = sorted(seen, key=repr)
    ok = wrong = abstain = claimed_cells = total_cells = 0
    first_wrong = ""
    for _ in range(n):
        s = states[rng.randrange(len(states))]
        a = rng.randint(1, 4)
        before = level_frame(prep, s, palette)
        nxt, _status = rules.apply(prep, s, a)
        after = level_frame(prep, nxt, palette)
        pred = fn(before, ActionArg(a))
        verdict, why = score_prediction(pred, before, after)
        total_cells += int(after.size)
        if verdict == "ok":
            ok += 1
            claimed_cells += int((np.asarray(pred) != -1).sum())
        elif verdict == "abstain":
            abstain += 1
        else:
            wrong += 1
            first_wrong = first_wrong or why
    return {
        "pairs": n,
        "states": len(states),
        "ok": ok,
        "wrong": wrong,
        "abstain": abstain,
        "accuracy": ok / float(ok + wrong) if ok + wrong else 0.0,
        "coverage": claimed_cells / float(total_cells) if total_cells else 0.0,
        "first_wrong": first_wrong,
    }
