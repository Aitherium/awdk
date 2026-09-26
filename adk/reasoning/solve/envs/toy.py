"""Toy environments: tiny, deterministic, no domain dependencies beyond numpy.

The vendored core books states as numpy arrays (``state.copy()``, ``.size``), so
these do too; import this module only where numpy is installed (``adk[reason]``).

* :class:`Counter1D` -- a 1x1 counter. Action 1 adds one, action 2 subtracts
  one, action 3 does nothing. Reaching ``target`` clears the level (the counter
  resets to 0); clearing ``levels`` levels wins.
* :class:`GridWalk5` -- a dot on a 5x5 board. Actions 1-4 move up/down/left/right
  (walls stop it). Reaching the bottom-right corner clears the level; the dot
  restarts top-left.
"""

from __future__ import annotations

from typing import Any, List, Optional

import numpy as np

from .._types import Action, Obs

__all__ = ["Counter1D", "GridWalk5"]


class Counter1D:
    def __init__(self, target: int = 5, levels: int = 2) -> None:
        self.target = int(target)
        self.levels = int(levels)
        self.n = 0
        self.level = 0
        self.over = False
        self.taken: List[Action] = []

    def _state(self) -> np.ndarray:
        return np.array([[self.n]], dtype=np.int8)

    def observe(self) -> Obs:
        return Obs(self._state(), level=self.level)

    def act(self, action: Action, source: str = "model") -> Obs:
        if self.over:
            return Obs(self._state(), level=self.level, done=True)
        self.taken.append(tuple(int(v) for v in action))  # type: ignore[arg-type]
        a = int(action[0])
        self.n += 1 if a == 1 else -1 if a == 2 else 0
        if self.n == self.target:
            win = self._state()
            self.level += 1
            self.n = 0
            self.over = self.level >= self.levels
            return Obs(
                self._state(),
                level=self.level,
                level_up=True,
                done=self.over,
                win_state=win,
                info={"won": self.over} if self.over else {},
            )
        return Obs(self._state(), level=self.level)

    def available_actions(self) -> List[int]:
        return [1, 2, 3]

    def done(self) -> bool:
        return self.over

    def primer(self) -> str:
        return (
            "A 1x1 integer counter. Actions: 1, 2, 3 (no coordinates). "
            "Clear %d levels." % self.levels
        )


class GridWalk5:
    MOVES = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}

    def __init__(self, levels: int = 2) -> None:
        self.levels = int(levels)
        self.pos = (0, 0)
        self.level = 0
        self.over = False

    def _state(self) -> np.ndarray:
        s = np.zeros((5, 5), dtype=np.int8)
        s[self.pos] = 1
        return s

    def observe(self) -> Obs:
        return Obs(self._state(), level=self.level)

    def act(self, action: Action, source: str = "model") -> Obs:
        if self.over:
            return Obs(self._state(), level=self.level, done=True)
        dr, dc = self.MOVES.get(int(action[0]), (0, 0))
        r = min(4, max(0, self.pos[0] + dr))
        c = min(4, max(0, self.pos[1] + dc))
        self.pos = (r, c)
        if self.pos == (4, 4):
            win = self._state()
            self.level += 1
            self.pos = (0, 0)
            self.over = self.level >= self.levels
            return Obs(
                self._state(),
                level=self.level,
                level_up=True,
                done=self.over,
                win_state=win,
                info={"won": self.over} if self.over else {},
            )
        return Obs(self._state(), level=self.level)

    def available_actions(self) -> List[int]:
        return [1, 2, 3, 4]

    def done(self) -> bool:
        return self.over

    def auto_action(self) -> Optional[Any]:
        """A non-LLM explorer that walks the diagonal home."""
        return (2, -1, -1) if self.pos[0] <= self.pos[1] else (4, -1, -1)
