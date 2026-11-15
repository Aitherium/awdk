"""Pure geometry planner for window arrangements -- no ctypes, no I/O.

Every input and output rectangle is in *placeable-frame* pixels: the rectangle
``SetWindowPos`` consumes. Coordinates may be negative (a monitor to the left of
or above the primary one), so nothing here is ever clamped to zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["MODES", "Rect", "split"]

#: Layout modes understood by :func:`split`.
MODES = ("side_by_side", "stacked", "grid")


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle: origin ``(x, y)``, size ``w`` x ``h``."""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def contains(self, px: int, py: int) -> bool:
        """True when the point lies inside (right/bottom edges exclusive)."""
        return self.x <= px < self.right and self.y <= py < self.bottom

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


def _axis(start: int, length: int, cells: int, gap: int) -> list[tuple[int, int]]:
    """Split one axis into ``cells`` spans of ``(start, length)``.

    The gap is clamped so ``gap * (cells - 1)`` never eats the whole axis (each
    cell keeps at least one pixel), and the integer remainder is absorbed by the
    last cell so its far edge lands exactly on ``start + length``.
    """
    if cells == 1:
        return [(start, length)]
    gap = max(0, min(gap, (length - cells) // (cells - 1)))
    base = (length - gap * (cells - 1)) // cells
    spans: list[tuple[int, int]] = []
    for i in range(cells):
        pos = start + i * (base + gap)
        size = base if i < cells - 1 else start + length - pos
        spans.append((pos, size))
    return spans


def split(area: Rect, count: int, *, mode: str = "side_by_side", gap: int = 8) -> list[Rect]:
    """Divide ``area`` into ``count`` cells.

    Args:
        area: The work area to fill (placeable-frame pixels; may be negative).
        count: Number of windows. Zero or less returns an empty list.
        mode: ``side_by_side`` (columns), ``stacked`` (rows) or ``grid``
            (``ceil(sqrt(count))`` columns, filled row-major).
        gap: Pixels between neighbouring cells; clamped to fit the axis.

    Returns:
        One Rect per window, in input order.

    Raises:
        ValueError: unknown mode, or an area too small to hold ``count`` cells.
    """
    count = int(count)
    if count <= 0:
        return []
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    if mode == "side_by_side":
        cols, rows = count, 1
    elif mode == "stacked":
        cols, rows = 1, count
    else:
        cols = math.ceil(math.sqrt(count))
        rows = math.ceil(count / cols)
    if area.w < cols or area.h < rows:
        raise ValueError(f"area {area.w}x{area.h} cannot hold {cols}x{rows} cells")
    gap = max(0, int(gap))
    xs = _axis(area.x, area.w, cols, gap)
    ys = _axis(area.y, area.h, rows, gap)
    out: list[Rect] = []
    for i in range(count):
        row, col = divmod(i, cols)
        (x, w), (y, h) = xs[col], ys[row]
        out.append(Rect(x, y, w, h))
    return out
