"""The orchestrator contract (orchestration spec section 2, owner 2026-09-22).

Every orchestrator call returns STRICT JSON -- intent, effort, route, plan -- validated
before anything is dispatched. Invalid JSON gets ONE retry with the schema and the named
problems re-injected; a second failure is a hard ``OrchestratorContractError`` carrying
its telemetry. There is no silent "just send it to a chat model".

Validation is split in two, because a live 8B-class control-plane model (measured
2026-09-22) answers in the right SHAPE with the wrong VOCABULARY: it put a
sentence into ``route.model_hint`` and ``code_edit`` into a plan step's ``lane``.

  * fatal     -- not JSON, no object, intent outside the enum, no plan, effort not an
                 int. These trigger the retry, then the hard failure.
  * normalized -- a lane synonym mapped onto a real lane (``code_edit`` -> reasoning),
                 a ``model_hint`` that is not a catalogued model cleared, effort clamped
                 to 0..3. Recorded in ``RouteDecision.normalized`` so the telemetry shows
                 how far the model is from the contract, but never a failure.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

INTENTS = ("code_edit", "qa", "perception", "tool_use", "planning", "conversation")
LANES = ("perception", "reasoning", "chat")
#: Effort levels E0..E3 (spec section 3).
EFFORT_MIN, EFFORT_MAX = 0, 3
#: What the model says -> the lane it means. Anything else is a normalization problem.
LANE_SYNONYMS = {
    "code_edit": "reasoning", "coding": "reasoning", "code": "reasoning",
    "tool_use": "reasoning", "planning": "reasoning", "qa": "reasoning",
    "deep": "reasoning", "deep_reasoning": "reasoning", "analysis": "reasoning",
    "conversation": "chat", "reflex": "chat",
    "vision": "perception", "image": "perception",
}

SCHEMA_TEXT = (
    "Return ONLY one JSON object, no prose, no code fence:\n"
    '{"intent": one of ' + "|".join(INTENTS) + ", "
    '"effort": integer 0-3 (0 reflex, 1 standard, 2 deep reasoning, 3 escalated), '
    '"route": {"lane": one of ' + "|".join(LANES) + ', "model_hint": a catalogued model id '
    'or "", "fallback": [model ids]}, '
    '"plan": [{"step": 1, "lane": one of ' + "|".join(LANES) + ', "goal": "one short '
    'imperative"}, ...] (1-6 steps), '
    '"escalation": {"stuck_threshold": integer, "next_effort": integer}}'
)

SYSTEM_PROMPT = (
    "You are the control plane of an agent system: a classifier and planner, not a chat "
    "model. Classify the task's intent and effort, choose the lane that should execute it, "
    "and write a short step plan. " + SCHEMA_TEXT
)


class OrchestratorContractError(RuntimeError):
    """The orchestrator failed the contract twice. ``telemetry`` says how."""

    def __init__(self, message: str, telemetry: dict):
        super().__init__(message)
        self.telemetry = telemetry


@dataclass
class RouteDecision:
    intent: str
    effort: int
    route: dict
    plan: list[dict]
    escalation: dict
    attempts: int = 1
    normalized: list[str] = field(default_factory=list)
    wall_s: float = 0.0

    def to_dict(self) -> dict:
        return {"intent": self.intent, "effort": self.effort, "route": self.route,
                "plan": self.plan, "escalation": self.escalation,
                "attempts": self.attempts, "normalized": list(self.normalized),
                "wall_s": round(self.wall_s, 2)}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
#: A reasoning model answers with its scratchpad first. Measured 2026-09-22 on
#: an 8B control-plane model: every first attempt opened with <think>...</think>, and the
#: JSON that followed was then cut off by the token cap -- "not JSON" on a model that
#: had, in fact, understood the contract. Strip the block, and ask for thinking off.
_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """The first JSON object in ``text`` (fences and leading prose tolerated). Raises
    ValueError when there is none."""
    s = _FENCE.sub("", _THINK.sub("", text or "").strip())
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in the answer")
    obj, _end = json.JSONDecoder().raw_decode(s[start:])
    return obj


def _lane(value: Any, where: str, normalized: list[str]) -> Optional[str]:
    v = str(value or "").strip().lower()
    if v in LANES:
        return v
    if v in LANE_SYNONYMS:
        normalized.append(f"{where}: lane {v!r} -> {LANE_SYNONYMS[v]!r}")
        return LANE_SYNONYMS[v]
    return None


def validate(obj: Any, *,
              models: Optional[Iterable[str]] = None
              ) -> tuple[Optional[RouteDecision], list[str]]:
    """``(decision, fatal_problems)``. ``decision`` is None when any problem is fatal."""
    fatal: list[str] = []
    normalized: list[str] = []
    known = {m for m in (models or ())}
    if not isinstance(obj, dict):
        return None, ["the answer is not a JSON object"]

    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        fatal.append(f"intent {intent!r} is not one of {'|'.join(INTENTS)}")

    effort_raw = obj.get("effort")
    try:
        effort = int(effort_raw)
    except (TypeError, ValueError):
        fatal.append(f"effort {effort_raw!r} is not an integer")
        effort = 0
    if not EFFORT_MIN <= effort <= EFFORT_MAX:
        normalized.append(f"effort {effort} clamped to {EFFORT_MIN}..{EFFORT_MAX}")
        effort = max(EFFORT_MIN, min(EFFORT_MAX, effort))

    route_in = obj.get("route") if isinstance(obj.get("route"), dict) else {}
    lane = _lane(route_in.get("lane"), "route", normalized)
    if lane is None:
        normalized.append(f"route: lane {route_in.get('lane')!r} unknown -> 'reasoning'")
        lane = "reasoning"
    hint = str(route_in.get("model_hint") or "").strip()
    if hint and known and hint not in known:
        normalized.append(f"route: model_hint {hint[:60]!r} is not a catalogued model -> ''")
        hint = ""
    fallback = [str(m) for m in (route_in.get("fallback") or []) if isinstance(m, str)]
    if known:
        dropped = [m for m in fallback if m not in known and not m.startswith("cloud:")]
        if dropped:
            normalized.append(f"route: fallback dropped {dropped}")
        fallback = [m for m in fallback if m not in dropped]
    route = {"lane": lane, "model_hint": hint, "fallback": fallback}

    plan_in = obj.get("plan")
    plan: list[dict] = []
    if not isinstance(plan_in, list) or not plan_in:
        fatal.append("plan is missing or empty")
    else:
        for i, st in enumerate(plan_in[:6], 1):
            if not isinstance(st, dict):
                normalized.append(f"plan[{i}] is not an object -- dropped")
                continue
            goal = str(st.get("goal") or "").strip()
            if not goal:
                normalized.append(f"plan[{i}] has no goal -- dropped")
                continue
            sl = _lane(st.get("lane"), f"plan[{i}]", normalized) or lane
            plan.append({"step": len(plan) + 1, "lane": sl, "goal": goal[:300]})
        if not plan:
            fatal.append("plan has no usable step")

    esc_in = obj.get("escalation") if isinstance(obj.get("escalation"), dict) else {}
    try:
        stuck = int(esc_in.get("stuck_threshold", 8))
    except (TypeError, ValueError):
        stuck = 8
    try:
        nxt = int(esc_in.get("next_effort", min(effort + 1, EFFORT_MAX)))
    except (TypeError, ValueError):
        nxt = min(effort + 1, EFFORT_MAX)
    escalation = {"stuck_threshold": max(1, stuck),
                  "next_effort": max(EFFORT_MIN, min(EFFORT_MAX, nxt))}

    if fatal:
        return None, fatal
    return RouteDecision(intent=intent, effort=effort, route=route, plan=plan,
                         escalation=escalation, normalized=normalized), []


Caller = Callable[[list[dict]], Awaitable[str]]


async def route_task(call: Caller, task: str, *, models: Optional[Iterable[str]] = None,
                     max_task_chars: int = 6000) -> RouteDecision:
    """Classify + plan ``task`` through the orchestrator. One retry with the schema and the
    named problems; then ``OrchestratorContractError``. Never a silent fallback."""
    models = list(models or ())
    t0 = time.time()
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (task or "")[:max_task_chars]}]
    problems: list[str] = []
    answers: list[str] = []
    for attempt in (1, 2):
        text = await call(messages)
        answers.append((text or "")[:400])
        try:
            obj = extract_json(text)
        except ValueError as exc:
            problems = [f"not JSON: {exc}"]
        else:
            decision, problems = validate(obj, models=models)
            if decision is not None:
                decision.attempts = attempt
                decision.wall_s = time.time() - t0
                return decision
        if attempt == 1:
            messages = messages + [
                {"role": "assistant", "content": text or ""},
                {"role": "user", "content": "That answer broke the contract: "
                 + "; ".join(problems) + ". " + SCHEMA_TEXT},
            ]
    raise OrchestratorContractError(
        "orchestrator contract failed twice: " + "; ".join(problems),
        {"attempts": 2, "problems": problems, "answers": answers,
         "wall_s": round(time.time() - t0, 2)})


def render_plan(decision: RouteDecision) -> str:
    """The plan as the executor sees it: a short block ahead of the task."""
    lines = [f"## Orchestrator plan (intent {decision.intent}, effort E{decision.effort}, "
             f"lane {decision.route['lane']})"]
    lines += [f"{s['step']}. [{s['lane']}] {s['goal']}" for s in decision.plan]
    return "\n".join(lines)


def make_openai_caller(url: str, model: str, *, api_key: str = "",
                       timeout: float = 120.0, max_tokens: int = 1400) -> Caller:
    """An OpenAI-compatible chat call, greedy, returning the message text."""
    base = url.rstrip("/")

    async def _call(messages: list[dict]) -> str:
        import httpx

        try:
            from adk._tls import tls_verify

            verify: Any = tls_verify()
        except Exception:  # noqa: BLE001
            verify = True
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        body = {"model": model, "messages": messages, "temperature": 0,
                "max_tokens": max_tokens,
                # The control plane classifies; it does not need a scratchpad, and the
                # scratchpad is what truncated the JSON (see _THINK). Servers whose
                # template does not take the kwarg answer 400 -- retry without it once.
                "chat_template_kwargs": {"enable_thinking": False}}
        async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
            r = await client.post(f"{base}/chat/completions", headers=headers, json=body)
            if r.status_code == 400 and "chat_template_kwargs" in body:
                body.pop("chat_template_kwargs")
                r = await client.post(f"{base}/chat/completions", headers=headers, json=body)
            r.raise_for_status()
            return (r.json()["choices"][0]["message"].get("content") or "")

    return _call
