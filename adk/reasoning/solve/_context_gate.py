"""The context-permission gate on the reasoning loop (ARC first slice).

Wires :mod:`adk.reasoning.solve.context` into a vendored ``ReasoningLoop`` (or the
``PlanningLoop`` over it) WITHOUT editing the vendored core: every check sits on a
seam the core already calls through an attribute -- ``loop.step``, ``loop.daydream``,
``loop.prism.rotate``, ``loop.llm.chat`` and the namespace's ``plan`` / ``note``.

The ARC policy ("context dictates what is permissible"):

* ``plan`` -- only while a predict rule is replay-verified IN THIS LEVEL's context:
  it made cell claims on at least ``level_support`` of this level's transitions,
  none wrong, all after the last contradiction. Verification on earlier levels is
  CARRIED, not held.
* ``goal`` -- plan() toward a goal only while that goal is consistent here: a goal
  hypothesis that passed this level's contract audit (not already satisfied on the
  level's first frame) and replays over this level's frames. An unregistered goal
  function has no provenance and is refused.
* ``strategy`` -- ``analogy`` only when a previous level's program exists; the auto
  strategies and the rule/goal-finding strategies are always permitted.
* revocation -- a refuted rule or goal NARROWS the context at that transition; a
  rule re-earns permission only with evidence newer than the contradiction.
* contract audit -- at every level start the held beliefs become CARRIED and are
  tested on the new level's first frame (a rule must still produce a well-formed
  prediction; a goal must not already be satisfied).

``mode``: ``"enforce"`` refuses; ``"shadow"`` records what WOULD be refused and lets
it through (the measurement arm); ``"off"`` installs only the invariant layer.
Invariants are enforced in every mode.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from .context import (
    CONTRADICTED,
    Context,
    Decision,
    Evidence,
    Invariants,
    InvariantViolation,
    Provenance,
    Request,
    permits,
)

_LOG = logging.getLogger(__name__)

__all__ = ["ContextGate", "install_context_gate", "ARC_POLICY", "MODES"]

MODES = ("off", "shadow", "enforce")

#: Model text that claims a grant it cannot hold (recorded as an inferred fact, never obeyed).
_GRANT_CLAIM = re.compile(
    r"\b(owner|operator|user|admin)\b[^.\n]{0,40}\b(allow(?:s|ed)?|approv(?:e|es|ed)|authori[sz](?:e|es|ed)|"
    r"permit(?:s|ted)?|grant(?:s|ed)?|said (?:it'?s )?ok)",
    re.IGNORECASE,
)


def _plan_rule(ctx: Context, req: Request) -> Optional[Decision]:
    goal = req.args.get("goal_name")
    if goal == "<unregistered>":
        return Decision(
            False,
            "that goal is not a hypothesis: hypothesize(goal_fn, 'goal') first",
            "context",
            "goal_needs_record",
        )
    if goal and not ctx.holds("goal:" + goal):
        return Decision(
            False,
            "goal %r is not consistent in this level (%s)" % (goal, ctx.status("goal:" + goal)),
            "context",
            "goal_needs_consistency",
        )
    if not ctx.held("rule:"):
        carried = sorted(
            k[5:] for k, f in ctx.facts.items() if k.startswith("rule:") and f.status != "held"
        )
        why = "no predict rule is replay-verified on this level yet (%d transitions here)" % int(
            ctx.domain.get("level_transitions", 0)
        )
        if carried:
            why += "; carried/contradicted: %s -- act to re-verify them here first" % ", ".join(
                carried[:4]
            )
        return Decision(False, why, "context", "plan_needs_level_verified_rule")
    return Decision(
        True, "rules held here: %s" % ", ".join(k[5:] for k in ctx.held("rule:")), "context", "plan"
    )


def _act_rule(ctx: Context, req: Request) -> Optional[Decision]:
    # acting is how evidence is gathered: always permitted, except a step the planner
    # takes, which needs the same context plan() needs
    if req.name == "plan":
        return _plan_rule(ctx, Request("plan", args=req.args))
    return None


def _strategy_rule(ctx: Context, req: Request) -> Optional[Decision]:
    if req.name == "analogy" and not ctx.domain.get("previous_program"):
        return Decision(
            False,
            "analogy needs a previous level's winning program",
            "context",
            "analogy_needs_prior",
        )
    return None


ARC_POLICY: Dict[str, Callable[[Context, Request], Optional[Decision]]] = {
    "plan": _plan_rule,
    "act": _act_rule,
    "strategy": _strategy_rule,
}


class ContextGate:
    """One per loop. Holds the context, the frozen invariants and the policy."""

    def __init__(
        self,
        loop: Any,
        *,
        mode: str = "shadow",
        invariants: Optional[Invariants] = None,
        level_support: int = 2,
        route: Optional[str] = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError("context mode must be one of %s" % (MODES,))
        self.loop = loop
        self.mode = mode
        self.invariants = invariants or Invariants()
        self.level_support = max(1, int(level_support))
        self.route = route
        self.level: Optional[int] = None
        self.level_start = 0
        self._refuted_seen = 0
        self._cache: Dict[str, Any] = {}
        self.ctx = Context(
            "%s/level:0" % getattr(loop, "episode_id", "episode"),
            owner_verifier=self.invariants.owner_verifier,
        )
        self.stats: Dict[str, Any] = {
            "mode": mode,
            "checks": {},
            "refused": {},
            "shadow_refused": {},
            "invariant_refusals": [],
            "audits": [],
            "revocations": 0,
            "reframing_claims": 0,
        }

    # ------------------------------------------------------------ the one entry point
    def check(self, req: Request) -> Decision:
        """permits() with this gate's context, invariants and policy, plus bookkeeping."""
        if self.mode != "off" and getattr(self.loop, "obs", None) is not None:
            self.refresh()
        d = permits(
            self.ctx,
            req,
            invariants=self.invariants,
            policy=ARC_POLICY if self.mode != "off" else None,
        )
        k = req.kind
        self.stats["checks"][k] = self.stats["checks"].get(k, 0) + 1
        if not d.allowed:
            bucket = (
                "refused"
                if (d.layer == "invariant" or self.mode == "enforce")
                else "shadow_refused"
            )
            self.stats[bucket][d.rule] = self.stats[bucket].get(d.rule, 0) + 1
            if d.layer == "invariant":
                self.stats["invariant_refusals"].append(
                    {"kind": k, "name": req.name, "reason": d.reason}
                )
            self._log(
                {
                    "event": "permission_refused",
                    "kind": k,
                    "name": req.name,
                    "layer": d.layer,
                    "rule": d.rule,
                    "reason": d.reason,
                    "enforced": bucket == "refused",
                }
            )
        return d

    def enforced(self, d: Decision) -> bool:
        """True when ``d`` must stop the request."""
        return not d.allowed and (d.layer == "invariant" or self.mode == "enforce")

    # ------------------------------------------------------------ context upkeep
    def refresh(self) -> None:
        loop = self.loop
        if self.level != loop.level:
            self.on_level_start()
        clock = len(loop.history)
        self.ctx.domain.update(
            level=loop.level,
            level_transitions=clock - self.level_start,
            previous_program=bool(getattr(loop, "level_programs", {})),
        )
        # narrowing: every refutation since the last refresh contradicts what it backed
        refuted = loop.hyps.refuted
        for h in refuted[self._refuted_seen :]:
            if h.kind not in ("predict", "goal"):
                continue
            key = ("rule:" if h.kind == "predict" else "goal:") + h.name
            if key in self.ctx.facts and self.ctx.facts[key].status != CONTRADICTED:
                self.ctx.narrow(
                    key,
                    Evidence("replay", "t%d" % h.refuted_at_transition, h.reason[:120]),
                    clock=clock,
                )
                self.stats["revocations"] += 1
                self._log(
                    {"event": "permission_revoked", "fact": key, "at": clock, "why": h.reason[:160]}
                )
        self._refuted_seen = len(refuted)
        # widening: level-local replay of every surviving verified rule / goal
        for name, h in list(loop.hyps.active.items()):
            if h.fn is None or h.kind not in ("predict", "goal"):
                continue
            key = ("rule:" if h.kind == "predict" else "goal:") + name
            f = self.ctx.facts.get(key)
            if f is not None and f.bears_permission and f.scope == self.ctx.scope:
                continue
            if h.kind == "predict" and h.status != "verified":
                continue
            start = self.level_start
            if f is not None and f.status == CONTRADICTED:
                start = max(start, f.since)
            if clock <= start:
                continue
            rep = self._replay(name, h, start, clock)
            if not rep.get("ok"):
                continue
            if h.kind == "predict":
                support = int(rep.get("cell_claims", 0))
                if support < self.level_support:
                    continue
                ev = Evidence(
                    "replay", "t%d..t%d" % (start, clock - 1), "%d cell claims, 0 wrong" % support
                )
            else:
                if f is not None and f.status == CONTRADICTED:
                    continue  # a goal contradicted on this level stays out until the next audit
                ev = Evidence(
                    "replay", "t%d..t%d" % (start, clock - 1), "consistent on this level's frames"
                )
            try:
                self.ctx.widen(
                    key, name, evidence=(ev,), provenance=Provenance.VERIFIED, clock=clock
                )
            except PermissionError:
                continue

    def _replay(self, name: str, h: Any, start: int, clock: int) -> Dict[str, Any]:
        ck = (name, id(h.fn), start, clock)
        hit = self._cache.get(name)
        if hit is not None and hit[0] == ck:
            return hit[1]
        rep = self.loop.hyps.replay_check(
            h.fn, self.loop.history, h.kind, caller=self.loop._hyp_call, start=start
        )
        self._cache[name] = (ck, rep)
        return rep

    def on_level_start(self) -> Dict[str, Any]:
        """The contract audit: carry every held belief, then test it on the new level's
        first frame. Returns (and logs) the audit report."""
        loop = self.loop
        self.level = loop.level
        # the level began after the last level-up transition (the gate may first look late)
        self.level_start = 0
        for t in reversed(loop.history.items):
            if t.level_up:
                self.level_start = t.i + 1
                break
        self._cache.clear()
        carried = self.ctx.rescope(
            "%s/level:%d" % (getattr(loop, "episode_id", "episode"), loop.level)
        )
        report: Dict[str, Any] = {
            "level": loop.level,
            "at": self.level_start,
            "carried": carried,
            "contradicted": [],
            "passed": [],
        }
        obs = getattr(loop, "obs", None)
        state = getattr(obs, "state", None)
        if self.level_start < len(
            loop.history
        ):  # audit the level's FIRST frame, not the current one
            state = loop.history[self.level_start].before
        if state is not None and getattr(state, "size", 0):
            acts = self._audit_actions()
            for name, h in list(loop.hyps.active.items()):
                if h.fn is None or h.kind not in ("predict", "goal"):
                    continue
                key = ("rule:" if h.kind == "predict" else "goal:") + name
                why = self._audit_one(h, state, acts)
                if why:
                    self.ctx.narrow(
                        key,
                        Evidence("audit", "level %d start" % loop.level, why),
                        clock=self.level_start,
                    )
                    report["contradicted"].append({"fact": key, "why": why})
                elif h.kind == "goal":
                    # a goal that is not yet satisfied on the first frame is consistent here
                    self.ctx.widen(
                        key,
                        name,
                        evidence=(
                            Evidence(
                                "audit",
                                "level %d start" % loop.level,
                                "not satisfied on the first frame",
                            ),
                        ),
                        clock=self.level_start,
                    )
                    report["passed"].append(key)
                else:
                    report["passed"].append(key + " (well-formed; carried until it replays here)")
        self.stats["audits"].append(report)
        self._log({"event": "contract_audit", **report})
        return report

    def _audit_actions(self) -> List[Any]:
        loop = self.loop
        acts = [tuple(int(v) for v in a) for a in (loop._hook("candidates", default=[]) or [])]
        if not acts:
            acts = [(int(a), -1, -1) for a in (loop._hook("available_actions", default=[]) or [])]
        return acts[:8]

    def _audit_one(self, h: Any, state: Any, acts: List[Any]) -> str:
        from ._vendor.memory import ActionArg, goal_satisfied

        loop = self.loop
        if h.kind == "goal":
            try:
                v = loop._hyp_call(h.fn, state.copy())
            except Exception as exc:  # noqa: BLE001 - a raising goal is contradicted, not a crash
                return "raised on the first frame: %s" % type(exc).__name__
            return "already satisfied on this level's first frame" if goal_satisfied(v) else ""
        errors = 0
        for a in acts:
            try:
                p = loop._hyp_call(h.fn, state.copy(), ActionArg(a))
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            shape = getattr(p, "shape", None)
            if shape is not None and tuple(shape) != tuple(state.shape):
                return "predicts a %s frame on a %s level" % (tuple(shape), tuple(state.shape))
        if acts and errors == len(acts):
            return "raised on every action from the first frame"
        return ""

    # ------------------------------------------------------------ install
    def install(self) -> "ContextGate":
        loop = self.loop
        ep = self._episode_name()
        d = self.check(Request("episode", ep))
        if not d.allowed:
            raise InvariantViolation(d)
        ns = loop.sandbox.ns
        loop.step = self._wrap_step(loop.step)
        if "plan" in ns:
            ns["plan"] = self._wrap_plan(ns["plan"])
        if hasattr(loop, "daydream"):
            loop.daydream = self._wrap_daydream(loop.daydream)
        if loop.prism is not None:
            loop.prism.rotate = self._wrap_rotate(loop.prism.rotate)
        loop._turn = self._wrap_turn(loop._turn)
        if "note" in ns:
            ns["note"] = self._wrap_note(ns["note"])
        ns["permissions"] = self.permissions
        if loop.llm is not None:
            self._wrap_llm(loop.llm)
        loop.context_gate = self
        return self

    def _episode_name(self) -> str:
        env = getattr(self.loop, "hooks", None)
        for attr in ("game_id", "seed", "episode"):
            v = getattr(env, attr, None)
            if isinstance(v, (str, int)) and str(v):
                return str(v)
        return str(getattr(self.loop, "episode_id", "episode"))

    def permissions(self) -> Dict[str, Any]:
        """(namespace) What this context permits right now, and why. Read-only."""
        d = self.check(Request("plan"))
        return {"plan": d.allowed, "why": d.reason, "mode": self.mode, **self.ctx.view()}

    def _goal_name(self, goal: Any) -> Optional[str]:
        hyps = self.loop.hyps.active
        if goal is None:
            gs = [h for h in hyps.values() if h.kind == "goal" and h.fn is not None]
            if not gs:
                return None
            gs.sort(key=lambda h: (h.status != "verified", -h.positives, -h.support, h.name))
            return gs[0].name
        if isinstance(goal, str):
            if goal == "novel":
                return None
            h = hyps.get(goal)
            return goal if h is not None and h.kind == "goal" else "<unregistered>"
        if callable(goal):
            for name, h in hyps.items():
                if h.kind == "goal" and h.fn is goal:
                    return name
        return "<unregistered>"

    def _wrap_plan(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        gate = self

        def plan(goal: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            d = gate.check(Request("plan", args={"goal_name": gate._goal_name(goal)}))
            if gate.enforced(d):
                return {
                    "ok": False,
                    "found": False,
                    "refused": True,
                    "path": [],
                    "reason": d.reason,
                }
            return fn(goal, *args, **kwargs)

        plan.__doc__ = getattr(fn, "__doc__", None)
        return plan

    def _wrap_step(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        gate = self

        def step(action: Any, source: str = "model", expect: Any = None) -> Any:
            if gate.mode != "off" and source == "plan":
                d = gate.check(Request("act", source, args={"action": tuple(action)}))
                if gate.enforced(d):
                    raise ValueError("refused by context: " + d.reason)
            t = fn(action, source, expect)
            if gate.mode != "off" and getattr(t, "level_up", False):
                gate.refresh()  # the contract audit runs on the new level's first frame
            return t

        return step

    def _wrap_daydream(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        gate = self

        def daydream(*args: Any, **kwargs: Any) -> Any:
            d = gate.check(
                Request("plan", name="daydream", args={"goal_name": gate._goal_name(None)})
            )
            if gate.enforced(d):
                gate.loop.sandbox.ns["DREAM"] = None
                return None
            return fn(*args, **kwargs)

        return daydream

    def _wrap_rotate(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        gate = self

        def rotate(diagnosis: Any) -> Any:
            s = fn(diagnosis)
            return gate._permit_strategy(s)

        return rotate

    def _permit_strategy(self, s: Any) -> Any:
        from ._vendor.prism import BY_ID

        prism = self.loop.prism
        tried = 0
        while s is not None and tried < len(BY_ID):
            d = self.check(Request("strategy", s.id))
            if not self.enforced(d):
                return s
            tried += 1
            if s.id not in prism.exhausted:
                prism.exhausted.append(s.id)
            nxt = prism.best(exclude=prism.exhausted) or "rule_first"
            prism.active = BY_ID[nxt]
            s = prism.active
        return s

    def _wrap_turn(self, fn: Callable[[], None]) -> Callable[[], None]:
        gate = self

        def turn() -> None:
            if gate.mode != "off":
                gate.refresh()
                if gate.loop.prism is not None:
                    gate._permit_strategy(gate.loop.prism.active)
            fn()

        return turn

    def _wrap_note(self, fn: Callable[[str], None]) -> Callable[[str], None]:
        gate = self

        def note(text: str) -> None:
            gate.ctx.infer("note:%d" % len(gate.ctx.log), str(text)[:160], source="model:note")
            gate._scan_claims(str(text), "model:note")
            fn(text)

        return note

    def _scan_claims(self, text: str, source: str) -> None:
        for m in _GRANT_CLAIM.finditer(text or ""):
            self.stats["reframing_claims"] += 1
            self.ctx.infer(
                "claimed_grant:%d" % self.stats["reframing_claims"], m.group(0)[:120], source=source
            )
            self._log({"event": "reframing_claim", "source": source, "text": m.group(0)[:120]})

    def _wrap_llm(self, llm: Any) -> None:
        gate = self
        chat = llm.chat

        def route_of(obj: Any) -> str:
            for attr in ("route", "model", "name"):
                v = getattr(obj, attr, None)
                if isinstance(v, str) and v:
                    return v
            backend = getattr(obj, "backend", None)
            return route_of(backend) if backend is not None and backend is not obj else ""

        def guarded(*args: Any, **kwargs: Any) -> Any:
            route = gate.route or route_of(llm)
            d = gate.check(Request("model_route", route))
            if not d.allowed:
                raise InvariantViolation(d)
            res = chat(*args, **kwargs)
            served = getattr(res, "model", None)
            if isinstance(served, str) and served and served != route:
                d = gate.check(Request("model_route", served))
                if not d.allowed:
                    raise InvariantViolation(d)
            gate._scan_claims(str(getattr(res, "content", "") or ""), "model:reply")
            return res

        llm.chat = guarded

    def _log(self, rec: Dict[str, Any]) -> None:
        log = getattr(self.loop, "_log", None)
        if log is not None:
            try:
                log(rec)
            except Exception:  # noqa: BLE001 - logging never costs the turn
                _LOG.debug("context-gate log record dropped", exc_info=True)

    def summary(self) -> Dict[str, Any]:
        out = dict(self.stats)
        out["held"] = self.ctx.held()
        out["audits"] = self.stats["audits"][-6:]
        out["invariant_refusals"] = self.stats["invariant_refusals"][-6:]
        return out


def install_context_gate(
    loop: Any,
    *,
    mode: str = "shadow",
    invariants: Optional[Invariants] = None,
    level_support: int = 2,
    route: Optional[str] = None,
) -> ContextGate:
    """Build and install a :class:`ContextGate`; ``loop.summary()`` gains ``context``."""
    gate = ContextGate(
        loop, mode=mode, invariants=invariants, level_support=level_support, route=route
    ).install()
    summary = loop.summary

    def summary_with_context() -> Dict[str, Any]:
        s = summary()
        s["context"] = gate.summary()
        return s

    loop.summary = summary_with_context
    return gate
