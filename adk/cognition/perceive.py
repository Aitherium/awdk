"""Perception for grid games (the library Sense's ``/perceive`` hosts; design section 3).

``perceive(frames, transitions)`` -> facts with ``observation`` evidence. Every fact
is derived by a rule that is SOUND for the push family's rendering, so a fact is only
emitted when the observations force it; anything that is merely likely is not emitted
at all (precision over recall -- hop H2 fails on a single wrong fact):

* ``colour:wall``   -- the colour of every border cell (the border is all wall).
* ``colour:floor``  -- the most common interior colour.
* ``colour:player`` -- the one colour that occurs exactly once before and after a
  changing transition and moved by exactly the action's direction, on every such
  transition.
* ``action:<a>:effective`` -- action ``a`` changed the frame at least once.
* ``colour:goal``   -- a cell the player just left that is not floor (after a move the
  vacated cell can only be floor or goal), or the not-floor cell a box was pushed into.
* ``colour:box``    -- the colour a pushed object takes on a floor cell.
* ``colour:box_on`` -- the colour a pushed object takes on a goal cell (a pushed object
  only enters a free cell, and a free cell renders floor or goal).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

__all__ = ["DIRS", "perceive", "Perception", "find_single", "COLOUR_ROLES"]

#: ARC-AGI-3 simple actions: 1 up, 2 down, 3 left, 4 right; (dx, dy).
DIRS: Dict[int, Tuple[int, int]] = {1: (0, -1), 2: (0, 1), 3: (-1, 0), 4: (1, 0)}
COLOUR_ROLES = ("wall", "floor", "player", "box", "goal", "box_on")

Transition = Tuple[np.ndarray, int, np.ndarray]


class Perception:
    """The facts plus the observation refs behind each one."""

    def __init__(self) -> None:
        self.facts: Dict[str, Any] = {}
        self.refs: Dict[str, List[str]] = {}
        self.conflicts: List[str] = []

    def put(self, key: str, value: Any, ref: str) -> None:
        old = self.facts.get(key)
        if old is not None and old != value:
            # two sound derivations disagree: the rendering assumption is broken, so
            # neither is emitted (a conflict is a finding, not a fact)
            self.conflicts.append("%s: %r vs %r at %s" % (key, old, value, ref))
            return
        self.facts[key] = value
        self.refs.setdefault(key, [])
        if ref not in self.refs[key]:
            self.refs[key].append(ref)

    def colour(self, role: str) -> Optional[int]:
        v = self.facts.get("colour:" + role)
        return None if v is None else int(v)


def find_single(frame: np.ndarray, colour: int) -> Optional[Tuple[int, int]]:
    """``(x, y)`` of the one cell of ``colour``, or None if it is not exactly one."""
    ys, xs = np.nonzero(frame == colour)
    if len(xs) != 1:
        return None
    return int(xs[0]), int(ys[0])


def _in(frame: np.ndarray, x: int, y: int) -> bool:
    return 0 <= y < frame.shape[0] and 0 <= x < frame.shape[1]


def perceive(
    first: np.ndarray, transitions: Sequence[Transition], scope: str, clock0: int = 0
) -> Perception:
    """Facts from a level's first frame and its transitions ``(before, action, after)``;
    transition ``i`` is cited as ``<scope>/t<clock0 + i>``."""
    p = Perception()
    first = np.asarray(first)
    border = np.concatenate([first[0, :], first[-1, :], first[:, 0], first[:, -1]])
    if len(set(int(v) for v in border)) == 1:
        p.put("colour:wall", int(border[0]), "%s/first:border" % scope)
    interior = first[1:-1, 1:-1].ravel()
    if interior.size:
        ((floor, n),) = Counter(int(v) for v in interior).most_common(1)
        if n * 2 > interior.size:
            p.put("colour:floor", floor, "%s/first:interior" % scope)

    # player: intersect candidates over every changing transition
    cands: Optional[set] = None
    for i, (b, a, af) in enumerate(transitions):
        if int(a) not in DIRS or not (b != af).any():
            continue
        dx, dy = DIRS[int(a)]
        here = set()
        for c in set(int(v) for v in np.unique(b)):
            pb, pa = find_single(b, c), find_single(af, c)
            if pb and pa and (pa[0] - pb[0], pa[1] - pb[1]) == (dx, dy):
                here.add(c)
        cands = here if cands is None else cands & here
    if cands is not None and len(cands) == 1:
        pc = next(iter(cands))
        for i, (b, a, af) in enumerate(transitions):
            if (b != af).any() and int(a) in DIRS:
                p.put("colour:player", pc, "%s/t%d" % (scope, clock0 + i))
                break

    for i, (b, a, af) in enumerate(transitions):
        if (b != af).any():
            p.put("action:%d:effective" % int(a), True, "%s/t%d" % (scope, clock0 + i))

    # goal / box / box_on need the player and floor colours; iterate to a fixpoint
    pc, floor = p.colour("player"), p.colour("floor")
    if pc is None or floor is None:
        return p
    for _ in range(3):
        for i, (b, a, af) in enumerate(transitions):
            if int(a) not in DIRS or not (b != af).any():
                continue
            ref = "%s/t%d" % (scope, clock0 + i)
            dx, dy = DIRS[int(a)]
            pb, pa = find_single(b, pc), find_single(af, pc)
            if not pb or not pa or (pa[0] - pb[0], pa[1] - pb[1]) != (dx, dy):
                continue
            left = int(af[pb[1], pb[0]])
            if left != floor:
                p.put("colour:goal", left, ref)
            bx, by = pa[0] + dx, pa[1] + dy
            if not _in(b, bx, by) or int(b[by, bx]) == int(af[by, bx]):
                continue  # nothing was pushed
            moved_to, under = int(af[by, bx]), int(b[by, bx])
            # a pushed object only enters a free cell, which renders floor or goal
            if under == floor:
                p.put("colour:box", moved_to, ref)
            else:
                p.put("colour:goal", under, ref)
                p.put("colour:box_on", moved_to, ref)
    if p.conflicts:
        for c in p.conflicts:
            key = c.split(": ", 1)[0]
            p.facts.pop(key, None)
            p.refs.pop(key, None)
    return p
