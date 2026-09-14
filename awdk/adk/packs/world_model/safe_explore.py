"""Learn-safely explore loop — Curiosity-driven epsilon-greedy exploration.

Implements a simple but effective learn-safely policy: pick actions that balance
exploration (random) with exploitation (lowest surprise = high certainty, so pick
the opposite = highest surprise = curiosity-seeking).

The loop respects a hard budget cap and logs transitions for the world model.
Every step is executed through the caller's adapter; no retries on failure.

CursorWorldAdapter provides a minimal built-in deterministic grid world for
testing the loop without external dependencies.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Sequence, Tuple

logger = logging.getLogger("world_model_explore")


class CursorWorldAdapter:
    """Deterministic 12x12 cursor grid world for sandbox testing.

    The cursor is a single cell that moves under actions [0..3] (up, down,
    left, right), leaves a trail behind, and the board wraps at edges.
    Action 4 paints a trail at the current cell without moving.
    Actions 5+ are no-ops.

    Fully deterministic: the same action always produces the same result.
    Perfect for tabular world-model learning.

    Contract:
        Conforms to world_model.contracts.EnvironmentAdapter.
    """

    CURSOR_COLOR = 2
    TRAIL_COLOR = 3

    def __init__(self, grid_size: int = 12, seed: int = 0) -> None:
        self.grid_size = grid_size
        self.seed = seed
        self._cursor_row = grid_size // 2
        self._cursor_col = grid_size // 2
        self._grid = [[0] * grid_size for _ in range(grid_size)]
        self._grid[self._cursor_row][self._cursor_col] = self.CURSOR_COLOR
        self.domain = "sandbox"

    def observe(self, env_state: Any = None) -> str:
        """Convert grid state to a hashable string representation.

        Returns a compact string encoding of the grid for hashing into embeddings.
        """
        if env_state is not None:
            # Accept an external state; encode it
            return str(env_state)
        # Use current internal state
        lines = []
        for row in self._grid:
            lines.append("".join(str(c) for c in row))
        return "|".join(lines)

    def actions(self) -> Sequence[int]:
        """Return the discrete action vocabulary: [0, 1, 2, 3, 4, 5, 6]."""
        return [0, 1, 2, 3, 4, 5, 6]

    def step(self, action: int) -> Tuple[str, float, bool, Dict[str, Any]]:
        """Execute an action and return (next_obs, reward, done, info).

        The cursor world never terminates (done is always False).
        Reward is always 0.0 (exploration only, no objective).

        Args:
            action: Integer in [0..6]:
                0 = move up
                1 = move down
                2 = move left
                3 = move right
                4 = paint trail at cursor (no move)
                5-6 = no-op

        Returns:
            (next_obs: str, reward: float, done: bool, info: dict)
        """
        action = int(action)
        old_row, old_col = self._cursor_row, self._cursor_col

        # Execute action
        if action == 0:  # up
            self._cursor_row = max(0, self._cursor_row - 1)
        elif action == 1:  # down
            self._cursor_row = min(self.grid_size - 1, self._cursor_row + 1)
        elif action == 2:  # left
            self._cursor_col = max(0, self._cursor_col - 1)
        elif action == 3:  # right
            self._cursor_col = min(self.grid_size - 1, self._cursor_col + 1)
        elif action == 4:  # paint trail at cursor (no move)
            self._grid[self._cursor_row][self._cursor_col] = self.TRAIL_COLOR
            # Replant the cursor on top
            self._grid[self._cursor_row][self._cursor_col] = self.CURSOR_COLOR
        # actions 5-6 are no-ops

        # If we moved, leave a trail
        if (self._cursor_row, self._cursor_col) != (old_row, old_col):
            self._grid[old_row][old_col] = self.TRAIL_COLOR
            self._grid[self._cursor_row][self._cursor_col] = self.CURSOR_COLOR

        next_obs = self.observe()
        reward = 0.0
        done = False

        return next_obs, reward, done, {"action": action, "moved": True}


def explore(
    adapter: Any,
    budget: int,
    epsilon: float = 0.3,
    wm_observe_fn: Any = None,
) -> Dict[str, Any]:
    """Learn-safely explore loop: epsilon-greedy curiosity-seeking.

    Runs a hard-capped exploration loop: observe state, pick action (epsilon-
    greedy: random vs. lowest-surprise = highest-curiosity), step adapter,
    record transition into world model, repeat until budget exhausted.

    Actions that fail (adapter.step raises) are NOT retried; loop skips that
    step and continues.

    Args:
        adapter: An EnvironmentAdapter (conforms to contracts.EnvironmentAdapter).
        budget: Maximum number of steps to execute. Loop stops at budget even
                if the adapter wants to continue.
        epsilon: Exploration probability (default 0.3). Fraction of steps that
                 pick a random action; others pick lowest-surprise (curiosity).
        wm_observe_fn: Callable to record transitions. If None, transitions are
                       counted but not recorded. Expected signature:
                       wm_observe_fn(obs, action, next_obs, reward, done, domain)
                       and returns {"ok": bool, ...}.

    Returns:
        {"steps": <int>, "transitions_recorded": <int>, "mean_surprise_start": <float>,
         "mean_surprise_end": <float>, "budget_exhausted": <bool>, "degraded": <bool>}
        degraded=True indicates the adapter was invalid or the loop could not run.
    """
    if not hasattr(adapter, "observe") or not callable(adapter.observe):
        logger.error("adapter missing observe() method")
        return {
            "steps": 0,
            "transitions_recorded": 0,
            "mean_surprise_start": None,
            "mean_surprise_end": None,
            "budget_exhausted": False,
            "degraded": True,
        }

    if not hasattr(adapter, "step") or not callable(adapter.step):
        logger.error("adapter missing step() method")
        return {
            "steps": 0,
            "transitions_recorded": 0,
            "mean_surprise_start": None,
            "mean_surprise_end": None,
            "budget_exhausted": False,
            "degraded": True,
        }

    if not hasattr(adapter, "actions") or not callable(adapter.actions):
        logger.error("adapter missing actions() method")
        return {
            "steps": 0,
            "transitions_recorded": 0,
            "mean_surprise_start": None,
            "mean_surprise_end": None,
            "budget_exhausted": False,
            "degraded": True,
        }

    # Import wm_surprise tool if wm_observe_fn is provided but we need surprise
    try:
        from . import tools as wm_tools
        if wm_observe_fn is None:
            wm_observe_fn = wm_tools.wm_observe
        wm_surprise_fn = wm_tools.wm_surprise
    except ImportError:
        logger.warning("Could not import wm tools; no WM recording")
        wm_observe_fn = None
        wm_surprise_fn = None

    domain = getattr(adapter, "domain", "sandbox")
    actions = list(adapter.actions())

    obs = adapter.observe()
    steps_taken = 0
    transitions_recorded = 0
    done = False

    surprises_start: list[float] = []
    surprises_end: list[float] = []

    while steps_taken < budget and not done:
        # Epsilon-greedy: random or curiosity-seeking
        import random

        if random.random() < epsilon:
            # Random exploration
            action = random.choice(actions)
        else:
            # Exploit: pick highest-surprise action (curiosity)
            if wm_surprise_fn is None:
                action = random.choice(actions)
            else:
                # Score all actions and pick the highest-surprise one
                items = [
                    {
                        "id": str(i),
                        "obs": obs,
                        "action": a,
                        "next_obs": None,  # We don't know next_obs yet
                    }
                    for i, a in enumerate(actions)
                ]
                result = wm_surprise_fn(items, domain=domain)
                if result.get("ok"):
                    surprises = result.get("surprises", {})
                    # Pick the action with highest surprise (most uncertain)
                    best_idx = 0
                    best_surprise = -1.0
                    for i, a in enumerate(actions):
                        s = surprises.get(str(i))
                        if s is not None and s > best_surprise:
                            best_surprise = s
                            best_idx = i
                    action = actions[best_idx]
                    if best_idx < len(surprises_start):
                        surprises_start.append(best_surprise)
                else:
                    action = random.choice(actions)

        # Take the step
        try:
            next_obs, reward, done, info = adapter.step(action)
        except Exception as e:
            logger.debug("adapter.step failed: %s; skipping step", e)
            steps_taken += 1
            continue

        # Record transition if we have a recorder
        if wm_observe_fn is not None:
            obs_result = wm_observe_fn(
                obs, action, next_obs, reward=reward, done=done, domain=domain
            )
            if obs_result.get("ok"):
                transitions_recorded += 1

        steps_taken += 1
        obs = next_obs

        # Optionally score the transition we just took (to measure learning progress)
        if wm_surprise_fn is not None and steps_taken % 10 == 0:
            result = wm_surprise_fn(
                [{"id": "curr", "obs": obs, "action": action, "next_obs": next_obs}],
                domain=domain,
            )
            if result.get("ok"):
                s = result.get("surprises", {}).get("curr")
                if s is not None and steps_taken >= budget // 2:
                    surprises_end.append(s)

    mean_surprise_start = (
        sum(surprises_start) / len(surprises_start)
        if surprises_start
        else None
    )
    mean_surprise_end = (
        sum(surprises_end) / len(surprises_end) if surprises_end else None
    )

    return {
        "steps": steps_taken,
        "transitions_recorded": transitions_recorded,
        "mean_surprise_start": mean_surprise_start,
        "mean_surprise_end": mean_surprise_end,
        "budget_exhausted": steps_taken >= budget,
        "degraded": False,
    }
