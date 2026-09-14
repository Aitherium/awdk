"""Smart policy for ARC-AGI-3 — coordinate selection based on grid analysis.

This module provides intelligent coordinate selection for ACTION6 (click action)
instead of uniform random sampling. The policy analyzes the grid to:
1. Identify distinct cell values (non-background cells)
2. Prefer cells that differ from the most common value
3. Track recent actions to avoid repeating no-ops
4. Support the pluggable policy interface for harness optimization.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


def analyze_grid(grid: Optional[List[List[int]]]) -> Dict[str, Any]:
    """Analyze a grid and return features for smart action selection.

    Args:
        grid: A 2D list of integers representing the grid state.

    Returns:
        A dict with keys:
        - "valid": whether the grid is analyzable
        - "width", "height": grid dimensions
        - "background": the most common cell value (assumed background)
        - "foreground_cells": list of (row, col, value) tuples for non-background cells
        - "distinct_values": count of distinct cell values
    """
    if not grid or not isinstance(grid, list):
        return {"valid": False}

    try:
        height = len(grid)
        if height == 0:
            return {"valid": False}

        width = len(grid[0]) if grid[0] else 0
        if width == 0:
            return {"valid": False}

        # Flatten to count cell values
        cells = []
        for row in grid:
            for cell in row:
                cells.append(int(cell) if cell is not None else 0)

        if not cells:
            return {"valid": False}

        # Most common value is the background
        counter = Counter(cells)
        background = counter.most_common(1)[0][0]
        distinct = len(counter)

        # Find foreground cells (non-background)
        foreground = []
        for row_idx, row in enumerate(grid):
            for col_idx, cell in enumerate(row):
                val = int(cell) if cell is not None else 0
                if val != background:
                    foreground.append((row_idx, col_idx, val))

        return {
            "valid": True,
            "width": width,
            "height": height,
            "background": background,
            "foreground_cells": foreground,
            "distinct_values": distinct,
        }
    except Exception:
        return {"valid": False}


def select_smart_coordinates(
    grid: Optional[List[List[int]]],
    width: int = 64,
    height: int = 64,
    recent_actions: Optional[List[Tuple[int, int]]] = None,
) -> Tuple[int, int]:
    """Select intelligent (x, y) coordinates for ACTION6 (click action).

    Strategy:
    1. Analyze the grid for foreground (non-background) cells
    2. If foreground cells exist, pick one at random (biased exploration)
    3. If no foreground, sample uniformly but avoid recent coordinates
    4. Fallback to random if grid is invalid

    Args:
        grid: The current game grid (2D list of integers)
        width, height: Grid dimensions (typically 64x64)
        recent_actions: List of recent (x, y) coordinates to avoid

    Returns:
        A tuple (x, y) of coordinates within [0, width) and [0, height)
    """
    import random

    analysis = analyze_grid(grid)

    if not analysis.get("valid"):
        # Fallback to random uniform sampling
        return random.randint(0, width - 1), random.randint(0, height - 1)

    foreground = analysis.get("foreground_cells", [])
    recent = set(recent_actions or [])

    # If we found distinct regions, sample from them
    if foreground and len(foreground) > 0:
        # Prefer foreground cells with slight randomization
        # (sample around each foreground cell)
        row, col, _ = random.choice(foreground)
        # Add noise to explore nearby cells too
        noise = random.randint(-2, 2)
        x = max(0, min(width - 1, col + noise))
        y = max(0, min(height - 1, row + noise))
        return x, y

    # No clear foreground — sample uniformly, avoiding recent coordinates
    if recent and len(recent) < width * height / 10:
        # If we have a reasonable number of recent actions, avoid them
        candidates = [
            (x, y)
            for x in range(width)
            for y in range(height)
            if (x, y) not in recent
        ]
        if candidates:
            return random.choice(candidates)

    # Fallback: uniform random
    return random.randint(0, width - 1), random.randint(0, height - 1)


class SmartPolicyTracker:
    """Tracks recent actions and grid states for the smart policy."""

    def __init__(self, window_size: int = 10):
        """Initialize the tracker.

        Args:
            window_size: How many recent actions to remember
        """
        self.window_size = window_size
        self.recent_actions: List[Tuple[int, int]] = []
        self.recent_grids: List[str] = []
        self.no_op_count = 0

    def add_action(self, x: int, y: int, grid_before: str, grid_after: str) -> bool:
        """Record an action and check if it was a no-op.

        Args:
            x, y: The coordinates of the action
            grid_before: Grid observation before the action (as string)
            grid_after: Grid observation after the action (as string)

        Returns:
            True if the action was a no-op (grid unchanged), False otherwise
        """
        self.recent_actions.append((x, y))
        if len(self.recent_actions) > self.window_size:
            self.recent_actions.pop(0)

        is_noop = grid_before == grid_after
        if is_noop:
            self.no_op_count += 1

        self.recent_grids.append(grid_after)
        if len(self.recent_grids) > self.window_size:
            self.recent_grids.pop(0)

        return is_noop

    def should_stop_exploring(self) -> bool:
        """Check if we should stop exploring (too many no-ops).

        Returns:
            True if recent actions have been mostly no-ops
        """
        if len(self.recent_actions) < 5:
            return False
        # If >60% of recent actions were no-ops, stop
        return self.no_op_count / len(self.recent_actions) > 0.6


def select_action_with_policy(
    grid: Optional[List[List[int]]],
    tracker: SmartPolicyTracker,
    available_actions: List[int],
) -> int:
    """Select an action using the smart policy.

    For ACTION6, uses smart coordinate selection.
    For other actions, uses the available actions list.

    Args:
        grid: The current game grid
        tracker: The policy tracker instance
        available_actions: List of available action ids

    Returns:
        The selected action id
    """
    import random

    # Stop exploring if too many no-ops
    if tracker.should_stop_exploring():
        # Fallback to action 1 (safe, non-spatial action)
        return 1

    # Prefer ACTION6 (the interesting one) if available
    if 6 in available_actions:
        return 6

    # Otherwise pick from available actions
    if available_actions:
        return random.choice(available_actions)

    return 1  # Safe default
