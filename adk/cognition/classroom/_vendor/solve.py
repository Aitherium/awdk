# vendored from aither-kaggle-agent@a5cd9b6532eb039dd5b7c27633293dfd55672a9c:agent/synth/solve.py -- import path only (see _provenance.py)
"""Breadth-first optimal solver for one synthetic level.

Runs the SAME transition function the generated game embeds (``rules.apply``),
so the length it returns is the true minimum number of counted actions from the
level's start state to a win, never passing through a losing state.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from . import rules

Action = Tuple[int, Optional[Tuple[int, int]]]


class SearchLimit(Exception):
    """The state space exceeded ``max_states`` before a solution was found."""


def bfs(level: Dict[str, Any], max_states: int = 400_000) -> Optional[List[Action]]:
    """Shortest winning action sequence, ``[]`` if the start already wins,
    ``None`` if no win is reachable. Raises :class:`SearchLimit` past ``max_states``."""
    P = rules.prep(level)
    start = rules.initial_state(P)
    if rules.is_win(P, start):
        return []
    acts = rules.board_actions(P)
    parent: Dict[Any, Tuple[Any, Action]] = {start: (None, (0, None))}
    q = deque([start])
    while q:
        s = q.popleft()
        for a, cell in acts:
            n, status = rules.apply(P, s, a, cell)
            if status == rules.SY_LOSE or n in parent:
                continue
            parent[n] = (s, (a, cell))
            if status == rules.SY_WIN:
                path: List[Action] = []
                cur = n
                while cur != start:
                    prev, act = parent[cur]
                    path.append(act)
                    cur = prev
                return path[::-1]
            if len(parent) > max_states:
                raise SearchLimit(len(parent))
            q.append(n)
    return None


def optimal_length(level: Dict[str, Any], max_states: int = 400_000) -> Optional[int]:
    path = bfs(level, max_states)
    return None if path is None else len(path)


def replay(level: Dict[str, Any], path: List[Action]) -> int:
    """Play ``path`` through the rules; the final status (for tests)."""
    P = rules.prep(level)
    s = rules.initial_state(P)
    status = rules.SY_PLAY
    for a, cell in path:
        s, status = rules.apply(P, s, a, cell)
        if status != rules.SY_PLAY:
            break
    return status
