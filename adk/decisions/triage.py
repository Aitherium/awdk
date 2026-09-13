"""Classify decision cards into DECISIONS vs CONTEXT.

A DECISION is something the owner needs to actively choose or approve:
  - Has selectable options (the owner picks one)
  - Has a deadline (time-dependent)
  - Kind is 'credential' (blocked on secret input)
  - Kind is 'blocked' (unblocks the session, no options)

CONTEXT is information-only — logged so the owner can review but requires
no action and should not interrupt:
  - Kind is 'info' with no options
  - No options and no deadline
  - Status-only phrases like "background run in progress"

Context cards are auto-acknowledged after 24 hours by a daemon sweep.
DECISION cards stay open until the owner acts or they are cancelled.
"""

import re
import time
from typing import Literal


TriageResult = Literal["decision", "context"]

# Patterns that indicate a status-only card (information, not a choice).
# These match the option LABEL to detect whether it's a status update or a real choice.
# An option label that is a status phrase does NOT make the card actionable.
_STATUS_ONLY = (
    r"\b(?:is|are)\s+(?:both\s+|still\s+|currently\s+)?running",
    r"\b(?:everything|the system)\s+is\s+(?:still\s+)?(?:working|running|proceeding)",
    r"\bthe only open items?\b",
    r"\beverything else is\b",
    r"\bevery other item\b",
    r"\b(?:is|are)\s+(?:an?\s+)?in[- ]progress\b",
    r"\bwaiting (?:on|for) (?:it|them|that|the run|completion|input)\b",
    r"\b(?:watched\s+)?background run\b",
)

_ACTION_VERB = re.compile(
    r"^\s*(?:cancel|stop|kill|abort|retry|rerun|re-run|restart|merge|revert|"
    r"roll ?back|deploy|publish|tag|delete|remove|approve|reject|skip|proceed|"
    r"wait for|escalate|pause|resume|force|override|rebuild|redeploy)\b",
    re.IGNORECASE,
)
_START_WITH = re.compile(r"^\s*start with:\s*", re.IGNORECASE)


def _status_only_phrase(label: str) -> str | None:
    """Return the status phrase making `label` undecidable, or None if it is a choice."""
    text = _START_WITH.sub("", label or "")
    if not text.strip():
        return None
    if _ACTION_VERB.search(text):
        return None
    for pattern in _STATUS_ONLY:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(0)
    return None


def triage(card: dict) -> tuple[TriageResult, str]:
    """Classify a card as DECISION or CONTEXT.

    Returns (classification, reason) where reason explains the verdict.

    Args:
        card: A decision card dict (from store.py::DecisionCard.to_dict()).

    Returns:
        ("decision", reason) or ("context", reason)
    """
    kind = (card.get("kind") or "decision").strip().lower()
    status = (card.get("status") or "open").strip().lower()
    options = card.get("options") or []
    deadline = card.get("deadline")
    title = (card.get("title") or "").strip()

    # Not open? Outside scope of triage.
    if status != "open":
        return "context", f"status={status}"

    # Credentials: blocked on vault input, always a decision (no options by design).
    if kind == "credential":
        return "decision", "kind=credential"

    # Blocked: unblocks a session, even without options.
    if kind == "blocked":
        return "decision", "kind=blocked"

    # Has a deadline? Time-dependent, so it's a decision.
    if deadline is not None and deadline > time.time():
        return "decision", f"deadline in {deadline - time.time():.0f}s"

    # Has actionable options? Closed question the owner can choose.
    if options:
        # Check each option label to filter out status-only phrases.
        # An option whose label is "everything is running" is not a choice.
        actionable_options = [
            o for o in options
            if not _status_only_phrase(o.get("label", ""))
        ]
        if actionable_options:
            return "decision", f"{len(actionable_options)} option(s)"
        # All options are status-only — treat as context.
        return "context", "options are status-only phrases"

    # No options, no deadline. Information-only.
    # Hourly digests, status updates, etc. are context.
    return "context", "info-only (no options, no deadline)"


# ── triage patterns for the desk ──────────────────────────────────────────────
# These patterns are read by decision-cards.cjs and must agree with the Python triage above.

TRIAGE_PATTERNS = {
    "decision_kinds": frozenset({"credential", "blocked"}),
    "context_phrases": _STATUS_ONLY,
}


def is_decision_pattern_json() -> dict:
    """Export the triage patterns as JSON for desk-side classification.

    The desk reads this at runtime and uses it to classify cards the same way
    the daemon does, without re-running Python. This keeps the two in sync.
    """
    return {
        "decision_kinds": list(TRIAGE_PATTERNS["decision_kinds"]),
        "context_phrases": list(TRIAGE_PATTERNS["context_phrases"]),
    }
