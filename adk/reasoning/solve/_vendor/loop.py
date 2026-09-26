# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/loop.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""The h30 reasoning loop: a model plays an Environment by writing Python.

Game-agnostic.  Per turn:

1. INTENT -- classify the situation (new_level / exploring / stuck /
   contradiction / near_goal); it picks the directive and the action budget.
2. PRISM  -- on ``stuck``, diagnose the active strategy and rotate to the
   best remaining one; AUTO strategies (hand-off, scan) act without a model
   call.
3. SASE   -- the model answers Situation / Analysis / Synthesis / Execution;
   the python blocks run in a sandboxed persistent namespace.
4. LEARN  -- before each act the ledger records predictions (inline
   ``expect=`` and every active predict hypothesis); after it, misses refute
   with evidence and hits raise confidence.  All hypotheses are replayed on
   the new transitions every turn.

``mode="plain"`` switches 1-4 off (one python block, fixed budget, no
predictions requested): the ablation baseline.

Memory: (a) ``WorkingMemory`` (bounded turns + rolling summary),
(b) ``Episodic`` transitions, (c) ``HypothesisStore`` (code, replay-checked),
(d) ``SkillLibrary`` + PRISM scores through a ``MemoryBackend``.

Imports: stdlib, numpy and this package only (asserted by a test).
"""
from __future__ import annotations

import logging  # SEAM(adk) drop
import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np
_LOG = logging.getLogger(__name__)  # SEAM(adk) drop

from .evidence import EvidenceTable
from .intent import CONTRADICTION, NEW_LEVEL, STUCK, IntentClassifier, Signals
from .interfaces import Action, Obs
from .learning import PredictionLearner
from .memory import (
    PREDICT_FORMAT,
    ActionArg,
    Episodic,
    HypothesisStore,
    SkillLibrary,
    Transition,
    TurnGist,
    WorkingMemory,
    called_names,
    est_tokens,
    _norm_source,
    goal_satisfied,
    goal_score,
    merge_predictions,
    top_level_defs,
)
from .prism import BY_ID, Prism, diagnose
from .sase import PLAIN_PROTOCOL, SASE_PROTOCOL, grounded_protocol, parse_reply
from .sandbox import ActionCap, GameStopped, RunResult, Sandbox, TurnEnd


@dataclass
class LoopConfig:
    mode: str = "sase"            # "sase" | "plain"
    prism: bool = True
    max_calls: int = 40
    turn_s: float = 20.0
    turn_actions: int = 40        # hard ceiling; intents pick lower budgets
    ctx_tokens: int = 8192
    max_out: int = 1100
    wm_tokens: int = 1800
    temperature: float = 0.4
    fallback_actions: int = 8
    zero_act_turns: int = 2
    print_cap: int = 1500
    stuck_actions: int = 30
    stuck_turns: int = 3
    auto_turns: int = 2           # consecutive AUTO-strategy turns before handing back to the model
    llm_extra: Dict[str, Any] = field(default_factory=dict)
    grounded: bool = True         # h31: evidence table + induced candidates + cited rules (sase only)
    candidates_shown: int = 6
    grounding_chars: int = 2000   # prompt budget of the table + candidates (halved on a context overflow)
    plan_s: float = 5.0           # wall budget of one plan() search
    state_name: str = "state"
    log_path: Optional[str] = None

    @property
    def sase(self) -> bool:
        return self.mode == "sase"


SYSTEM_CORE = """You play an unknown interactive environment by writing Python. Nobody tells you the rules or the goal: discover them by experiment, and reach the goal in as FEW actions as possible.

Code runs in a persistent namespace (variables and functions survive between turns). Per turn: a limited number of actions and {turn_s:.0f} s of compute; the turn ends by itself when a level is completed or lost.

CORE API (already defined):
 act(a, x=None, y=None, expect=None) -> r   # one action; r.changed, r.level_up, r.died, r.diff, r.{state_name}
                                  #   expect = your prediction (dict of claims, full next state, or a hypothesis name)
 history                          # every transition: history[i].before/.after/.action=(a,x,y)/.changed/.level/.level_up/.died;
                                  #   history.where(action=1, changed=True), history.last(3), history.summary()
 replay_check(fn, kind="predict") # score predict(state, action) against ALL transitions (level-ups and deaths skipped)
 hypothesize(fn, kind="predict"|"goal", note="")  # keep a rule predict(state, action) or goal(state)->bool/[0..1];
                                  #   kept only while every claim it makes replays correctly; H[name] = verified ones
                                  #   predict: action == its id (int), action[1:] = (x, y); PREDICT_RET
                                  #   HUD / step-counter cells are never judged; claim only what the rule knows
 {plan_api}note(text)                       # pin a short fact into memory
 save_skill(fn, doc="")           # keep a helper for later episodes (defs used in successful turns are kept automatically)
 print(...)                       # shown to you next turn (capped). Modules: np, math, collections, itertools, heapq
{domain}

{protocol}"""


PLAN_API = (
    "plan(goal=None, max_depth=30)    # BFS in your VERIFIED predict rules (a simulator) to a state where goal(state) is\n"
    "                                  #   True (default: a verified goal, else any unseen state), then act that path\n"
    " evidence()                       # the full EVIDENCE TABLE as text\n ")


class ActResult:
    __slots__ = ("state", "frame", "changed", "board_changed", "level_up", "died", "diff", "level", "action")

    def __init__(self, t: Transition, state: np.ndarray, level: int, diff: str) -> None:
        self.state = self.frame = state
        self.changed = t.changed
        self.level_up = t.level_up
        self.died = t.died
        self.diff = diff
        self.board_changed = not diff.startswith(("no change", "NO board change"))
        self.level = level
        self.action = t.action

    def __repr__(self) -> str:
        return "<act %s: changed=%d level_up=%s died=%s | %s>" % (
            self.action, self.changed, self.level_up, self.died, self.diff)


class ReasoningLoop:
    """One episode (game).  ``run()`` plays until the environment stops it."""

    def __init__(self, env: Any, llm: Any, cfg: LoopConfig, backend: Any = None,
                 hooks: Any = None, episode_id: str = "episode", *,
                 sink: Optional[Callable[[Dict[str, Any]], None]] = None, governor: Any = None) -> None:
        # SEAM(adk): ``sink(record)`` receives every log record; ``governor.check()``
        # runs at the head of each turn and on every traced line of model code.
        # Both None = h30 behaviour.
        self.sink = sink
        self.governor = governor
        self.env = env
        self.llm = llm
        self.cfg = cfg
        self.hooks = hooks if hooks is not None else env
        self.episode_id = episode_id
        self.history = Episodic()
        self.hyps = HypothesisStore(ignore_fn=getattr(self.hooks, "ignore_cells", None),
                                    changed_fn=getattr(self.hooks, "significant_change", None),
                                    skip_fn=getattr(self.hooks, "not_dynamics", None))
        self.wm = WorkingMemory(budget_tokens=cfg.wm_tokens)
        self.skills = SkillLibrary(backend)
        self.prism = Prism(backend) if cfg.prism else None
        self.intents = IntentClassifier(stuck_actions=cfg.stuck_actions, stuck_turns=cfg.stuck_turns,
                                        max_turn_actions=cfg.turn_actions)
        self.sandbox = Sandbox(time_cap_s=cfg.turn_s, print_cap=cfg.print_cap,
                               cancel_check=governor.check if governor is not None else None)  # SEAM(adk)
        self.learner = PredictionLearner(self.hyps, caller=self._hyp_call,
                                         changed_fn=getattr(self.hooks, "significant_change", None))
        self.notes: deque = deque(maxlen=8)
        domain = self._hook("primer", default="")
        protocol = PLAIN_PROTOCOL
        if cfg.sase:
            protocol = grounded_protocol() if cfg.grounded else SASE_PROTOCOL
        self.system = SYSTEM_CORE.format(turn_s=cfg.turn_s, state_name=cfg.state_name, domain=domain,
                                         protocol=protocol, plan_api=PLAN_API if cfg.sase and cfg.grounded else ""
                                         ).replace("PREDICT_RET", PREDICT_FORMAT)
        # h31 grounding: the evidence table, induced candidate rules, refutation evidence
        self.evidence = EvidenceTable()
        self.candidates: Dict[str, Dict[str, Any]] = {}
        self.passing: Dict[str, Dict[str, Any]] = {}
        self.turn_refutations: List[str] = []
        self.grounding_budget = int(cfg.grounding_chars)
        self.obs: Optional[Obs] = None
        self.level = 0
        self.actions = 0
        self.level_start_action = 0
        # novelty (stuck detection)
        self.seen_keys: Set[str] = set()
        self.actions_since_novel = 0
        self.turn_novel = 0
        # turn state
        self.turn = 0
        self.turn_cap = cfg.turn_actions
        self.turn_actions = self.turn_level_ups = self.turn_deaths = self.turn_changed = 0
        self.turn_defs: Dict[str, Dict[str, str]] = {}
        self.all_defs: Dict[str, Dict[str, str]] = {}
        self.hyp_names: Set[str] = set()
        self.last_result = "(first turn: nothing has happened yet)"
        self.refuted_pending: List[str] = []
        self.levels_at_last_turn = -1
        self.turns_without_progress = 0
        self.zero_streak = 0
        self.consecutive_errors = 0
        self.endpoint_dead = False
        self._dead_chunks = 0
        self.fatal: Optional[str] = None
        self.auto_streak = 0
        self.strategy_turns = 0
        self.strategy_window = {"actions": 0, "novel": 0, "verified": 0, "refuted": 0, "errors": 0, "turns": 0}
        self.intent_name = ""
        self.level_programs: Dict[int, Dict[str, Any]] = {}
        self._level_code: List[str] = []
        self._level_actions: List[Action] = []
        self._log_fh: Any = None
        self._t0 = time.perf_counter()
        self.stats: Dict[str, Any] = {
            "llm_calls": 0, "llm_errors": 0, "llm_s": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            "turns": 0, "turn_errors": 0, "timeouts": 0, "no_code": 0, "fallback_actions": 0,
            "actions_model": 0, "actions_explore": 0, "actions_bfs": 0, "actions_scan": 0, "deaths": 0,
            "skills_loaded": 0, "skills_saved_new": 0, "skill_reuse_calls": 0, "skills_reused": [],
            "evicted_turns": 0, "intents": {}, "sase_turns_all_phases": 0, "sase_phases_seen": 0,
            "auto_turns": 0, "level_attribution": [], "mode": cfg.mode, "prism": cfg.prism,
            "grounded": bool(cfg.grounded and cfg.sase), "uncited_rejected": 0, "hyp_origin": {},
            "candidates_passing_max": 0, "candidates_adopted": [], "actions_plan": 0, "plan_calls": 0,
            "plan_found": 0, "grounding_s": 0.0}
        self._install_namespace()

    # ------------------------------------------------------------ helpers
    def _hook(self, name: str, *args: Any, default: Any = None) -> Any:
        fn = getattr(self.hooks, name, None)
        if fn is None:
            return default
        return fn(*args)

    def _key(self, state: np.ndarray) -> str:
        k = self._hook("state_key", state)
        if k is not None:
            return str(k)
        return hashlib.blake2b(np.ascontiguousarray(state).tobytes(), digest_size=8).hexdigest()

    def _resolve_expect(self, expect: Any, action: Action) -> Any:
        """``expect`` may be claims, a full next state, a predict function,
        or the NAME of one.  An active predict hypothesis is already run by
        the ledger; any other named/callable predictor is evaluated here and
        scored as an inline prediction.  Never raises: a bad prediction must
        not cost the actions."""
        if isinstance(expect, str):
            h = self.hyps.active.get(expect)
            if h is not None and h.kind == "predict" and h.fn is not None:
                return None
            expect = self.sandbox.ns.get(expect)
        if callable(expect):
            try:
                assert self.obs is not None
                v = self._hyp_call(expect, self.obs.state.copy(), ActionArg(action))
            except Exception:  # noqa: BLE001 - an unusable prediction is just skipped
                return None
            return v if v is None or isinstance(v, dict) else np.asarray(v)
        return expect

    def _hyp_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return self.sandbox.call(fn, *args, cap_s=max(2.0, self.cfg.turn_s / 2))

    def _describe(self, t: Transition) -> str:
        d = self._hook("describe", t)
        if d is not None:
            return str(d)
        return "%d cells changed" % t.changed if t.changed else "no change"

    # ------------------------------------------------------------ acting
    def step(self, action: Action, source: str = "model", expect: Any = None) -> Transition:
        """One environment action, fully booked (episodic, novelty, learning)."""
        assert self.obs is not None
        before = self.obs.state.copy()
        level = self.level
        preds = self.learner.before(before, action, expect) if source in ("model", "plan") else []
        with self.sandbox.paused():
            obs = self.env.act(action, source=source)
        self.obs = obs
        cost = int(obs.info.get("cost", 1)) if obs.info else 1
        self.actions += cost
        self.turn_actions += 1
        key = "actions_" + source
        self.stats[key] = self.stats.get(key, 0) + 1
        after = obs.info.get("transition_after") if obs.info else None
        if after is None:
            after = obs.state
        t = self.history.add(level, action, before, after if after.size else before, level_up=obs.level_up,
                             died=obs.died, win=obs.win_state, source=source, turn=self.turn)
        self._level_actions.append(action)
        if preds:
            self.learner.after(preds, before, t.after, obs.level_up, t.i, action, self.turn, level,
                               died=obs.died or self.hyps.not_dynamics(t))
        if t.changed:
            self.turn_changed += 1
        k = self._key(obs.state) if obs.state.size else ""
        if k and k not in self.seen_keys:
            self.seen_keys.add(k)
            self.actions_since_novel = 0
            self.turn_novel += 1
        else:
            self.actions_since_novel += 1
        if obs.died:
            self.turn_deaths += 1
            self.stats["deaths"] += 1
        if obs.level_up:
            self.level = obs.level
            self.turn_level_ups += 1
            self.stats["level_attribution"].append({
                "level": level + 1, "source": source, "intent": self.intent_name,
                "strategy": self.prism.active.id if self.prism else None, "turn": self.turn,
                "actions_on_level": self.actions - self.level_start_action})
            self.level_programs[level] = {"actions": list(self._level_actions),
                                          "code": list(self._level_code[-2:])}
            self._level_actions, self._level_code = [], []
            self.level_start_action = self.actions
            self.seen_keys = {k} if k else set()
            self.actions_since_novel = 0
            self._log({"event": "level_up", "level": self.level, "actions": self.actions})
        if obs.done or self.env.done():
            raise GameStopped("episode over")
        return t

    def auto(self, n: int, source: str = "explore") -> Dict[str, Any]:
        """n actions from the domain's non-LLM policy."""
        k = ch = deaths = 0
        lvl = False
        for _ in range(max(0, int(n))):
            a = self._hook("auto_action")
            if a is None:
                break
            t = self.step(tuple(int(v) for v in a), source)  # type: ignore[arg-type]
            k += 1
            ch += t.changed > 0
            deaths += t.died
            if t.level_up:
                lvl = True
                break
        return {"actions": k, "changed": ch, "deaths": deaths, "level_up": lvl, "level": self.level}

    def scan(self, n: int) -> Dict[str, Any]:
        """Systematic pass: each candidate not yet tried on this level, once."""
        tried = {t.action for t in self.history.where(level=self.level)}
        cands = [tuple(int(v) for v in a) for a in (self._hook("candidates", default=[]) or [])]
        todo = [a for a in cands if a not in tried][:max(0, int(n))]
        k = ch = 0
        lvl = False
        for a in todo:
            t = self.step(a, "scan")  # type: ignore[arg-type]
            k += 1
            ch += t.changed > 0
            if t.level_up:
                lvl = True
                break
        return {"actions": k, "changed": ch, "level_up": lvl, "candidates": len(cands), "untried": len(todo)}

    # ------------------------------------------------------------ the run
    def run(self) -> None:
        try:
            self.obs = self.env.observe()
            self.level = int(self.obs.level)
            if self.obs.state.size:
                self.seen_keys.add(self._key(self.obs.state))
            self.stats["skills_loaded"] = len(self.skills.install(self.sandbox.define))
            self._open_log()
            while True:
                if self.env.done():
                    break
                if self.endpoint_dead or self.llm is None or self.stats["llm_calls"] >= self.cfg.max_calls:
                    r = self.auto(25)
                    self.stats["fallback_actions"] += r["actions"]
                    if r["actions"] == 0:
                        break
                    if self.endpoint_dead and not self.fatal:  # an outage may be transient: re-probe
                        self._dead_chunks += 1
                        if self._dead_chunks >= 4:
                            self._dead_chunks = 0
                            self.endpoint_dead = False
                            self.consecutive_errors = 2  # one more failure re-declares it dead
                    continue
                self._turn()
        except GameStopped:
            _LOG.debug("game stopped", exc_info=True)  # SEAM(adk) pass
        finally:
            self._finish()

    def _signals(self) -> Signals:
        gp = 0.0
        if self.obs is not None and self.obs.state.size:
            for h in list(self.hyps.active.values()):
                if h.kind == "goal" and h.fn is not None:
                    try:
                        gp = max(gp, goal_score(self._hyp_call(h.fn, self.obs.state.copy())))
                    except Exception:  # noqa: BLE001 - a broken goal simply scores 0
                        continue
        s = Signals(first_turn=self.turn == 1,
                    levels_gained=max(0, self.level - self.levels_at_last_turn) if self.levels_at_last_turn >= 0 else 0,
                    refuted_since_last=list(self.refuted_pending),
                    actions_since_novel=self.actions_since_novel,
                    turns_without_progress=self.turns_without_progress, goal_progress=gp)
        return s

    def _turn(self) -> None:
        if self.governor is not None:
            self.governor.check()  # SEAM(adk)
        self.turn += 1
        self.stats["turns"] += 1
        self.turn_actions = self.turn_level_ups = self.turn_deaths = self.turn_changed = 0
        self.turn_novel = 0
        verified_before = set(self.hyps.n_verified_ever)
        refuted_before = self.hyps.n_refuted
        level_before = self.level
        decision = None
        if self.cfg.sase or self.prism is not None:
            decision = self.intents.classify(self._signals())
            self.intent_name = decision.intent
            self.stats["intents"][decision.intent] = self.stats["intents"].get(decision.intent, 0) + 1
        self.refuted_pending = []
        self.levels_at_last_turn = self.level
        self.turn_cap = decision.turn_actions if (decision and self.cfg.sase) else self.cfg.turn_actions

        # PRISM: rotate on a stall; AUTO strategies act without the model
        strategy = self.prism.active if self.prism else None
        if self.prism is not None and decision is not None:
            if decision.intent == STUCK and self.strategy_turns >= 1:
                w = self.strategy_window
                strategy = self.prism.rotate(diagnose(self.prism.active.id, w["actions"], w["novel"],
                                                      w["verified"], w["refuted"], w["errors"], w["turns"]))
                self._reset_window()
                self.turns_without_progress = 0
                self.actions_since_novel = 0
                self._log({"event": "rotate", "turn": self.turn, "to": strategy.id,
                           "why": self.prism.log[-1]["why"]})
            elif decision.intent == NEW_LEVEL and strategy is not None and strategy.kind == "auto":
                strategy = self.prism.active = BY_ID[self.prism.best(initial=True) or "rule_first"]
                self._reset_window()
        if strategy is not None and strategy.kind == "auto":
            self._auto_turn(strategy, verified_before, level_before)
            return
        self.auto_streak = 0
        rr, content, ok_code = self._model_turn(decision, strategy)
        if rr is None:
            return
        self._post_turn(rr, content, ok_code, verified_before, refuted_before, level_before, strategy)

    def _reset_window(self) -> None:
        self.strategy_turns = 0
        self.strategy_window = {"actions": 0, "novel": 0, "verified": 0, "refuted": 0, "errors": 0, "turns": 0}

    def _auto_turn(self, strategy: Any, verified_before: Set[str], level_before: int) -> None:
        self.stats["auto_turns"] += 1
        self.auto_streak += 1
        if strategy.id == "click_scan":
            r = self.scan(strategy.auto_actions)
            if r["actions"] == 0:  # nothing left to scan: explorer instead
                r = self.auto(strategy.auto_actions)
        else:
            r = self.auto(strategy.auto_actions)
        refuted = self.hyps.recheck(self.history, caller=self._hyp_call)
        self.refuted_pending += refuted
        self._rule_first_on_verified(set(self.hyps.n_verified_ever) - verified_before)
        new_ver = len(set(self.hyps.n_verified_ever) - verified_before)
        assert self.prism is not None
        self.prism.record(strategy.id, self.turn_actions, self.level - level_before, new_ver)
        self._window_add(self.turn_actions, self.turn_novel, new_ver, len(refuted), 0)
        self.last_result = "(strategy %s acted without you: %d actions, %d changed the state, %d new states, " \
                           "levels +%d)" % (strategy.id, r.get("actions", 0), r.get("changed", 0),
                                            self.turn_novel, self.level - level_before)
        self._progress_bookkeeping(level_before, new_ver)
        self._log({"event": "auto", "turn": self.turn, "strategy": strategy.id, "result": r})
        if self.auto_streak >= self.cfg.auto_turns and self.level == level_before:
            # hand back to the model; with every reasoning frame exhausted, start a new cycle
            llm_left = [sid for sid in self.prism.allowed
                        if BY_ID[sid].kind == "llm" and sid not in self.prism.exhausted]
            if not llm_left:
                self.prism.exhausted = [sid for sid in self.prism.exhausted if BY_ID[sid].kind != "llm"]
            best = self.prism.best(initial=True, exclude=self.prism.exhausted) or "rule_first"
            if BY_ID[best].kind != "llm":
                best = "rule_first"
            self.prism.active = BY_ID[best]
            self.auto_streak = 0
            self._reset_window()

    def _window_add(self, actions: int, novel: int, verified: int, refuted: int, errors: int) -> None:
        w = self.strategy_window
        w["actions"] += actions
        w["novel"] += novel
        w["verified"] += verified
        w["refuted"] += refuted
        w["errors"] += errors
        w["turns"] += 1
        self.strategy_turns += 1

    def _progress_bookkeeping(self, level_before: int, new_verified: int) -> None:
        if self.turn_novel > 0 or self.level > level_before or new_verified > 0:
            self.turns_without_progress = 0
        else:
            self.turns_without_progress += 1

    # ------------------------------------------------------------ model turn
    def _render_user(self, decision: Any, strategy: Any) -> str:
        assert self.obs is not None
        last = self.history[-1] if len(self.history) else None
        parts: List[str] = []
        if decision is not None and self.cfg.sase:
            parts.append("INTENT: %s (%s). %s  [this turn: at most %d actions]" % (
                decision.intent.upper(), decision.reason, decision.directive, self.turn_cap))
            if decision.intent == CONTRADICTION and self.learner.ledger.recent_misses:
                parts.append("Evidence: " + " | ".join(self.learner.ledger.recent_misses[-3:]))
        if strategy is not None and self.prism is not None:
            extra = ""
            if strategy.id == "analogy" or (decision is not None and decision.intent == NEW_LEVEL):
                extra = self._analogy_text()
            parts.append(self.prism.overlay(extra))
        situation = self._hook("render", self.obs, last)
        if situation is None:
            situation = "level %d, actions %d, available %s" % (
                self.level + 1, self.actions, self._hook("available_actions", default=[]))
        parts.append(("SITUATION (observed):\n" if self.cfg.sase else "") + str(situation))
        if self._grounded():
            parts.append(self._grounding_text())
        parts.append("Result of your previous code:\n" + self.last_result)
        mem = [
            "MEMORY",
            "Episodic (this level): " + self.history.summary(level=self.level),
            "Earlier turns: " + (self.wm.summary_text() or "(none dropped)"),
            "Notes: " + (" | ".join(self.notes) if self.notes else "(none)"),
            "Hypotheses (rechecked on ALL history every turn):\n" + self.hyps.digest(),
            "Skills (callable now):\n" + self.skills.digest(),
        ]
        if self.cfg.sase:
            lg = self.learner.ledger
            mem.append("Predictions so far: %d made, %d right (calibration %.0f%%)" % (
                lg.made, lg.hits, 100 * lg.calibration()))
        parts.append("\n".join(mem))
        parts.append("Now write SITUATION / ANALYSIS / SYNTHESIS / EXECUTION." if self.cfg.sase
                     else "Write the next python block.")
        return "\n\n".join(parts)

    def _analogy_text(self) -> str:
        if not self.level_programs:
            return ""
        lv = max(self.level_programs)
        prog = self.level_programs[lv]
        acts = _compress_actions(prog["actions"])
        code = "\n".join(prog["code"])[-900:]
        return "Previous level (%d) was won with %d actions: %s%s" % (
            lv + 1, len(prog["actions"]), acts, ("\nIts last code:\n" + code) if code else "")

    def _model_turn(self, decision: Any, strategy: Any) -> Tuple[Optional[RunResult], str, bool]:
        if self._grounded():
            self._refresh_grounding()
        user_now = self._render_user(decision, strategy)
        header = "[turn %d | level %d | actions %d%s]\n" % (
            self.turn, self.level + 1, self.actions, (" | intent %s" % decision.intent) if decision else "")
        fixed = est_tokens(self.system) + est_tokens(user_now) + self.cfg.max_out + 300
        self.stats["evicted_turns"] += self.wm.evict_to(max(0, self.cfg.ctx_tokens - fixed))
        msgs = [{"role": "system", "content": self.system}] + self.wm.messages() + [
            {"role": "user", "content": user_now}]
        t0 = time.perf_counter()
        try:
            res = self.llm.chat(msgs, max_tokens=self.cfg.max_out, temperature=self.cfg.temperature,
                                extra=self.cfg.llm_extra or None)
        except Exception as exc:  # noqa: BLE001 - every client failure means "fall back"
            self.stats["llm_errors"] += 1
            self.stats["llm_s"] += time.perf_counter() - t0
            if getattr(exc, "fatal", False):  # e.g. a cross-model route: never measure the wrong model
                self.fatal = str(exc)[:300]
                self.stats["fatal"] = self.fatal
                self.endpoint_dead = True
                self._log({"event": "fatal", "turn": self.turn, "error": self.fatal})
                return None, "", False
            # a reply that ran out of tokens while reasoning is the MODEL's failure, not the
            # endpoint's: it costs the turn but never declares the endpoint dead
            if "token limit while reasoning" not in str(exc):
                self.consecutive_errors += 1
            else:
                self.stats["reasoning_overruns"] = self.stats.get("reasoning_overruns", 0) + 1
            if getattr(exc, "status", 0) == 400 and "context length" in str(exc) and self._grounded():
                # the grounding block is the part of the prompt that grows: shrink it
                self.grounding_budget = max(300, self.grounding_budget // 2)
                self.stats["grounding_squeezed"] = self.stats.get("grounding_squeezed", 0) + 1
            if getattr(exc, "status", 0) == 400 and self.wm.pairs:
                self.stats["evicted_turns"] += self.wm.evict_to(max(0, self.wm.tokens() - 1))
            if self.consecutive_errors >= 3:
                self.endpoint_dead = True
            r = self.auto(self.cfg.fallback_actions)
            self.stats["fallback_actions"] += r["actions"]
            self.last_result = "(the model call failed; the explorer took %d actions)" % r["actions"]
            self._log({"event": "llm_error", "turn": self.turn, "error": str(exc)[:300]})
            return None, "", False
        self.consecutive_errors = 0
        self.stats["llm_calls"] += 1
        served = str(getattr(res, "model", "") or "?")
        sm = self.stats.setdefault("served_models", {})
        sm[served] = sm.get(served, 0) + 1
        self.stats["llm_s"] += time.perf_counter() - t0
        usage = getattr(res, "usage", {}) or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        self.stats["completion_tokens"] += int(usage.get("completion_tokens", 0))
        content = getattr(res, "content", "") or ""
        parsed = parse_reply(content)
        if self.cfg.sase:
            self.stats["sase_phases_seen"] += len(parsed.found)
            if len(parsed.found) == 4:
                self.stats["sase_turns_all_phases"] += 1
            blocks = parsed.blocks
        else:
            blocks = parsed.blocks[-1:]  # plain: the last block, as before
        code = "\n\n".join(c for _p, c in blocks)
        self.turn_defs = top_level_defs(code) if code else {}
        self.all_defs.update(self.turn_defs)
        ns = self.sandbox.ns
        ns[self.cfg.state_name] = self.obs.state.copy() if self.obs is not None else None
        ns["available"] = list(self._hook("available_actions", default=[]) or [])
        ns["level"] = self.level
        if not blocks:
            self.stats["no_code"] += 1
            rr = RunResult(False, "", "no ```python block found in your reply")
        else:
            outs, errs, stop, ok = [], [], "", True
            for i, (phase, c) in enumerate(blocks):
                r = self.sandbox.run(c, filename="<h30-turn-%d-%d>" % (self.turn, i))
                if r.stdout.strip():
                    outs.append(("[%s] " % phase if phase else "") + r.stdout.rstrip())
                if r.error:
                    errs.append(("[%s] " % phase if phase else "") + r.error)
                ok = ok and r.ok
                if r.stopped_by:
                    stop = r.stopped_by
                    break
            rr = RunResult(ok, "\n".join(outs), "\n".join(errs), stop)
        if code:
            self._level_code.append(code[-1500:])
        self._log({"event": "turn", "turn": self.turn, "level": self.level, "actions": self.actions,
                   "intent": decision.intent if decision else None,
                   "strategy": strategy.id if strategy is not None else None,
                   "usage": usage, "latency_s": round(float(getattr(res, "latency_s", 0.0)), 2),
                   "phases": parsed.found, "content": content})
        self._pending_header = header
        return rr, content, bool(code)

    def _post_turn(self, rr: RunResult, content: str, has_code: bool, verified_before: Set[str],
                   refuted_before: int, level_before: int, strategy: Any) -> None:
        if not rr.ok:
            self.stats["turn_errors"] += 1
        if rr.stopped_by == "timeout":
            self.stats["timeouts"] += 1
        refuted = self.hyps.recheck(self.history, caller=self._hyp_call)
        online_refuted = [h.name for h in self.hyps.refuted[refuted_before:] if h.kind != "claim"
                          and h.name not in refuted]
        for name in refuted + online_refuted:
            self.sandbox.ns["H"].pop(name, None)
        verified_names = {n for n, h in self.hyps.verified().items()}
        self.refuted_pending += [n for n in refuted + online_refuted if n in self.hyps.n_verified_ever]
        newly_verified = set(self.hyps.n_verified_ever) - verified_before
        by_name = {h.name: h for h in self.hyps.refuted[refuted_before:]}
        for name in refuted + online_refuted:
            h = by_name.get(name)
            if h is not None:
                self.turn_refutations.append("%s%s: %s" % (
                    name, (" [cites %s]" % ",".join(h.cites)) if h.cites else "", h.reason[:200]))
        self._rule_first_on_verified(newly_verified)
        for name, h in self.hyps.verified().items():
            if h.fn is not None:
                self.sandbox.ns["H"][name] = h.fn
        success = (rr.ok and self.turn_actions > 0 and (self.turn_changed > 0 or self.turn_level_ups > 0)) \
            or bool(newly_verified) or self.turn_level_ups > 0
        self._promote_skills(self._last_code(content), success)
        if self.prism is not None and strategy is not None:
            self.prism.record(strategy.id, self.turn_actions, self.level - level_before, len(newly_verified))
            self._window_add(self.turn_actions, self.turn_novel, len(newly_verified),
                             len(refuted) + len(online_refuted), 0 if rr.ok else 1)
        self._progress_bookkeeping(level_before, len(newly_verified))
        result = self._render_result(rr, refuted + online_refuted, newly_verified)
        gist = TurnGist(turn=self.turn, acts=self.turn_actions, level_ups=self.turn_level_ups,
                        deaths=self.turn_deaths, text=_gist_text(rr, content))
        self.wm.add_turn(self._pending_header + "Result of your previous code:\n" + self.last_result, content, gist)
        self._log({"event": "result", "turn": self.turn, "result": result, "success": success,
                   "verified_now": sorted(verified_names)})
        self.last_result = result
        if self.turn_actions == 0:
            self.zero_streak += 1
            if self.zero_streak >= self.cfg.zero_act_turns:
                r = self.auto(self.cfg.fallback_actions)
                self.stats["fallback_actions"] += r["actions"]
                self.last_result += "\n(no actions for %d turns, so the explorer took %d)" % (
                    self.zero_streak, r["actions"])
                self.zero_streak = 0
        else:
            self.zero_streak = 0

    def _last_code(self, content: str) -> str:
        parsed = parse_reply(content)
        blocks = parsed.blocks if self.cfg.sase else parsed.blocks[-1:]
        return "\n\n".join(c for _p, c in blocks)

    def _promote_skills(self, code: str, success: bool) -> None:
        if not code:
            return
        changed = False
        for name in called_names(code):
            if name in self.hyp_names:
                continue
            if name in self.all_defs:
                if success:
                    d = self.all_defs[name]
                    if self.skills.record_success(name, d["source"], d["doc"], d["sig"], self.episode_id):
                        self.stats["skills_saved_new"] += 1
                    changed = True
            elif name in self.skills.skills:
                cross = self.skills.record_use(name, self.episode_id, success)
                changed = True
                if cross:
                    self.stats["skill_reuse_calls"] += 1
                    if name not in self.stats["skills_reused"]:
                        self.stats["skills_reused"].append(name)
        if changed:
            try:
                self.skills.save()
            except OSError:
                _LOG.debug("skills.save() failed", exc_info=True)  # SEAM(adk) pass

    def _render_result(self, rr: RunResult, refuted: List[str], verified: Set[str]) -> str:
        parts = []
        if rr.stdout.strip():
            parts.append("stdout:\n" + rr.stdout.rstrip())
        if rr.error:
            label = {"timeout": "STOPPED (compute cap)", "action_cap": "STOPPED (action cap)",
                     "turn_end": "TURN ENDED"}.get(rr.stopped_by, "ERROR")
            parts.append("%s: %s" % (label, rr.error))
        parts.append("You took %d action(s); %d changed the state; %d new states; level ups %d; deaths %d." % (
            self.turn_actions, self.turn_changed, self.turn_novel, self.turn_level_ups, self.turn_deaths))
        if verified:
            parts.append("Newly VERIFIED hypotheses: %s" % ", ".join(sorted(verified)))
        if refuted:
            parts.append("REFUTED by new evidence: %s" % ", ".join(refuted))
        if self.turn_refutations:
            parts.append("REFUTATION EVIDENCE (the data that broke these rules -- repair against it, never "
                         "re-propose):\n" + "\n".join("  " + r for r in self.turn_refutations[-6:]))
            self.turn_refutations = []
        misses = self.learner.ledger.recent_misses
        if self.cfg.sase and misses:
            parts.append("Recent prediction misses: " + " | ".join(misses[-3:]))
        return "\n".join(parts)

    # ------------------------------------------------------------ h31 grounding
    def _grounded(self) -> bool:
        return bool(self.cfg.sase and self.cfg.grounded)

    def _rule_first_on_verified(self, newly: Set[str]) -> None:
        """A newly verified predict rule is a simulator: switch PRISM to
        rule_first, whose heuristics say to plan() with it."""
        if self.prism is None or not newly:
            return
        if not any(n in self.hyps.active and self.hyps.active[n].kind == "predict" for n in newly):
            return
        if self.prism.active.id != "rule_first":
            self.prism.active = BY_ID["rule_first"]
            self._reset_window()
            self.auto_streak = 0
            self._log({"event": "rule_first", "turn": self.turn, "verified": sorted(newly)})

    def _refresh_grounding(self) -> None:
        """Before SYNTHESIS: rebuild the evidence table from episodic memory and
        replay the domain proposer's candidate rules; keep those that pass."""
        t0 = time.perf_counter()
        try:
            self._hook("ingest", self.history)
            self.evidence.build(self.history, changed_fn=getattr(self.hooks, "significant_change", None),
                                family_fn=getattr(self.hooks, "family", None),
                                effects_fn=getattr(self.hooks, "effects", None),
                                skip_fn=self.hyps.not_dynamics)
            fams = self.evidence.families()
            props = self._hook("propose", self.history, fams, default=[]) or []
        except Exception as exc:  # noqa: BLE001 - grounding never costs the turn
            self._log({"event": "grounding_error", "turn": self.turn, "error": str(exc)[:300]})
            props, fams = [], self.evidence.families()
        keep: Dict[str, Dict[str, Any]] = {}
        for p in props:
            fam = p.get("family")
            c = self._check_candidate(str(p.get("name", "")), str(p.get("source", "")), str(p.get("note", "")),
                                      [fams[fam]] if fam in fams else [])
            if c is not None:
                keep[c["name"]] = c
        passing = {n: c for n, c in keep.items() if self._cand_passes(c)}
        if len(passing) >= 2:
            names = sorted(passing, key=lambda n: -int(passing[n]["rep"]["cell_claims"]))
            rows = sorted({r for n in names for r in passing[n]["rows"]}, key=lambda r: int(r[1:]))
            src = ('def wm_all(frame, action):\n    """%s: the candidates %s in one rule"""\n'
                   "    return merge_predictions([%s], frame.shape)\n" % (
                       ",".join(rows), ", ".join(names), ", ".join("%s(frame, action)" % n for n in names)))
            c = self._check_candidate("wm_all", src, "%s: union of %d candidates" % (",".join(rows), len(names)),
                                      rows)
            if c is not None:
                keep["wm_all"] = c
                if self._cand_passes(c):
                    passing["wm_all"] = c
        self.candidates = keep
        self.passing = passing
        self.stats["candidates_passing_max"] = max(self.stats["candidates_passing_max"], len(passing))
        self.stats["grounding_s"] += time.perf_counter() - t0

    def _cand_passes(self, c: Dict[str, Any]) -> bool:
        rep = c["rep"]
        return bool(rep.get("ok")) and int(rep.get("cell_claims", 0)) >= self.hyps.min_support

    def _check_candidate(self, name: str, source: str, note: str, rows: List[str]) -> Optional[Dict[str, Any]]:
        """Define a candidate in the namespace and replay it (incrementally
        when its source is unchanged since the last turn)."""
        if not name or not source:
            return None
        c = self.candidates.get(name)
        n = len(self.history)
        if c is None or c["source"] != source:
            err = self.sandbox.define(source, "<h30-cand:%s>" % name)
            fn = self.sandbox.ns.get(name) if err is None else None
            if not callable(fn):
                return None
            rep = self.hyps.replay_check(fn, self.history, "predict", caller=self._hyp_call)
            c = {"name": name, "source": source, "fn": fn, "note": note, "rows": list(rows), "rep": rep,
                 "checked_at": n, "fp": _norm_source(source)}
        else:
            c["rows"] = list(rows) or c["rows"]
            if c["checked_at"] < n and c["rep"].get("ok"):
                new = self.hyps.replay_check(c["fn"], self.history, "predict", caller=self._hyp_call,
                                             start=c["checked_at"])
                old = c["rep"]
                merged: Dict[str, Any] = {k: int(old.get(k, 0)) + int(new.get(k, 0))
                                          for k in ("claimed", "cell_claims", "abstained", "wrong", "errors",
                                                    "unscorable")}
                merged["ok"] = bool(new.get("ok"))
                merged["first_fail"] = new.get("first_fail")
                c["rep"] = merged
            c["checked_at"] = n
        self.sandbox.ns[name] = c["fn"]
        return c

    def _grounding_text(self) -> str:
        """The table and the passing candidates within ``grounding_budget``
        characters (rows first, then candidates, then one example source)."""
        budget = self.grounding_budget
        lines = ["EVIDENCE TABLE (computed by the harness from all %d transitions; HUD/step-counter changes "
                 "excluded; cite rows by id):" % len(self.history)]
        used = len(lines[0])
        rows = [ln[:220] for ln in self.evidence.render(max_rows=40).splitlines()]
        for i, ln in enumerate(rows):
            if used + len(ln) > budget * 0.55 and i > 0:
                lines.append("  (+%d more rows: print(evidence()))" % (len(rows) - i))
                break
            lines.append(ln)
            used += len(ln) + 1
        if self.passing:
            lines.append("CANDIDATE RULES (induced from the table; each ALREADY replays over all history with "
                         "0 wrong):")
            top = sorted(self.passing.values(), key=lambda c: -int(c["rep"]["cell_claims"]))
            for k, c in enumerate(top[:self.cfg.candidates_shown]):
                adopted = " ADOPTED" if c["name"] in self.hyps.active else ""
                ln = "  %s [%s; predicts cells on %d transitions]%s: %s" % (
                    c["name"], ",".join(c["rows"]), int(c["rep"]["cell_claims"]), adopted, c["note"][:110])
                if used + len(ln) > budget * 0.85 and k > 0:
                    break
                lines.append(ln)
                used += len(ln) + 1
            ex = "  e.g.\n" + "\n".join("    " + ln for ln in top[0]["source"].splitlines())
            if used + len(ex) <= budget:
                lines.append(ex)
            lines.append("  SELECT: hypothesize(%s). COMBINE: call several inside one predict. GENERALISE: widen "
                         "one. A rule you write cites its row(s) (note=\"E1: ...\") and must predict cells on MORE "
                         "transitions than the candidates for those rows." % top[0]["name"])
        else:
            lines.append("CANDIDATE RULES: none replay cleanly yet. Write rules from the table rows and cite them "
                         "(note=\"E1: ...\").")
        return "\n".join(lines)

    def _plan(self, goal: Any = None, max_depth: int = 30, execute: bool = True,
              max_nodes: int = 4000) -> Dict[str, Any]:
        """BFS over states simulated by the VERIFIED predict rules (merged
        claims applied to the current state) to the nearest state where
        ``goal`` holds; then act the path, each step carrying its simulated
        prediction so a wrong simulator is caught (and refuted) at once."""
        self.stats["plan_calls"] += 1
        preds = [h.fn for h in self.hyps.verified().values() if h.kind == "predict" and h.fn is not None]
        if not preds:
            return {"ok": False, "reason": "plan() simulates VERIFIED predict rules and none is verified yet: "
                                           "adopt a candidate rule or verify your own first"}
        assert self.obs is not None
        goal_fn: Optional[Callable[..., Any]] = None
        if isinstance(goal, str) and goal != "novel":
            goal = self.sandbox.ns.get(goal)
        if callable(goal):
            goal_fn = goal
        elif goal is None:
            gs = [h.fn for h in self.hyps.verified().values() if h.kind == "goal" and h.fn is not None]
            goal_fn = gs[0] if gs else None
        mode = "goal" if goal_fn is not None else "novel"
        acts = [tuple(int(v) for v in a) for a in (self._hook("candidates", default=[]) or [])]
        if not acts:
            acts = [(int(a), -1, -1) for a in (self._hook("available_actions", default=[]) or [])
                    if not self._hook("needs_xy", a, default=False)]
        start = self.obs.state.copy()

        def sim(s: np.ndarray, a: Tuple[int, ...]) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
            outs = []
            for f in preds:
                try:
                    outs.append(f(s.copy(), ActionArg(a)))
                except Exception:  # noqa: BLE001 - a rule that raises simply abstains here
                    continue
            m = merge_predictions(outs, s.shape)
            if m is None:
                return None, None
            nxt = s.copy()
            mask = m != -1
            nxt[mask] = m[mask].astype(s.dtype)
            return nxt, m

        def hit(s: np.ndarray) -> bool:
            if goal_fn is None:
                return self._key(s) not in self.seen_keys
            try:
                return goal_satisfied(goal_fn(s.copy()))
            except Exception:  # noqa: BLE001
                return False

        t0 = time.perf_counter()
        seen = {start.tobytes()}
        frontier: List[Tuple[np.ndarray, List[Tuple[Tuple[int, ...], np.ndarray]]]] = [(start, [])]
        found: Optional[List[Tuple[Tuple[int, ...], np.ndarray]]] = None
        unknown: List[Tuple[float, int, List[Tuple[Tuple[int, ...], np.ndarray]], Tuple[int, ...]]] = []
        nodes = 0

        def spent() -> bool:
            return time.perf_counter() - t0 > self.cfg.plan_s or nodes >= max_nodes

        while frontier and found is None and not spent():
            nxt_frontier = []
            for s, path in frontier:
                if len(path) >= max_depth or found is not None or spent():
                    break
                for a in acts:
                    if spent():
                        break
                    s2, m = sim(s, a)
                    nodes += 1
                    if s2 is None or m is None:
                        # the verified rules do not know this step: a probe candidate
                        if len(unknown) < 256:
                            unknown.append((-self._goal_progress(goal_fn, s), len(path), path, a))
                        continue
                    k = s2.tobytes()
                    if k in seen:
                        continue
                    seen.add(k)
                    p2 = path + [(a, m)]
                    if hit(s2):
                        found = p2
                        break
                    nxt_frontier.append((s2, p2))
            frontier = nxt_frontier
        out: Dict[str, Any] = {"ok": True, "mode": mode, "nodes": nodes, "found": found is not None,
                               "search_s": round(time.perf_counter() - t0, 2)}
        probe: Optional[Tuple[int, ...]] = None
        if found is None and unknown and goal_fn is not None:
            # no known path: walk to the most promising state whose next step the
            # rules cannot simulate (highest goal progress, then nearest) and take it
            _g, _n, found, probe = min(unknown, key=lambda u: (u[0], u[1]))
            out["probe"] = list(probe)
        if found is None:
            out["reason"] = "no simulated path within max_depth=%d (%d nodes searched)" % (max_depth, nodes)
            return out
        self.stats["plan_found"] += 1
        out["path"] = [a for a, _m in found]
        out["path_len"] = len(found)
        if not execute:
            return out
        done = 0
        for a, m in found:
            if self.turn_actions >= self.turn_cap:
                raise ActionCap("turn action cap %d reached" % self.turn_cap)
            t = self.step(a, "plan", expect=m)  # type: ignore[arg-type]
            done += 1
            self.sandbox.ns[self.cfg.state_name] = self.obs.state.copy()
            if t.level_up:
                raise TurnEnd("LEVEL UP after plan step %d -- now on level %d" % (done, self.level + 1))
            if t.died:
                raise TurnEnd("LOST during plan step %d; the level was restarted" % done)
            mask = m != -1
            ign = self.hyps.ignore_mask(t.before, t.after)
            if ign is not None and ign.shape == mask.shape:
                mask &= ~ign
            if t.after.shape == m.shape and bool((t.after[mask] != m[mask]).any()):
                out.update(reached=False, acts=done, reason="the game deviated from the simulation at step %d "
                                                            "(the rule's miss is in the ledger)" % done)
                return out
        if probe is not None:
            if self.turn_actions >= self.turn_cap:
                raise ActionCap("turn action cap %d reached" % self.turn_cap)
            t = self.step(probe, "plan")  # type: ignore[arg-type]
            done += 1
            self.sandbox.ns[self.cfg.state_name] = self.obs.state.copy()
            if t.level_up:
                raise TurnEnd("LEVEL UP after plan probe %r -- now on level %d" % (probe, self.level + 1))
            if t.died:
                raise TurnEnd("LOST on the plan probe %r; the level was restarted" % (probe,))
            out.update(reached=False, acts=done, reason="probed %r: the rules could not simulate it" % (probe,))
            return out
        out.update(reached=True, acts=done)
        return out

    def _goal_progress(self, goal_fn: Optional[Callable[..., Any]], s: np.ndarray) -> float:
        if goal_fn is None:
            return 0.0
        try:
            return goal_score(goal_fn(s.copy()))
        except Exception:  # noqa: BLE001
            return 0.0


    # ------------------------------------------------------------ namespace
    def _install_namespace(self) -> None:
        ns = self.sandbox.ns
        loop = self

        def act(a: int, x: Optional[int] = None, y: Optional[int] = None, expect: Any = None) -> ActResult:
            if loop.turn_actions >= loop.turn_cap:
                raise ActionCap("turn action cap %d reached" % loop.turn_cap)
            a = int(a)
            avail = loop._hook("available_actions", default=None)
            if avail and a not in avail:
                raise ValueError("action %d is not available; available = %s" % (a, avail))
            if x is not None and y is not None:
                action = (a, int(x), int(y))
            else:
                if loop._hook("needs_xy", a, default=False):
                    raise ValueError("action %d needs x and y" % a)
                action = (a, -1, -1)
            expect = loop._resolve_expect(expect, action)
            t = loop.step(action, "model", expect=expect)  # type: ignore[arg-type]
            assert loop.obs is not None
            ns[loop.cfg.state_name] = loop.obs.state.copy()
            r = ActResult(t, ns[loop.cfg.state_name], loop.level, loop._describe(t))
            if t.level_up:
                raise TurnEnd("LEVEL UP after %r -- now on level %d" % (t, loop.level + 1))
            if t.died:
                raise TurnEnd("LOST after %r; the level was restarted" % t)
            return r

        def replay_check(fn: Callable[..., Any], kind: str = "predict") -> Dict[str, Any]:
            return loop.hyps.replay_check(fn, loop.history, kind, caller=loop._hyp_call)

        def hypothesize(fn: Any, kind: str = "predict", note: str = "") -> Dict[str, Any]:
            if isinstance(fn, str):  # a candidate (or any namespace function) by name
                got = ns.get(fn)
                if not callable(got):
                    return {"name": fn, "status": "rejected", "reason": "no function named %r" % fn}
                fn = got
            name = getattr(fn, "__name__", "hyp%d" % loop.hyps.n_proposed)
            src = loop.all_defs.get(name, {}).get("source", "")
            cand = loop.candidates.get(name)
            origin = "model"
            cites: Tuple[str, ...] = ()
            if cand is not None and cand["fn"] is fn and (not src or _norm_source(src) == cand["fp"]):
                origin, src, cites = "proposer", cand["source"], tuple(cand["rows"])
            else:
                cites = tuple(sorted({"E%s" % m for m in re.findall(r"\bE(\d+)\b", "%s\n%s" % (note, src))},
                                     key=lambda r: int(r[1:])))
            must_beat = 0
            if loop._grounded() and kind == "predict" and origin == "model":
                valid = tuple(c for c in cites if c in loop.evidence.rows)
                if not valid:
                    loop.stats["uncited_rejected"] += 1
                    why = ("no evidence row cited: say which EVIDENCE TABLE row(s) this rule comes from, e.g. "
                           "hypothesize(%s, note=\"E1: <what E1 shows>\"); rows now: %s" % (
                               name, ", ".join(sorted(loop.evidence.rows, key=lambda r: int(r[1:]))) or "none"))
                    loop.turn_refutations.append("%s: REJECTED -- %s" % (name, why))
                    return {"name": name, "status": "rejected", "reason": why}
                cites = valid
                must_beat = max([int(c["rep"]["cell_claims"]) for c in loop.passing.values()
                                 if set(c["rows"]) & set(valid)] or [0])
            loop.hyp_names.add(name)
            og = loop.stats["hyp_origin"]
            og[origin] = og.get(origin, 0) + 1
            if origin == "proposer" and name not in loop.stats["candidates_adopted"]:
                loop.stats["candidates_adopted"].append(name)
            rep = loop.hyps.propose(name, fn, src, kind, loop.history, turn=loop.turn,
                                    level=loop.level, note=str(note)[:120], caller=loop._hyp_call,
                                    origin=origin, cites=cites, must_beat=must_beat)
            rep["origin"] = origin
            if cites:
                rep["cites"] = list(cites)
            if rep.get("status") == "refuted":
                loop.turn_refutations.append("%s%s: %s" % (
                    name, (" [cites %s]" % ",".join(cites)) if cites else "", str(rep.get("reason", ""))[:200]))
            if rep.get("status") == "verified":
                ns["H"][name] = fn
                loop._rule_first_on_verified({name})
            return rep

        def plan(goal: Any = None, max_depth: int = 30, execute: bool = True) -> Dict[str, Any]:
            return loop._plan(goal, int(max_depth), bool(execute))

        def evidence() -> str:
            return loop.evidence.render(max_rows=200)

        def note(text: str) -> None:
            loop.notes.append(str(text)[:160])

        def save_skill(fn: Callable[..., Any], doc: str = "") -> bool:
            name = getattr(fn, "__name__", "")
            d = loop.all_defs.get(name)
            if not d:
                return False
            loop.skills.record_success(name, d["source"], doc or d["doc"], d["sig"], loop.episode_id)
            loop.skills.save()
            return True

        import collections
        import heapq
        import itertools
        import math
        ns.update({"np": np, "act": act, "history": self.history, "replay_check": replay_check,
                   "hypothesize": hypothesize, "note": note, "save_skill": save_skill, "H": {},
                   "merge_predictions": merge_predictions,
                   "math": math, "collections": collections, "itertools": itertools, "heapq": heapq})
        if self._grounded():  # h31: the grounded tools (a host may install its own planner over them)
            ns.update({"plan": plan, "evidence": evidence})
        tools = self._hook("tools", self, default={}) or {}
        for name, (fn, _doc) in tools.items():
            ns[name] = fn

    # ------------------------------------------------------------ reporting
    def _open_log(self) -> None:
        if not self.cfg.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.cfg.log_path) or ".", exist_ok=True)
            self._log_fh = open(self.cfg.log_path, "w", encoding="utf-8")
            self._log({"event": "start", "episode": self.episode_id, "mode": self.cfg.mode,
                       "prism": self.cfg.prism, "skills_loaded": self.stats["skills_loaded"],
                       "prism_start": self.prism.active.id if self.prism else None,
                       "system_tokens_est": est_tokens(self.system)})
        except OSError:
            self._log_fh = None

    def _log(self, rec: Dict[str, Any]) -> None:
        if self.sink is not None:  # SEAM(adk)
            try:
                self.sink(dict(rec))
            except Exception:  # noqa: BLE001 - an event sink never fails an episode
                _LOG.debug("event sink raised", exc_info=True)  # SEAM(adk) pass
        if self._log_fh is None:
            return
        try:
            rec = dict(rec, t=round(time.perf_counter() - self._t0, 2))
            self._log_fh.write(json.dumps(rec, default=str) + "\n")
            self._log_fh.flush()
        except Exception:  # noqa: BLE001 - logging never fails an episode
            _LOG.debug("log write failed", exc_info=True)  # SEAM(adk) pass

    def _finish(self) -> None:
        if self.prism is not None:
            try:
                self.prism.save()
            except OSError:
                _LOG.debug("prism.save() failed", exc_info=True)  # SEAM(adk) pass
        self.stats["wall_s"] = round(time.perf_counter() - self._t0, 2)

    def close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log({"event": "end", "stats": self.summary()})
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                _LOG.debug("log close failed", exc_info=True)  # SEAM(adk) pass
            self._log_fh = None

    def summary(self) -> Dict[str, Any]:
        s = dict(self.stats)
        s["llm_s"] = round(float(s["llm_s"]), 2)
        s["hypotheses"] = self.hyps.stats()
        s["verified_names"] = sorted(self.hyps.verified())
        s["refuted_names"] = [h.name for h in self.hyps.refuted][-20:]
        s["skills"] = self.skills.stats()
        s["predictions"] = self.learner.stats()
        s["prism"] = self.prism.stats() if self.prism else None
        s["transitions"] = len(self.history)
        s["levels"] = self.level
        s["grounding_s"] = round(float(s.get("grounding_s", 0.0)), 2)
        s["evidence_rows"] = len(self.evidence.rows)
        s["candidates_passing_last"] = sorted(self.passing)
        return s


def _compress_actions(actions: List[Action], limit: int = 40) -> str:
    out: List[str] = []
    prev, n = None, 0
    for a in actions:
        if a == prev:
            n += 1
            continue
        if prev is not None:
            out.append(_act_str(prev) + ("x%d" % n if n > 1 else ""))
        prev, n = a, 1
    if prev is not None:
        out.append(_act_str(prev) + ("x%d" % n if n > 1 else ""))
    text = " ".join(out[:limit])
    return text + (" ..." if len(out) > limit else "")


def _act_str(a: Action) -> str:
    return "A%d" % a[0] if a[1] < 0 else "A%d(%d,%d)" % a


def _gist_text(rr: RunResult, content: str) -> str:
    if rr.error:
        return ("stopped: " if rr.stopped_by else "error: ") + rr.error.splitlines()[0][:90]
    first = next((ln.strip() for ln in (content or "").splitlines()
                  if ln.strip() and not ln.strip().startswith("```")), "")
    return first[:90]


__all__ = ["ReasoningLoop", "LoopConfig", "ActResult", "SYSTEM_CORE"]
