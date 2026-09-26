# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/intent.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""Intent: classify the loop's situation every turn; the intent picks the
prompt directive and the turn budget.

Ported in spirit from AitherOS ``lib/faculties/IntentEngine.py`` (fast,
deterministic classification first, then effort scaling from the class) --
without its fleet dependencies (NanoGPT neuron, Flux, tenant caps).  Here the
"utterance" is the loop's own telemetry, so a rule table is exact and free.

Priority when several apply: new_level > contradiction > stuck > near_goal >
exploring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

NEW_LEVEL = "new_level"
CONTRADICTION = "contradiction"
STUCK = "stuck"
NEAR_GOAL = "near_goal"
EXPLORING = "exploring"
INTENTS = (NEW_LEVEL, CONTRADICTION, STUCK, NEAR_GOAL, EXPLORING)


@dataclass
class Signals:
    """What the loop measured since the previous turn."""

    first_turn: bool = False
    levels_gained: int = 0            # since the previous turn
    refuted_since_last: List[str] = field(default_factory=list)  # verified hypotheses that failed
    actions_since_novel: int = 0      # actions since a never-seen state
    turns_without_progress: int = 0   # model turns with no novel state, level or verified hypothesis
    goal_progress: float = 0.0        # best graded goal score on the current state (0..1)


@dataclass
class IntentDecision:
    intent: str
    reason: str
    turn_actions: int
    directive: str


# intent -> (turn action budget, directive).  The directive is the prompt's
# first instruction; the budget caps act() calls for that turn.
PROFILES: Dict[str, "tuple[int, str]"] = {
    NEW_LEVEL: (15, "NEW LEVEL. The rules you verified on earlier levels almost certainly still hold. "
                    "Re-locate the objects, re-use the previous level's winning program if it applies, "
                    "and check your goal hypothesis against this layout before acting."),
    CONTRADICTION: (6, "CONTRADICTION. A hypothesis you relied on just failed on new evidence (see below). "
                       "Explain the failure in ANALYSIS, then repair or replace the rule in SYNTHESIS so it "
                       "passes replay_check on ALL history. Act only to test the repaired rule."),
    STUCK: (20, "STUCK. Recent actions found no new state. Do not repeat what you did; follow the strategy "
                "below."),
    NEAR_GOAL: (40, "NEAR GOAL. Your goal predicate is partly satisfied. Plan the shortest action sequence "
                    "that completes it, predict each step, and execute it."),
    EXPLORING: (20, "EXPLORING. Probe what you do not understand yet, one question per action, and turn "
                    "what you learn into predict()/goal() hypotheses."),
}


class IntentClassifier:
    def __init__(self, stuck_actions: int = 30, stuck_turns: int = 3, near_goal: float = 0.5,
                 budget_scale: float = 1.0, max_turn_actions: int = 40) -> None:
        self.stuck_actions = int(stuck_actions)
        self.stuck_turns = int(stuck_turns)
        self.near_goal = float(near_goal)
        self.budget_scale = float(budget_scale)
        self.max_turn_actions = int(max_turn_actions)
        self.counts: Dict[str, int] = {k: 0 for k in INTENTS}

    def classify(self, s: Signals) -> IntentDecision:
        if s.first_turn or s.levels_gained > 0:
            intent, why = NEW_LEVEL, ("first turn" if s.first_turn else "+%d level(s)" % s.levels_gained)
        elif s.refuted_since_last:
            intent, why = CONTRADICTION, "refuted: " + ", ".join(s.refuted_since_last[:3])
        elif s.actions_since_novel >= self.stuck_actions or s.turns_without_progress >= self.stuck_turns:
            intent, why = STUCK, "%d actions since a new state, %d turns without progress" % (
                s.actions_since_novel, s.turns_without_progress)
        elif self.near_goal <= s.goal_progress < 1.0:
            intent, why = NEAR_GOAL, "goal progress %.2f" % s.goal_progress
        else:
            intent, why = EXPLORING, "default"
        self.counts[intent] += 1
        budget, directive = PROFILES[intent]
        budget = max(1, min(self.max_turn_actions, int(round(budget * self.budget_scale))))
        return IntentDecision(intent, why, budget, directive)


__all__ = ["IntentClassifier", "IntentDecision", "Signals", "INTENTS", "PROFILES",
           "NEW_LEVEL", "CONTRADICTION", "STUCK", "NEAR_GOAL", "EXPLORING"]
