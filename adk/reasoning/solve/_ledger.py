"""The prediction ledger, calibrated: every scored prediction lands in an awdecide
Brier ledger next to the IDENTITY baseline on the same transition.

The core's ledger (``_vendor/learning.py``) counts hits / made -- calibration as a
hit rate, with no probability, no Brier score and no baseline. A hit rate cannot
say whether a hypothesis knows anything: on a world where most actions change
nothing, "nothing changes" is right most of the time for free (a learned world
model can score well on hit rate and still lose to exactly that baseline).

``LoopConfig(ledger="<file>.db")`` installs :func:`install_ledger`: after the core
scores a transition's predictions, each one is written to an ``awdecide.ledger.Ledger``
(SQLite) as a resolved bool decision:

* ``backend`` -- ``hyp:<name>`` for an active predict hypothesis, ``inline`` for the
  model's own ``expect=``, and ``identity`` for the baseline claim ``{"changed":
  False}`` scored with the SAME rules (domain significant-change, HUD cells ignored);
* ``probability`` -- that backend's Laplace track record in this run BEFORE the
  outcome, ``(hits + 1) / (made + 2)``: the confidence the loop could have had;
* ``correct`` -- the core's own verdict.

``loop.summary()["ledger"]`` then reports, per backend, n / hit rate / Brier of its
track-record confidence, and ``beats_identity``: on the SAME transitions (paired), did
the source hit more often than "nothing changes"? A source that does not carries no
information about the dynamics. Across runs the database is the Six Pillars track
record (``awdecide`` ``reliability()``: is the track-record confidence calibrated --
Brier vs climatology and the confidence buckets).

Requesting a ledger without ``awdecide`` installed is an error at build time, not
a silent no-op.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

__all__ = ["install_ledger", "CalibratedLedger", "IDENTITY"]

IDENTITY = "identity"


def _import_ledger() -> Any:
    try:
        from awdecide.ledger import Ledger
    except ImportError as exc:  # pragma: no cover - exercised by the install error test
        raise ImportError(
            "LoopConfig(ledger=...) needs awdecide (pip install 'awdk[decide]'): %s" % exc
        ) from exc
    return Ledger


class CalibratedLedger:
    """Mirror of one loop's scored predictions into an awdecide ledger."""

    def __init__(self, loop: Any, path: str, episode_id: str) -> None:
        from pathlib import Path

        self.loop = loop
        self.episode_id = episode_id
        self.path = str(path)
        self.db = _import_ledger()(Path(path))
        self.track: Dict[str, List[float]] = {}  # backend -> [made, hits, sum brier]
        self.paired: Dict[str, List[int]] = {}  # backend -> [n, hits, identity hits]
        self.rows = 0
        self.errors = 0

    def _backend(self, source: str) -> str:
        return source if source in ("inline", IDENTITY) else "hyp:" + source

    def book(self, backend: str, ok: bool, t_index: int, n: int, state: str) -> None:
        made, hits, sq = self.track.get(backend, [0.0, 0.0, 0.0])
        p = (hits + 1.0) / (made + 2.0)
        o = 1.0 if ok else 0.0
        self.track[backend] = [made + 1, hits + o, sq + (p - o) ** 2]
        did = "%s-%d-%d-%s" % (self.episode_id, t_index, n, hashlib.sha256(
            backend.encode("utf-8")).hexdigest()[:8])
        try:
            self.db.ingest(did, key="solve:%s:%s" % (self.episode_id, backend), kind="bool",
                           value="hit", probability=p, backend=backend, state_sha=state,
                           correct=bool(ok))
            self.rows += 1
        except Exception:  # noqa: BLE001 - a ledger write never costs the action
            self.errors += 1

    def summary(self) -> Dict[str, Any]:
        by: Dict[str, Dict[str, Any]] = {}
        for b, (made, hits, sq) in sorted(self.track.items()):
            by[b] = {"n": int(made), "hit_rate": round(hits / made, 3) if made else None,
                     "brier": round(sq / made, 4) if made else None}
        paired = {b: {"n": n, "hits": h, "identity_hits": ih}
                  for b, (n, h, ih) in sorted(self.paired.items())}
        beats = {b: v["hits"] > v["identity_hits"] for b, v in paired.items() if v["n"]}
        return {"db": self.path, "rows": self.rows, "errors": self.errors,
                "by_backend": by, "paired": paired, "beats_identity": beats}

    def pair(self, backend: str, ok: bool, identity_ok: bool) -> None:
        st = self.paired.setdefault(backend, [0, 0, 0])
        st[0] += 1
        st[1] += int(bool(ok))
        st[2] += int(bool(identity_ok))


def install_ledger(loop: Any, path: str, episode_id: str = "episode") -> CalibratedLedger:
    """Wrap ``loop.learner.after`` so every scored transition is booked; adds
    ``summary()["ledger"]``. The core's own ledger and verdicts are unchanged."""
    from ._vendor.learning import check_claim

    led = CalibratedLedger(loop, path, episode_id)
    learner = loop.learner
    orig = learner.after

    def after(preds: Any, before: Any, after_state: Any, level_up: bool, t_index: int,
              action: Any, turn: int, level: int, died: bool = False) -> Any:
        out = orig(preds, before, after_state, level_up, t_index, action, turn, level, died=died)
        state = hashlib.sha256(bytes(getattr(before, "tobytes", lambda: b"")())).hexdigest()[:16]
        for n, o in enumerate(out):
            led.book(led._backend(o.source), bool(o.ok), int(t_index), n, state)
        if out and not (level_up or died):
            ignore: Optional[Any] = None
            if getattr(before, "shape", None) == getattr(after_state, "shape", None):
                ignore = learner.hyps.ignore_mask(before, after_state)
            ok, _why = check_claim({"changed": False}, before, after_state, level_up,
                                   learner.changed_fn, ignore)
            led.book(IDENTITY, bool(ok), int(t_index), len(out), state)
            for o in out:
                led.pair(led._backend(o.source), bool(o.ok), bool(ok))
        return out

    learner.after = after
    summary = loop.summary

    def summary_with_ledger() -> Dict[str, Any]:
        s = summary()
        s["ledger"] = led.summary()
        return s

    loop.summary = summary_with_ledger
    loop.calibrated_ledger = led
    return led
