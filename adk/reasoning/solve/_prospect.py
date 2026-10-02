"""The Prospector as a PRISM strategy: a frontier explorer ranked by ONLINE rarity.

Port of a filesystem cartographer's ``prospector`` policy, which reached 4x an
LLM's rate on a filesystem-exploration task (reach rate 0.667 vs 0.167).  There
the frontier priority is ``alpha * kind_rarity + beta * purpose_rarity * depth_likely
+ gamma * novelty`` where ``kind_rarity = 1 / (1 + freq[kind])`` is an online
histogram of the kinds the policy has SEEN THIS RUN -- fair by construction: no
model, no offline prior, only the policy's own observation stream.

Here a frontier entry is an untried ``(state, action)``:

* ``kind``     -- the action's arm: ``("A", id)`` for a plain action, ``("C", colour)``
                  for a coordinate action on a 2-D grid state (what the cell under the
                  click looks like, which is visible BEFORE acting);
* ``kind_rarity = 1 / (1 + freq[kind])`` where ``freq`` counts every candidate of
                  that kind the policy has seen in any visited state this run;
* ``novelty``  -- 3.0 for an arm never tried (the cartographer's "inf novelty");
                  otherwise ``3 * mean(1 / (1 + outcome_freq[o]))`` over the outcomes
                  this arm produced, so an arm that once caused a RARE outcome (a door
                  opening, a level up) stays interesting and one that only ever did
                  the common thing fades;
* the beta term needs an LLM's purpose reading and is dropped (beta = 0);
* a frontier elsewhere is reached over the level's known transition graph and pays
  ``dist_cost`` per step.

Nothing here edits the reasoning core: :func:`install_prospector` registers the
``prospect`` strategy with PRISM (auto kind, like ``handoff``) and routes the
loop's automatic actions to :class:`Prospector` only while that strategy is
active.  Every other path (LLM fallback, the ``explore()`` tool) is unchanged.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, Hashable, List, Optional, Set, Tuple

PROSPECT_ID = "prospect"
_UNSET: Any = object()

Action = Tuple[int, int, int]


def prospect_strategy(strategy_cls: Any) -> Any:
    """The PRISM manifest for the prospector, built with the core's ``Strategy``."""
    return strategy_cls(
        PROSPECT_ID,
        "Prospector",
        "auto",
        "Rank untried actions by how RARE their kind is in what this run has seen, "
        "and walk to the best frontier through the known transition graph.",
        auto_actions=25,
    )


def _grid(state: Any) -> Any:
    try:
        import numpy as np

        a = np.asarray(state)
    except Exception:  # noqa: BLE001 - a non-array state has no grid features
        return None
    return a if a.ndim == 2 and a.size else None


class Prospector:
    """Online-rarity frontier policy over one loop's own observation stream."""

    def __init__(self, loop: Any, alpha: float = 1.0, gamma: float = 0.5,
                 dist_cost: float = 0.05, max_depth: int = 40) -> None:
        self.loop = loop
        self.alpha = float(alpha)
        self.gamma = float(gamma)
        self.dist_cost = float(dist_cost)
        self.max_depth = int(max_depth)
        self.kind_freq: Dict[Hashable, int] = {}
        self.outcome_freq: Dict[Hashable, int] = {}
        self.arm_outcomes: Dict[Hashable, List[Hashable]] = {}
        self.seen_i = 0
        self.level: Optional[int] = None
        self.stats: Dict[str, int] = {"actions": 0, "frontier_here": 0, "walked": 0,
                                      "fallback": 0, "ingested": 0, "hook_errors": 0}
        self._new_level()

    # -- per-level graph -----------------------------------------------------
    def _new_level(self) -> None:
        self.edges: Dict[str, Dict[Action, str]] = {}
        self.tried: Dict[str, Set[Action]] = {}
        self.cands: Dict[str, List[Tuple[Action, Hashable]]] = {}
        self.deadly: Set[Tuple[str, Action]] = set()

    def _key(self, state: Any) -> str:
        # history stores states cast to int8 (Episodic.add) while loop.obs.state keeps
        # the env's dtype; the core's default key hashes raw bytes, so key ONE form or
        # tried/deadly/edges are filed under keys cands and the BFS never look up
        try:
            import numpy as np

            return str(self.loop._key(np.asarray(state, dtype=np.int8)))
        except (TypeError, ValueError, OverflowError):  # non-array: the loop's own key
            return str(self.loop._key(state))

    def _kind(self, state: Any, a: Action) -> Hashable:
        if a[1] >= 0 and a[2] >= 0:
            g = _grid(state)
            if g is not None and 0 <= a[2] < g.shape[0] and 0 <= a[1] < g.shape[1]:
                return ("C", int(g[a[2], a[1]]))
            return ("C", -1)
        return ("A", int(a[0]))

    def _outcome(self, t: Any) -> Hashable:
        if t.level_up:
            return "L"
        if t.died:
            return "D"
        hook = getattr(self.loop.hooks, "significant_change", None)
        try:
            if t.changed == 0 or (hook is not None and not hook(t.before, t.after)):
                return "0"
        except Exception:  # noqa: BLE001 - a failing hook falls back to the raw diff
            if t.changed == 0:
                return "0"
        b, a = _grid(t.before), _grid(t.after)
        if b is None or a is None or b.shape != a.shape:
            return ("S",)
        diff = b != a
        ign = getattr(self.loop.hooks, "ignore_cells", None)
        if ign is not None:
            try:
                m = ign(t.before, t.after)
                if m is not None and m.shape == diff.shape:
                    diff = diff & ~m
            except Exception:  # noqa: BLE001 - a failing mask hook means the unmasked diff
                self.stats["hook_errors"] += 1
        pairs: Dict[Tuple[int, int], int] = {}
        for x, y in zip(b[diff].tolist(), a[diff].tolist()):
            pairs[(x, y)] = pairs.get((x, y), 0) + 1
        top = tuple(sorted(sorted(pairs, key=lambda p: -pairs[p])[:3]))
        n = int(diff.sum())
        return ("S", 0 if n <= 4 else 1 if n <= 32 else 2, top)

    def _candidates(self, state: Any) -> List[Action]:
        raw = self.loop._hook("candidates", default=None) or []
        out = [tuple(int(v) for v in a) for a in raw]
        if not out:
            try:
                ids = list(self.loop.env.available_actions())
            except Exception:  # noqa: BLE001
                ids = []
            out = [(int(i), -1, -1) for i in ids if int(i) != 0]
        return out  # type: ignore[return-value]

    def _ingest(self) -> None:
        """Book every transition the loop recorded since the last call (any source)."""
        items = self.loop.history.items
        for t in items[self.seen_i:]:
            if self.level is None or t.level != self.level:
                continue
            s = self._key(t.before)
            a = tuple(t.action)
            self.tried.setdefault(s, set()).add(a)  # type: ignore[arg-type]
            arm = self._kind(t.before, a)  # type: ignore[arg-type]
            o = self._outcome(t)
            self.outcome_freq[o] = self.outcome_freq.get(o, 0) + 1
            self.arm_outcomes.setdefault(arm, []).append(o)
            if t.died:
                self.deadly.add((s, a))  # type: ignore[arg-type]
            elif not t.level_up:
                self.edges.setdefault(s, {})[a] = self._key(t.after)  # type: ignore[index]
            self.stats["ingested"] += 1
        self.seen_i = len(items)

    def _visit(self, s: str, state: Any) -> None:
        if s in self.cands:
            return
        lst = []
        for a in self._candidates(state):
            k = self._kind(state, a)
            self.kind_freq[k] = self.kind_freq.get(k, 0) + 1
            lst.append((a, k))
        self.cands[s] = lst

    # -- scoring ---------------------------------------------------------------
    def novelty(self, arm: Hashable) -> float:
        outs = self.arm_outcomes.get(arm)
        if not outs:
            return 3.0
        r = sum(1.0 / (1.0 + self.outcome_freq.get(o, 0)) for o in outs) / len(outs)
        return min(3.0, 3.0 * r)

    def priority(self, arm: Hashable) -> float:
        kr = 1.0 / (1.0 + self.kind_freq.get(arm, 0))
        return self.alpha * kr + self.gamma * self.novelty(arm)

    def _frontier(self, s: str) -> List[Tuple[Action, float]]:
        tried = self.tried.get(s, set())
        return [(a, self.priority(k)) for a, k in self.cands.get(s, [])
                if a not in tried and (s, a) not in self.deadly]

    def choose(self) -> Optional[Action]:
        """The next action: the best frontier entry, net of the walk to reach it."""
        loop = self.loop
        state = loop.obs.state
        if int(loop.level) != self.level:
            self.level = int(loop.level)
            self._new_level()
            self.seen_i = len(loop.history.items)
        self._ingest()
        s = self._key(state)
        self._visit(s, state)
        best: Optional[Tuple[float, Action]] = None
        here = self._frontier(s)
        for a, p in here:
            if best is None or p > best[0]:
                best = (p, a)
        # BFS over the known graph: a better frontier elsewhere, net of its distance
        first: Dict[str, Action] = {}
        seen = {s}
        q: deque = deque([(s, 0)])
        while q:
            u, d = q.popleft()
            if d >= self.max_depth:
                continue
            for a, v in self.edges.get(u, {}).items():
                if v in seen or (u, a) in self.deadly:
                    continue
                seen.add(v)
                first[v] = first.get(u, a) if u != s else a
                q.append((v, d + 1))
                for _fa, p in self._frontier(v):
                    net = p - self.dist_cost * (d + 1)
                    if best is None or net > best[0]:
                        best = (net, first[v])
        if best is None:
            return None
        if any(a == best[1] for a, _p in here):
            self.stats["frontier_here"] += 1
        else:
            self.stats["walked"] += 1
        return best[1]

    # -- the loop's ``auto`` contract -------------------------------------------
    def run(self, n: int, source: str = "prospect") -> Dict[str, Any]:
        """``n`` actions, same return shape as ``ReasoningLoop.auto``."""
        loop = self.loop
        k = ch = deaths = 0
        lvl = False
        for _ in range(max(0, int(n))):
            a = self.choose()
            if a is None:  # nothing left in the known graph: the domain explorer's turn
                a = loop._hook("auto_action")
                self.stats["fallback"] += 1
                if a is None:
                    break
            t = loop.step(tuple(int(v) for v in a), PROSPECT_ID)
            self.stats["actions"] += 1
            k += 1
            ch += t.changed > 0
            deaths += t.died
            if t.level_up:
                lvl = True
                break
        return {"actions": k, "changed": ch, "deaths": deaths, "level_up": lvl, "level": loop.level}


def install_prospector(loop: Any, by_id: Optional[Dict[str, Any]] = None,
                       strategy_cls: Any = None, **kw: Any) -> Optional[Prospector]:
    """Register ``prospect`` with this loop's PRISM and route its auto turns.

    ``by_id`` / ``strategy_cls`` default to the vendored core's ``prism.BY_ID`` /
    ``prism.Strategy`` (a host with its own copy of the core passes its own).  Registration is
    additive: a loop whose PRISM does not list ``prospect`` never selects it.
    Returns None when the loop runs without PRISM.
    """
    if by_id is None or strategy_cls is None:
        from ._vendor.prism import BY_ID, Strategy

        by_id, strategy_cls = BY_ID, Strategy
    if getattr(loop, "prism", None) is None:
        return None
    if PROSPECT_ID not in by_id:
        by_id[PROSPECT_ID] = prospect_strategy(strategy_cls)
    if PROSPECT_ID not in loop.prism.allowed:
        loop.prism.allowed.append(PROSPECT_ID)
    prospector = Prospector(loop, **kw)
    orig = loop._auto_turn

    def _auto_turn(strategy: Any, verified_before: Any, level_before: int) -> None:
        if getattr(strategy, "id", None) != PROSPECT_ID:
            return orig(strategy, verified_before, level_before)
        # the core's _auto_turn calls self.auto(n); put back whatever instance-level
        # ``auto`` another installer left there instead of deleting it
        prev = vars(loop).get("auto", _UNSET)
        loop.auto = prospector.run
        try:
            return orig(strategy, verified_before, level_before)
        finally:
            if prev is _UNSET:
                vars(loop).pop("auto", None)
            else:
                loop.auto = prev

    loop._auto_turn = _auto_turn
    summary = loop.summary

    def summary_with_prospect() -> Dict[str, Any]:
        out = summary()
        out["prospect"] = dict(prospector.stats)
        return out

    loop.summary = summary_with_prospect
    loop.prospector = prospector
    return prospector


__all__ = ["PROSPECT_ID", "Prospector", "install_prospector", "prospect_strategy"]
