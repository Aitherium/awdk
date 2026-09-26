"""The planning loop: ``plan()`` / ``disagree()`` as REPL tools, and daydreaming.

:class:`PlanningLoop` is the vendored :class:`ReasoningLoop` (unchanged, so its
pinned hash holds) plus, in ``sase`` mode:

* two tools in the model's namespace -- ``plan(goal=None, ...)`` searches inside
  the model assembled from VERIFIED predict hypotheses (see ``_mcts.plan``) and
  ``disagree()`` ranks actions by how much the SURVIVING predict hypotheses
  disagree on their outcome (see ``_mcts.disagree``);
* a daydream step at the head of every turn whose intent is ``exploring`` or
  ``stuck``: offline, with no environment action, it plans toward the best goal
  hypothesis and looks for the discriminating experiment. The result lands in
  ``DREAM`` (namespace), in the pinned notes the next prompt shows, and in a
  ``daydream`` log event. It runs under the sandbox clock (``daydream_s``) and
  the Governor (cancel / wall budget), and only when the wall budget leaves
  room for it.

The ``plain`` mode (the h30 ablation baseline) is left exactly as h30 has it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import _mcts
from ._vendor.intent import EXPLORING, STUCK
from ._vendor.loop import ReasoningLoop
from ._vendor.sandbox import SandboxTimeout

__all__ = ["PlanningLoop", "PLAN_API"]

PLAN_API = (
    '\n'
    'PLANNING API (no actions spent; searches inside your VERIFIED predict() rules):\n'
    ' plan(goal=None, max_depth=40)    # shortest action path from the current state to goal (a '
    'goal hypothesis name or\n'
    '                                  #   fn; default: your best one) -> {"found", "path", '
    '"steps", "reason", ...}\n'
    '                                  #   run it: for a in p["path"]: act(*a)\n'
    ' disagree()                       # the action whose predicted outcome differs most across '
    'your surviving predict\n'
    '                                  #   rules -> {"best": action, "ranked": [...]}: the most '
    'informative experiment\n'
    ' DREAM                            # what the loop found offline before this turn: '
    'DREAM["plan"], DREAM["experiment"]'
)


class PlanningLoop(ReasoningLoop):
    """:class:`ReasoningLoop` with ``plan`` / ``disagree`` tools and a daydream step."""

    def __init__(
        self,
        *args: Any,
        daydream: bool = True,
        daydream_s: float = 2.0,
        plan_states: int = 5000,
        **kwargs: Any,
    ) -> None:
        # set before super().__init__, which calls _install_namespace
        self.dream_enabled = bool(daydream)
        self.dream_s = float(daydream_s)
        self.plan_states = int(plan_states)
        self.dream: Optional[Dict[str, Any]] = None
        self._dream_sig: Any = None
        super().__init__(*args, **kwargs)
        if self.cfg.sase:
            self.system += PLAN_API
        self.stats.update(
            {
                "daydreams": 0,
                "dream_plans": 0,
                "dream_experiments": 0,
                "plan_calls": 0,
                "disagree_calls": 0,
            }
        )

    # ------------------------------------------------------------ namespace
    def _install_namespace(self) -> None:
        super()._install_namespace()
        if not self.cfg.sase:
            return
        ns = self.sandbox.ns
        ns["plan"] = self._tool_plan  # the MCTS planner replaces the core's grounded BFS plan()
        ns.setdefault("disagree", self._tool_disagree)
        ns["DREAM"] = None

    def _tool_plan(
        self,
        goal: Any = None,
        max_depth: int = 40,
        method: str = "auto",
        max_states: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.stats["plan_calls"] = self.stats.get("plan_calls", 0) + 1
        return self.plan(goal, max_depth=max_depth, method=method, max_states=max_states)

    def _tool_disagree(self, top: int = 5) -> Dict[str, Any]:
        self.stats["disagree_calls"] = self.stats.get("disagree_calls", 0) + 1
        return self.disagree(top=top)

    # ------------------------------------------------------------ the model
    def predictors(self, verified_only: bool = True) -> List[Tuple[str, Callable[..., Any]]]:
        """Active predict hypotheses, strongest first (verified ones, or all surviving)."""
        hs = [
            h
            for h in self.hyps.active.values()
            if h.kind == "predict"
            and h.fn is not None
            and (h.status == "verified" or not verified_only)
        ]
        hs.sort(key=lambda h: (h.status != "verified", -h.support, h.name))
        return [(h.name, h.fn) for h in hs]  # type: ignore[misc]

    def goal_fn(self, goal: Any = None) -> Optional[Callable[..., Any]]:
        """A callable, a hypothesis / namespace name, or None for the best goal hypothesis."""
        if callable(goal):
            return goal
        if isinstance(goal, str):
            h = self.hyps.active.get(goal)
            if h is not None and h.fn is not None:
                return h.fn
            fn = self.sandbox.ns.get(goal)
            return fn if callable(fn) else None
        gs = [h for h in self.hyps.active.values() if h.kind == "goal" and h.fn is not None]
        if not gs:
            return None
        gs.sort(key=lambda h: (h.status != "verified", -h.positives, -h.support, h.name))
        return gs[0].fn

    def observed(self) -> Dict[Tuple[str, Any], Tuple[Any, bool, bool]]:
        """Every recorded transition, keyed as the planner keys states."""
        return {
            (self._key(t.before), tuple(t.action)): (t.after, bool(t.level_up), bool(t.died))
            for t in self.history
        }

    def plan_actions(self) -> List[Tuple[int, int, int]]:
        """Actions to plan over: the simple available ones, candidates for the
        ones that need coordinates, and every action already tried."""
        out: List[Tuple[int, int, int]] = []
        seen = set()

        def add(a: Any) -> None:
            t = tuple(int(v) for v in a)
            if t not in seen:
                seen.add(t)
                out.append(t)  # type: ignore[arg-type]

        xy = set()
        for a in self._hook("available_actions", default=[]) or []:
            if self._hook("needs_xy", a, default=False):
                xy.add(int(a))
            else:
                add((int(a), -1, -1))
        if xy:
            for c in (self._hook("candidates", default=[]) or [])[:64]:
                if int(c[0]) in xy:
                    add(c)
        for t in self.history.where(level=self.level):
            add(t.action)
        return out

    # ------------------------------------------------------------ the tools
    def plan(
        self,
        goal: Any = None,
        *,
        state: Any = None,
        max_depth: int = 40,
        method: str = "auto",
        max_states: Optional[int] = None,
        time_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Search inside the verified model; never calls ``env.act``."""
        start = state if state is not None else (self.obs.state if self.obs is not None else None)
        if start is None:
            return {"found": False, "path": [], "reason": "no observation yet"}
        preds = self.predictors(verified_only=True)
        fn = self.goal_fn(goal)
        if goal is not None and fn is None:
            return {"found": False, "path": [], "reason": "no goal named %r" % (goal,)}
        return _mcts.plan(
            start,
            preds,
            fn,
            self.plan_actions(),
            observed=self.observed(),
            key=self._key,
            call=self._hyp_call,
            max_depth=int(max_depth),
            max_states=int(max_states or self.plan_states),
            time_s=time_s,
            method=method,
        )

    def disagree(self, *, state: Any = None, top: int = 5) -> Dict[str, Any]:
        """Rank actions by disagreement among the surviving predict hypotheses."""
        start = state if state is not None else (self.obs.state if self.obs is not None else None)
        hyps = self.predictors(verified_only=False)
        if start is None or len(hyps) < 2:
            return {
                "best": None,
                "ranked": [],
                "hypotheses": [n for n, _ in hyps],
                "reason": "need at least 2 surviving predict hypotheses",
            }
        return _mcts.disagree(
            start, hyps, self.plan_actions(), key=self._key, call=self._hyp_call, top=top
        )

    # ------------------------------------------------------------ daydreaming
    def _peek_intent(self) -> str:
        """The intent the coming turn will classify, without counting it."""
        s = self._signals()
        s.first_turn = False
        saved = dict(self.intents.counts)
        try:
            return self.intents.classify(s).intent
        finally:
            self.intents.counts = saved

    def _budget_allows(self) -> bool:
        g = self.governor
        if g is None:
            return True
        g.check()
        wall = getattr(g.budget, "max_wall_s", None)
        return wall is None or wall - g.wall_s() > 2 * self.dream_s

    def _turn(self) -> None:
        if (
            self.dream_enabled
            and self.cfg.sase
            and self.turn >= 1
            and self.obs is not None
            and not self.endpoint_dead
        ):
            intent = self._peek_intent()
            if intent in (EXPLORING, STUCK) and self._budget_allows():
                self.daydream(intent)
        super()._turn()

    def daydream(self, intent: str = EXPLORING) -> Optional[Dict[str, Any]]:
        """Offline search between turns. Returns the dream (also in ``DREAM``) or None
        when there is nothing to dream on. Never calls ``env.act``."""
        if self.obs is None or not self.obs.state.size:
            return None
        surviving = self.predictors(verified_only=False)
        if not surviving:
            return None
        verified = self.predictors(verified_only=True)
        here = self._key(self.obs.state)
        sig = (
            here,
            len(self.history),
            tuple(sorted((h.name, h.status) for h in self.hyps.active.values())),
        )
        if sig == self._dream_sig:
            return self.dream
        self._dream_sig = sig
        acts_before = self.actions
        dream: Dict[str, Any] = {"turn": self.turn + 1, "intent": intent, "state": here}

        def work() -> None:
            if verified and self.goal_fn() is not None:
                p = self.plan(time_s=self.dream_s * 0.6)
                dream["plan"] = p
            d = self.disagree(top=3)
            if d.get("best") is not None:
                top = d["ranked"][0]
                dream["experiment"] = {
                    "path": [],
                    "action": d["best"],
                    "entropy": top["entropy"],
                    "groups": top["groups"],
                    "depth": 0,
                }
            elif len(surviving) >= 2 and verified:
                f = _mcts.find_disagreement(
                    self.obs.state,
                    verified,
                    surviving,  # type: ignore[union-attr]
                    self.plan_actions(),
                    observed=self.observed(),
                    key=self._key,
                    call=self._hyp_call,
                    time_s=self.dream_s * 0.3,
                )
                if f is not None:
                    dream["experiment"] = f

        try:
            self.sandbox.call(work, cap_s=self.dream_s)
        except SandboxTimeout:
            dream["timeout"] = True
        dream["env_actions"] = self.actions - acts_before
        self.dream = dream
        self.sandbox.ns["DREAM"] = dream
        self.stats["daydreams"] += 1
        self._dream_notes(dream)
        self._log({"event": "daydream", **_loggable(dream)})
        return dream

    def _dream_notes(self, dream: Dict[str, Any]) -> None:
        p = dream.get("plan")
        if p and p.get("found"):
            self.stats["dream_plans"] += 1
            self.notes.append(
                "DAYDREAM: your verified rules reach the goal in %d steps: %s -- "
                "for a in DREAM['plan']['path']: act(*a)" % (p["steps"], _acts_text(p["path"]))
            )
        e = dream.get("experiment")
        if e:
            self.stats["dream_experiments"] += 1
            where = "here" if not e["path"] else "after %s" % _acts_text(e["path"])
            self.notes.append(
                "DAYDREAM: rules %s disagree on A%d %s -- that experiment decides "
                "(DREAM['experiment'])"
                % (" vs ".join("/".join(g) for g in e["groups"][:3]), e["action"][0], where)
            )

    def summary(self) -> Dict[str, Any]:
        s = super().summary()
        s["dream_last"] = _loggable(self.dream) if self.dream else None
        return s


def _acts_text(path: List[Any], limit: int = 12) -> str:
    parts = ["A%d" % a[0] if a[1] < 0 else "A%d(%d,%d)" % (a[0], a[1], a[2]) for a in path[:limit]]
    return " ".join(parts) + (" ..." if len(path) > limit else "")


def _loggable(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The dream without arrays (log records are JSON)."""
    if not d:
        return {}
    out = dict(d)
    for k in ("plan", "experiment"):
        if isinstance(out.get(k), dict):
            out[k] = {kk: vv for kk, vv in out[k].items() if kk not in ("state",)}
    return out
