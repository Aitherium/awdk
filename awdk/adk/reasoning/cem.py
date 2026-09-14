"""Cross-Entropy Method planner — for tasks where the action space is
better modelled as a *distribution* over candidate sequences than a discrete
tree.

CEM is the natural complement to :class:`MCTSPlanner`:

* MCTS shines when actions are clearly enumerable and we benefit from deep
  lookahead under a UCB1 explore/exploit balance.
* CEM shines when actions are best described by a continuous mixture
  (temperature, sampling-weight, candidate-rank) — e.g. "produce 10 styles
  of refactor, keep the top 2, re-sample around them, repeat".

The implementation here is intentionally simple and backend-agnostic:

1. Round ``r`` samples ``N`` candidate plans by prompting the
   ``ORCHESTRATOR`` tier with an instruction whose *temperature* /
   *sampling hint* is drawn from the current distribution.
2. Each plan is scored by the ``REASONING`` tier (same scorer protocol as
   :func:`adk.reasoning.planner.MCTSPlanner._score`).
3. The top ``elite_frac * N`` plans become the elite set; the next round's
   distribution is refit to bias proposals toward their *style hints*.
4. After ``iterations`` rounds we return the best plan seen.

Returns :class:`CEMResult` with the winning plan, its score, and per-round
diagnostics.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from adk.core.logging import get_logger

from .planner import _extract_score, _parse_action_list
from .tiers import ModelTier, ReasoningRouter

_log = get_logger("adk.reasoning.cem")


# ---------------------------------------------------------------------------
# Style-hint distribution
# ---------------------------------------------------------------------------
#
# We can't actually parameterise free-text generation by a continuous vector,
# so we approximate the "continuous action" by a small set of *style hints*
# (e.g. "concise", "exploratory", "rigorous"). The distribution is a vector
# of probabilities over the hint vocabulary, refit each round to match the
# elite samples' empirical distribution.

DEFAULT_HINTS: tuple[str, ...] = (
    "concise and direct",
    "exploratory — list alternatives",
    "rigorous — justify each step",
    "creative — try an unusual angle",
    "conservative — minimise risk",
    "aggressive — optimise for speed",
)


@dataclass(slots=True)
class _Sample:
    hint: str
    plan: list[str]
    score: float
    raw: str


@dataclass(slots=True)
class CEMRound:
    """One iteration of CEM."""

    index: int
    samples: int
    mean_score: float
    elite_mean_score: float
    distribution: dict[str, float]


@dataclass(slots=True)
class CEMResult:
    """Output of :meth:`CEMPlanner.plan`."""

    goal: str
    best_plan: list[str]
    best_score: float
    best_hint: str
    iterations: int
    rounds: list[CEMRound] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"CEM plan for: {self.goal}",
            f"Winning hint: {self.best_hint}",
            f"Score: {self.best_score:.3f}  Iterations: {self.iterations}",
            "Steps:",
            *(f"  {i + 1}. {step}" for i, step in enumerate(self.best_plan)),
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class CEMPlanner:
    """Cross-Entropy Method planner over a fixed style-hint vocabulary.

    Parameters
    ----------
    router:
        Source of orchestrator/reasoning backends. Shared with MCTSPlanner.
    hints:
        The style-hint vocabulary. Override to specialise for a domain
        (e.g. ``("imperative", "declarative", "functional")``).
    samples_per_round:
        How many candidate plans to draw each round.
    elite_frac:
        Fraction of samples that count as "elite" and bias the next round.
    smoothing:
        ``0.0`` snaps the distribution to the elite empirical; ``1.0`` keeps
        the previous distribution unchanged. Defaults to ``0.5`` for a
        balanced refit.
    """

    def __init__(
        self,
        router: ReasoningRouter,
        *,
        hints: Sequence[str] = DEFAULT_HINTS,
        samples_per_round: int = 6,
        elite_frac: float = 0.34,
        smoothing: float = 0.5,
        rng: random.Random | None = None,
    ) -> None:
        if not hints:
            raise ValueError("CEMPlanner requires at least one style hint")
        if not 0.0 < elite_frac <= 1.0:
            raise ValueError("elite_frac must be in (0, 1]")
        if not 0.0 <= smoothing <= 1.0:
            raise ValueError("smoothing must be in [0, 1]")
        self.router = router
        self.hints = tuple(hints)
        self.samples_per_round = max(2, int(samples_per_round))
        self.elite_frac = elite_frac
        self.smoothing = smoothing
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(
        self,
        goal: str,
        *,
        iterations: int = 3,
        constraints: str = "",
    ) -> CEMResult:
        """Run CEM for ``iterations`` rounds and return the best plan."""

        # Uniform start distribution.
        dist: dict[str, float] = {h: 1.0 / len(self.hints) for h in self.hints}
        result = CEMResult(
            goal=goal,
            best_plan=[],
            best_score=float("-inf"),
            best_hint="",
            iterations=0,
        )

        for r in range(max(1, int(iterations))):
            samples = await self._sample_round(goal, constraints, dist)
            samples.sort(key=lambda s: s.score, reverse=True)

            elite_count = max(1, int(math.ceil(self.elite_frac * len(samples))))
            elite = samples[:elite_count]
            empirical = self._empirical(elite)

            # Refit: blend previous dist with elite empirical.
            dist = {
                h: self.smoothing * dist.get(h, 0.0)
                + (1.0 - self.smoothing) * empirical.get(h, 0.0)
                for h in self.hints
            }
            # Renormalise — floating-point drift safety.
            total = sum(dist.values()) or 1.0
            dist = {h: p / total for h, p in dist.items()}

            mean = sum(s.score for s in samples) / len(samples)
            elite_mean = sum(s.score for s in elite) / len(elite)
            result.rounds.append(
                CEMRound(
                    index=r,
                    samples=len(samples),
                    mean_score=mean,
                    elite_mean_score=elite_mean,
                    distribution=dict(dist),
                )
            )

            top = elite[0]
            if top.score > result.best_score:
                result.best_score = top.score
                result.best_plan = list(top.plan)
                result.best_hint = top.hint
            result.iterations = r + 1

            _log.info(
                "cem.round",
                extra={
                    "round": r,
                    "mean": round(mean, 3),
                    "elite_mean": round(elite_mean, 3),
                    "best": round(result.best_score, 3),
                },
            )

        if result.best_score == float("-inf"):
            result.best_score = 0.0
        return result

    def plan_sync(
        self, goal: str, *, iterations: int = 3, constraints: str = ""
    ) -> CEMResult:
        """Synchronous convenience wrapper for CLI / scripts."""
        return asyncio.run(
            self.plan(goal, iterations=iterations, constraints=constraints)
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _sample_round(
        self, goal: str, constraints: str, dist: dict[str, float]
    ) -> list[_Sample]:
        tasks = [
            self._one_sample(goal, constraints, self._draw_hint(dist))
            for _ in range(self.samples_per_round)
        ]
        return await asyncio.gather(*tasks)

    def _draw_hint(self, dist: dict[str, float]) -> str:
        # weighted choice without numpy
        r = self._rng.random()
        acc = 0.0
        for hint, p in dist.items():
            acc += p
            if r <= acc:
                return hint
        return self.hints[-1]

    async def _one_sample(
        self, goal: str, constraints: str, hint: str
    ) -> _Sample:
        plan_text = await self._propose_plan(goal, constraints, hint)
        steps = _parse_action_list(plan_text, limit=8)
        if not steps:
            # Fall back to a single-step plan around the raw text.
            steps = [plan_text.strip()[:240] or "(no plan)"]
        score = await self._score_plan(goal, steps, hint)
        return _Sample(hint=hint, plan=steps, score=score, raw=plan_text)

    async def _propose_plan(self, goal: str, constraints: str, hint: str) -> str:
        prompt = (
            f"Goal: {goal}\n"
            f"Constraints: {constraints or 'none'}\n"
            f"Style hint: {hint}\n"
            "Propose a 3-5 step plan. Return either a JSON array of strings "
            "or a numbered list."
        )
        assignment = self.router.resolve(tier=ModelTier.ORCHESTRATOR)
        return await _generate(assignment.backend, prompt, max_tokens=assignment.spec.max_tokens)

    async def _score_plan(self, goal: str, steps: list[str], hint: str) -> float:
        prompt = (
            f"Score the following plan from 0.0 (useless) to 1.0 (excellent) "
            f"for achieving the goal. Respond with ONLY a decimal number.\n"
            f"Goal: {goal}\n"
            f"Style: {hint}\n"
            "Plan:\n"
            + "\n".join(f"- {s}" for s in steps)
        )
        assignment = self.router.resolve(tier=ModelTier.REASONING)
        text = await _generate(assignment.backend, prompt, max_tokens=64)
        return _extract_score(text)

    def _empirical(self, elite: list[_Sample]) -> dict[str, float]:
        counts: dict[str, int] = {h: 0 for h in self.hints}
        for s in elite:
            counts[s.hint] = counts.get(s.hint, 0) + 1
        total = sum(counts.values()) or 1
        return {h: c / total for h, c in counts.items()}


async def _generate(backend: Any, prompt: str, *, max_tokens: int) -> str:
    """Call a ModelBackend with a single user prompt and return the text."""
    from adk.core.model import Message

    messages = [Message(role="user", content=prompt)]
    resp = await backend.generate(messages, max_tokens=max_tokens, temperature=0.7)
    return getattr(resp, "text", "") or ""


__all__ = [
    "CEMPlanner",
    "CEMResult",
    "CEMRound",
    "DEFAULT_HINTS",
]
