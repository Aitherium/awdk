# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/prism.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""PRISM: rotate the whole strategy on a stall instead of retrying.

Ported from AitherOS ``lib/cognitive/PrismStrategy.py`` + ``prism/``
(registry of strategy manifests, scorer, rotation controller, diagnosis) with
no fleet imports.  What is kept:

* manifests carry a principle, heuristics and anti-patterns that become a
  prompt OVERLAY (``controller.generate_prompt_overlay``);
* ATTEMPT -> DIAGNOSE -> ROTATE: the stalled strategy is marked exhausted for
  this stall episode and the best remaining one by priority is chosen;
* a diagnosis condenses the failed attempt so the next strategy knows WHAT
  failed without inheriting HOW it was thought about ("mindset bleed").  The
  fleet asks an LLM for it; here it is computed from the loop's telemetry;
* scores persist (``MemoryBackend``, namespace ``procedural``, key
  ``prism``), so later games in the run start with the best-scoring strategy.

Score = (levels gained + new verified hypotheses) per action, per strategy.
Two strategies are AUTO (no model call): ``handoff`` (the domain's non-LLM
policy) and ``click_scan`` (a systematic pass over ``candidates()``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Strategy:
    id: str
    name: str
    kind: str  # "llm" | "auto"
    principle: str
    heuristics: List[str] = field(default_factory=list)
    anti_patterns: List[str] = field(default_factory=list)
    auto_actions: int = 0


STRATEGIES: List[Strategy] = [
    Strategy("rule_first", "Rule first", "llm",
             "Understand the dynamics before planning: a predict() that replays 100% is a simulator.",
             ["Write predict(frame, action) and call replay_check until it has 0 wrong transitions.",
              "Split cases by action; return None for cases you do not understand yet.",
              "Once verified, plan(goal) simulates the verified rules (BFS) and acts the path."],
             ["Acting without a hypothesis to test.", "Editing a rule without re-running replay_check."]),
    Strategy("goal_first", "Goal first", "llm",
             "Work backwards from what the winning frames had in common.",
             ["Compare frames where a level was won (history.where(level_up=True)) with ordinary ones.",
              "Write goal(frame) returning a progress number in [0,1] and hypothesize it.",
              "Then pick actions that raise goal(frame)."],
             ["Random probing.", "Goals that are True on frames that did not win."]),
    Strategy("analogy", "Analogy", "llm",
             "The previous level's winning program is the best prior for this level.",
             ["Read the previous level's winning actions/program (below).",
              "Map its objects to this level's objects and replay the adapted program."],
             ["Re-deriving rules that are already verified."]),
    Strategy("handoff", "Explorer hand-off", "auto",
             "A systematic non-LLM explorer covers states faster than reasoning when nothing is understood.",
             auto_actions=25),
    Strategy("click_scan", "Systematic scan", "auto",
             "Try every untried candidate action once in the current state.",
             auto_actions=20),
]
BY_ID = {s.id: s for s in STRATEGIES}


@dataclass
class Diagnosis:
    strategy: str
    summary: str
    gaps: List[str] = field(default_factory=list)


class Prism:
    NAMESPACE = "procedural"
    KEY = "prism"

    def __init__(self, backend: Any = None, default: str = "rule_first",
                 prior_score: float = 0.02, allowed: Optional[List[str]] = None) -> None:
        self.backend = backend
        self.allowed = [s.id for s in STRATEGIES if allowed is None or s.id in allowed]
        self.scores: Dict[str, Dict[str, float]] = {}
        if backend is not None:
            got = backend.get(self.NAMESPACE, self.KEY, {}) or {}
            self.scores = {k: dict(v) for k, v in got.items() if isinstance(v, dict)}
        self.loaded_scores = {k: dict(v) for k, v in self.scores.items()}
        self.prior_score = float(prior_score)
        self.exhausted: List[str] = []
        self.diagnoses: List[Diagnosis] = []
        self.rotations = 0
        self.log: List[Dict[str, Any]] = []
        self.active: Strategy = BY_ID[self.best(initial=True) or default]
        self.game_stats: Dict[str, Dict[str, float]] = {}
        self._saved: Dict[str, Dict[str, float]] = {}

    # -- scoring ------------------------------------------------------------
    def score(self, sid: str) -> float:
        s = self.scores.get(sid)
        if not s or s.get("actions", 0) <= 0:
            return self.prior_score
        return (s.get("levels", 0) + s.get("verified", 0)) / max(1.0, s["actions"])

    def best(self, initial: bool = False, exclude: Optional[List[str]] = None) -> Optional[str]:
        cands = [sid for sid in self.allowed if sid not in (exclude or [])]
        if initial:  # a game starts with an LLM strategy; auto ones are stall breakers
            cands = [sid for sid in cands if BY_ID[sid].kind == "llm"] or cands
        if not cands:
            return None
        order = {sid: i for i, sid in enumerate(self.allowed)}
        return max(cands, key=lambda sid: (self.score(sid), -order[sid]))

    def record(self, sid: str, actions: int, levels: int, verified: int) -> None:
        for table in (self.scores, self.game_stats):
            s = table.setdefault(sid, {"uses": 0, "actions": 0, "levels": 0, "verified": 0})
            s["uses"] += 1
            s["actions"] += int(actions)
            s["levels"] += int(levels)
            s["verified"] += int(verified)
        if levels > 0:  # progress ends the stall episode
            self.exhausted = []

    def save(self) -> None:
        if self.backend is None:
            return
        game = self.game_stats
        prev_saved = self._saved

        def merge(disk: Any) -> Dict[str, Any]:
            base = {k: dict(v) for k, v in (disk or {}).items() if isinstance(v, dict)}
            for sid, st in game.items():
                b = base.setdefault(sid, {"uses": 0, "actions": 0, "levels": 0, "verified": 0})
                prev = prev_saved.get(sid, {})
                for k in ("uses", "actions", "levels", "verified"):
                    b[k] = b.get(k, 0) + st.get(k, 0) - prev.get(k, 0)
            return base

        self._saved = {k: dict(v) for k, v in game.items()}
        self.scores = self.backend.update(self.NAMESPACE, self.KEY, merge)

    # -- rotation -----------------------------------------------------------
    def rotate(self, diagnosis: Diagnosis) -> Strategy:
        """The active strategy stalled: exhaust it, keep the diagnosis, pick
        the best remaining one (all exhausted -> start a new cycle)."""
        self.diagnoses.append(diagnosis)
        old = self.active.id
        if old not in self.exhausted:
            self.exhausted.append(old)
        # the diagnosis steers the choice: no new states -> a non-LLM strategy
        # covers the state space faster; otherwise another reasoning frame
        want = "auto" if "no new states reached" in diagnosis.gaps else "llm"
        pool = [sid for sid in self.allowed if BY_ID[sid].kind != want]
        nxt = self.best(exclude=self.exhausted + pool) or self.best(exclude=self.exhausted)
        if nxt is None:
            self.exhausted = [old]
            nxt = self.best(exclude=self.exhausted) or old
        self.active = BY_ID[nxt]
        self.rotations += 1
        self.log.append({"from": old, "to": nxt, "why": diagnosis.summary[:120]})
        return self.active

    def overlay(self, extra: str = "") -> str:
        s = self.active
        lines = ["=== STRATEGY: %s -- %s ===" % (s.name, s.principle)]
        for h in s.heuristics:
            lines.append("- " + h)
        if s.anti_patterns:
            lines.append("Avoid: " + "; ".join(s.anti_patterns))
        if self.diagnoses:
            lines.append("Strategies that already stalled (do NOT repeat their approach):")
            for d in self.diagnoses[-3:]:
                lines.append("- [%s] %s%s" % (d.strategy, d.summary,
                                              (" gaps: " + ", ".join(d.gaps)) if d.gaps else ""))
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        return {"active": self.active.id, "rotations": self.rotations, "log": self.log[-12:],
                "game": self.game_stats,
                "start_scores": {k: round(self._score_of(v), 5) for k, v in self.loaded_scores.items()}}

    @staticmethod
    def _score_of(v: Dict[str, float]) -> float:
        return (v.get("levels", 0) + v.get("verified", 0)) / max(1.0, v.get("actions", 0))


def diagnose(strategy: str, actions: int, novel: int, verified: int, refuted: int,
             errors: int, turns: int) -> Diagnosis:
    """Deterministic port of the PRISM diagnosis: what was tried, what it
    yielded, and the gap it leaves."""
    summary = "%d turns, %d actions, %d new states, %d hypotheses verified, %d refuted, %d code errors" % (
        turns, actions, novel, verified, refuted, errors)
    gaps = []
    if novel == 0:
        gaps.append("no new states reached")
    if verified == 0:
        gaps.append("no verified rule")
    if errors >= max(1, turns // 2):
        gaps.append("code kept failing")
    return Diagnosis(strategy, summary, gaps)


__all__ = ["Prism", "Strategy", "STRATEGIES", "BY_ID", "Diagnosis", "diagnose"]
