"""Canonical supervised reasoning (Layer 3) — a parallel watcher that keeps a
background reasoning loop goal-aligned and escalates depth when it's stuck.

Builds directly on Layer 2 (``adk.reasoning_session``): the supervisor runs
alongside a ``ReasoningSession``, READS the loop's progress from the session's
event stream (``observe()``) and STEERS it back on course by injecting guidance
through the SAME steering queue (``session.steer()``) the loop already drains —
no new channel. It also emits ``supervision`` events for the UI.

Three responsibilities (the plan's Layer 3):
  • **Goal tracking**  — hold the conversation ``Goal`` and remind the loop of it.
  • **Alignment hooks** — "does this serve the goal? proceed / refocus / escalate"
    via ``alignment_score`` + ``should_escalate``; pluggable ``align_fn`` so a
    caller can use an LLM (Genesis wires OODAReflection / CouncilReview here).
  • **Reasoning-as-a-tool** — ``register_reasoning_tool`` adds a ``deep_reasoning``
    tool the loop can call to escalate a hard sub-problem to a high-effort
    reasoning pass (the reasoning tier / DGX qwen3.6 in production).

Everything is deterministic + injectable so it unit-tests without a live model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from adk.reasoning_session import ReasoningSession


@dataclass
class Goal:
    """The conversation's working goal (what success looks like)."""
    text: str
    conversation_id: str = ""


@dataclass
class SupervisionVerdict:
    action: str           # "proceed" | "nudge" | "escalate"
    score: float          # alignment 0..1
    reason: str = ""


# (state, goal) -> alignment score 0..1 ; sync or async
AlignFn = Callable[[dict, Goal], "float | Awaitable[float]"]


@dataclass
class Supervisor:
    """Watches a ReasoningSession and keeps it aligned + escalates when stuck."""

    session: ReasoningSession
    goal: Goal
    align_fn: Optional[AlignFn] = None
    align_threshold: float = 0.5     # below → nudge to refocus
    fail_threshold: int = 2          # tool failures → escalate to deep reasoning
    drift_steps: int = 4             # many steps + no answer → escalate
    reasoning_tool_name: str = "deep_reasoning"   # the escalation tool to suggest
    reasoner: Optional[Callable[[str], Awaitable[str]]] = None
    steer_fn: Optional[Callable[[str], Awaitable[bool]]] = None
    emit_events: bool = True
    # internal
    _nudged: bool = field(default=False, init=False)
    _escalated: bool = field(default=False, init=False)
    last_verdict: Optional[SupervisionVerdict] = field(default=None, init=False)

    # ── pure assessment (testable) ───────────────────────────────────────────
    def should_escalate(self, state: dict) -> bool:
        """Stuck → escalate to deep reasoning: repeated tool failures, or many
        steps without a final answer."""
        if int(state.get("failures", 0)) >= self.fail_threshold:
            return True
        if int(state.get("steps", 0)) >= self.drift_steps and not state.get("answered"):
            return True
        return False

    async def alignment_score(self, state: dict) -> float:
        if self.align_fn is None:
            return 1.0                       # no judge wired → assume aligned
        res = self.align_fn(state, self.goal)
        if hasattr(res, "__await__"):
            res = await res                  # type: ignore[assignment]
        try:
            return max(0.0, min(1.0, float(res)))
        except (TypeError, ValueError):
            return 1.0

    async def assess(self, state: dict) -> SupervisionVerdict:
        if self.should_escalate(state):
            return SupervisionVerdict(
                "escalate", 0.0,
                "loop appears stuck (repeated failures / no progress) — escalate to deep reasoning")
        score = await self.alignment_score(state)
        if score < self.align_threshold:
            return SupervisionVerdict(
                "nudge", score, f"drifting from the goal (alignment {score:.2f}) — refocus")
        return SupervisionVerdict("proceed", score, "on track")

    # ── intervention ─────────────────────────────────────────────────────────
    async def _act(self, verdict: SupervisionVerdict) -> None:
        if self.emit_events:
            self.session.emit({"type": "supervision", "action": verdict.action,
                               "score": verdict.score, "reason": verdict.reason,
                               "goal": self.goal.text})
        if verdict.action == "escalate" and not self._escalated:
            self._escalated = True
            if self.reasoner is not None:
                # FORCED escalation: call the reasoner ON THE AGENT'S BEHALF and
                # inject the conclusion through the same steering queue the loop
                # drains. A fast router does not self-diagnose being stuck —
                # measured: deep_reasoning was registered AND advertised on 12/12
                # instances and called 0 times. Availability is not reach.
                conclusion = ""
                try:
                    conclusion = (await self.reasoner(self.goal.text) or "").strip()
                except Exception as exc:  # noqa: BLE001 — fall back to advisory
                    await self._steer(
                        f"[Supervisor] This sub-problem looks hard and the deep reasoner "
                        f"failed ({exc}). Call the {self.reasoning_tool_name} tool to think "
                        f"it through rigorously, in service of the goal: {self.goal.text}")
                if conclusion:
                    await self._steer(
                        f"[Supervisor] Deep-reasoning conclusion for the goal "
                        f"'{self.goal.text}':\n{conclusion}")
                else:
                    await self._steer(
                        f"[Supervisor] This sub-problem looks hard. Call the "
                        f"{self.reasoning_tool_name} tool to think it through rigorously, "
                        f"in service of the goal: {self.goal.text}")
            else:
                await self._steer(
                    f"[Supervisor] This sub-problem looks hard. Call the "
                    f"{self.reasoning_tool_name} tool to think it through rigorously, "
                    f"in service of the goal: {self.goal.text}")
        elif verdict.action == "nudge" and not self._nudged:
            self._nudged = True
            await self._steer(
                f"[Supervisor] Stay focused on the goal: {self.goal.text}. {verdict.reason}")

    async def _steer(self, text: str) -> None:
        """Inject guidance through the loop's steering channel.

        Defaults to the session's steering queue (the Layer-2 mechanism the loop
        drains each step). Callers that can't hold a ReasoningSession (e.g. the
        adk daemon bridge) pass ``steer_fn`` instead. Guarded on session
        liveness: the reasoner await can outlast the loop, and a steer into a
        finished session is dropped anyway — steering a dead session would make
        'escalated and the conclusion was useless' look identical to 'never
        escalated' in the log.
        """
        if self.steer_fn is not None:
            await self.steer_fn(text)
        elif self.session.is_active():
            self.session.steer(text)

    def _state_from_events(self, counters: dict) -> dict:
        return {"steps": counters.get("steps", 0),
                "tool_calls": counters.get("tool_calls", 0),
                "failures": counters.get("failures", 0),
                "answered": counters.get("answered", False)}

    async def run(self, *, tick_s: float = 0.5) -> SupervisionVerdict:
        """Watch the session's event stream until the loop ends, assessing on each
        step boundary and intervening (steer/emit). Returns the final verdict."""
        c = {"steps": 0, "tool_calls": 0, "failures": 0, "answered": False}
        verdict = SupervisionVerdict("proceed", 1.0, "no activity")
        async for ev in self.session.observe(backfill=True, idle_timeout=tick_s):
            t = ev.get("type") or ev.get("event")
            if t == "turn_start":
                c["steps"] += 1
            if t in ("tool", "tool_start", "step"):
                c["tool_calls"] += 1
            # Vocab-agnostic failure detection: adk stream_react emits
            # tool_result{error}; awkit run_react emits step{ok:False}. Either
            # way ``ok`` is False or ``error`` is set.
            if ev.get("ok") is False or ev.get("error"):
                c["failures"] += 1
            if t in ("answer_segment", "final", "done", "complete"):
                c["answered"] = True
            # Assess on step boundaries (and on terminal events).
            if t in ("turn_start", "step", "tool_result", "done", "complete"):
                verdict = await self.assess(self._state_from_events(c))
                self.last_verdict = verdict
                if verdict.action != "proceed":
                    await self._act(verdict)
        return verdict


def register_reasoning_tool(
    agent,
    *,
    reasoner: Optional[Callable[[str], Awaitable[str]]] = None,
    name: str = "deep_reasoning",
    enabled: bool = True,
) -> Optional[str]:
    """Register a ``deep_reasoning`` tool on an AitherAgent — reasoning-as-a-tool.

    The loop calls it to escalate a HARD sub-problem to a high-effort reasoning
    pass. Default reasoner = the agent's own LLM at effort 9 (→ the reasoning tier
    / DGX qwen3.6 in production). Pass ``reasoner`` to route elsewhere (Genesis
    wires ``lib/core/AgentRuntime.deep_reasoning`` → SASE / Council / MCTS).
    ``enabled=False`` (e.g. EffortScaler says the tool isn't warranted) → no-op.
    """
    if not enabled:
        return None

    async def _default_reasoner(problem: str) -> str:
        from adk.llm import Message, strip_internal_tags
        msgs = [
            Message(role="system", content=(
                "You are a deep-reasoning engine. Reason rigorously and step by step, "
                "then return a single clear, correct conclusion.")),
            Message(role="user", content=problem),
        ]
        resp = await agent.llm.chat(msgs, effort=9)
        return strip_internal_tags(getattr(resp, "content", "") or "")

    _reason = reasoner or _default_reasoner

    async def deep_reasoning(problem: str) -> str:
        """Escalate a hard sub-problem to deep reasoning; returns a rigorous
        conclusion. Use when a step is genuinely difficult or you are stuck."""
        return await _reason(problem)

    agent._tools.register(
        deep_reasoning, name=name,
        description=("Escalate a hard sub-problem to deep reasoning (high-effort "
                     "reasoning model). Input: the problem statement. Returns a "
                     "rigorous conclusion."))
    return name


def derive_goal(message: str, conversation_id: str = "") -> Goal:
    """Derive the working goal from the user's request. Initially the request IS
    the goal; callers may refine it with an LLM/AitherGoals later."""
    return Goal(text=(message or "").strip()[:500], conversation_id=conversation_id)


__all__ = [
    "Goal", "SupervisionVerdict", "Supervisor", "AlignFn",
    "register_reasoning_tool", "derive_goal",
]
