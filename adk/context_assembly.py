"""Budgeted context assembly: choose which context blocks reach the model this turn.

``AitherAgent.stream_react`` collects one ``Source`` per provider (constraints, crystal
facts, code graph, skills, graph memory, neurons) and calls ``assemble`` once per turn.
Before this, every provider appended its block with a fixed cap of its own, so six
blocks could together outgrow a small model's window: on an 8k-token model the old
caps summed to more than the whole window before the first tool result.

The policy, in order:

1. DEDUPE. A source whose normalized text equals one already offered (or a history
   message) is evicted. Inside a source, a content line already shown, in the history
   or in a higher-ranked source, is dropped; the history carries it already, and the
   model reads a repeated fact as a second, independent confirmation. A source left
   with nothing but its header line is evicted.
2. SCORE. ``priority * (1 + overlap)``, where ``overlap`` is the share of the query's
   words the block contains (0..1). Priority sets the order between providers; overlap
   lets a block that is actually about this request outrank a generic one.
3. FIT. Pinned sources first, then by score; each kept WHOLE while it fits
   ``budget_chars``, otherwise evicted WHOLE.

Rules every implementation keeps:

- A source is kept WHOLE or evicted WHOLE; a half block reads as a complete one.
  (Dropping a line the model can already see elsewhere is not a half block.)
- ``pinned`` sources are never evicted and never edited, even over budget (safety
  constraints).
- FAIL OPEN: on any internal fault, return every source as kept in input order, so a
  bug in assembly degrades to the old behaviour rather than an empty context.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

logger = logging.getLogger("adk.context_assembly")

# A line shorter than this is structure (a header fragment, a one-word bullet), not a
# fact; deduping it would strip the block's shape rather than a repeat.
_MIN_DEDUPE_LINE = 12
_WORD = re.compile(r"[a-z0-9_]{3,}")
_STOP = frozenset({
    "the", "and", "for", "you", "your", "what", "with", "this", "that", "are", "was",
    "can", "how", "why", "who", "from", "have", "has", "not", "but", "about", "into",
    "please", "tell", "show", "give", "does", "did", "will", "would", "could", "should",
})


@dataclass
class Source:
    """One context block offered to the turn.

    Attributes:
        name: provider name for the trace ("crystal", "skills", "neurons", ...).
        text: the block exactly as it would be shown; "" = nothing to offer.
        priority: higher is kept first.
        pinned: never evicted (constraints, owner rules).
    """

    name: str
    text: str
    priority: int
    pinned: bool = False


def _norm_line(line: str) -> str:
    s = line.strip().lstrip("-*• \t").strip().lower()
    return " ".join(s.split())


def _norm_text(text: str) -> str:
    return "\n".join(n for n in (_norm_line(ln) for ln in text.splitlines()) if n)


def _sha1(text: str) -> str:
    return hashlib.sha1(_norm_text(text).encode("utf-8", "replace")).hexdigest()


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _overlap(query_words: set[str], text: str) -> float:
    if not query_words:
        return 0.0
    return len(query_words & _words(text)) / len(query_words)


def _fact_lines(text: str) -> set[str]:
    return {n for n in map(_norm_line, text.splitlines()) if len(n) >= _MIN_DEDUPE_LINE}


def _drop_seen_lines(text: str, seen: set[str]) -> tuple[str, int]:
    """Remove content lines whose normalized form is in ``seen``.

    The first line of a multi-line block is its header and always stays. Returns
    ``(text, content_lines_left)``; 0 left means the block only repeats what is shown.
    """
    lines = text.splitlines()
    if len(lines) <= 1:
        n = _norm_line(text)
        return text, 0 if (len(n) >= _MIN_DEDUPE_LINE and n in seen) else 1
    out = [lines[0]]
    left = 0
    for ln in lines[1:]:
        n = _norm_line(ln)
        if len(n) >= _MIN_DEDUPE_LINE and n in seen:
            continue
        out.append(ln)
        if n:
            left += 1
    return "\n".join(out), left


def assemble(
    sources: Iterable[Source],
    budget_chars: int,
    query: str,
    history_texts: Sequence[str] = (),
) -> tuple[list[Source], list[str]]:
    """Pick the sources that fit ``budget_chars``.

    Args:
        sources: candidate blocks; empty-text sources are dropped silently.
        budget_chars: the char budget for all context blocks together (at most 20% of
            the compaction window, in chars).
        query: the user's message, for relevance scoring.
        history_texts: texts already in the conversation, for dedupe.

    Returns ``(kept, evicted)``: ``kept`` in the order they should be shown (pinned
    first, then by descending score, stable), ``evicted`` the NAMES of sources that
    did not fit or only repeated what the model already sees, for the trace.
    """
    items = [s for s in sources if s is not None and (s.text or "")]
    try:
        budget = max(0, int(budget_chars))
        qwords = _words(query)
        seen_lines: set[str] = set()
        seen_hashes: set[str] = set()
        for h in history_texts or ():
            if h:
                seen_hashes.add(_sha1(str(h)))
                seen_lines.update(_fact_lines(str(h)))

        scored = [(s, int(s.priority) * (1.0 + _overlap(qwords, s.text)), i)
                  for i, s in enumerate(items)]
        ordered = sorted(scored, key=lambda t: (not t[0].pinned, -t[1], t[2]))

        kept: list[Source] = []
        evicted: list[str] = []
        used = 0
        for s, _score, _i in ordered:
            if s.pinned:
                kept.append(s)
                used += len(s.text)
                seen_hashes.add(_sha1(s.text))
                seen_lines.update(_fact_lines(s.text))
                continue
            digest = _sha1(s.text)
            if digest in seen_hashes:
                evicted.append(s.name)
                continue
            text, left = _drop_seen_lines(s.text, seen_lines)
            if left == 0 or used + len(text) > budget:
                evicted.append(s.name)
                continue
            kept.append(s if text == s.text else replace(s, text=text))
            used += len(text)
            seen_hashes.add(digest)
            seen_lines.update(_fact_lines(text))
        return kept, evicted
    except Exception:  # noqa: BLE001 -- fail open: old behaviour, every block kept
        logger.warning("context assembly failed; keeping every source", exc_info=True)
        return items, []
