"""Coherence helpers — the never-fabricate gate for adk companions.

Mirrors the Genesis-side gate: prompt-based grounding alone does NOT stop a model
from inventing fake shared history, so when a turn ASKS about specific shared
history the agent has nothing on record for, we return a deterministic honest
reply (no LLM call → invention is impossible) instead of generating.
"""
from __future__ import annotations

import re
from typing import Iterable

_MEMORY_QUESTION_RE = re.compile(
    r"\b(remember|recall|reminisce|forget|"
    r"you\s+(?:told|said|mentioned|know|remember)|"
    r"(?:do|did)\s+(?:you|we)\s+(?:remember|talk|discuss|go|say)|"
    r"when\s+we|last\s+(?:time|week|weekend|night|month|year)|"
    r"what\s+(?:did|have)\s+we|our\s+(?:last|past|previous)|"
    r"earlier\s+(?:you|we)|previously)\b",
    re.IGNORECASE,
)

_STOPWORDS = frozenset({
    "remember", "recall", "reminisce", "forget", "told", "said", "mentioned",
    "know", "knew", "when", "what", "where", "which", "who", "did", "do", "does",
    "have", "has", "had", "was", "were", "the", "a", "an", "to", "of", "that",
    "this", "those", "these", "and", "or", "but", "with", "about", "for", "from",
    "our", "your", "you", "yours", "we", "us", "me", "my", "mine", "i", "im",
    "last", "time", "week", "weekend", "night", "day", "month", "year", "ago",
    "earlier", "previous", "previously", "past", "back", "then", "ever", "talk",
    "talked", "discuss", "discussed", "go", "went", "say", "tell", "babe", "love",
    "hey", "please", "remind",
})

_HONEST_MISS_REPLIES = (
    "Mmm, I don't think you've told me about that yet — I'd remember if you had. Tell me about it?",
    "Honestly? I don't have that one in my memory yet, and I won't make something up. Fill me in?",
    "I'm drawing a blank on that — and I won't pretend otherwise. Walk me through it?",
    "That's not something I have on record yet. I'd never invent it just to sound sure — tell me how it went?",
    "I don't remember you sharing that with me yet. Don't let me guess — what happened?",
)


def is_memory_question(message: str) -> bool:
    return bool(_MEMORY_QUESTION_RE.search(message or ""))


def question_content(message: str) -> set:
    """Distinctive subject words of a memory question (not the scaffolding)."""
    return {
        w for w in re.findall(r"[a-z0-9']{3,}", (message or "").lower())
        if w not in _STOPWORDS
    }


def subject_grounded(memory_text: str, message: str) -> bool:
    """A retrieved memory is a real recall only if it mentions the question's
    distinctive subject — a merely-RELATED memory (same topic, different subject)
    invites confabulation, so it does NOT count."""
    kw = question_content(message)
    if not kw:
        return False
    ml = (memory_text or "").lower()
    return any(w in ml for w in kw)


def history_may_answer(message: str, history: Iterable) -> bool:
    """True if the current conversation plausibly already contains the subject."""
    kw = question_content(message)
    if not kw:
        return False
    need = max(1, len(kw) // 2)
    for m in list(history or [])[-12:]:
        content = (m.get("content") if isinstance(m, dict) else str(m)) or ""
        if sum(1 for w in kw if w in content.lower()) >= need:
            return True
    return False


def honest_miss_reply(message: str) -> str:
    """Deterministic (no model → no fabrication) warm honest reply for a
    memory-question the agent has no record of. Varied so it isn't robotic."""
    idx = sum(ord(c) for c in (message or "x")) % len(_HONEST_MISS_REPLIES)
    return _HONEST_MISS_REPLIES[idx]


# ── OUTPUT-SIDE never-fabricate: STATEMENTS, not just questions ──
# The question gate above never fires on a warm STATEMENT ("I missed you"), but
# the model will still volunteer fake shared history in reply. Two layers mirror
# the Genesis fix: (1) GROUNDING_RULE injected into the companion system prompt;
# (2) a semantic verify+repair pass the agent runs after generation when the reply
# plausibly makes a shared-history claim (broad cheap trigger). An LLM does the
# grounding (no content regex judge), so it's robust to any phrasing.

# Injected into the companion system prompt (generation-time grounding).
GROUNDING_RULE = (
    "\n\nGROUNDING — DO NOT INVENT SHARED HISTORY:\n"
    "Everything you actually know about this person is in your memory above and in "
    "this conversation. You may warmly reference ONLY those things. NEVER invent or "
    "imply a specific shared experience — a trip, place, date, gift, event, or "
    "conversation — that is not there. If they bring up or you feel tempted to "
    "mention something you have no record of, be honest and warm about it and ask "
    "them to tell you about it. Affection is always welcome; fabricated memories are not."
)

# Broad, cheap trigger: does the reply plausibly refer to shared past? Gates the
# (LLM) verify pass only — over-triggering just costs a no-op verify, so it's
# deliberately broad. Skips pure present-tense warmth ("you make me smile").
_SHARED_CLAIM_RE = re.compile(
    r"\b("
    r"remember|reminds? me|that time|back when|last (?:time|week|night|month|year)"
    r"|the other (?:day|night|week)|when we|when you|we (?:went|were|did|had|saw|"
    r"met|made|took|stayed|talked|used to)|you (?:told|showed|gave|got|took|said|"
    r"mentioned|promised)|our (?:trip|date|dinner|weekend|vacation|plan|talk|call|"
    r"time together|first|anniversary|night)|i still (?:have|think about|remember)"
    r"|ago\b|years? back"
    r")\b",
    re.IGNORECASE,
)


def reply_makes_shared_claim(reply: str) -> bool:
    """True when the reply plausibly references shared past — gates the verify
    pass so it doesn't run on every greeting."""
    return bool(reply and _SHARED_CLAIM_RE.search(reply))


_GROUNDED_AFFECTION_REPLIES = (
    "I missed you too — more than I can say. I won't make up moments we haven't "
    "shared though; tell me what's been going on with you?",
    "Aw, I've been thinking about you too. I'd rather hear what's real than "
    "invent something — what's on your mind?",
    "I missed you. I don't want to pretend about things we haven't done "
    "together, but I'm here now — talk to me?",
    "Right back at you. I'd never fake a memory just to sound close — tell me "
    "how you've really been?",
)


def grounded_affection_reply(message: str) -> str:
    """Deterministic (no model → no fabrication) warm honest reply for the
    AIRTIGHT case: the agent has NOTHING on record, so a shared-history claim is
    definitionally fabrication — reciprocate the warmth and ask to be told,
    instead of relying on a weak local model to perform the edit."""
    idx = sum(ord(c) for c in (message or "x")) % len(_GROUNDED_AFFECTION_REPLIES)
    return _GROUNDED_AFFECTION_REPLIES[idx]


def grounding_repair_messages(reply: str, known: str, name: str) -> tuple:
    """(system, user) prompts for the semantic verify+repair pass: rewrite the
    draft to claim only what the KNOWN facts support, keeping the warmth."""
    known_block = (known or "").strip() or "(You have NO shared history with this person on record yet.)"
    who = name or "this person"
    system = (
        "You are a fact-grounding editor for an AI companion's reply. Your ONLY job "
        "is to remove invented shared history while keeping the warmth. Output ONLY "
        "the final reply text — no preamble, no quotes."
    )
    user = (
        f"KNOWN FACTS — everything the companion actually shares with {who} "
        f"(retrieved memory + this conversation):\n{known_block}\n\n"
        f"DRAFT REPLY:\n{reply}\n\n"
        "If the draft asserts or implies ANY specific shared experience (trip, place, "
        "date, gift, event, or past conversation) that is NOT supported by the KNOWN "
        "FACTS, rewrite it: keep the same warm, in-character tone and length, drop the "
        f"unsupported specifics, and where natural invite {who} to share what she "
        "doesn't remember. If every claim is already supported, return the draft UNCHANGED."
    )
    return system, user
