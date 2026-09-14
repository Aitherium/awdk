"""Monte Carlo Tree Search planner for long-horizon plans.

This is what makes AitherOS different from a chain-of-thought agent. Given
a goal, the planner expands a tree of candidate next-actions, simulates
short rollouts with the orchestrator tier, and uses the reasoning tier to
score terminal states. Each iteration follows the four classical phases:

    select  →  expand  →  simulate  →  backpropagate

Plans are returned as a path from the root to the best-scoring leaf.

This module is **transport-agnostic**: it talks to a
:class:`adk.reasoning.tiers.ReasoningRouter`, so tests can inject a
fake router that returns canned text. Production runs use whatever tiers
are configured in ``~/.aither/reasoning.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from dataclasses import dataclass, field
from typing import Iterable

from adk.core.model import Message
from .tiers import ModelTier, ReasoningRouter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan nodes
# ---------------------------------------------------------------------------


@dataclass
class PlanNode:
    """A node in the MCTS plan tree.

    ``state`` is the running plan: ordered list of action strings taken so
    far. The root node has ``state=[]``.
    """

    state: list[str] = field(default_factory=list)
    parent: "PlanNode | None" = None
    children: list["PlanNode"] = field(default_factory=list)
    untried: list[str] = field(default_factory=list)
    visits: int = 0
    total_value: float = 0.0
    terminal: bool = False

    @property
    def depth(self) -> int:
        n, d = self.parent, 0
        while n is not None:
            n, d = n.parent, d + 1
        return d

    @property
    def mean_value(self) -> float:
        return self.total_value / self.visits if self.visits else 0.0

    def ucb1(self, parent_visits: int, c: float) -> float:
        if self.visits == 0:
            return math.inf
        exploit = self.mean_value
        explore = c * math.sqrt(math.log(max(parent_visits, 1)) / self.visits)
        return exploit + explore

    def best_child(self, c: float) -> "PlanNode":
        return max(self.children, key=lambda ch: ch.ucb1(self.visits, c))


@dataclass(slots=True)
class PlanResult:
    """Final search result."""

    goal: str
    best_path: list[str]
    best_score: float
    iterations: int
    expanded_nodes: int

    def render(self) -> str:
        if not self.best_path:
            return f"# Plan for: {self.goal}\n(no actions proposed)\n"
        lines = [f"# Plan for: {self.goal}", f"score={self.best_score:.3f}", ""]
        for i, step in enumerate(self.best_path, 1):
            lines.append(f"{i}. {step}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers — small JSON-list parser tolerant of LLM noise
# ---------------------------------------------------------------------------


_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.MULTILINE)


def _parse_action_list(text: str, *, limit: int) -> list[str]:
    """Pull a clean list of action strings out of an LLM response.

    Accepts JSON arrays (preferred), Markdown bulleted lists, or
    numbered lists. Trims to ``limit`` entries.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Prefer fenced or bare JSON arrays.
    fence = _FENCE_RE.search(text)
    candidates = [fence.group(1)] if fence else []
    if text.lstrip().startswith("["):
        candidates.append(text)
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out = [str(item).strip() for item in data if str(item).strip()]
            if out:
                return out[:limit]

    # Fall back to bullet/number extraction.
    out: list[str] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            item = m.group(1).strip()
            if item:
                out.append(item)
        if len(out) >= limit:
            break
    return out[:limit]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class MCTSPlanner:
    """Monte Carlo Tree Search over candidate action plans.

    Args:
        router: The :class:`ReasoningRouter` providing tiered backends.
        max_depth: Plan length cap (default 6 steps).
        branching: How many candidate actions to expand per node.
        rollout_depth: How many extra simulated steps the rollout walks
            before being scored.
        c: UCB1 exploration constant.
        rng: Optional :class:`random.Random` for reproducible tests.
    """

    def __init__(
        self,
        router: ReasoningRouter,
        *,
        max_depth: int = 6,
        branching: int = 3,
        rollout_depth: int = 2,
        c: float = math.sqrt(2),
        rng: random.Random | None = None,
    ):
        self.router = router
        self.max_depth = max(1, int(max_depth))
        self.branching = max(1, int(branching))
        self.rollout_depth = max(0, int(rollout_depth))
        self.c = c
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------
    # Public entrypoint
    # ------------------------------------------------------------------

    async def plan(
        self,
        goal: str,
        *,
        iterations: int = 8,
        constraints: Iterable[str] | None = None,
    ) -> PlanResult:
        """Search for the best plan to achieve ``goal``."""
        constraints_list = list(constraints or [])
        root = PlanNode()
        root.untried = await self._propose_actions(goal, root.state, constraints_list)

        expanded = 0
        for _ in range(max(1, iterations)):
            # 1. SELECT
            node = self._select(root)

            # 2. EXPAND
            if not node.terminal and node.untried and node.depth < self.max_depth:
                action = node.untried.pop(self.rng.randrange(len(node.untried)))
                child_state = node.state + [action]
                child = PlanNode(state=child_state, parent=node)
                if len(child_state) >= self.max_depth:
                    child.terminal = True
                else:
                    child.untried = await self._propose_actions(
                        goal, child_state, constraints_list
                    )
                    if not child.untried:
                        child.terminal = True
                node.children.append(child)
                expanded += 1
                node = child

            # 3. SIMULATE + SCORE
            value = await self._simulate(goal, node.state, constraints_list)

            # 4. BACKPROPAGATE
            self._backpropagate(node, value)

        best = self._best_path(root)
        score = max((n.mean_value for n in best), default=0.0)
        return PlanResult(
            goal=goal,
            best_path=[n.state[-1] for n in best if n.state],
            best_score=score,
            iterations=iterations,
            expanded_nodes=expanded,
        )

    # ------------------------------------------------------------------
    # MCTS phases
    # ------------------------------------------------------------------

    def _select(self, root: PlanNode) -> PlanNode:
        node = root
        while node.children and not node.untried and not node.terminal:
            node = node.best_child(self.c)
        return node

    def _backpropagate(self, node: PlanNode, value: float) -> None:
        n: PlanNode | None = node
        while n is not None:
            n.visits += 1
            n.total_value += value
            n = n.parent

    def _best_path(self, root: PlanNode) -> list[PlanNode]:
        path: list[PlanNode] = []
        node = root
        while node.children:
            node = max(node.children, key=lambda ch: (ch.mean_value, ch.visits))
            path.append(node)
        return path

    # ------------------------------------------------------------------
    # Model-driven phases (mockable in tests via router injection)
    # ------------------------------------------------------------------

    async def _propose_actions(
        self,
        goal: str,
        state: list[str],
        constraints: list[str],
    ) -> list[str]:
        """Use the ORCHESTRATOR tier to propose ``branching`` next actions."""
        assignment = self.router.resolve(tier=ModelTier.ORCHESTRATOR)
        prompt = self._propose_prompt(goal, state, constraints)
        resp = await assignment.backend.generate(
            [Message(role="user", content=prompt)],
            temperature=assignment.spec.temperature,
            max_tokens=assignment.spec.max_tokens,
        )
        actions = _parse_action_list(resp.text, limit=self.branching)
        return actions

    async def _simulate(
        self,
        goal: str,
        state: list[str],
        constraints: list[str],
    ) -> float:
        """Rollout + score.

        Rollout uses the orchestrator (cheap). Scoring uses the reasoning
        tier (strong). When both rollout and scoring would call the same
        backend, we collapse to a single call to save tokens.
        """
        rollout = list(state)
        if self.rollout_depth > 0 and len(rollout) < self.max_depth:
            assignment = self.router.resolve(tier=ModelTier.ORCHESTRATOR)
            for _ in range(self.rollout_depth):
                if len(rollout) >= self.max_depth:
                    break
                prompt = self._propose_prompt(goal, rollout, constraints)
                resp = await assignment.backend.generate(
                    [Message(role="user", content=prompt)],
                    temperature=assignment.spec.temperature,
                    max_tokens=assignment.spec.max_tokens,
                )
                next_actions = _parse_action_list(resp.text, limit=1)
                if not next_actions:
                    break
                rollout.append(next_actions[0])

        return await self._score(goal, rollout, constraints)

    async def _score(
        self,
        goal: str,
        rollout: list[str],
        constraints: list[str],
    ) -> float:
        """Use the REASONING tier to estimate value in [0, 1]."""
        assignment = self.router.resolve(tier=ModelTier.REASONING)
        prompt = self._score_prompt(goal, rollout, constraints)
        resp = await assignment.backend.generate(
            [Message(role="user", content=prompt)],
            temperature=min(assignment.spec.temperature, 0.3),
            max_tokens=128,
        )
        return _extract_score(resp.text)

    # ------------------------------------------------------------------
    # Prompts (overridable by subclasses)
    # ------------------------------------------------------------------

    def _propose_prompt(self, goal: str, state: list[str], constraints: list[str]) -> str:
        plan_so_far = (
            "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(state))
            if state
            else "  (no actions taken yet)"
        )
        constraint_block = (
            "\nConstraints:\n" + "\n".join(f"- {c}" for c in constraints)
            if constraints
            else ""
        )
        remaining = self.max_depth - len(state)
        return (
            "You are a planning assistant. Propose the next concrete action to "
            "make progress toward the goal.\n\n"
            f"Goal: {goal}\n"
            f"Plan so far:\n{plan_so_far}\n"
            f"Remaining slots in the plan: {remaining}\n"
            f"{constraint_block}\n\n"
            f"Respond with a JSON array of up to {self.branching} distinct next "
            "actions, each a short imperative sentence. JSON only, no commentary."
        )

    def _score_prompt(self, goal: str, rollout: list[str], constraints: list[str]) -> str:
        body = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(rollout)) or "  (empty)"
        constraint_block = (
            "\nConstraints:\n" + "\n".join(f"- {c}" for c in constraints)
            if constraints
            else ""
        )
        return (
            "Evaluate how well this plan would achieve the goal.\n\n"
            f"Goal: {goal}\n"
            f"Plan:\n{body}\n"
            f"{constraint_block}\n\n"
            "Respond with a single number between 0.0 and 1.0 where 1.0 means "
            "the plan completely achieves the goal. Number only."
        )


# ---------------------------------------------------------------------------
# Score parser
# ---------------------------------------------------------------------------


_SCORE_RE = re.compile(r"[-+]?\d*\.?\d+")


def _extract_score(text: str) -> float:
    """Pull a [0, 1] score out of model output. Clamps & guards."""
    if not text:
        return 0.0
    match = _SCORE_RE.search(text)
    if not match:
        return 0.0
    try:
        value = float(match.group(0))
    except ValueError:
        return 0.0
    # Normalize common encodings models emit.
    if value > 1.0:
        if value <= 1.5:
            # Slight overshoot of the [0, 1] range — just clamp.
            value = 1.0
        elif value <= 10.0:
            # "7", "8.5" — treat as "x out of 10".
            value = value / 10.0
        elif value <= 100.0:
            # "85" — treat as percentage.
            value = value / 100.0
        else:
            value = 1.0
    return max(0.0, min(1.0, value))


# Convenience for synchronous callers (CLI).
def plan_sync(planner: MCTSPlanner, goal: str, **kwargs) -> PlanResult:
    """Synchronously run :meth:`MCTSPlanner.plan`."""
    return asyncio.run(planner.plan(goal, **kwargs))
