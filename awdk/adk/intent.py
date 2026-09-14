"""Canonical LLM-based intent router for AitherADK.

ONE shared place that decides, for a single chat turn:
  • intent type         (conversation, question, analysis, …)
  • effort              (1-10 — drives model tier + loop budget)
  • agentic             (whether the tool-using ReAct loop is even warranted)
  • reasoning_depth     (skip | gate | light | sase)

This replaces the brittle keyword classifiers that were copied across Genesis
(lib/faculties + lib/orchestration), awkit-backend (agent_core/intent.py),
and ad-hoc per-product logic — every surface now imports THIS.

Design:
  • A SINGLE fast LLM call. The caller supplies ``llm_complete`` (an async
    ``(messages) -> str``) so this module is backend-agnostic — it works with the
    ADK ``LLMRouter`` (``AitherAgent.classify_intent``), Genesis's gateway, or a
    direct MicroScheduler ``/llm/generate`` call. No hardcoded endpoints.
  • Thinking models are fine: MicroScheduler/vLLM separate ``<think>`` reasoning
    from the final ``content``, and we additionally extract the JSON object from
    whatever text we get, so a stray think block never breaks parsing.
  • NEVER raises. On any failure (LLM down, bad JSON) it falls back to a cheap
    keyword heuristic so callers degrade gracefully instead of crashing.
"""

from __future__ import annotations

import json as _json
import re as _re
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

INTENT_VALUES = (
    "conversation", "question", "analysis", "creation", "document",
    "generation", "command", "research", "planning", "business",
    "calendar", "email",
)


@dataclass
class IntentDecision:
    """The routing decision for one chat turn."""
    intent: str
    effort: int               # 1-10
    agentic: bool             # run the tool-using ReAct loop?
    reasoning_depth: str      # skip | gate | light | sase
    # requires_grounding: the answer DEPENDS on data/tools the model does not
    # have (calendar, email, files, web, account/live info) — so the instant
    # first-pass must NOT fabricate; it should honestly say it's retrieving it.
    requires_grounding: bool = False
    grounding_label: str = ""  # short honest phrase, e.g. "your calendar", "the web"
    source: str = "llm"       # llm | keyword (fallback)

    def as_dict(self) -> dict:
        return {
            "intent": self.intent, "effort": self.effort,
            "agentic": self.agentic, "reasoning_depth": self.reasoning_depth,
            "requires_grounding": self.requires_grounding,
            "grounding_label": self.grounding_label,
            "source": self.source,
        }


# Async (messages: list[{"role","content"}]) -> raw model text.
LLMComplete = Callable[[list], Awaitable[str]]

_SYS = (
    "You are the intent router for an AI assistant. Read the user's latest "
    "message (with any recent conversation for context) and decide how much "
    "work it actually needs. Respond with ONE compact JSON object and NOTHING "
    "else — no prose, no code fences:\n"
    '{"intent":"<conversation|question|analysis|creation|document|generation|'
    'command|research|planning|business|calendar|email>","effort":<1-10>,'
    '"agentic":<true|false>,"reasoning_depth":"<skip|gate|light|sase>",'
    '"requires_grounding":<true|false>,"grounding_label":"<short phrase or empty>"}\n'
    "Guidance: greetings, small talk, thanks, acknowledgements → "
    'intent="conversation", effort 1, agentic false, reasoning_depth "skip". '
    "A question answerable directly from general knowledge → "
    '"question", effort 2, agentic false. Set agentic=true ONLY when the '
    "request genuinely needs tools or multi-step work — reading/creating files "
    "or documents, calendar, email, web or workspace search, data analysis, or "
    "building an artifact. Scale effort with real complexity: 1-2 trivial, "
    "3-5 moderate, 6-8 complex, 9-10 deep multi-step.\n"
    "Set requires_grounding=true when answering DEPENDS on information you do "
    "NOT have and must fetch — the user's calendar, email, files/documents, "
    "bookings, account data, or live/web/current info. Set it false for general "
    "knowledge, reasoning, opinions, math, or chit-chat you can answer directly. "
    "This is critical: when true, the assistant must NOT guess the data. When "
    'true, set grounding_label to a SHORT human phrase for what is being '
    'retrieved (e.g. "your calendar", "the web", "your documents"); else "".'
)


def depth_for_effort(effort: int) -> str:
    """Canonical effort → reasoning-depth mapping (matches EffortScaler)."""
    if effort <= 1:
        return "skip"
    if effort <= 5:
        return "gate"
    if effort <= 6:
        return "light"
    return "sase"


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if not m:
        return None
    try:
        obj = _json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ── Keyword fallback (cheap, never calls the network, never raises) ──────────
_GREETING = _re.compile(r"^\s*(hi|hey|hello|yo|sup|thanks|thank you|ty|gm|good (morning|evening|afternoon)|how are you|how's it going|what's up)\b", _re.I)
_TOOL_VERB = _re.compile(r"\b(search|look up|find|email|send|schedule|book|calendar|read|open|upload|download|create|build|generate|write|draft|analyze|analyse|compare|research|investigate|deploy|run|execute|fix|debug)\b", _re.I)


def keyword_intent(message: str) -> IntentDecision:
    """Last-resort heuristic used only when the LLM router is unavailable."""
    msg = (message or "").strip()
    if not msg:
        return IntentDecision(intent="conversation", effort=1, agentic=False,
                              reasoning_depth="skip", requires_grounding=False, source="keyword")
    if _GREETING.search(msg) and len(msg) <= 60:
        return IntentDecision(intent="conversation", effort=1, agentic=False,
                              reasoning_depth="skip", requires_grounding=False, source="keyword")
    needs_tools = bool(_TOOL_VERB.search(msg))
    if needs_tools:
        # Tool/data verbs imply we'll need to fetch something — ground it, don't guess.
        return IntentDecision(intent="command", effort=5, agentic=True,
                              reasoning_depth="gate", requires_grounding=True,
                              grounding_label="your data", source="keyword")
    # A plain question / statement: answer directly, no loop.
    return IntentDecision(intent="question", effort=2, agentic=False,
                          reasoning_depth="gate", requires_grounding=False, source="keyword")


async def classify_intent(
    message: str,
    *,
    llm_complete: LLMComplete,
    tool_hint: str = "",
    history: Optional[list] = None,
) -> IntentDecision:
    """Classify one chat turn via a single fast LLM call.

    ``llm_complete`` is an async ``(messages) -> str``; ``messages`` are
    OpenAI-style ``{"role","content"}`` dicts. Returns an :class:`IntentDecision`;
    falls back to :func:`keyword_intent` on any error (never raises).
    """
    msg = (message or "").strip()
    if not msg:
        return IntentDecision(intent="conversation", effort=1, agentic=False,
                              reasoning_depth="skip", requires_grounding=False, source="keyword")

    sys = _SYS + (f"\nTools available to the assistant: {tool_hint}." if tool_hint else "")
    messages: list = [{"role": "system", "content": sys}]
    for m in (history or [])[-4:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content"):
            messages.append({"role": m["role"], "content": str(m["content"])[:1000]})
    messages.append({"role": "user", "content": msg})

    try:
        raw = await llm_complete(messages)
    except Exception:  # noqa: BLE001
        return keyword_intent(msg)

    obj = _extract_json(raw or "")
    if not obj:
        return keyword_intent(msg)

    intent = str(obj.get("intent", "")).lower().strip()
    if intent not in INTENT_VALUES:
        intent = "question"
    try:
        effort = max(1, min(int(obj.get("effort", 2)), 10))
    except Exception:  # noqa: BLE001
        effort = 2
    agentic = bool(obj.get("agentic", False))
    depth = str(obj.get("reasoning_depth", "")).lower().strip()
    if depth not in ("skip", "gate", "light", "sase"):
        depth = depth_for_effort(effort)
    grounding = bool(obj.get("requires_grounding", False))
    glabel = str(obj.get("grounding_label", "") or "").strip()[:40]
    return IntentDecision(intent=intent, effort=effort, agentic=agentic,
                          reasoning_depth=depth, requires_grounding=grounding,
                          grounding_label=glabel, source="llm")


# ── Coarse code intent — fail-open fallback when classifier is skipped ──────────
# Mirrors ContextPipeline._coarse_code_intent for intent-gated context assembly
# in the public SDK. Used when classify_intent() is unavailable or to gate tool
# availability by intent (CODE vs CONVERSATION). Cheap, regex-only, non-fatal.
_CODE_KEYWORD_RE = _re.compile(
    r"\b(?:def|class|import|function|method|module|traceback|exception|"
    r"stack ?trace|bug|refactor|implement|compile|endpoint|repo|commit|"
    r"api|regex|async|await|null|None)\b",
    _re.I,
)
_CODE_IDENT_RE = _re.compile(
    r"`[^`]+`|[A-Za-z_][A-Za-z0-9_]*\([^)]*\)|[a-z0-9]+_[a-z0-9]+|"
    r"[a-z][a-zA-Z0-9]*[A-Z][a-z]|"
    r"\.(?:py|ts|tsx|js|jsx|go|rs|java|cpp|sh|yaml|yml|json|md)\b"
)
_CONVERSATIONAL_RE = _re.compile(
    r"\b(hi|hello|hey|thanks|thank you|good (?:morning|evening|afternoon)|"
    r"how are you|who are you|what can you do|what'?s up|what time|"
    r"sup|yo|please|nevermind)\b",
    _re.I,
)


def coarse_code_intent(prompt: str) -> str:
    """Cheap keyword heuristic for intent type (fail-open fallback).

    Used to gate tool availability and context scaling when a full intent
    classifier is skipped (low-effort trivial turns) or unavailable. Returns
    'CODE' on code-like signals, 'CONVERSATION' on short conversational
    queries, else 'DEFAULT' (neutral, current behavior).

    Never raises; regex-only, instant.
    """
    p = (prompt or "").strip()
    if not p:
        return "DEFAULT"
    if _CODE_KEYWORD_RE.search(p) or _CODE_IDENT_RE.search(p):
        return "CODE"
    if len(p.split()) <= 8 and _CONVERSATIONAL_RE.search(p):
        return "CONVERSATION"
    return "DEFAULT"


__all__ = [
    "IntentDecision", "classify_intent", "keyword_intent",
    "depth_for_effort", "INTENT_VALUES", "LLMComplete",
    "coarse_code_intent",  # NEW: intent discrimination gate
]
