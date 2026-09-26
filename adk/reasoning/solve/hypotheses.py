"""Semantic memory that outlives a run: hypotheses-as-code with their evidence.

The vendored h30 ``HypothesisStore`` is in-process only. This module stores
what it learned through any ``Memory`` backend (namespace ``hypotheses``, key
``store``; ``TypedMemoryBackend`` scopes that per game) and reads it back:

* every **refuted** hypothesis -- source, name, kind, the evidence that refuted
  it (``reason``, ``refuted_at_transition``) -- keyed by the sha256 of its
  name-independent structural fingerprint (h30 ``_norm_source``). Loading them
  seeds the store's refuted fingerprints, so after a restart the same code
  under any name is refused ("not retried") instead of costing replays or,
  worse, being re-admitted on a history too short to contradict it.
* every **verified / consistent / unsupported** hypothesis with its support,
  positives and online-prediction confidence, for a later warm start (which
  must re-pass replay before trusting it; not done here).

Merge (``merge_hypothesis_maps``) is monotone: a refutation is never undone,
the first recorded evidence for it is kept, and a fingerprint refuted anywhere
drops out of ``verified``.

The vendored module imports numpy, so it is imported only inside functions.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional

__all__ = [
    "NAMESPACE",
    "KEY",
    "fingerprint",
    "hypothesis_record",
    "merge_hypothesis_maps",
    "save_hypotheses",
    "load_hypotheses",
    "load_refuted",
]

NAMESPACE = "hypotheses"
KEY = "store"


def fingerprint(source: str) -> str:
    """sha256 (24 hex) of the name-independent structural form of ``source``."""
    from ._vendor.memory import _norm_source

    return hashlib.sha256(_norm_source(source).encode("utf-8")).hexdigest()[:24]


def hypothesis_record(h: Any, episode: str = "") -> Dict[str, Any]:
    """A JSON record of one vendored ``Hypothesis`` (the function object is dropped)."""
    return {
        "name": h.name,
        "kind": h.kind,
        "source": h.source,
        "note": h.note,
        "status": h.status,
        "support": int(h.support),
        "positives": int(h.positives),
        "confidence": int(h.confidence),
        "reason": h.reason,
        "turn": int(h.turn),
        "level": int(h.level),
        "refuted_at_transition": int(h.refuted_at_transition),
        "episode": episode,
        "updated": time.time(),
    }


def merge_hypothesis_maps(disk: Optional[Dict[str, Any]], mine: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two ``{"refuted": {fp: rec}, "verified": {fp: rec}}`` maps."""
    disk = disk if isinstance(disk, dict) else {}
    refuted: Dict[str, Any] = dict(disk.get("refuted") or {})
    for fp, rec in (mine.get("refuted") or {}).items():
        refuted.setdefault(fp, rec)  # the first refutation's evidence stands
    verified: Dict[str, Any] = {}
    for src in (disk.get("verified") or {}, mine.get("verified") or {}):
        for fp, rec in src.items():
            if fp in refuted:
                continue
            old = verified.get(fp)
            if old is None or (int(rec.get("support", 0)), float(rec.get("updated", 0))) >= (
                int(old.get("support", 0)),
                float(old.get("updated", 0)),
            ):
                verified[fp] = rec
    return {"refuted": refuted, "verified": verified}


def _snapshot(store: Any, episode: str) -> Dict[str, Any]:
    refuted: Dict[str, Any] = {}
    for h in store.refuted:
        if h.source and h.kind != "claim":
            refuted.setdefault(fingerprint(h.source), hypothesis_record(h, episode))
    verified: Dict[str, Any] = {}
    for h in store.active.values():
        if h.source:
            verified[fingerprint(h.source)] = hypothesis_record(h, episode)
    return {"refuted": refuted, "verified": verified}


def save_hypotheses(store: Any, backend: Any, episode: str = "") -> Dict[str, Any]:
    """Merge ``store``'s hypotheses into ``backend``; returns the merged map."""
    mine = _snapshot(store, episode)
    return backend.update(NAMESPACE, KEY, lambda disk: merge_hypothesis_maps(disk, mine))


def load_hypotheses(backend: Any) -> Dict[str, Any]:
    got = backend.get(NAMESPACE, KEY, None)
    return merge_hypothesis_maps(got, {})


def load_refuted(store: Any, backend: Any) -> int:
    """Seed ``store`` with every persisted refutation; returns how many."""
    from ._vendor.memory import _norm_source
    from .memory import _broken

    if _broken():
        return 0
    n = 0
    for rec in load_hypotheses(backend)["refuted"].values():
        src = rec.get("source") or ""
        if not src:
            continue
        norm = _norm_source(src)  # recomputed: ast.dump differs across Pythons
        if norm not in store._refuted_fp:
            store._refuted_fp[norm] = str(rec.get("name", "?"))
            n += 1
    return n
