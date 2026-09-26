"""Frame perception for ARC-AGI-3: a learned HUD mask, diffs and compact renders.

Many ARC-AGI-3 games draw a step counter, budget bar or timer on the frame. It
changes on (almost) every action, so a novelty key over the raw frame calls every
state new and a search over states never closes. :class:`HudMask` learns, per
level, which cells are such a meter and leaves them out of the key.

The rule (one detector, judged within the current life of the level):

* a transition changes a few cells (``1..max_line_cells``) on one row or column;
* the change is at most ``max_width`` lines thick (a moving piece changes several
  adjacent lines at once);
* none of those cells has returned to a value it already held in this life -- a
  meter only drains or fills, while a piece the player moves leaves cells behind
  that revert to the background;
* the line is inside the frame's border strip, or the same-coloured run the change
  lies on reaches the frame edge;
* the same line has shown ``confirm`` such ticks -- and, for a run away from the
  border, under at least two different action ids (a counter ticks whatever the
  player does; a piece pushed one way by one action does not qualify).

Then the WHOLE run on that line (the cells that held one of the ticked colours in
the level's first frame) is masked, so the meter's future cells are covered before
they tick. The mask is capped at ``max_frac`` of the frame, survives a death or
RESET (a refilled bar is still a bar) and is cleared on a level change (the layout
can differ).

Requires numpy; imported only when an :class:`~.env_arc.ArcAgi3Environment` is built.
"""

from __future__ import annotations

import zlib
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

__all__ = [
    "HudMask",
    "board_map",
    "colour_counts",
    "diff_text",
    "edge_tick_split",
    "masked_key",
    "view_text",
]


def masked_key(state: Any, mask: Optional[np.ndarray] = None) -> str:
    """Stable hex key of a frame with ``mask`` cells blanked (shape is part of the key)."""
    arr = np.ascontiguousarray(np.asarray(state), dtype=np.int16)
    if mask is not None and mask.shape == arr.shape and mask.any():
        arr = arr.copy()
        arr[mask] = -1
    head = ("%s|" % (arr.shape,)).encode()
    return "%08x%08x" % (zlib.crc32(head + arr.tobytes()), zlib.adler32(arr.tobytes()))


def edge_tick_split(
    before: np.ndarray,
    after: np.ndarray,
    mask: Optional[np.ndarray] = None,
    max_cells: int = 8,
) -> Tuple[np.ndarray, np.ndarray]:
    """Changed cells split into ``(board, hud)``.

    HUD = changes under ``mask`` plus a small change (``1..max_cells``) on an
    outermost row or column, which is a bar or counter ticking even before the
    mask has learned it.
    """
    d = before != after
    h = np.zeros_like(d)
    if mask is not None and mask.shape == d.shape:
        h = d & mask
        d = d & ~mask
    if d.ndim == 2 and d.any():
        rows, cols = d.shape
        for sl in (
            (0, slice(None)),
            (rows - 1, slice(None)),
            (slice(None), 0),
            (slice(None), cols - 1),
        ):
            k = int(d[sl].sum())
            if 0 < k <= max_cells:
                h[sl] = h[sl] | d[sl]
                d[sl] = False
    return d, h


def diff_text(
    before: Any, after: Any, mask: Optional[np.ndarray] = None, max_pairs: int = 6
) -> str:
    """One transition as text: board cells changed, colour pairs, HUD cells ignored."""
    b, a = np.asarray(before), np.asarray(after)
    if b.shape != a.shape:
        return "frame shape changed %s -> %s" % (b.shape, a.shape)
    d, h = edge_tick_split(b, a, mask)
    n, nh = int(d.sum()), int(h.sum())
    if n == 0:
        if nh == 0:
            return "no change"
        return "no board change (only %d HUD cells changed: a counter or meter)" % nh
    rows, cols = np.nonzero(d)
    pairs: Dict[Tuple[int, int], int] = {}
    for r, c in zip(rows.tolist(), cols.tolist()):
        k = (int(b[r, c]), int(a[r, c]))
        pairs[k] = pairs.get(k, 0) + 1
    top = sorted(pairs.items(), key=lambda kv: -kv[1])[:max_pairs]
    return "%d board cells changed in rows %d-%d cols %d-%d; colour a->b: %s%s" % (
        n,
        int(rows.min()),
        int(rows.max()),
        int(cols.min()),
        int(cols.max()),
        ", ".join("%d->%d x%d" % (x, y, k) for (x, y), k in top),
        (" (+%d HUD cells ignored)" % nh) if nh else "",
    )


def colour_counts(frame: Any) -> List[Tuple[int, int]]:
    """``[(colour, cells), ...]`` most common first."""
    arr = np.asarray(frame).astype(np.int64).ravel()
    if arr.size == 0:
        return []
    counts = np.bincount(arr % 16, minlength=16)
    return [(int(c), int(counts[c])) for c in np.argsort(-counts, kind="stable") if counts[c]]


def board_map(frame: Any, k: int = 2, mask: Optional[np.ndarray] = None) -> str:
    """Downsampled hex map: one char per ``k x k`` block, ``.`` = background,
    else the rarest colour in the block; masked (HUD) blocks print ``#``."""
    arr = np.asarray(frame)
    if arr.ndim != 2 or arr.size == 0:
        return "(no frame)"
    bg = colour_counts(arr)[0][0]
    rows, cols = arr.shape
    out = []
    for r in range(0, rows, k):
        line = []
        for c in range(0, cols, k):
            if mask is not None and mask.shape == arr.shape and mask[r : r + k, c : c + k].all():
                line.append("#")
                continue
            block = arr[r : r + k, c : c + k].astype(np.int64).ravel() % 16
            vals = [v for v in block.tolist() if v != bg]
            if not vals:
                line.append(".")
                continue
            freq: Dict[int, int] = {}
            for v in vals:
                freq[v] = freq.get(v, 0) + 1
            line.append("%x" % min(freq, key=lambda v: (freq[v], v)))
        out.append("".join(line))
    return "\n".join(out)


def view_text(frame: Any, r0: int = 0, c0: int = 0, r1: int = 63, c1: int = 63) -> str:
    """A hex crop of rows ``r0..r1`` and columns ``c0..c1`` (inclusive), one row per line."""
    arr = np.asarray(frame)
    if arr.ndim != 2 or arr.size == 0:
        return "(no frame)"
    r0, c0 = max(0, int(r0)), max(0, int(c0))
    r1, c1 = min(arr.shape[0] - 1, int(r1)), min(arr.shape[1] - 1, int(c1))
    lines = ["rows %d-%d cols %d-%d" % (r0, r1, c0, c1)]
    for r in range(r0, r1 + 1):
        lines.append("%2d " % r + "".join("%x" % (int(v) % 16) for v in arr[r, c0 : c1 + 1]))
    return "\n".join(lines)


class HudMask:
    """Per-level learned mask of HUD / meter cells (see the module docstring)."""

    def __init__(
        self,
        *,
        border: int = 3,
        max_line_cells: int = 8,
        max_width: int = 2,
        max_frac: float = 0.10,
        confirm: int = 2,
        redraw_frac: float = 0.30,
    ) -> None:
        self.border = int(border)
        self.max_line_cells = int(max_line_cells)
        self.max_width = int(max_width)
        self.max_frac = float(max_frac)
        self.confirm = int(confirm)
        self.redraw_frac = float(redraw_frac)
        self.mask: Optional[np.ndarray] = None
        self.start: Optional[np.ndarray] = None
        self.refused = 0
        self.lines_masked = 0
        self._seen: Optional[np.ndarray] = None
        self._reverted: Optional[np.ndarray] = None
        self._ticks: Dict[Tuple[int, int], Tuple[int, frozenset]] = {}

    # -- lifecycle -------------------------------------------------------------
    def new_level(self, frame: Any) -> None:
        """A new level (or the first frame): forget the mask, start a new life."""
        arr = np.asarray(frame)
        self.start = arr.copy()
        self.mask = np.zeros(arr.shape, bool) if arr.ndim == 2 else None
        self.new_life(arr)

    def new_life(self, frame: Any) -> None:
        """A death or RESET restarted the level: keep the mask, restart reversion tracking."""
        arr = np.asarray(frame)
        if self.start is None or self.start.shape != arr.shape:
            self.start = arr.copy()
            self.mask = np.zeros(arr.shape, bool) if arr.ndim == 2 else None
        self._seen = np.left_shift(1, np.clip(arr, 0, 15).astype(np.int32)).astype(np.int32)
        self._reverted = np.zeros(arr.shape, bool)
        self._ticks = {}

    # -- learning --------------------------------------------------------------
    def observe(self, before: Any, after: Any, action_id: Optional[int] = None) -> bool:
        """One transition within a life (``action_id`` caused it). True when the mask grew."""
        b, a = np.asarray(before), np.asarray(after)
        if a.ndim != 2 or a.size == 0:
            return False
        if self.start is None or self.start.shape != a.shape or b.shape != a.shape:
            self.new_level(a)
            return False
        assert self._seen is not None and self._reverted is not None
        d = b != a
        if not d.any() or d.sum() > self.redraw_frac * d.size:
            return False
        bit = np.left_shift(1, np.clip(a, 0, 15).astype(np.int32)).astype(np.int32)
        self._reverted |= d & ((self._seen & bit) != 0)
        self._seen[d] |= bit[d]
        rows, cols = a.shape
        add = np.zeros(a.shape, bool)
        ys, xs = np.nonzero(d)
        for axis, lines in ((0, sorted(set(ys.tolist()))), (1, sorted(set(xs.tolist())))):
            n = rows if axis == 0 else cols
            for i in lines:
                sel = (i, slice(None)) if axis == 0 else (slice(None), i)
                cells = d[sel]
                k = int(cells.sum())
                if not 0 < k <= self.max_line_cells:
                    continue
                if (self._reverted[sel] & cells).any():
                    self._ticks.pop((axis, i), None)
                    continue
                if not self._thin(d, axis, i, cells):
                    continue  # a moving piece changes several adjacent lines at once
                run = self._run(axis, i, cells, b[sel], a[sel])
                if run is None:
                    continue
                lo, hi = run
                in_border = i < self.border or i >= n - self.border
                anchored = lo == 0 or hi == (cols if axis == 0 else rows) - 1
                if not (in_border or anchored):
                    continue
                count, ids = self._ticks.get((axis, i), (0, frozenset()))
                if action_id is not None:
                    ids = ids | {int(action_id)}
                self._ticks[(axis, i)] = (count + 1, ids)
                if count + 1 >= self.confirm and (in_border or len(ids) >= 2):
                    if axis == 0:
                        add[i, lo : hi + 1] = True
                    else:
                        add[lo : hi + 1, i] = True
        return self._grow(add)

    def _thin(self, d: np.ndarray, axis: int, i: int, cells: np.ndarray) -> bool:
        """The change on line ``i`` is at most ``max_width`` lines thick."""
        idx = np.nonzero(cells)[0]
        lo, hi = int(idx.min()), int(idx.max())
        n = d.shape[axis]

        def touched(j: int) -> bool:
            if j < 0 or j >= n:
                return False
            seg = d[j, lo : hi + 1] if axis == 0 else d[lo : hi + 1, j]
            return bool(seg.any())

        width = 1
        for step in (1, -1):
            j = i + step
            while touched(j) and width <= self.max_width:
                width += 1
                j += step
        return width <= self.max_width

    def _run(
        self, axis: int, i: int, cells: np.ndarray, before: np.ndarray, after: np.ndarray
    ) -> Optional[Tuple[int, int]]:
        """The same-coloured run of the level's first frame that covers the change."""
        assert self.start is not None
        line0 = self.start[i, :] if axis == 0 else self.start[:, i]
        idx = np.nonzero(cells)[0]
        vals = set(line0[idx].tolist()) | set(before[idx].tolist()) | set(after[idx].tolist())
        lo, hi = int(idx.min()), int(idx.max())
        if not all(int(v) in vals for v in line0[lo : hi + 1].tolist()):
            return None  # the change spans unrelated cells: not one meter
        while lo > 0 and int(line0[lo - 1]) in vals:
            lo -= 1
        while hi < len(line0) - 1 and int(line0[hi + 1]) in vals:
            hi += 1
        return lo, hi

    def _grow(self, add: np.ndarray) -> bool:
        if not add.any() or self.mask is None:
            return False
        if not (add & ~self.mask).any():
            return False
        new = self.mask | add
        if new.sum() > self.max_frac * new.size:
            self.refused += 1
            return False
        self.mask = new
        self.lines_masked += 1
        return True

    # -- use -------------------------------------------------------------------
    def key(self, state: Any) -> str:
        return masked_key(state, self.mask)

    def current(self) -> Optional[np.ndarray]:
        """A copy of the mask, or None when nothing is masked."""
        if self.mask is None or not self.mask.any():
            return None
        return self.mask.copy()

    def stats(self) -> Dict[str, int]:
        return {
            "mask_cells": 0 if self.mask is None else int(self.mask.sum()),
            "lines_masked": self.lines_masked,
            "refused": self.refused,
        }
