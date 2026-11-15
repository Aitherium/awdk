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

Context cards of kind=info are auto-closed after 24 hours by the grouping
sweep (adk.decisions.grouping G3, run on a schedule).
DECISION cards stay open until the owner acts or they are cancelled.
"""

import re
import time
from typing import Any, Iterable, Literal

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

    # Not open? Outside scope of triage.
    if status != "open":
        return "context", f"status={status}"

    # Credentials: blocked on vault input, always a decision (no options by design).
    if kind == "credential":
        return "decision", "kind=credential"

    # Blocked: unblocks a session, even without options.
    if kind == "blocked":
        return "decision", "kind=blocked"

    # Has a deadline? Time-dependent, so it's a decision -- unless it is INFO.
    # An info card is a report, never a decision, deadline or not: before this
    # an info digest carrying a deadline counted toward "N decisions waiting",
    # the exact noise the badge exists to keep out (adk.decisions.grouping G3).
    if kind == "info" and not options:
        return "context", "info-only (kind=info, no options)"
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


def decisions_waiting(cards: Iterable[Any]) -> list:
    """The cards that count toward "N decisions waiting": those :func:`triage`
    calls a decision. Accepts ``DecisionCard`` objects or their dicts and
    returns them unchanged. An info digest never counts (grouping G3), so the
    toast, the CLI summary and the desk badge all read the same number."""
    out = []
    for card in cards:
        data = card if isinstance(card, dict) else card.to_dict()
        if triage(data)[0] == "decision":
            out.append(card)
    return out


def info_ttl_seconds() -> float:
    """The ONE info-card TTL -- the store's, currently 6h.

    ``grouping.INFO_TTL_SECONDS`` still says 24h; that is a BACKSTOP for a card
    the store never re-reads, not a second policy. Anything asking "how long does
    an info card live" reads this, so the two cannot drift into different
    answers on the same screen.
    """
    from adk.decisions.store import info_ttl_seconds as _store_ttl

    return _store_ttl()


# ── the list-first grouping every surface shares ──────────────────────────────
#
# The window, the terminal, awdesk, Fairy and Alt+Space all show the SAME
# partition of the queue, so "where did that card go" has one answer:
#
#   * **Needs you** -- every open card that is not stale, plus every PROTECTED
#     card (credential / blocked / blocker) however old. Capped when displayed;
#     the total and the "N more" count travel with it so the cap is visible
#     rather than a card silently going missing.
#   * **Older than the staleness line** -- open, past
#     :func:`store.stale_after_seconds` (7 days by default), and NOT protected.
#     Collapsed by default; the only group "Dismiss all stale" may touch.
#
# Ordering is :func:`store.rank_key` inside both groups, so a card cannot move
# between surfaces by being resorted differently.

#: How many rows "Needs you" shows before the real "N more" row.
NEEDS_YOU_CAP = 5

#: Kinds that stay in "Needs you" at any age and can never be bulk-dismissed.
PROTECTED_KINDS = frozenset({"credential", "blocked", "blocker"})


def _as_card(obj: Any) -> Any:
    """A ``DecisionCard`` for either a card object or its dict."""
    if isinstance(obj, dict):
        from adk.decisions.store import DecisionCard

        return DecisionCard.from_dict(obj)
    return obj


def stale_view(cards: Iterable[Any], now: float | None = None, *,
               cap: int = NEEDS_YOU_CAP,
               stale_after: float | None = None) -> dict:
    """The grouped, ranked view of a queue. Pure: reads the cards it is given.

    Returns a dict with ``needs_you`` (the capped rows), ``needs_you_total``,
    ``needs_you_more``, ``needs_you_cap``, ``older``, ``older_count``,
    ``open`` (every open card, ranked -- the cursor walks THIS) and
    ``total_open``. Counting happens over the whole queue, never over the page:
    a "5 more" computed from the visible rows is how a card looks dismissed
    when it is not.
    """
    from adk.decisions import store as _store

    t = time.time() if now is None else float(now)
    line = _store.stale_after_seconds() if stale_after is None else float(stale_after)

    ranked = []
    for obj in cards:
        card = _as_card(obj)
        if (getattr(card, "status", "") or "open") != "open":
            continue
        ranked.append(card)
    ranked.sort(key=lambda c: _store.rank_key(c, t))

    needs: list[Any] = []
    older: list[Any] = []
    for card in ranked:
        kind = (getattr(card, "kind", "") or "").strip().lower()
        if kind in PROTECTED_KINDS:
            needs.append(card)
            continue
        age = t - float(getattr(card, "created_at", 0.0) or 0.0)
        (older if age > line else needs).append(card)

    shown = needs[:max(0, int(cap))]
    return {
        "needs_you": shown,
        "needs_you_total": len(needs),
        "needs_you_more": max(0, len(needs) - len(shown)),
        "needs_you_cap": max(0, int(cap)),
        "older": older,
        "older_count": len(older),
        "open": ranked,
        "total_open": len(ranked),
    }


# ── triage patterns for the desk ──────────────────────────────────────────────
# These patterns are read by decision-cards.cjs and must agree with the Python triage above.

TRIAGE_PATTERNS = {
    "decision_kinds": frozenset({"credential", "blocked"}),
    "context_phrases": _STATUS_ONLY,
    "context_kinds": frozenset({"info"}),
}


def is_decision_pattern_json() -> dict:
    """Export the triage patterns as JSON for desk-side classification.

    The desk reads this at runtime and uses it to classify cards the same way
    the daemon does, without re-running Python. This keeps the two in sync.
    """
    # The queue order travels with the patterns, so the desk ranks cards the
    # way store.rank does (both are pinned by card_rank_cases.json).
    from adk.decisions.store import (
        AUTO_DEFAULT_RANK,
        NOOP_SUGGESTION_LABELS,
        RANK_FIRST_KINDS,
        URGENCY_RANK,
        stale_after_seconds,
    )

    return {
        "decision_kinds": sorted(TRIAGE_PATTERNS["decision_kinds"]),
        "context_phrases": list(TRIAGE_PATTERNS["context_phrases"]),
        "context_kinds": sorted(TRIAGE_PATTERNS["context_kinds"]),
        "rank_first_kinds": list(RANK_FIRST_KINDS),
        "urgency_rank": dict(URGENCY_RANK),
        "stale_after_seconds": stale_after_seconds(),
        # Cards that answer themselves (a deadline applies their default, or the
        # suggestion is one of these no-ops) sort BELOW cards needing the owner.
        "noop_labels": list(NOOP_SUGGESTION_LABELS),
        "auto_default_rank": AUTO_DEFAULT_RANK,
    }
