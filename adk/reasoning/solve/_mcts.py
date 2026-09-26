"""Planning inside the learned model (design slice 3): ``plan()`` and ``disagree()``.

The loop's verified ``predict(state, action) -> next | None`` hypotheses are a
model of the environment. This module searches INSIDE that model -- no
environment action is ever taken here -- for two things:

* :func:`plan` -- the shortest action path from a state to one where a ``goal``
  hypothesis holds (or across an observed winning edge). The imagined graph is
  expanded breadth-first into an :class:`~adk.reasoning.mcts.ObservedTransitionModel`
  (real recorded transitions first, then the model's predictions) and the path
  is read back with its ``path_to_reward``, so it is optimal within the model
  whenever the goal lies inside ``max_states``. When the reachable space is
  larger than that, :class:`~adk.reasoning.mcts.UnifiedMCTS` searches an
  :class:`ImaginedEnv` instead and its tree is replayed into a fresh table for
  the same extraction (``method="mcts"``).
* :func:`disagree` -- active learning: for each action, the outcomes the
  SURVIVING hypotheses predict. The action whose predictions split them most
  (entropy over the outcome partition) is the most informative experiment.
  :func:`find_disagreement` walks the imagined graph to the nearest state
  where they split, when they agree everywhere here.

Stdlib only at import time; numpy is used only through the states passed in.
3.10-compatible.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from adk.reasoning.mcts import MCTSConfig, ObservedTransitionModel, UnifiedMCTS

__all__ = [
    "WorldModel",
    "ImaginedEnv",
    "plan",
    "disagree",
    "find_disagreement",
    "state_key",
    "PLAN_SELFTEST_BREAK",
]

Act = Tuple[int, int, int]
Named = Tuple[str, Callable[..., Any]]
#: Observed real edges: ``(state_key, action) -> (after_state, level_up, died)``.
Observed = Dict[Tuple[str, Act], Tuple[Any, bool, bool]]

#: ``PLAN_SELFTEST_BREAK=1`` makes the model predict nothing (the break arm the
#: plan tests must fail under).
PLAN_SELFTEST_BREAK = "PLAN_SELFTEST_BREAK"

WIN, LOSS, UNKNOWN_REWARD = 1.0, -1.0, -0.05


def _broken() -> bool:
    import os

    return os.environ.get(PLAN_SELFTEST_BREAK, "") == "1"


def state_key(state: Any) -> str:
    """Default state key: shape + bytes (the core's default, plus the shape)."""
    raw = state.tobytes() if hasattr(state, "tobytes") else repr(state).encode("utf-8")
    shape = repr(getattr(state, "shape", "")).encode("utf-8")
    return hashlib.blake2b(shape + b"|" + raw, digest_size=8).hexdigest()


def _as_array(v: Any, like: Any) -> Any:
    if hasattr(v, "shape") or not hasattr(like, "shape"):
        return v
    import numpy as np  # only reached with array states, i.e. numpy is installed

    return np.asarray(v)


def _score(v: Any) -> float:
    """A goal returns a bool or a progress number in [0, 1] (as the core's goal_score)."""
    if v is None:
        return 0.0
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 1.0 if v else 0.0


def _direct(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)


def _act(a: Any) -> Act:
    if isinstance(a, int):
        return (int(a), -1, -1)
    t = tuple(int(v) for v in a)
    return (t + (-1, -1, -1))[:3] if len(t) < 3 else t[:3]  # type: ignore[return-value]


class WorldModel:
    """``step(state, action)`` from observed edges first, then the predictors.

    ``predictors`` are ``(name, predict)`` pairs, strongest first; the first one
    that does not abstain decides. ``step`` returns ``(next, level_up, died)`` or
    ``None`` when nothing knows the transition.
    """

    def __init__(
        self,
        predictors: Sequence[Named],
        *,
        observed: Optional[Observed] = None,
        key: Callable[[Any], str] = state_key,
        call: Callable[..., Any] = _direct,
    ) -> None:
        self.predictors = list(predictors)
        self.observed = dict(observed or {})
        self.key = key
        self.call = call
        self.errors = 0
        self.conflicts = 0

    def step(self, state: Any, action: Act) -> Optional[Tuple[Any, bool, bool]]:
        if _broken():
            return None
        hit = self.observed.get((self.key(state), action))
        if hit is not None:
            return hit
        for _name, fn in self.predictors:
            try:
                v = self.call(fn, state.copy() if hasattr(state, "copy") else state, action)
            except Exception:  # noqa: BLE001 - a raising rule abstains here
                self.errors += 1
                continue
            if v is not None:
                return _as_array(v, state), False, False
        return None


class _Graph:
    """The imagined graph a breadth-first expansion recorded."""

    def __init__(self, start_key: str) -> None:
        self.table = ObservedTransitionModel(unknown_reward=UNKNOWN_REWARD)
        self.states: Dict[str, Any] = {}
        self.order: List[str] = []
        self.parent: Dict[str, Tuple[str, Act]] = {}
        self.start = start_key
        self.unknown = 0
        self.goal_edges = 0
        self.truncated = False
        self.timed_out = False

    def path_to(self, k: str) -> List[Act]:
        out: List[Act] = []
        while k != self.start:
            k, a = self.parent[k]
            out.append(a)
        out.reverse()
        return out


def _expand(
    start: Any,
    model: WorldModel,
    actions: Sequence[Act],
    goal: Optional[Callable[[Any], float]],
    *,
    max_states: int,
    max_depth: int,
    deadline: Optional[float],
    stop_at_goal: bool = True,
) -> _Graph:
    """Breadth-first imagination from ``start``; every edge goes into ``g.table``."""
    k0 = model.key(start)
    g = _Graph(k0)
    g.states[k0] = start
    g.order.append(k0)
    q: deque = deque([(k0, 0)])
    while q:
        k, depth = q.popleft()
        if depth >= max_depth:
            continue
        s = g.states[k]
        for a in actions:
            if deadline is not None and time.monotonic() > deadline:
                g.timed_out = True
                return g
            out = model.step(s, a)
            if out is None:
                g.unknown += 1
                continue
            nxt, level_up, died = out
            nk = model.key(nxt)
            won = level_up or (goal is not None and _score(goal(nxt)) >= 1.0)
            if won:
                g.table.record(k, a, nk, WIN, True)
                g.goal_edges += 1
                if stop_at_goal:  # breadth-first: the first goal edge is a shortest one
                    return g
                continue
            if died:
                g.table.record(k, a, nk, LOSS, True)
                continue
            g.table.record(k, a, nk, 0.0, False)
            if nk in g.states:
                continue
            if len(g.states) >= max_states:
                g.truncated = True
                continue
            g.states[nk] = nxt
            g.order.append(nk)
            g.parent[nk] = (k, a)
            q.append((nk, depth + 1))
    return g


class ImaginedEnv:
    """An :class:`~adk.reasoning.mcts.MCTSEnvironment` that steps the model, never the world.

    An unknown transition ends the branch with a small penalty, a loss with -1,
    and a goal with +1; ``evaluate`` is the goal's graded score.
    """

    def __init__(
        self,
        state: Any,
        model: WorldModel,
        actions: Sequence[Act],
        goal: Optional[Callable[[Any], float]],
    ) -> None:
        self.state = state
        self.model = model
        self.actions = list(actions)
        self.goal = goal
        self.over = False

    def get_state_hash(self) -> int:
        return hash(self.model.key(self.state))

    def get_actions(self) -> List[Act]:
        return [] if self.over else list(self.actions)

    def step(self, action: Act) -> Tuple[Any, float, bool]:
        out = self.model.step(self.state, action)
        if out is None:
            self.over = True
            return self.state, UNKNOWN_REWARD, True
        nxt, level_up, died = out
        self.state = nxt
        if level_up or (self.goal is not None and _score(self.goal(nxt)) >= 1.0):
            self.over = True
            return nxt, WIN, True
        if died:
            self.over = True
            return nxt, LOSS, True
        return nxt, 0.0, False

    def evaluate(self) -> float:
        if self.goal is None:
            return 0.0
        try:
            return _score(self.goal(self.state))
        except Exception:  # noqa: BLE001
            return 0.0

    def clone(self) -> "ImaginedEnv":
        c = ImaginedEnv(self.state, self.model, self.actions, self.goal)
        c.over = self.over
        return c


def _run_coro(coro: Any) -> Any:
    """Run a coroutine to completion from sync code, on any thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}

    def _t() -> None:
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["e"] = exc

    th = threading.Thread(target=_t, name="plan-mcts", daemon=True)
    th.start()
    th.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _mcts_plan(
    start: Any,
    model: WorldModel,
    actions: Sequence[Act],
    goal: Optional[Callable[[Any], float]],
    *,
    max_depth: int,
    iterations: int,
    time_s: float,
) -> Dict[str, Any]:
    env = ImaginedEnv(start, model, actions, goal)
    cfg = MCTSConfig(
        iterations=iterations,
        time_limit_ms=max(1.0, time_s * 1000.0),
        max_depth=max_depth,
        simulation_depth=min(max_depth, 30),
        max_branching_factor=max(1, len(actions)),
    )
    res = _run_coro(UnifiedMCTS(cfg).search(env))
    # Replay the tree into a table and read the shortest winning path back.
    table = ObservedTransitionModel(unknown_reward=UNKNOWN_REWARD)
    stack = [res.root]
    while stack:
        node = stack.pop()
        for ch in node.children:
            table.record(node.state_hash, ch.action, ch.state_hash, ch.reward, ch.terminal)
            stack.append(ch)
    path = table.path_to_reward(res.root.state_hash, min_reward=0.5, max_depth=max_depth)
    return {
        "found": path is not None,
        "path": [tuple(a) for a in (path or res.best_action_path)],
        "visits": res.iterations_used,
        "value": round(float(res.best_value), 4),
    }


def plan(
    start: Any,
    predictors: Sequence[Named],
    goal: Optional[Callable[[Any], Any]],
    actions: Iterable[Any],
    *,
    observed: Optional[Observed] = None,
    key: Callable[[Any], str] = state_key,
    call: Callable[..., Any] = _direct,
    max_depth: int = 40,
    max_states: int = 5000,
    time_s: Optional[float] = None,
    method: str = "auto",
    mcts_iterations: int = 400,
) -> Dict[str, Any]:
    """Shortest action path from ``start`` to the goal, inside the model only.

    ``method``: ``"exact"`` (breadth-first over the imagined graph), ``"mcts"``
    (UnifiedMCTS over :class:`ImaginedEnv`), or ``"auto"`` (exact, then MCTS when
    the exact expansion was cut short by ``max_states``). Never acts.
    """
    acts = [_act(a) for a in actions]
    model = WorldModel(predictors, observed=observed, key=key, call=call)
    gfn: Optional[Callable[[Any], Any]] = None
    if goal is not None:

        def gfn(s: Any) -> Any:
            try:
                return call(goal, s.copy() if hasattr(s, "copy") else s)
            except Exception:  # noqa: BLE001 - a raising goal is simply not met
                return 0.0

    t0 = time.monotonic()
    deadline = t0 + time_s if time_s else None
    out: Dict[str, Any] = {
        "found": False,
        "path": [],
        "steps": 0,
        "method": "none",
        "predictors": [n for n, _ in model.predictors],
        "env_actions": 0,
        "states": 0,
        "unknown_edges": 0,
        "reason": "",
    }
    if not acts:
        out["reason"] = "no actions to plan over"
        return out
    if gfn is None and not any(v[1] for v in model.observed.values()):
        out["reason"] = "no goal: hypothesize a goal(state) or observe a win first"
        return out
    if gfn is not None and _score(gfn(start)) >= 1.0:
        out.update(found=True, method="already", reason="the goal already holds here")
        return out
    if method in ("auto", "exact"):
        g = _expand(
            start, model, acts, gfn, max_states=max_states, max_depth=max_depth, deadline=deadline
        )
        out.update(states=len(g.states), unknown_edges=g.unknown, method="exact")
        path = g.table.path_to_reward(g.start, min_reward=0.5, max_depth=max_depth)
        if path is not None:
            out.update(found=True, path=[tuple(a) for a in path], steps=len(path))
        elif method == "exact" or not (g.truncated or g.timed_out):
            out["reason"] = _why_not(g, max_depth)
        else:
            method = "mcts"
    if method == "mcts" and not out["found"]:
        left = (deadline - time.monotonic()) if deadline is not None else 5.0
        m = _mcts_plan(
            start,
            model,
            acts,
            gfn,
            max_depth=max_depth,
            iterations=mcts_iterations,
            time_s=max(0.05, left),
        )
        out.update(
            method="mcts",
            found=m["found"],
            path=m["path"],
            steps=len(m["path"]),
            mcts_visits=m["visits"],
            mcts_value=m["value"],
        )
        if not m["found"]:
            out["reason"] = "MCTS found no goal inside the model; path is its best partial line"
    out["model_errors"] = model.errors
    out["elapsed_s"] = round(time.monotonic() - t0, 4)
    return out


def _why_not(g: _Graph, max_depth: int) -> str:
    bits = ["goal not reachable inside the model (%d states imagined)" % len(g.states)]
    if g.unknown:
        bits.append(
            "the rules abstain on %d (state, action) pairs -- experiment there "
            "(disagree()) to extend the model" % g.unknown
        )
    if g.timed_out:
        bits.append("time cap reached")
    elif g.truncated:
        bits.append("state cap reached")
    else:
        bits.append("depth cap %d" % max_depth)
    return "; ".join(bits)


def _outcome_key(v: Any, key: Callable[[Any], str]) -> str:
    try:
        return key(v)
    except Exception:  # noqa: BLE001
        return repr(v)[:200]


def disagree(
    state: Any,
    hypotheses: Sequence[Named],
    actions: Iterable[Any],
    *,
    key: Callable[[Any], str] = state_key,
    call: Callable[..., Any] = _direct,
    top: int = 5,
) -> Dict[str, Any]:
    """Rank actions by how much the hypotheses' predicted outcomes disagree.

    For each action the hypotheses that do not abstain are partitioned by the
    next state they predict; the score is the partition's entropy in bits. The
    top action is the experiment that eliminates the most hypotheses whichever
    way it turns out. ``best`` is None when every action gets one answer.
    """
    rows: List[Dict[str, Any]] = []
    for a in (_act(x) for x in actions):
        groups: Dict[str, List[str]] = {}
        abstain: List[str] = []
        for name, fn in hypotheses:
            if _broken():
                abstain.append(name)
                continue
            try:
                v = call(fn, state.copy() if hasattr(state, "copy") else state, a)
            except Exception:  # noqa: BLE001 - a raising rule abstains here
                abstain.append(name)
                continue
            if v is None:
                abstain.append(name)
                continue
            groups.setdefault(_outcome_key(_as_array(v, state), key), []).append(name)
        n = sum(len(v) for v in groups.values())
        ent = -sum((len(v) / n) * math.log2(len(v) / n) for v in groups.values()) if n else 0.0
        rows.append(
            {
                "action": a,
                "entropy": round(ent, 4),
                "outcomes": len(groups),
                "groups": sorted(groups.values(), key=len, reverse=True),
                "abstain": abstain,
            }
        )
    rows.sort(key=lambda r: (-r["entropy"], -r["outcomes"], len(r["abstain"])))
    best = rows[0]["action"] if rows and rows[0]["entropy"] > 0 else None
    return {
        "best": best,
        "ranked": rows[: max(1, int(top))],
        "hypotheses": [n for n, _ in hypotheses],
    }


def find_disagreement(
    start: Any,
    predictors: Sequence[Named],
    hypotheses: Sequence[Named],
    actions: Iterable[Any],
    *,
    observed: Optional[Observed] = None,
    key: Callable[[Any], str] = state_key,
    call: Callable[..., Any] = _direct,
    max_states: int = 400,
    max_depth: int = 20,
    time_s: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """The nearest imagined state where the hypotheses disagree.

    States are imagined with ``predictors`` (the verified model); at each, in
    breadth-first order, :func:`disagree` runs over ``hypotheses`` (all
    surviving ones). Returns ``{"path", "action", "entropy", "groups", "depth"}``
    -- walk ``path`` (predicted, not yet taken), then ``action`` is the
    experiment -- or None.
    """
    acts = [_act(a) for a in actions]
    if len(hypotheses) < 2 or not acts:
        return None
    model = WorldModel(predictors, observed=observed, key=key, call=call)
    deadline = time.monotonic() + time_s if time_s else None
    g = _expand(
        start,
        model,
        acts,
        None,
        max_states=max_states,
        max_depth=max_depth,
        deadline=deadline,
        stop_at_goal=False,
    )
    for k in g.order:
        if deadline is not None and time.monotonic() > deadline:
            break
        d = disagree(g.states[k], hypotheses, acts, key=key, call=call, top=1)
        if d["best"] is not None:
            path = g.path_to(k)
            top = d["ranked"][0]
            return {
                "path": path,
                "action": d["best"],
                "entropy": top["entropy"],
                "groups": top["groups"],
                "depth": len(path),
            }
    return None
