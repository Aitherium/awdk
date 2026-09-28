"""The learned tail of the world model: what answers when the table has never seen a state.

The awm table (``adk/world.py``) answers an exact (state, action) it has seen --
RECALLED -- and nothing else precisely. A learner is consulted only on that miss, and
its answer is labelled PREDICTED with the engine's name, never folded into a recall.

Three pieces:

``AwmStateAdapter``
    ``WorldState`` <-> a fixed-width float vector so vector engines (awpredict) can
    encode states. Width ``STATE_WIDTH = 256``: each (slot, value) pair sets ONE
    feature at ``sha256("slot\\x00value") mod 256`` (a collision adds, never
    overwrites), then the vector is L2-normalised. ``action_vector`` does the same for
    the action text at ``ACTION_WIDTH = 32``. Hashing is not invertible, so
    ``decode`` maps a predicted vector back to the NEAREST of the candidate states it
    is given -- a vector engine's output is only ever read against states the caller
    can name.

``FactoredLearner`` (pure stdlib, always available)
    A factored model: for each (action, slot) it tallies the slot's next OUTCOME
    (unchanged / removed / set to v) conditioned on every set of up to three other
    slots, and picks as that slot's parents the set with the best leave-one-out
    accuracy on its own training data (a slot whose every value is unique scores 0 --
    an id is never mistaken for a cause; ties go to the smaller set). A conjunctive
    precondition ("adjacent AND open") needs a set, not one parent. Prediction reads
    each changing slot from its parents' current values, so a FULL state never seen
    before is predicted from the slots that were. When no parent set saw the values it
    abstains (None); an answer the data never TESTED (a coarser fallback set, or a
    non-parent slot at a value never seen and never varied) is capped at confidence
    0.5 -- a guess must not read as a recall.

``AwpredictLearner``
    Wraps an awpredict engine (``awpredict.core.mlp.MLPWorldModel`` shape: hashed
    state ids + descriptions). Its answers are decoded only when the predicted state
    id is one the wrapper has observed; anything else is counted as ``undecodable``
    and returned as a miss. ``make_learner()`` chains it in front of the factored
    learner when awpredict imports, and reports in ``TELEMETRY`` when it does not.

``train_from_transitions`` rebuilds a learner from the transitions already in awm.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from adk.world import TELEMETRY, _awm, _count, _degrade

#: Width of the hashed slot-feature vector. Documented contract; engines size to it.
STATE_WIDTH = 256
#: Width of the hashed action vector.
ACTION_WIDTH = 32

_ABSENT = "\x00absent"
_SAME = "="
_GONE = "-"


def _bucket(text: str, width: int) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % width


def _l2(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _slots_of(state: Any) -> Dict[str, str]:
    if isinstance(state, dict):
        return dict(state)
    slots = getattr(state, "slots", None)
    if not isinstance(slots, dict):
        raise TypeError(f"expected a WorldState or a slot dict, got {type(state).__name__}")
    return dict(slots)


class AwmStateAdapter:
    """WorldState <-> fixed-width hashed feature vectors (see the module docstring)."""

    domain = "awm"

    def __init__(self, width: int = STATE_WIDTH, action_width: int = ACTION_WIDTH) -> None:
        if width < 8 or action_width < 4:
            raise ValueError("width >= 8 and action_width >= 4")
        self.width = int(width)
        self.action_width = int(action_width)

    def to_vector(self, state: Any) -> List[float]:
        vec = [0.0] * self.width
        for k, v in sorted(_slots_of(state).items()):
            vec[_bucket(f"{k}\x00{v}", self.width)] += 1.0
        return _l2(vec)

    def action_vector(self, action: Any) -> List[float]:
        vec = [0.0] * self.action_width
        text = action if isinstance(action, str) else json.dumps(action, sort_keys=True)
        vec[_bucket(text, self.action_width)] = 1.0
        return vec

    def describe(self, state: Any) -> str:
        """Canonical text of a state (an engine's ``state_desc``)."""
        return json.dumps(sorted(_slots_of(state).items()), ensure_ascii=False,
                          separators=(",", ":"))

    @staticmethod
    def state_id(state: Any) -> int:
        """A stable 60-bit integer id for a state (an engine's ``state_hash``)."""
        text = json.dumps(sorted(_slots_of(state).items()), ensure_ascii=False,
                          separators=(",", ":"))
        return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:15], 16)

    def decode(self, vector: Sequence[float],
               candidates: Sequence[Dict[str, str]]) -> Optional[Tuple[Dict[str, str], float]]:
        """The candidate state nearest ``vector`` (cosine) and its similarity; None if none."""
        if len(vector) != self.width:
            raise ValueError(f"vector width {len(vector)} != {self.width}")
        best: Optional[Tuple[Dict[str, str], float]] = None
        for c in candidates:
            sim = _cos(vector, self.to_vector(c))
            if best is None or sim > best[1]:
                best = (dict(c), sim)
        return best


@dataclass(frozen=True)
class LearnedDelta:
    """A learner's answer: the delta, its confidence, and which engine said it."""

    delta: Dict[str, Optional[str]]
    confidence: float
    engine: str


def _outcome(before: Dict[str, str], after: Dict[str, str], key: str) -> str:
    if before.get(key) == after.get(key):
        return _SAME
    if key not in after:
        return _GONE
    return "+" + after[key]


def _majority(c: Counter) -> Tuple[str, int]:
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0]


class FactoredLearner:
    """Per-slot conditional frequencies given the action and a small PARENT SET. Stdlib only.

    For each (action, slot) every conditioning set of up to ``max_parents`` slots is
    scored by leave-one-out accuracy on its own training data; the best set is the
    slot's parents. One parent cannot express a conjunctive precondition ("close"
    closes the door only NEXT to it AND while it is open); a set can.

    Confidence is honest about what the data tested:

    * ``LOO(set) * purity(group)`` for the group the current parent values select;
    * when the best set never saw these parent values and a coarser set answers
      instead, the answer is capped at ``EXTRAPOLATED_CAP``;
    * when a slot OUTSIDE the parent set holds a value the group never saw, and that
      slot never varied inside the group, independence from it was never tested --
      capped at ``EXTRAPOLATED_CAP`` too. (A slot that varied inside the group without
      moving the outcome -- a counter next to a toggle -- was tested; no cap.) A slot
      the action was never observed with at all is untested in the same way: capped.
    """

    name = "factored-stdlib"
    ok = True
    #: Largest parent set searched. Sets grow as C(slots, k): 3 is enough for an
    #: "adjacent AND open" precondition over (x, y, door) and stays cheap.
    max_parents = 3
    #: Confidence ceiling on an answer the training data never tested.
    EXTRAPOLATED_CAP = 0.5
    #: Parent sets whose leave-one-out accuracy is within this of the best explain the
    #: data equally well: a larger set loses a little LOO to singleton groups alone.
    TIE_TOL = 0.02

    def __init__(self) -> None:
        # action -> [(before slots, after slots)]
        self._obs: Dict[str, List[Tuple[Dict[str, str], Dict[str, str]]]] = {}
        # action -> target slot -> Counter(outcome) over every observation
        self._marg: Dict[str, Dict[str, Counter]] = {}
        # action -> every slot name seen with it
        self._keys: Dict[str, set] = {}
        # action -> parent set -> parent values -> ({slot: Counter(outcome)},
        #                                          {non-parent slot: values seen})
        self._grp: Dict[str, Dict[Tuple[str, ...], Dict[Tuple[str, ...],
                                                        Tuple[Dict[str, Counter],
                                                              Dict[str, set]]]]] = {}
        # (action, slot) -> [(parent name | parent tuple, loo)], best first
        self._parents: Dict[Tuple[str, str], List[Tuple[Any, float]]] = {}
        self._stale: set = set()
        self.n = 0

    @property
    def _dirty(self) -> bool:
        return bool(self._stale)

    def _add(self, act: str, before: Dict[str, str], after: Dict[str, str]) -> None:
        keys = self._keys[act]
        outs = {k: _outcome(before, after, k) for k in keys}
        marg = self._marg.setdefault(act, {})
        for k, o in outs.items():
            marg.setdefault(k, Counter())[o] += 1
        for ps, groups in self._grp[act].items():
            vals = tuple(before.get(j, _ABSENT) for j in ps)
            counts, seen = groups.setdefault(vals, ({}, {}))
            for k, o in outs.items():
                counts.setdefault(k, Counter())[o] += 1
                if k not in ps:
                    seen.setdefault(k, set()).add(before.get(k, _ABSENT))

    def _rebuild(self, act: str) -> None:
        keys = sorted(self._keys[act])
        self._marg[act] = {}
        self._grp[act] = {ps: {} for ps in self._candidate_sets(keys)}
        for before, after in self._obs[act]:
            self._add(act, before, after)

    def observe(self, slots: Any, action: Any, next_slots: Any, *_: Any, **__: Any) -> None:
        before, after = _slots_of(slots), _slots_of(next_slots)
        act = action if isinstance(action, str) else json.dumps(action, sort_keys=True)
        known = self._keys.setdefault(act, set())
        grew = not (set(before) <= known and set(after) <= known)
        known.update(before)
        known.update(after)
        self._obs.setdefault(act, []).append((dict(before), dict(after)))
        if grew or act not in self._grp:
            self._rebuild(act)          # new slots: new parent sets, recount this action
        else:
            self._add(act, before, after)
        self.n += 1
        self._stale.add(act)

    @staticmethod
    def _loo(groups: Dict[Any, Counter]) -> float:
        """Leave-one-out accuracy of predicting the outcome from these parent values."""
        total = right = 0
        for c in groups.values():
            for o, n in c.items():
                total += n
                rival = max((m for oo, m in c.items() if oo != o), default=0)
                if n - 1 > rival:
                    right += n
        return right / total if total else 0.0

    def _candidate_sets(self, keys: List[str]) -> List[Tuple[str, ...]]:
        size = self.max_parents if len(keys) <= 10 else (2 if len(keys) <= 30 else 1)
        out: List[Tuple[str, ...]] = []
        for r in range(1, min(size, len(keys)) + 1):
            out.extend(itertools.combinations(keys, r))
        return out

    def train_step(self, *_: Any, **__: Any) -> Dict[str, Any]:
        """Re-rank the parent sets of every slot of every action observed since last time."""
        for act in sorted(self._stale):
            for k in sorted(self._keys[act]):
                if set(self._marg[act].get(k, {})) <= {_SAME}:
                    self._parents.pop((act, k), None)
                    continue
                ranked = []
                for ps, groups in self._grp[act].items():
                    score = self._loo({v: g[0].get(k, Counter()) for v, g in groups.items()})
                    ranked.append((ps, score))
                # best LOO; among equals the SMALLEST set (Occam), then by name
                ranked.sort(key=lambda r: (-r[1], len(r[0]), r[0]))
                self._parents[(act, k)] = [(ps[0] if len(ps) == 1 else ps, sc)
                                           for ps, sc in ranked]
        self._stale.clear()
        return {"trained": True, "engine": self.name, "n": self.n,
                "actions": len(self._obs), "parents": len(self._parents)}

    def _rivals_agree(self, act: str, k: str, ranked: List[Tuple[Any, float]], score: float,
                      before: Dict[str, str], outcome: str) -> bool:
        """Every parent set that explains the data AS WELL (LOO within TIE_TOL) has seen these
        values and predicts the same outcome. Otherwise the data does not decide
        between them here -- e.g. "down" never tried at (2, 2): (pos.y) and
        (pos.x, pos.y) fit history equally, only the second has an opinion to lose."""
        for pk, sc in ranked:
            if sc < score - self.TIE_TOL:
                break
            ps = (pk,) if isinstance(pk, str) else tuple(pk)
            grp = self._grp[act][ps].get(tuple(before.get(j, _ABSENT) for j in ps))
            if not grp or not grp[0].get(k) or _majority(grp[0][k])[0] != outcome:
                return False
        return True

    def predict(self, slots: Any, action: Any) -> Optional[LearnedDelta]:
        """The factored delta, or None when any changing slot cannot be read."""
        before = _slots_of(slots)
        act = action if isinstance(action, str) else json.dumps(action, sort_keys=True)
        if act not in self._obs:
            return None
        if act in self._stale:
            self.train_step()
        delta: Dict[str, Optional[str]] = {}
        confs: List[float] = []
        changing = [k for k in sorted(self._keys[act])
                    if not set(self._marg[act].get(k, {})) <= {_SAME}]
        if not changing:
            # The action never changed anything it was seen doing. "No effect here too"
            # is an extrapolation to an unseen state (the learner is consulted only on
            # a table miss), not a tested fact: capped, never 1.0.
            return LearnedDelta(delta={}, confidence=self.EXTRAPOLATED_CAP,
                                engine=self.name)
        for k in changing:
            ranked = self._parents.get((act, k), [])
            best = ranked[0][1] if ranked else 0.0
            answer = None
            for rank, (pk, score) in enumerate(ranked):
                if score <= 0.0:
                    break
                ps = (pk,) if isinstance(pk, str) else tuple(pk)
                grp = self._grp[act][ps].get(tuple(before.get(j, _ABSENT) for j in ps))
                if not grp or not grp[0].get(k):
                    continue
                counts, seen = grp[0][k], grp[1]
                o, n = _majority(counts)
                c = score * n / sum(counts.values())
                untested = [j for j, vals in seen.items()
                            if len(vals) < 2 and before.get(j, _ABSENT) not in vals]
                # A slot the action was NEVER observed with is the extreme case: its
                # independence was never tested at all (a "lock" that appears and
                # blocks the toggle). `seen` only lists slots already in `_keys`.
                untested += [j for j in before if j not in self._keys[act]]
                rivals_agree = self._rivals_agree(act, k, ranked, score, before, o)
                if (rank > 0 and score < best) or untested or not rivals_agree:
                    c = min(c, self.EXTRAPOLATED_CAP)
                answer = (o, c)
                break
            if answer is None:
                return None
            o, c = answer
            confs.append(c)
            if o == _GONE:
                if k in before:
                    delta[k] = None
            elif o != _SAME and before.get(k) != o[1:]:
                delta[k] = o[1:]
        return LearnedDelta(delta=delta, confidence=min(confs) if confs else 1.0,
                            engine=self.name)


class AwpredictLearner:
    """An awpredict engine as a learner. Answers only in states it can decode."""

    def __init__(self, engine: Any, adapter: Optional[AwmStateAdapter] = None) -> None:
        self.engine = engine
        self.adapter = adapter or AwmStateAdapter()
        self.name = "awpredict:" + type(engine).__name__
        self._states: Dict[int, Dict[str, str]] = {}
        self.undecodable = 0

    @property
    def ok(self) -> bool:
        return bool(getattr(self.engine, "ok", True))

    def observe(self, slots: Any, action: Any, next_slots: Any, *_: Any, **__: Any) -> None:
        a, b = _slots_of(slots), _slots_of(next_slots)
        ha, hb = self.adapter.state_id(a), self.adapter.state_id(b)
        self._states[ha], self._states[hb] = a, b
        self.engine.observe(ha, action, hb, 0.0, False, self.adapter.describe(a),
                            self.adapter.describe(b))

    def train_step(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.engine.train_step(*args, **kwargs)

    def predict(self, slots: Any, action: Any) -> Optional[LearnedDelta]:
        before = _slots_of(slots)
        out = self.engine.predict(self.adapter.state_id(before), action)
        if out is None:
            return None
        nxt = self._states.get(out[0] if isinstance(out, tuple) else out)
        if nxt is None:
            self.undecodable += 1
            _count("awpredict.undecodable")
            return None
        delta = {k: nxt.get(k) for k in sorted(set(before) | set(nxt))
                 if before.get(k) != nxt.get(k)}
        return LearnedDelta(delta=delta, confidence=0.5, engine=self.name)


class ChainLearner:
    """Ask each learner in order; the first answer wins and carries its engine's name."""

    def __init__(self, learners: Sequence[Any]) -> None:
        self.learners = list(learners)
        self.name = " > ".join(getattr(learner, "name", type(learner).__name__)
                               for learner in self.learners)

    @property
    def ok(self) -> bool:
        return any(getattr(learner, "ok", True) for learner in self.learners)

    def observe(self, *args: Any, **kwargs: Any) -> None:
        for learner in self.learners:
            learner.observe(*args, **kwargs)

    def train_step(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {getattr(learner, "name", str(i)): learner.train_step(*args, **kwargs)
                for i, learner in enumerate(self.learners)}

    def predict(self, slots: Any, action: Any) -> Optional[LearnedDelta]:
        for learner in self.learners:
            out = learner.predict(slots, action)
            if out is not None:
                return out
        return None


def load_awpredict_engine() -> Any:
    """``awpredict.core.mlp.MLPWorldModel()`` or None, with the reason in TELEMETRY."""
    try:
        from awpredict.core.mlp import MLPWorldModel  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 -- optional plane
        _degrade(f"awpredict:{type(exc).__name__}: {exc}")
        return None
    try:
        return MLPWorldModel(state_dim=STATE_WIDTH, action_dim=ACTION_WIDTH)
    except Exception as exc:  # noqa: BLE001
        _degrade(f"awpredict:engine:{type(exc).__name__}: {exc}")
        return None


def make_learner(prefer: str = "auto") -> Any:
    """The learner the agent should use.

    ``"factored"``: the stdlib learner alone. ``"auto"``: an awpredict engine chained in
    front of it when awpredict imports, the stdlib learner alone (degradation recorded)
    when it does not.
    """
    factored = FactoredLearner()
    if prefer == "factored":
        return factored
    engine = load_awpredict_engine()
    if engine is None:
        return factored
    return ChainLearner([AwpredictLearner(engine), factored])


def train_from_transitions(source: Any, engine: Any, epochs: int = 1, *,
                           scope: Any = None, prefix: Optional[str] = None) -> Dict[str, Any]:
    """Feed every visible awm transition to ``engine`` ONCE, then ``train_step`` x epochs.

    ``source`` is a ``WorldModelAgent``, an ``AwmWorldModelBackend`` or an
    ``awm.MemoryStore`` (then ``scope`` is required). A transition's before-state is
    rebuilt from ``slots_for_transition`` when the source has it (the backend), else
    by ``encode_state(as_of=before_ts)`` under ``prefix``; a rebuilt state whose digest
    differs from the recorded one is SKIPPED and counted, never trained on.
    """
    got = _awm()
    if got is None:
        return {"engine": getattr(engine, "name", None), "fed": 0, "error": "awm unavailable",
                "degraded": list(TELEMETRY["degraded"])}
    _, aw = got
    resolver = getattr(source, "slots_for_transition", None)
    if hasattr(source, "store") and hasattr(source, "scope"):
        store, sc = source.store, source.scope
        prefix = prefix or getattr(source, "prefix", None)
    elif hasattr(source, "_ready") and hasattr(source, "_store"):
        source._ready()
        store, sc = source._store, source._scope
        prefix = prefix or "wm"
    else:
        store, sc = source, scope
    if store is None or sc is None:
        return {"engine": getattr(engine, "name", None), "fed": 0,
                "error": "no store/scope (a degraded backend, or scope= missing)"}
    if not isinstance(sc, aw.Scope):
        import awm  # type: ignore[import-not-found]
        sc = awm.Scope.parse(str(sc))
    rows = store.transitions(sc)
    pairs = []
    mismatch = unresolved = 0
    for t in rows:
        before = resolver(t) if callable(resolver) else None
        if before is None and not callable(resolver):
            before = dict(store.encode_state(sc, prefix=prefix, as_of=t.before_ts).slots)
        if before is None:
            unresolved += 1
            continue
        if aw.state_digest(before) != t.state_digest:
            mismatch += 1
            continue
        after = aw.apply_delta(before, t.delta)
        if aw.state_digest(after) != t.next_digest:
            mismatch += 1
            continue
        pairs.append((before, t.action, after))
    # Each transition is OBSERVED exactly once: an observation is evidence, and a
    # counting learner (FactoredLearner) would read a repeat as a second, independent
    # sighting -- one transition fed twice passed its leave-one-out gate at 1.0.
    # `epochs` repeats only the training pass over what was observed.
    for before, action, after in pairs:
        engine.observe(before, action, after)
    steps = [engine.train_step() for _ in range(max(1, int(epochs)))]
    return {"engine": getattr(engine, "name", type(engine).__name__), "transitions": len(rows),
            "fed": len(pairs), "epochs": max(1, int(epochs)),
            "skipped": {"digest_mismatch": mismatch, "unresolved": unresolved},
            "train_steps": len(steps), "last": steps[-1] if steps else None}


__all__ = [
    "ACTION_WIDTH", "AwmStateAdapter", "AwpredictLearner", "ChainLearner", "FactoredLearner",
    "LearnedDelta", "STATE_WIDTH", "load_awpredict_engine", "make_learner",
    "train_from_transitions",
]
