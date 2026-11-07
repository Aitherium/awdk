"""The attention beam: what needs THIS person, now — at most a few items, ranked.

Owner, 2026-10-28: *"I need to have multiplexed information and also have it
compressed into a laser beam right in front of my eyes."* The failure that day:
an approval card raised that hour could not be found — #29 of 132 open cards,
under eight "Claude Code needs you" cards from sessions that had ended days
earlier (resolved by :func:`still_waiting`).

This is the ENGINE half: pure functions, stdlib only, no I/O. Every surface
renders from one :func:`build` result, so two of them can never disagree about
what needs the owner first.

Order vs :func:`adk.decisions.store.rank` — deliberately DIFFERENT, and both are
right for their question:

* ``store.rank`` answers "what has waited longest" (queue order, oldest first
  inside a tier). It is the right order for the inbox list.
* :func:`ask_rank` answers "what should I act on now": credential/blocked, then
  urgency, then NEWEST first. Oldest-first inside a tier is precisely how the
  card raised that hour sank to #29; a fresh ask has not had its turn yet, and
  an old one is surfaced by its age in the count line, not by leading.

An ask is NEW for :data:`FRESH_SECONDS`; after that it is counted, never
headlined, so a red beam means "now" again instead of being red every day
forever.

Client mirror: ``src/beam.ts`` in the awsh CLI (the top bar).
The two must change together; ``tests/fixtures/beam_cases.json`` pins the rules
both are expected to implement.

Liveness (:func:`still_waiting`): a ``cc-waiting:<session>:<stamp>`` card is
raised by the Claude Code Notification hook and withdrawn only when the owner
next types in THAT session. A session that ended while waiting never types
again, so its card stayed open forever. A card therefore counts only while its
session is alive and not busy; ``live_session_ids=None`` means "not measured"
and counts it, because an unknown must never hide an ask.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Optional, Sequence

#: An ask is NEW for a day; after that it is counted, never headlined.
FRESH_SECONDS = 24 * 3600
#: How many asks a surface should name when it has room.
BEAM_MAX = 3

URGENCY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
RANK_FIRST_KINDS = frozenset({"credential", "blocked"})
CC_WAIT_PREFIX = "cc-waiting:"


def _get(card: Any, key: str, default: Any = None) -> Any:
    """Read a field from a mapping or an object (the store hands out DecisionCard)."""
    if isinstance(card, Mapping):
        return card.get(key, default)
    return getattr(card, key, default)


def _id(card: Any) -> str:
    return str(_get(card, "id", "") or "")


def _created(card: Any) -> float:
    try:
        return float(_get(card, "created_at", 0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _kind(card: Any) -> str:
    return str(_get(card, "kind", "") or "").strip().lower()


def _urgency(card: Any) -> str:
    return str(_get(card, "urgency", "") or "").strip().lower()


def _options(card: Any) -> Sequence[Any]:
    opts = _get(card, "options", None)
    return opts if isinstance(opts, (list, tuple)) else ()


def age_seconds(card: Any, now: Optional[float] = None) -> float:
    t = time.time() if now is None else now
    created = _created(card)
    return max(0.0, t - created) if created else 0.0


def is_ask(card: Any) -> bool:
    """A card that WAITS on a person: it offers choices, or it is a secret/lock."""
    return bool(_options(card)) or _kind(card) in RANK_FIRST_KINDS


def still_waiting(card: Any, live_session_ids: Optional[Iterable[str]]) -> bool:
    """False for a "Claude Code needs you" card whose session is gone or busy.

    ``live_session_ids=None`` means liveness was not measured: count the card.
    """
    key = str(_get(card, "dedupe_key", "") or "")
    if not key.startswith(CC_WAIT_PREFIX):
        return True
    if live_session_ids is None:
        return True
    live = (live_session_ids if isinstance(live_session_ids, (set, frozenset, dict))
            else set(live_session_ids))
    source = _get(card, "source", None)
    sid = ""
    if isinstance(source, Mapping):
        sid = str(source.get("session_id") or "")
    if not sid:
        parts = key.split(":")
        sid = parts[1] if len(parts) > 1 else ""
    return sid in live


def ask_rank(card: Any, now: Optional[float] = None) -> tuple:
    """credential/blocked first, then urgency, then NEWEST first, then id."""
    return (
        0 if _kind(card) in RANK_FIRST_KINDS else 1,
        URGENCY_RANK.get(_urgency(card), 2),
        age_seconds(card, now),
        _id(card),
    )


def is_hot(card: Any) -> bool:
    """RED means "now": critical/high, a credential, or a session lock."""
    return _kind(card) in RANK_FIRST_KINDS or _urgency(card) in ("critical", "high")


def fmt_age(seconds: float) -> str:
    """Coarse age, the shapes every surface uses: 42s / 7m / 3h / 2d."""
    s = max(0.0, float(seconds or 0.0))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def build(
    cards: Iterable[Any],
    now: Optional[float] = None,
    live_session_ids: Optional[Iterable[str]] = None,
) -> dict:
    """The beam: ``fresh`` (asks raised in the last day, ranked head first),
    ``open`` (every ask still waiting) and ``oldest_age`` (seconds).

    ``fresh`` is what a surface NAMES; ``open`` is what it COUNTS. Nothing is
    dropped from the count, so a surface can never claim an empty inbox."""
    t = time.time() if now is None else now
    waiting = [c for c in cards if is_ask(c) and still_waiting(c, live_session_ids)]
    fresh = [c for c in waiting if age_seconds(c, t) < FRESH_SECONDS]
    fresh.sort(key=lambda c: ask_rank(c, t))
    oldest = max((age_seconds(c, t) for c in waiting), default=0.0)
    return {"fresh": fresh, "open": len(waiting), "oldest_age": oldest}


def compress(beam: Mapping[str, Any], now: Optional[float] = None,
             width: int = 100, limit: int = BEAM_MAX) -> list[dict]:
    """Segments for a ONE-ROW surface: one per named ask, then the count.

    Plain text only — colour is the surface's job. Each ask segment's ``key`` is
    ``decision:<id>`` so a click can open that card; the count's key is
    ``decisions``. Titles shorten until the row fits; asks that cannot fit are
    folded into the count, never silently dropped."""
    t = time.time() if now is None else now
    fresh = list(beam.get("fresh") or [])
    total = int(beam.get("open") or 0)
    if not total:
        return []
    budget = max(16, int(width) - 20)          # reserve the count line up front
    shown: list[dict] = []
    for card in fresh[:limit]:
        lead = f"{_id(card)} {fmt_age(age_seconds(card, t))} "
        title = " ".join(str(_get(card, "title", "") or "").split())
        room = budget - len(lead) - (3 if shown else 0)
        if room < 12:
            break                               # no useful title fits: the count carries it
        text = title if len(title) <= room else title[: room - 3].rstrip() + "..."
        shown.append({"key": f"decision:{_id(card)}", "text": lead + text,
                      "plain": lead + text, "hot": is_hot(card)})
        budget -= len(lead) + len(text) + 3
    if not shown:
        only = f"DECIDE {total} waiting"
        return [{"key": "decisions", "text": only, "plain": only, "hot": False}]
    more = max(0, len(fresh) - len(shown))
    suffix = f"(+{more}, {total} open)"
    shown.append({"key": "decisions", "text": suffix, "plain": suffix, "hot": False})
    return shown


def format_options(card: Any) -> str:
    """The card's answer options as one line, for a surface's detail view."""
    opts = _options(card)
    if not opts:
        return "no options — answer with a note"
    out = []
    for o in opts:
        key = str(_get(o, "key", "") or "")
        label = str(_get(o, "label", "") or "")
        out.append(f"{key} ({label})" if label else key)
    return " / ".join(out)


__all__ = [
    "BEAM_MAX", "FRESH_SECONDS", "RANK_FIRST_KINDS", "URGENCY_RANK",
    "age_seconds", "ask_rank", "build", "compress", "fmt_age", "format_options",
    "is_ask", "is_hot", "still_waiting",
]
