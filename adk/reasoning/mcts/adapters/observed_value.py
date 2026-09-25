"""Observed value model.

A tabular :class:`~adk.reasoning.mcts.models.ValueModel` learned online from
the engine's OWN backed-up search values -- the value half of the search
distillation flywheel, without any network or neural dependency.

* :meth:`learn_from_result` walks a finished search tree and records, for every
  visited node, the state value it implies. The engine backs up
  ``Q(node) = edge_reward + discount * V(node's state)``, so the state value is
  ``(avg_value - reward) / discount`` for a non-root node and ``avg_value`` for
  the root. Observations are visit-weighted running means per ``state_hash``.
* :meth:`value` returns the learned mean for a state seen at least
  ``min_visits`` times, else ``None`` -- the engine treats ``None`` as "no
  opinion" and falls back to its rollout, so an untrained model is exactly the
  base algorithm.
* :meth:`save` / :meth:`load` persist the table as JSONL for cross-run reuse.

The seam contract is a value in ``[0, 1]``; the engine clamps whatever this
returns, so domains whose returns exceed 1 should rescale ``evaluate`` rewards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class ObservedValueModel:
    """Online visit-weighted mean of backed-up state values, keyed by state hash."""

    def __init__(self, *, min_visits: int = 2) -> None:
        # state_hash -> (weighted value sum, total weight)
        self._t: Dict[Any, Tuple[float, float]] = {}
        self.min_visits = max(1, int(min_visits))

    def __len__(self) -> int:
        return len(self._t)

    # -- population -------------------------------------------------------

    def record(self, state_hash: Any, value: float, weight: float = 1.0) -> None:
        """Fold one observed state value into the running mean."""
        w = float(weight)
        if w <= 0:
            return
        s, n = self._t.get(state_hash, (0.0, 0.0))
        self._t[state_hash] = (s + float(value) * w, n + w)

    def learn_from_result(self, result: Any, discount: float = 1.0) -> int:
        """Record every visited node of ``result.root``; returns nodes learned.

        ``discount`` must be the ``MCTSConfig.discount`` the search ran with.
        """
        root = getattr(result, "root", None)
        if root is None:
            return 0
        gamma = float(discount) if discount else 1.0
        learned = 0
        stack = [root]
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.visits <= 0:
                continue
            q = node.value_sum / node.visits
            v = q if node.parent is None else (q - float(node.reward)) / gamma
            self.record(node.state_hash, v, weight=node.visits)
            learned += 1
        return learned

    # -- seam -------------------------------------------------------------

    def estimate(self, state_hash: Any) -> Optional[float]:
        """Learned value for ``state_hash``, or None below ``min_visits``."""
        entry = self._t.get(state_hash)
        if entry is None or entry[1] < self.min_visits:
            return None
        return entry[0] / entry[1]

    def value(self, state: Any) -> Optional[float]:
        """ValueModel seam: the engine passes the environment at the leaf."""
        get_hash = getattr(state, "get_state_hash", None)
        key = get_hash() if callable(get_hash) else state
        return self.estimate(key)

    # -- persistence ------------------------------------------------------

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for k, (s, n) in self._t.items():
                fh.write(json.dumps({"state": k, "sum": s, "weight": n}) + "\n")

    def load(self, path: str | Path) -> int:
        p = Path(path)
        if not p.exists():
            return 0
        count = 0
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    self.record(row["state"], row["sum"] / row["weight"], row["weight"])
                    count += 1
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    continue
        return count


__all__ = ["ObservedValueModel"]
