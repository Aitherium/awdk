"""Rank the campaign notes against the scene, without ever crossing a scope.

Presence gets the right notes ELIGIBLE; it does not decide which of them are
worth the budget. Once a campaign has a few hundred notes the brief is capped
long before the notes run out, and the cap is filled in whatever order the
store returned — so a fact about the thing the scene is actually about loses
its place to an older, duller one. Nobody notices, because a brief that is
merely mediocre still looks like a brief.

So this scores the ELIGIBLE notes against the recent turns and fills the
budget best-first.

**Retrieval never widens the boundary.** The candidate set is exactly what
`brief()` already allows — world notes plus the notes of characters present in
the scene — so a devastatingly relevant secret belonging to an absent
character is still not retrievable. Ranking decides ORDER within what the
store already permits, never membership. That is asserted directly, because a
retrieval layer bolted onto a scoped store is the obvious place for the scope
to quietly stop mattering.

The scorer is BM25-lite: term frequency saturated, rare terms weighted, length
normalised. No embeddings, no service, no network — the same constraint the
rest of this pack keeps, and enough to put the right note first, which is the
whole job. Ties break toward the MORE RECENTLY updated note, because in a
campaign the newer fact is usually the one that supersedes.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Sequence

#: Words carrying no discriminating power in prose of this kind. Deliberately
#: short: an aggressive stoplist throws away proper nouns in other languages,
#: and the IDF term already discounts anything that appears everywhere.
_STOP = frozenset("""
a an and are as at be been but by for from had has have he her his i if in is
it its me my not of on or our she that the their them they this to was we were
what when where which who will with you your
""".split())

_WORD = re.compile(r"[A-Za-z0-9']+")

#: BM25 constants. k1 saturates term frequency; b controls length
#: normalisation. The defaults are the usual ones and are not tuned here —
#: tuning them against a campaign nobody has played would be fitting noise.
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> List[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text or ""))
            if w not in _STOP and len(w) > 1]


def rank(notes: Sequence[Dict[str, Any]], scene: str,
         *, limit: int = 0) -> List[Dict[str, Any]]:
    """Order `notes` by relevance to `scene`. Membership is never changed.

    Returns the SAME dicts, ordered. An empty scene, or notes that share no
    term with it, fall back to the store's own order rather than a random one
    — an arbitrary shuffle would make the brief unstable between turns for no
    gain.
    """
    items = list(notes)
    q = tokenize(scene)
    if not q or not items:
        return items[:limit] if limit else items

    docs = [tokenize(str(n.get("value", ""))) for n in items]
    n_docs = len(docs)
    avg_len = sum(len(d) for d in docs) / n_docs if n_docs else 0.0

    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1

    scored = []
    for i, d in enumerate(docs):
        if not d:
            scored.append((0.0, 0.0, i))
            continue
        tf: Dict[str, int] = {}
        for term in d:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in q:
            f = tf.get(term)
            if not f:
                continue
            # +0.5/+0.5 smoothing keeps a term appearing in EVERY note from
            # scoring zero or negative, which plain IDF does and which would
            # silently drop the campaign's most common subject.
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            norm = f * (_K1 + 1) / (f + _K1 * (1 - _B + _B * len(d) / (avg_len or 1)))
            score += idf * norm
        scored.append((score, float(items[i].get("updated") or 0), i))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    out = [items[i] for _, _, i in scored]
    return out[:limit] if limit else out


def rank_for_scene(notes: Sequence[Dict[str, Any]],
                   messages: Sequence[Dict[str, Any]],
                   *, window: int = 12, limit: int = 0) -> List[Dict[str, Any]]:
    """`rank`, taking the scene from the recent turns the way presence does."""
    scene = " \n ".join(
        str(m.get("content") or "")
        for m in list(messages)[-window:]
        if m.get("role") in ("user", "assistant", "system"))
    return rank(notes, scene, limit=limit)
