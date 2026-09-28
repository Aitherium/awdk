"""Deterministic environments for the adk.world tests. Adapter shape = awpredict's.

KeyDoorGrid (5x5):

    y=4  K . # . .        K  key home (0,4)       #  wall (x=2, y != 2)
    y=3  . . # . .        D  door (2,2): passable only when open
    y=2  . . D . .        open: stand next to the door (1,2)/(3,2) holding the key
    y=1  . . # . .        close: stand next to it while it is open
    y=0  S . # . .        pickup/drop: only at the key's home cell
         x=0 1 2 3 4

Every action is reversible, so one curious episode can reach every (state, action)
pair: 82 reachable states x 8 actions = 656.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

AWM_SRC = os.environ.get("AWM_SRC")
if AWM_SRC and AWM_SRC not in sys.path:
    sys.path.insert(0, AWM_SRC)

awm = pytest.importorskip("awm")
if not hasattr(awm, "WorldState"):
    pytest.skip("awm without the world model (needs >= 0.5)", allow_module_level=True)

W = H = 5
WALL_X = 2
DOOR = (2, 2)
KEY_HOME = (0, 4)
START = (0, 0)
ACTIONS = ["up", "down", "left", "right", "pickup", "drop", "open", "close"]
MOVES = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}


def free_cells() -> List[Tuple[int, int]]:
    return [(x, y) for x in range(W) for y in range(H) if x != WALL_X or (x, y) == DOOR]


class KeyDoorGrid:
    domain = "grid"

    def __init__(self, teleport_at: Optional[int] = None, teleport_seed: int = 0) -> None:
        self.x, self.y = START
        self.key = "home"
        self.door = "closed"
        self.t = 0
        self.teleport_at = teleport_at
        self.teleport_seed = teleport_seed

    def _slots(self) -> Dict[str, str]:
        return {"pos.x": str(self.x), "pos.y": str(self.y), "key": self.key, "door": self.door}

    def observe(self, env_state: Any = None) -> Dict[str, str]:
        return dict(env_state) if isinstance(env_state, dict) else self._slots()

    def actions(self) -> List[str]:
        return list(ACTIONS)

    def _adjacent_to_door(self) -> bool:
        return self.y == DOOR[1] and abs(self.x - DOOR[0]) == 1

    def step(self, action: str) -> Tuple[Dict[str, str], float, bool, Dict[str, Any]]:
        if action in MOVES:
            dx, dy = MOVES[action]
            nx, ny = self.x + dx, self.y + dy
            if 0 <= nx < W and 0 <= ny < H and (nx, ny) in free_cells():
                if (nx, ny) != DOOR or self.door == "open":
                    self.x, self.y = nx, ny
        elif action == "pickup" and (self.x, self.y) == KEY_HOME and self.key == "home":
            self.key = "held"
        elif action == "drop" and (self.x, self.y) == KEY_HOME and self.key == "held":
            self.key = "home"
        elif action == "open" and self._adjacent_to_door() and self.key == "held" \
                and self.door == "closed":
            self.door = "open"
        elif action == "close" and self._adjacent_to_door() and self.door == "open":
            self.door = "closed"
        if self.teleport_at is not None and self.t == self.teleport_at:
            # Violation of expectation: the world moves the agent by itself.
            rng = random.Random(self.teleport_seed)
            cells = [c for c in free_cells() if c != (self.x, self.y) and c != DOOR]
            self.x, self.y = rng.choice(cells)
        self.t += 1
        return self._slots(), 0.0, False, {}


class Dial:
    """One slot n in 0..9; actions add_k (k = 0..19) set n = (n + k) % 10.

    20 actions: above the beam limit, so the planner takes the CEM path.
    """

    domain = "dial"

    def __init__(self) -> None:
        self.n = 0

    def observe(self, env_state: Any = None) -> Dict[str, str]:
        return {"n": str(self.n)}

    def actions(self) -> List[str]:
        return [f"add_{k}" for k in range(20)]

    def step(self, action: str) -> Tuple[Dict[str, str], float, bool, Dict[str, Any]]:
        self.n = (self.n + int(action.split("_")[1])) % 10
        return self.observe(), 0.0, False, {}


def open_store(path: Path, clock_start: float = 1_000.0) -> Any:
    """A MemoryStore with a deterministic clock and synchronous=OFF (tests only: speed)."""
    st = awm.MemoryStore(path)
    t = [clock_start]

    def clock() -> float:
        t[0] += 0.001
        return t[0]

    st._clock = clock
    st._db.execute("PRAGMA synchronous=OFF")
    return st


SCOPE = awm.Scope("acme", "tester", "grid")
