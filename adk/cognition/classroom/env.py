"""An in-process environment over one h25 level: the TRUE transition function
(:mod:`.rules`) rendered as a colour grid (rows = y, columns = x).

It renders the board only -- no HUD rows and no 64x64 letterboxing (rung C0 has no
HUD); the colours are the game's own shuffled palette, so perception still has to
work out which colour plays which role.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ._vendor import rules

__all__ = ["PushEnv", "level_frame"]


def level_frame(prepared: Dict[str, Any], state: Any, palette: List[int]) -> np.ndarray:
    roles = rules.render_roles(prepared, state)
    return np.array([[palette[r] for r in row] for row in roles], dtype=np.int8)


class PushEnv:
    """One level of one game. ``step`` counts every action, effective or not."""

    def __init__(self, spec: Dict[str, Any], level: int) -> None:
        self.spec = spec
        self.level = int(level)
        self.P = rules.prep(spec["levels"][self.level])
        self.palette: List[int] = list(spec["colors"])
        self.state: Any = rules.initial_state(self.P)
        self.status = rules.SY_PLAY
        self.actions = 0

    @property
    def optimal(self) -> int:
        return int(self.spec["optimal_actions"][self.level])

    @property
    def baseline(self) -> int:
        return int(self.spec["baseline_actions"][self.level])

    def frame(self, state: Optional[Any] = None) -> np.ndarray:
        return level_frame(self.P, self.state if state is None else state, self.palette)

    def reset(self) -> np.ndarray:
        """Back to the level start (a RESET; not a counted action here)."""
        self.state = rules.initial_state(self.P)
        self.status = rules.SY_PLAY
        return self.frame()

    def step(self, action: int) -> Tuple[np.ndarray, np.ndarray, int]:
        if self.status != rules.SY_PLAY:
            raise RuntimeError("level is over (status %d)" % self.status)
        before = self.frame()
        self.state, self.status = rules.apply(self.P, self.state, int(action))
        self.actions += 1
        return before, self.frame(), self.status

    @property
    def won(self) -> bool:
        return self.status == rules.SY_WIN
