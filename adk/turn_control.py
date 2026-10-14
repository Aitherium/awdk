"""Escalation control for one streaming turn: plan up front, hint on failure, rescue a stall.

``AitherAgent.stream_react`` builds one ``EscalationController`` per turn and calls its
three hooks. Each hook returns its neutral value ("" or None) whenever it has nothing
useful to add, so a loop that ignores the controller behaves exactly as before.

Rules every implementation keeps:

- FAIL OPEN. A planner or reasoner fault must never kill a turn: catch, log, return the
  neutral value, and queue ``{"type": "degraded", "stage": "turn_control", ...}`` on
  ``events`` (the caller drains them with ``drain_events()`` into its event stream). The
  caller also wraps each hook in a timeout plus try/except.
- ONE BUDGET PER TURN. Every consult of the reasoning tier in a turn (re-router
  escalation, loop-guard stall rescue, and the loop's own ask_reasoner consult after two
  failures, which reports itself through ``mark_consulted()``) shares one budget held on
  the controller. Once spent, no further consult happens this turn. The plan is a
  separate one-shot call on the agent's OWN model at low effort, not a reasoning-tier
  consult, so a planned hard turn can still be rescued once when it stalls.
- STEP, NOT TURN. A consult answers the one stalled step and its answer goes back as an
  observation. The turn keeps running on the tuned orchestrator model: the reasoning
  tier does not follow the text ACTION/FINAL protocol reliably, so a whole turn moved
  onto it breaks the loop it was meant to rescue.
- SMALL OUTPUT. A plan block is at most ``PLAN_MAX_CHARS``; a hint at most
  ``HINT_MAX_CHARS``. They go into the scratchpad, which is compacted at 75% of the
  window, so a long plan costs the turn its own observations.
- NO TREE SEARCH. MCTS rollouts are model calls on the local pool and would stall a
  live turn for minutes; it stays on ``adk solve``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger("adk.turn_control")

PLAN_MAX_CHARS = 1200
HINT_MAX_CHARS = 600

#: Reasoning-tier consults allowed per turn. Supervisor, ask_reasoner and a re-router
#: could each fire on the same stuck step; one budget keeps that to one deep call.
CONSULT_BUDGET = 1
#: Consecutive zero-progress steps before the re-router escalates.
STUCK_STEPS = 3
#: An effort at or above this always gets a plan; agentic turns get one from PLAN_AGENTIC.
PLAN_EFFORT = 7
PLAN_AGENTIC = 6

#: Tools that change the world. A successful call is progress even when its output
#: repeats (two identical "ok" writes are two writes).
_DELTA_TOOLS = frozenset({
    "file_edit", "file_write", "shell_exec", "python_exec", "run_tests", "apply_patch",
    "git_apply", "git_add", "git_commit", "repl_run",
})


#: Results that answer nothing even when the tool reports success.
_EMPTY_RESULTS = frozenset({"", "[]", "{}", "null", "none", "no results", "no matches",
                            "not found", "(no output)"})


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        logger.warning("turn_control: %s is not a number; using %s", name, default)
        return default


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _tool_answer(tool: str, raw: str) -> str:
    """The answer text in a reasoner tool's output. ``ToolRegistry.execute`` turns a tool
    exception into ``{"error": ...}``; that is a failed consult, never advice."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw.strip()
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(f"{tool}: {data['error']}")
        if "answer" in data:
            return str(data.get("answer") or "").strip()
    return raw.strip()


def _fingerprint(text: str) -> str:
    norm = " ".join((text or "").split())[:2000]
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


class EscalationController:
    """Per-turn escalation state.

    Args:
        agent: the ``AitherAgent`` running the turn.
        decision: the turn's ``adk.intent.IntentDecision``, or None when unclassified.
        max_steps: the loop's step budget for this turn.

    Attributes the caller may set after construction:
        task: the user's message. The plan and a consult need it; without it the plan
            is skipped and a consult describes only the stalled steps.
        effort: overrides ``decision.effort`` (for example a client ``reasoning`` flag).
    """

    def __init__(self, agent: Any, decision: Any, max_steps: int) -> None:
        self.agent = agent
        self.decision = decision
        self.max_steps = max_steps
        self.task: str = ""
        self.effort: int | None = None
        try:
            if decision is not None and getattr(decision, "effort", None) is not None:
                self.effort = int(decision.effort)
        except (TypeError, ValueError):
            logger.warning("turn_control: decision.effort is not an int: %r",
                           getattr(decision, "effort", None))
        self.events: list[dict] = []
        self.consults_left = CONSULT_BUDGET
        self.planned = False
        self._nudged = False
        self._seen: set[str] = set()
        self._recent: list[dict] = []
        self._router: Any = None
        try:
            from adk.rerouter import ReRouter

            # ceiling=0: the ReRouter's "3/4 of the ceiling and nothing landed" rule is
            # for edit tasks. Most awsh turns answer from reads and never land an edit,
            # so on a 6-14 step turn it would fire at step 4-10 of every research turn.
            # The spine's forced synthesis already handles the step limit.
            self._router = ReRouter(stuck_threshold=STUCK_STEPS, ceiling=0)
        except Exception as exc:  # noqa: BLE001 -- the re-router is optional
            self._degraded("rerouter", exc)

    # -- budget and events ---------------------------------------------------------

    @property
    def consulted(self) -> bool:
        """True once this turn has spent its reasoning-tier consult."""
        return self.consults_left <= 0

    def mark_consulted(self) -> None:
        """Spend the budget for a consult the loop made itself (its ask_reasoner call)."""
        self.consults_left = 0

    def drain_events(self) -> list[dict]:
        """Return and clear the events queued since the last drain."""
        out, self.events = self.events, []
        return out

    def _degraded(self, part: str, exc: BaseException | str) -> None:
        err = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        logger.warning("turn_control %s degraded: %s", part, err)
        self.events.append({"type": "degraded", "stage": "turn_control", "part": part,
                            "error": _clip(err, 300)})

    # -- plan ----------------------------------------------------------------------

    def _wants_plan(self) -> bool:
        if self.agent is None or not (self.task or "").strip() or self.effort is None:
            return False
        if self.effort >= PLAN_EFFORT:
            return True
        # Effort <= 5 gets a 6-step budget; a plan call costs more than it saves there.
        return bool(getattr(self.decision, "agentic", False)) and self.effort >= PLAN_AGENTIC

    async def before_loop(self) -> str:
        """Called once, before step 1. Returns a plan block (<= ``PLAN_MAX_CHARS``) to
        add to the system context, or "" for no plan (the default, and on any fault)."""
        if self.planned or not self._wants_plan():
            return ""
        self.planned = True
        try:
            from adk.orchestrate import render_plan, route_task

            timeout = _env_float("ADK_PLAN_TIMEOUT_S", 20.0)
            decision = await asyncio.wait_for(route_task(self._plan_caller(), self.task),
                                              timeout=timeout)
            block = _clip(render_plan(decision), PLAN_MAX_CHARS)
        except asyncio.TimeoutError:
            self._degraded("plan", "planner timed out")
            return ""
        except Exception as exc:  # noqa: BLE001 -- contract errors included: fail open
            self._degraded("plan", exc)
            return ""
        self.events.append({"type": "plan", "text": block, "effort": self.effort})
        return block

    def _plan_caller(self):
        agent = self.agent

        async def _call(messages: list[dict]) -> str:
            from adk.llm.base import Message

            msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
            # Same fast lane as classify_intent: the planner emits JSON, not a scratchpad.
            for m in reversed(msgs):
                if m.role == "user":
                    m.content = (str(m.content) + " /no_think").strip()
                    break
            resp = await agent.llm.chat(msgs, effort=2)
            return getattr(resp, "content", "") or ""

        return _call

    # -- per-step observation ------------------------------------------------------

    def _is_progress(self, name: str, args: dict, obs: str, ok: bool) -> bool:
        """A step made progress when it changed the world or learned something new.

        Strict "delta only" progress (the ReRouter default) would call every read-only
        research turn stuck after 3 reads; awsh turns are mostly that. A repeated call, a
        failure, an empty result or an observation already seen is zero progress.
        """
        if not ok:
            return False
        if name in _DELTA_TOOLS:
            return True
        if (obs or "").strip().strip(".").lower() in _EMPTY_RESULTS:
            return False
        try:
            call_key = name + "|" + json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            call_key = name + "|" + repr(args)
        keys = ("call:" + call_key, "obs:" + _fingerprint(obs))
        fresh = not any(k in self._seen for k in keys)
        self._seen.update(keys)
        return fresh

    async def observe(self, step: int, name: str, args: dict, obs: str, ok: bool) -> str | None:
        """Called after every tool call.

        Args:
            step: the 1-based loop step.
            name: the tool called.
            args: its arguments.
            obs: the observation text the loop will show the model.
            ok: False when the tool raised or reported an error.

        Returns an extra observation/hint (<= ``HINT_MAX_CHARS``) the loop appends after
        ``obs``, or None for nothing (the default, and on any fault).
        """
        try:
            if not isinstance(args, dict):
                # The loop hands over ``_parsed or {}``; a JSON list or a bare string
                # would make the ReRouter's target lookup raise on every step.
                args = {"_raw": repr(args)[:300]} if args else {}
            self._recent = (self._recent + [{"step": step, "tool": name, "args": args,
                                             "ok": ok, "obs": (obs or "")[:400]}])[-4:]
            if self._router is None:
                return None
            progress = self._is_progress(name, args or {}, obs or "", ok)
            d = self._router.after_step(
                {"tool": name, "ok": ok, "args": args or {}, "result_head": (obs or "")[:400],
                 "work_tree_changed": progress},
                step_index=step)
            if d.action == "continue":
                return None
            # ReRouter's "done" is a hint, not the end of the turn: the stream's own
            # {"type": "done"} is terminal, so it must not reach the stream under that name.
            etype = "reroute_done" if d.action == "done" else d.action
            self.events.append({"type": etype, "step": step, "reason": d.reason})
            if d.action == "escalate":
                advice = await self._consult(f"re-router: {d.reason}")
                if advice:
                    return advice
                return _clip(f"[RE-ROUTER] {d.reason}. Another call like the last ones will "
                             "not move this: state in one line what you now believe is "
                             "wrong, then take the one action that tests it, or answer "
                             "with what you have.", HINT_MAX_CHARS)
            return _clip(d.message(), HINT_MAX_CHARS) or None
        except Exception as exc:  # noqa: BLE001 -- a hint is optional; the turn is not
            self._degraded("observe", exc)
            return None

    async def on_stall(self) -> str | None:
        """Called when the loop guard reports the turn is stuck (repeats, no progress).

        Returns a rescue hint (<= ``HINT_MAX_CHARS``) to inject before the next step, or
        None to let the loop guard's own action stand (the default, and on any fault).
        """
        try:
            if self.consulted or self.agent is None:
                return None
            self.events.append({"type": "escalate", "step": self._last_step(),
                                "reason": "loop guard: stalled"})
            return await self._consult("loop guard: stalled")
        except Exception as exc:  # noqa: BLE001
            self._degraded("on_stall", exc)
            return None

    def grounding_nudge(self, step: int, tools_run: int) -> str | None:
        """Called when the model answers FINAL. Returns a one-time nudge when the turn's
        answer depends on data the model does not have and no tool has run yet; None
        otherwise. ``step`` is recorded only.

        The classifier sets ``requires_grounding`` for calendar, mail, files, web and
        live data; a FINAL before any tool on such a turn is an answer from memory.
        """
        try:
            if self._nudged or tools_run > 0:
                return None
            if not bool(getattr(self.decision, "requires_grounding", False)):
                return None
            self._nudged = True
            label = str(getattr(self.decision, "grounding_label", "") or "the live data")
            self.events.append({"type": "nudge", "step": step, "reason": "grounding"})
            return _clip(f"This answer depends on {label}, and no tool has run yet. Call the "
                         "tool that reads it, then answer from what it returns.",
                         HINT_MAX_CHARS)
        except Exception as exc:  # noqa: BLE001
            self._degraded("grounding", exc)
            return None

    # -- the one consult -------------------------------------------------------------

    def _last_step(self) -> int:
        return int(self._recent[-1]["step"]) if self._recent else 0

    def _stalled_step_question(self, why: str) -> tuple[str, str]:
        task = _clip(self.task, 1500) or "(not given)"
        question = (f"The user asked: {task}\nI am stuck ({why}). Which ONE next action, "
                    "with exact arguments, gets me unstuck? If I already have enough to "
                    "answer, say so and give the answer.")
        lines = []
        for s in self._recent:
            try:
                a = json.dumps(s["args"], default=str)[:300]
            except (TypeError, ValueError):
                a = repr(s["args"])[:300]
            lines.append(f"step {s['step']}: {s['tool']}({a}) ok={s['ok']} -> {s['obs']}")
        return question, "Recent steps:\n" + "\n".join(lines)

    async def _consult(self, why: str) -> str | None:
        """Ask the reasoning tier about the stalled step, once per turn."""
        if self.consulted or self.agent is None:
            return None
        self.consults_left -= 1
        question, context = self._stalled_step_question(why)
        timeout = _env_float("ADK_ESCALATION_TIMEOUT_S", 90.0)
        try:
            source, answer = await asyncio.wait_for(self._ask(question, context),
                                                    timeout=timeout)
        except asyncio.TimeoutError:
            self._degraded("consult", f"reasoner timed out after {int(timeout)} s")
            return None
        except Exception as exc:  # noqa: BLE001
            self._degraded("consult", exc)
            return None
        if not answer:
            self._degraded("consult", f"{source} returned no answer")
            return None
        self.events.append({"type": "escalated", "source": source, "why": why,
                            "step": self._last_step()})
        return _clip(f"REASONER ({source}, consulted on the stalled step): {answer}",
                     HINT_MAX_CHARS)

    async def _ask(self, question: str, context: str) -> tuple[str, str]:
        """ask_reasoner (one-shot) first, then deep_reasoning, then the router's reasoning
        tier directly. One-shot comes first: a stall needs one concrete next action, and
        deep_reasoning may run a multi-step pass that outlives the turn."""
        tools = getattr(self.agent, "_tools", None)
        if tools is not None and tools.get("ask_reasoner") is not None:
            raw = str(await tools.execute("ask_reasoner",
                                          {"question": question, "context": context}))
            return "ask_reasoner", _tool_answer("ask_reasoner", raw)
        if tools is not None and tools.get("deep_reasoning") is not None:
            raw = str(await tools.execute("deep_reasoning",
                                          {"problem": question + "\n\n" + context}))
            return "deep_reasoning", _tool_answer("deep_reasoning", raw)
        llm = getattr(self.agent, "llm", None)
        if llm is None:
            raise RuntimeError("no reasoner: no ask_reasoner, deep_reasoning or llm")
        from adk.llm.base import Message

        resp = await llm.chat([Message(role="system", content="Answer exactly and briefly."),
                               Message(role="user", content=question + "\n\n" + context)],
                              effort=8)
        return "reasoning tier", str(getattr(resp, "content", "") or "").strip()
