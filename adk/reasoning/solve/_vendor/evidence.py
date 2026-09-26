# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/evidence.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""h31 evidence table: what episodic memory says each action family does,
computed by the harness BEFORE the model writes any rule.

The model guessed rules without looking at the data ("actions 1-4 always
change the board" -- 0 of 688 hypotheses verified).  This table is the data,
one row per action family, and every rule the model writes must cite a row.

Per family: how often it was tried, how often the state changed (the domain's
``significant_change``, so a HUD tick is not a change), deaths, level-ups, and
-- when the domain reports per-transition object effects -- which object
classes moved by which displacements and how often a class that moves under
this family stayed put (a BLOCKED case).

Game-agnostic: the family label and the object effects come from optional
domain hooks (``family(t)``, ``effects(t) -> [(class_label, (dy, dx))]``);
without them a family is the action id and the table has no object columns.
Stdlib + numpy.  3.10-compatible.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging  # SEAM(adk) drop
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
_LOG = logging.getLogger(__name__)  # SEAM(adk) drop


@dataclass
class EvidenceRow:
    rid: str
    family: str
    tried: int = 0
    changed: int = 0
    died: int = 0
    level_ups: int = 0
    lost_life: int = 0  # the domain's soft reset: every object back at the level start
    moved: Dict[str, Counter] = field(default_factory=dict)  # class -> Counter of (dy, dx)
    stayed: Dict[str, int] = field(default_factory=dict)      # class -> transitions it stayed
    examples: List[int] = field(default_factory=list)         # transition ids

    def blocked(self) -> Dict[str, int]:
        """Stays of a class that DOES move under this family elsewhere."""
        return {k: v for k, v in self.stayed.items() if v and self.moved.get(k)}

    def line(self, max_classes: int = 3) -> str:
        bits = ["%s %s: %d tried" % (self.rid, self.family, self.tried),
                "changed %d" % self.changed,
                "no change %d" % (self.tried - self.changed - self.died - self.level_ups - self.lost_life)]
        if self.died:
            bits.append("DIED %d" % self.died)
        if self.lost_life:
            bits.append("LOST-LIFE (reset to level start) %d" % self.lost_life)
        if self.level_ups:
            bits.append("LEVEL-UP %d" % self.level_ups)
        mv = sorted(self.moved.items(), key=lambda kv: -sum(kv[1].values()))[:max_classes]
        for cls, ds in mv:
            dist = ", ".join("(%+d,%+d)x%d" % (d[0], d[1], n) for d, n in ds.most_common(3))
            b = self.blocked().get(cls, 0)
            bits.append("%s moved %s%s" % (cls, dist, ("; stayed/blocked x%d" % b) if b else ""))
        return " | ".join(bits)


class EvidenceTable:
    """Rows keep stable ids (E1, E2, ...) in order of first appearance, so a
    citation stays valid as the game goes on."""

    def __init__(self) -> None:
        self.ids: Dict[str, str] = {}
        self.rows: Dict[str, EvidenceRow] = {}
        self.n = 0

    def rid(self, family: str) -> str:
        r = self.ids.get(family)
        if r is None:
            r = "E%d" % (len(self.ids) + 1)
            self.ids[family] = r
        return r

    def build(self, history: Any, changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]] = None,
              family_fn: Optional[Callable[[Any], str]] = None,
              effects_fn: Optional[Callable[[Any], List[Tuple[str, Tuple[int, int]]]]] = None,
              skip_fn: Optional[Callable[[Any], bool]] = None) -> "EvidenceTable":
        rows: Dict[str, EvidenceRow] = {}
        items = getattr(history, "items", history)
        for t in items:
            if int(t.action[0]) == 0:
                continue
            fam = _safe(family_fn, t) if family_fn is not None else None
            if not fam:
                fam = "A%d" % int(t.action[0])
            rid = self.rid(str(fam))
            row = rows.get(rid)
            if row is None:
                row = rows[rid] = EvidenceRow(rid, str(fam))
            row.tried += 1
            if len(row.examples) < 6:
                row.examples.append(int(t.i))
            if t.died:
                row.died += 1
                continue
            if skip_fn is not None and _safe(skip_fn, t):
                row.lost_life += 1
                continue
            if t.level_up:
                row.level_ups += 1
                continue
            if _changed(t, changed_fn):
                row.changed += 1
            for cls, d in (_safe(effects_fn, t) or []) if effects_fn is not None else []:
                d = (int(d[0]), int(d[1]))
                if d == (0, 0):
                    row.stayed[cls] = row.stayed.get(cls, 0) + 1
                else:
                    row.moved.setdefault(cls, Counter())[d] += 1
        self.rows = rows
        self.n = len(items)
        return self

    def render(self, max_rows: int = 14) -> str:
        if not self.rows:
            return "  (no transitions yet: every action family is untested)"
        rows = sorted(self.rows.values(), key=lambda r: int(r.rid[1:]))
        lines = ["  " + r.line() for r in rows[:max_rows]]
        if len(rows) > max_rows:
            lines.append("  (+%d more rows: print(evidence()))" % (len(rows) - max_rows))
        return "\n".join(lines)

    def families(self) -> Dict[str, str]:
        """family -> row id, for the rows that exist."""
        return {r.family: rid for rid, r in self.rows.items()}


def _safe(fn: Any, t: Any) -> Any:
    try:
        return fn(t)
    except Exception:  # noqa: BLE001 - perception extras never fail a turn
        return None


def _changed(t: Any, changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]]) -> bool:
    if changed_fn is not None and t.before.shape == t.after.shape:
        try:
            return bool(changed_fn(t.before, t.after))
        except Exception:  # noqa: BLE001
            _LOG.debug("changed_fn raised; falling back to t.changed", exc_info=True)  # SEAM(adk) pass
    return bool(t.changed)


__all__ = ["EvidenceTable", "EvidenceRow"]
