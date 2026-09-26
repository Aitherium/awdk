# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/learning.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""Six Pillars learning, the calibration half: predict before every action,
compare after, and write the result into semantic memory.

Ported from the LEARNING pillar of AitherOS ``lib/faculties/SixPillars.py``
("FALSIFIABILITY: how would I know if I'm wrong? ... TRACK RECORD: measure
predictions vs outcomes") without its fleet plumbing.

Before an action the ledger collects predictions from two sources:

* every active ``predict`` hypothesis (its output for this state/action);
* the model's own inline ``expect=`` -- a full next state, or a dict of
  claims ``{"changed": bool, "level_up": bool, "cells": {(r, c): v}}``.

After the action each prediction is scored.  A named hypothesis that missed
is REFUTED with the evidence; one that matched gains confidence.  An inline
claim that missed is stored as a refuted claim.  Calibration = hits / made.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .memory import ActionArg, HypothesisStore, score_prediction


@dataclass
class Prediction:
    source: str             # hypothesis name, or "inline"
    value: Any              # ndarray (full next state) or dict of claims
    text: str = ""


@dataclass
class Outcome:
    source: str
    ok: bool
    evidence: str


@dataclass
class Ledger:
    made: int = 0
    hits: int = 0
    misses: int = 0
    by_source: Dict[str, List[int]] = field(default_factory=dict)  # name -> [made, hits]
    recent_misses: List[str] = field(default_factory=list)

    def calibration(self) -> float:
        return self.hits / self.made if self.made else 0.0


def _describe_claim(value: Any) -> str:
    if isinstance(value, np.ndarray):
        return "full next state"
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (k, (v if k != "cells" else "%d cells" % len(v)))
                         for k, v in list(value.items())[:4])
    return repr(value)[:80]


def check_claim(value: Any, before: np.ndarray, after: np.ndarray, level_up: bool,
                changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]] = None,
                ignore: Optional[np.ndarray] = None) -> Tuple[bool, str]:
    """Score one prediction against the observed transition, with the SAME
    rules as ``HypothesisStore.replay_check`` (``score_prediction``): a
    ``changed`` claim uses the domain's significant change, HUD cells and -1
    cells of a predicted frame are not judged, anything claimed is judged
    strictly.  An unscorable value is ignored (it predicted nothing)."""
    verdict, why = score_prediction(value, before, after, level_up, changed_fn, ignore)
    if verdict == "wrong":
        return False, why
    if verdict == "ok":
        return True, "exact" if why.startswith("exact") else why
    return True, "unscorable prediction ignored" if verdict == "unscorable" else "no claim"


class PredictionLearner:
    def __init__(self, hyps: HypothesisStore, caller: Optional[Callable[..., Any]] = None,
                 max_auto: int = 4, changed_fn: Optional[Callable[[np.ndarray, np.ndarray], bool]] = None) -> None:
        self.hyps = hyps
        self.changed_fn = changed_fn
        self.caller = caller or (lambda f, *a: f(*a))
        self.max_auto = int(max_auto)
        self.ledger = Ledger()

    def before(self, state: np.ndarray, action: Tuple[int, int, int], expect: Any = None) -> List[Prediction]:
        preds: List[Prediction] = []
        if expect is not None:
            preds.append(Prediction("inline", expect, _describe_claim(expect)))
        n = 0
        for name, h in list(self.hyps.active.items()):
            if h.kind != "predict" or h.fn is None or n >= self.max_auto:
                continue
            try:
                v = self.caller(h.fn, state.copy(), ActionArg(action))
            except Exception:  # noqa: BLE001 - replay handles raising rules
                continue
            if v is None:
                continue
            preds.append(Prediction(name, v if isinstance(v, dict) else np.asarray(v)))
            n += 1
        return preds

    def after(self, preds: List[Prediction], before: np.ndarray, after: np.ndarray, level_up: bool,
              t_index: int, action: Tuple[int, int, int], turn: int, level: int,
              died: bool = False) -> List[Outcome]:
        out: List[Outcome] = []
        ignore = self.hyps.ignore_mask(before, after) if before.shape == after.shape else None
        for p in preds:
            if (level_up or died) and p.source != "inline":
                continue  # a new level's first frame / a game-over frame is not a dynamics outcome
            ok, why = check_claim(p.value, before, after, level_up, self.changed_fn, ignore)
            ev = "t#%d action %s: %s" % (t_index, action, why)
            self.ledger.made += 1
            st = self.ledger.by_source.setdefault(p.source, [0, 0])
            st[0] += 1
            if ok:
                self.ledger.hits += 1
                st[1] += 1
                if p.source == "inline":
                    self.hyps.record_claim(p.text, ev, True, turn, level)
                else:
                    self.hyps.confirm(p.source)
            else:
                self.ledger.misses += 1
                self.ledger.recent_misses = (self.ledger.recent_misses + ["%s: %s" % (p.source, ev)])[-6:]
                if p.source == "inline":
                    self.hyps.record_claim(p.text, ev, False, turn, level)
                else:
                    self.hyps.refute(p.source, ev)
            out.append(Outcome(p.source, ok, ev))
        return out

    def stats(self) -> Dict[str, Any]:
        lg = self.ledger
        return {"made": lg.made, "hits": lg.hits, "misses": lg.misses,
                "calibration": round(lg.calibration(), 3),
                "inline": lg.by_source.get("inline", [0, 0])}


__all__ = ["PredictionLearner", "Prediction", "Outcome", "Ledger", "check_claim"]
