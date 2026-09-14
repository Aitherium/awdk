"""Completion gate — verified-or-retried task execution for any agent.

An agent's ReAct loop returns whatever the model produced and calls it done
(``finish_reason`` = stop / max_steps / length): that is a *liveness* signal,
not a *completion* one, so an agent can "succeed" while doing nothing. The gate
closes that gap by COMPOSITION — no subclassing, no surgery in the loop. It runs
the agent, verifies the result against acceptance criteria, and retries with the
failure fed back — then fails HONESTLY (``finish_reason="unverified"``) instead
of silently succeeding.

    from adk.gate import CompletionGate

    gate = CompletionGate(
        criteria=['writes /work/out.json', 'output must contain "OK"'],
        max_retries=2,
    )
    resp = await gate.run(agent, "Generate the report to /work/out.json")
    assert resp.finish_reason == "verified"   # or "unverified" on honest failure

Verification is HARD-FIRST and un-soft-passable: a filesystem path a criterion
says must exist is checked on disk; a required literal token must appear in the
output. Only genuinely subjective criteria fall through to an optional LLM judge
(which reuses the agent's own model by default). Criteria present but nothing
able to verify them → the gate fails CLOSED. Never a false pass.

Design note: this is deliberately agent-agnostic. It wraps anything exposing
``async run(task, **kwargs) -> response`` where ``response`` has ``.content`` and
a ``finish_reason`` field (AitherAgent / core.Agent / a thin adapter over a
Genesis dispatch). Compose it; don't inherit it.
"""
from __future__ import annotations

import inspect
import json
import os
import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional, Sequence

# A path a criterion names as a required artifact (posix or windows).
_PATH_RE = re.compile(r"(/[\w./\-]+\.\w+|[A-Za-z]:\\[\w\\.\-]+)")
# A quoted literal token the output must surface as proof-of-work.
_TOKEN_RE = re.compile(r'"([^"]{2,80})"|`([^`]{2,80})`')
# A BARE, code-like literal token (has an underscore, or is 4+ chars of CAPS/
# digits) — so a natural criterion like ``output contains ADK_AUTO_OK`` is checked
# hard instead of falling to the LLM judge. Deliberately conservative: it only
# matches identifier/code shapes, never ordinary prose, so we don't invent false
# hard-fails from a descriptive sentence.
_BARE_TOKEN_RE = re.compile(r"\b([A-Za-z0-9]*_[A-Za-z0-9_]+|[A-Z][A-Z0-9]{3,})\b")
# Uppercase words that look code-like but are just describing the criterion.
_BARE_STOPWORDS = frozenset({
    "TOKEN", "OUTPUT", "STRING", "VALUE", "EXACT", "EXACTLY", "MUST", "CONTAIN",
    "CONTAINS", "CONTAINING", "RESULT", "ANSWER", "RESPONSE", "TRUE", "FALSE",
    "JSON", "TEXT", "WORD", "PHRASE", "LITERAL", "PROOF", "DONE", "NULL", "NONE",
    "SHOULD", "INCLUDE", "INCLUDES", "REPLY", "SENTENCE",
})

Judge = Callable[[str], Awaitable[str]]


def _first_json_object(text: str) -> Optional[dict]:
    """Extract the first complete JSON object from a possibly-noisy LLM reply.

    Models wrap verdicts in prose, markdown fences, or emit a second object —
    ``json.loads(text[first{:last}])`` then dies with "Extra data". ``raw_decode``
    parses ONE value from a start offset and ignores whatever trails it, so we
    scan for the first ``{`` that begins a valid object. Returns ``None`` when
    nothing parses (caller fails closed — never a soft pass)."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            return None
        try:
            obj, _ = decoder.raw_decode(text, start)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = start + 1


@dataclass(slots=True)
class GateVerdict:
    """The gate's decision on one result. ``method`` records HOW it was decided
    (``hard`` = mechanical check, ``judge`` = LLM, ``none`` = no verifier)."""

    approved: bool
    reason: str = ""
    attempts: int = 0
    method: str = "none"


def hard_checks(output: str, criteria: Sequence[str]) -> Optional[GateVerdict]:
    """Mechanical, un-soft-passable checks derived from the criteria.

    Returns a verdict when at least one criterion is machine-checkable (a path
    that must exist on disk, or — for a criterion that says the output must
    *contain* something — a required quoted token). Returns ``None`` when nothing
    is mechanically checkable, so the caller can fall through to a judge. A hard
    FAIL here cannot be fallback-approved — that is the whole point of the gate."""
    text = output or ""
    checked = False
    for crit in criteria:
        c = str(crit)
        for m in _PATH_RE.finditer(c):
            path = m.group(0)
            checked = True
            if not os.path.exists(path):
                return GateVerdict(False, f"required artifact missing: {path}", method="hard")
        if "contain" in c.lower() or "includ" in c.lower():
            found_quoted = False
            for m in _TOKEN_RE.finditer(c):
                token = m.group(1) or m.group(2)
                if token:
                    found_quoted = True
                    checked = True
                    if token not in text:
                        return GateVerdict(
                            False, f"output missing required token {token!r}", method="hard"
                        )
            # No quoted token? Fall back to a bare code-like literal so the natural
            # phrasing ``contains ADK_AUTO_OK`` is still enforced mechanically.
            if not found_quoted:
                for m in _BARE_TOKEN_RE.finditer(c):
                    token = m.group(1)
                    if token and token.upper() not in _BARE_STOPWORDS:
                        checked = True
                        if token not in text:
                            return GateVerdict(
                                False, f"output missing required token {token!r}", method="hard"
                            )
    return GateVerdict(True, "hard checks passed", method="hard") if checked else None


_JUDGE_TMPL = (
    "TASK:\n{task}\n\nACCEPTANCE CRITERIA:\n{criteria}\n\nAGENT OUTPUT:\n{output}\n\n"
    "Was the task ACTUALLY completed (not merely described, planned, or promised)? "
    'Reply with STRICT JSON only: {{"approved": true|false, "reason": "<short>"}}'
)


async def _llm_judge(judge: Judge, task: str, output: str, criteria: Sequence[str]) -> GateVerdict:
    crit = "\n".join(f"- {c}" for c in criteria)
    prompt = _JUDGE_TMPL.format(task=task[:2000], criteria=crit, output=(output or "")[:6000])
    text = await judge(prompt)
    data = _first_json_object(text)
    if data is None:
        # A judge that answered but emitted no parseable verdict must NOT soft-pass.
        return GateVerdict(
            False, "judge returned no parseable JSON verdict; failing closed", method="judge"
        )
    return GateVerdict(bool(data.get("approved")), str(data.get("reason", ""))[:200], method="judge")


def judge_from_agent(agent: Any) -> Optional[Judge]:
    """Build a judge callable from an agent's own LLM (``agent.llm.chat``).

    Returns ``None`` if the agent exposes no usable LLM, so the caller can decide
    to fail closed rather than soft-pass."""
    llm = getattr(agent, "llm", None)
    if llm is None or not hasattr(llm, "chat"):
        return None

    async def _judge(prompt: str) -> str:
        from adk.llm.base import Message

        resp = await llm.chat([Message(role="user", content=prompt)], effort=2)
        return getattr(resp, "content", "") or ""

    return _judge


class CompletionGate:
    """Wrap any agent so a task ends VERIFIED or RETRIED — never silent-success.

    Parameters
    ----------
    criteria:
        Acceptance criteria (natural language). Mechanically-checkable ones are
        enforced hard; the rest go to a judge.
    max_retries:
        Extra attempts after the first (each retry feeds the failure reason back
        into the task). ``0`` = single attempt.
    judge:
        Optional ``async (prompt) -> text`` used for subjective criteria. If not
        given, ``run`` derives one from the agent's own LLM; if that too is
        unavailable, subjective criteria fail CLOSED.
    verifier:
        Optional full override ``(output, criteria) -> GateVerdict`` (sync or
        async) for callers who want custom verification (tests-pass, HTTP probe…).
    auto_criteria:
        When no ``criteria`` are supplied, ask the judge/LLM to write a
        machine-checkable definition-of-done from the task itself.
    """

    def __init__(
        self,
        *,
        criteria: Optional[Sequence[str]] = None,
        max_retries: int = 2,
        judge: Optional[Judge] = None,
        verifier: Optional[Callable[..., Any]] = None,
        auto_criteria: bool = False,
    ) -> None:
        self.criteria = [str(c) for c in (criteria or [])]
        self.max_retries = max(0, int(max_retries))
        self.judge = judge
        self.verifier = verifier
        self.auto_criteria = auto_criteria
        self.last_verdict: Optional[GateVerdict] = None

    async def verify(
        self, task: str, output: str, criteria: Optional[Sequence[str]] = None
    ) -> GateVerdict:
        crit = list(criteria if criteria is not None else self.criteria)
        if self.verifier is not None:
            v = self.verifier(output, crit)
            return await v if inspect.isawaitable(v) else v
        if not crit:
            return GateVerdict(True, "no acceptance criteria — unverified liveness pass", method="none")
        hard = hard_checks(output, crit)
        if hard is not None:
            return hard
        if self.judge is not None:
            try:
                return await _llm_judge(self.judge, task, output, crit)
            except Exception as e:  # judge unavailable → do NOT soft-pass
                return GateVerdict(
                    False, f"judge unavailable ({type(e).__name__}: {e}); failing closed", method="judge"
                )
        return GateVerdict(
            False, "criteria present but no verifier available; failing closed", method="none"
        )

    async def run(self, agent: Any, task: str, **kwargs: Any) -> Any:
        """Run ``task`` on ``agent`` behind the gate. Returns the agent's own
        response type with ``finish_reason`` set to ``verified`` / ``unverified``."""
        if self.judge is None:
            self.judge = judge_from_agent(agent)  # reuse the agent's model, if any

        criteria = list(self.criteria)
        if not criteria and self.auto_criteria and self.judge is not None:
            criteria = await self._derive_criteria(task)

        feedback = ""
        resp: Any = None
        for attempt in range(1, self.max_retries + 2):
            run_task = task if not feedback else (
                f"{task}\n\n[PREVIOUS ATTEMPT FAILED THE COMPLETION CHECK]\n{feedback}\n"
                "Correct it and satisfy every acceptance criterion this time."
            )
            resp = await agent.run(run_task, **kwargs)
            # A turn paused for human tool-approval is not a failure — surface it
            # unchanged; the caller resumes and re-submits through the gate.
            if getattr(resp, "requires_action", False):
                return resp
            verdict = await self.verify(task, getattr(resp, "content", "") or "", criteria)
            verdict.attempts = attempt
            self.last_verdict = verdict
            if verdict.approved:
                return _with_finish(resp, "verified")
            feedback = verdict.reason
        return _with_finish(resp, "unverified")

    async def _derive_criteria(self, task: str) -> list[str]:
        prompt = (
            f"TASK:\n{task}\n\nWrite 2-4 CONCRETE machine-checkable acceptance criteria "
            "(a file path that must exist, a literal token the output must contain, an "
            'observable condition). STRICT JSON only: {"criteria": ["...", "..."]}'
        )
        try:
            text = await self.judge(prompt)  # type: ignore[misc]
            data = _first_json_object(text) or {}
            return [str(c) for c in data.get("criteria", []) if str(c).strip()][:6]
        except Exception:
            return []


def _with_finish(resp: Any, finish_reason: str) -> Any:
    """Return ``resp`` with ``finish_reason`` set, preferring immutable ``replace``
    for dataclasses and falling back to attribute assignment."""
    try:
        return replace(resp, finish_reason=finish_reason)
    except Exception:
        try:
            resp.finish_reason = finish_reason
        except Exception:
            pass
        return resp


async def gated_run(
    agent: Any,
    task: str,
    *,
    criteria: Optional[Sequence[str]] = None,
    max_retries: int = 2,
    judge: Optional[Judge] = None,
    auto_criteria: bool = False,
    **kwargs: Any,
) -> Any:
    """One-shot convenience: run ``task`` on ``agent`` behind a completion gate."""
    gate = CompletionGate(
        criteria=criteria, max_retries=max_retries, judge=judge, auto_criteria=auto_criteria
    )
    return await gate.run(agent, task, **kwargs)
