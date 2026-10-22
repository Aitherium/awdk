"""Rank discovery cards for a task: local BM25, merged with the gateway's live search.

* BM25 (k1=1.2, b=0.75) over each card's name (weighted x2), one-line text and tags,
  pure Python, so a query costs no model and no network.
* The gateway's ToolGraph search (``/v1/tools/search``) is asked live within a short
  budget; its order becomes a score that is merged into the ``tools`` domain. When the
  gateway is slow or down, the cached tool cards still answer (fail-open to cache,
  never fail-open on scope: live hits are re-filtered and must exist in the index).
* Scores are normalised PER DOMAIN to [0, 1] and blended 50/50 with query coverage
  (the idf-weighted share of the task's terms a card contains), so the best tool, skill
  and pack compete on one scale, a domain's weak best match does not read as 1.0, and
  every domain with a hit is still represented.
* The query is judged as the QUERYING caller: the stored caller is trusted only when
  its bearer fingerprint equals this process's; otherwise the strictest caller (unknown,
  no fingerprint) is used. Every card is re-filtered with that caller before ranking.
"""

from __future__ import annotations

import math
import re
from typing import Callable, Optional

from adk.discovery import index, scope, sources

DOMAINS = ("tools", "skills", "packs", "agents")
K1, B = 1.2, 0.75
LIVE_WEIGHT = 0.9
_STOP = frozenset("a an and the of in on for to with by from into at or is are be this that "
                  "my our your it its as use using how what do does".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def tokens(text: str) -> list:
    return [_stem(t) for t in _TOKEN.findall((text or "").lower()) if t not in _STOP]


def _doc_tokens(card: dict) -> list:
    name = str(card.get("name") or "").replace("_", " ").replace("-", " ").replace(":", " ")
    toks = tokens(name) * 2 + tokens(str(card.get("one_line") or ""))
    for tag in card.get("tags") or []:
        toks += tokens(str(tag))
    return toks


def bm25(cards: list, task: str) -> list:
    """Raw BM25 score per card (same order as ``cards``)."""
    return [s for s, _cov in bm25_cov(cards, task)]


def bm25_cov(cards: list, task: str) -> list:
    """(BM25 score, idf-weighted share of the query's terms the card contains) per card.

    Coverage is what keeps a per-domain normalisation honest: the best skill for a
    task no skill fits still normalises to 1.0, but it covers one term of five.
    """
    q = list(dict.fromkeys(tokens(task)))
    if not q or not cards:
        return [(0.0, 0.0)] * len(cards)
    docs = [_doc_tokens(c) for c in cards]
    n = len(docs)
    avg = (sum(len(d) for d in docs) / n) or 1.0
    df = {t: sum(1 for d in docs if t in d) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    total_idf = sum(idf.values()) or 1.0
    out = []
    for d in docs:
        dl = len(d) or 1
        s = 0.0
        covered = 0.0
        for t in q:
            f = d.count(t)
            if not f:
                continue
            covered += idf[t]
            s += idf[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avg))
        out.append((s, covered / total_idf))
    return out


def query(task: str, k: int = 8, cwd: Optional[str] = None, live: bool = True,
          live_timeout: float = 4.0, merged: Optional[dict] = None,
          search: Optional[Callable] = None, current_fp: Optional[str] = None) -> dict:
    """``{"task", "cards": [...], "live": {...}, "parts": {...}}``, best first.

    ``cards`` carries ``score`` (normalised within its domain), ``domain``,
    ``how_to_invoke``, ``hosting`` and ``license_flag``.
    """
    merged = merged if merged is not None else index.load_merged(cwd)
    caller, caller_note = resolve_caller(merged, current_fp)
    cards, dropped = scope.filter_cards(list(merged.get("cards") or []), caller)
    raw = bm25_cov(cards, task)
    scored: dict = {}
    coverage: dict = {}
    for card, (s, cov) in zip(cards, raw, strict=True):
        if s > 0:
            key = (card.get("domain"), card.get("name"))
            scored[key] = [card, s]
            coverage[key] = cov

    live_info = {"asked": False, "hits": 0, "error": ""}
    if live:
        live_info["asked"] = True
        names, err = (search or sources.gateway_search)(task, k=max(k, 8),
                                                         timeout=live_timeout)
        live_info["error"] = err
        by_name = {c.get("name"): c for c in cards if c.get("domain") == "tools"}
        best_tool = max((s for (d, _n), (_c, s) in scored.items() if d == "tools"),
                        default=1.0) or 1.0
        for i, name in enumerate(names):
            card = by_name.get(name)
            if card is None:
                continue  # not in this caller's filtered index -> not shown
            ok, _why = scope.admit(card, caller)
            if not ok:
                continue
            live_info["hits"] += 1
            s_live = LIVE_WEIGHT * best_tool * (1 - i / (len(names) + 1))
            key = ("tools", name)
            if key in scored:
                scored[key][1] = max(scored[key][1], s_live) + 0.1 * best_tool
            else:
                scored[key] = [card, s_live]
            # ToolGraph matched it semantically: count it as covering most of the task.
            coverage[key] = max(coverage.get(key, 0.0), 0.8 - 0.3 * i / (len(names) + 1))

    best = {d: max((s for (dd, _n), (_c, s) in scored.items() if dd == d), default=0.0)
            for d in DOMAINS}
    ranked = []
    for key, (card, s) in scored.items():
        d = key[0]
        if best.get(d):
            out = {k2: v for k2, v in card.items() if not k2.startswith("_")}
            out["score"] = round(0.5 * s / best[d] + 0.5 * coverage.get(key, 0.0), 4)
            out["coverage"] = round(coverage.get(key, 0.0), 4)
            ranked.append(out)
    ranked.sort(key=lambda c: (-c["score"], DOMAINS.index(c["domain"])
                               if c["domain"] in DOMAINS else 9, str(c["name"])))
    return {"task": task, "cards": _diverse(ranked, k), "live": live_info,
            "caller": {"note": caller_note, "platform": caller.platform,
                       "dropped": dropped},
            "parts": {p: (m or {}).get("built_at") for p, m in
                      (merged.get("parts") or {}).items()}}


def resolve_caller(merged: dict, current_fp: Optional[str] = None) -> "tuple[scope.Caller, str]":
    """(caller to judge this query as, note). The stored caller only when its bearer
    fingerprint is this process's; otherwise the strictest caller."""
    fp = sources.bearer_fingerprint() if current_fp is None else current_fp
    for part in ("user", "cwd"):
        meta = (merged.get("parts") or {}).get(part)
        stored = (meta or {}).get("caller")
        if not isinstance(stored, dict) or not stored.get("known"):
            continue
        if fp and stored.get("fingerprint") == fp:
            return scope.caller_from_me(stored), ""
        return scope.Caller(), "index built for another caller: judged as unknown"
    return scope.Caller(), "no known caller in the index: judged as unknown"


def _diverse(ranked: list, k: int) -> list:
    """Top ``k`` by score, but the best card of every domain that had a hit is kept."""
    if len(ranked) <= k:
        return ranked
    heads = []
    seen: set = set()
    for c in ranked:
        if c["domain"] not in seen:
            seen.add(c["domain"])
            heads.append(c)
    rest = [c for c in ranked if c not in heads]
    picked = heads + rest[: max(0, k - len(heads))]
    picked.sort(key=lambda c: -c["score"])
    return picked[:max(k, len(heads))]


__all__ = ["bm25", "query", "tokens"]
