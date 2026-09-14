"""Observed transition model.

A tabular :class:`~adk.reasoning.mcts.models.TransitionModel` populated online
from real environment steps. Ported from the tabular half of AitherOS'
``WorldModel`` (the neural / SurpriseDetector / LatentPredictor couplings are
dropped) and reshaped to the ``TransitionModel`` seam contract.

Behaviour:

* :meth:`record` stores the exact observed transition, keyed by
  ``(state_hash, action)``. A repeat observation overwrites (last-write-wins),
  keeping the map a single deterministic prediction per key.
* :meth:`step` — **HIT** returns the stored ``(next, reward, done)``. **MISS**
  returns the *identity* transition (``next == state``) with a small negative
  ``unknown_reward`` and ``done=False``, so an unseen transition looks mildly
  bad and does not spuriously terminate a rollout.
* :meth:`is_uncertain` reports whether ``(state, action)`` is unseen, so a
  caller can bias exploration toward learning unknown transitions.
* :meth:`save` / :meth:`load` persist the table as JSONL for cross-run reuse.

The engine calls ``step`` with a state **hash** as the ``state`` argument, so
``S`` here is ``int``; ``next`` values are whatever was recorded (typically the
next state's hash).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple


def _action_key(action: Any) -> Any:
    """Make an action hashable for dict keys (mirrors the engine helper)."""
    if isinstance(action, (str, int, float, tuple, bool, type(None))):
        return action
    try:
        hash(action)
        return action
    except TypeError:
        return str(action)


def _encode_key(k: Any) -> Any:
    """Encode a hashable key part into a JSON-round-trippable form.

    Tuples (e.g. ``('ACTION6', 31, 31)``) become ``{"__tuple__": [...]}`` so
    :func:`_decode_key` can rebuild the exact tuple — a plain JSON list would
    come back unhashable and never match the live tuple-keyed lookup. Primitives
    (str/int/float/bool/None) round-trip unchanged.
    """
    if isinstance(k, tuple):
        return {"__tuple__": [_encode_key(x) for x in k]}
    return k


def _decode_key(v: Any) -> Any:
    """Inverse of :func:`_encode_key` — rebuild tuples, pass primitives through."""
    if isinstance(v, dict) and "__tuple__" in v:
        return tuple(_decode_key(x) for x in v["__tuple__"])
    return v


class ObservedTransitionModel:
    """Online tabular transition model over ``(state_hash, action)`` keys."""

    def __init__(self, unknown_reward: float = -0.05) -> None:
        # (state_hash, action_key) -> (next, reward, done)
        self._t: Dict[Tuple[Any, Any], Tuple[Any, float, bool]] = {}
        self.unknown_reward = float(unknown_reward)

    # -- population -------------------------------------------------------

    def record(
        self,
        state_hash: Any,
        action: Any,
        next: Any,  # noqa: A002 — matches the seam's record() contract
        reward: float,
        done: bool,
    ) -> None:
        """Store an observed transition (last-write-wins per key)."""
        self._t[(state_hash, _action_key(action))] = (next, float(reward), bool(done))

    # -- TransitionModel seam --------------------------------------------

    def step(self, state: Any, action: Any) -> Tuple[Any, float, bool]:
        """HIT -> stored transition; MISS -> identity + negative unknown reward."""
        hit = self._t.get((state, _action_key(action)))
        if hit is not None:
            return hit
        return (state, self.unknown_reward, False)

    def is_uncertain(self, state_hash: Any, action: Any) -> bool:
        """True when ``(state, action)`` has never been observed."""
        return (state_hash, _action_key(action)) not in self._t

    def has(self, state_hash: Any, action: Any) -> bool:
        """True when a transition is recorded for ``(state, action)``."""
        return (state_hash, _action_key(action)) in self._t

    def __len__(self) -> int:
        return len(self._t)

    # -- persistence ------------------------------------------------------

    def save(self, path: str) -> None:
        """Serialize the table to JSONL at ``path`` (keys recoverably encoded)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(str(p), "w", encoding="utf-8") as f:
            for (state_hash, action_key), (nxt, reward, done) in self._t.items():
                try:
                    line = json.dumps(
                        {
                            "state_hash": state_hash,
                            "action": _encode_key(action_key),
                            "next": nxt,
                            "reward": reward,
                            "done": done,
                        }
                    )
                except (TypeError, ValueError):
                    continue  # skip a transition whose next/state isn't JSON-safe
                f.write(line + "\n")

    @classmethod
    def load(cls, path: str, unknown_reward: float = -0.05) -> "ObservedTransitionModel":
        """Load a table from JSONL (missing file => empty model)."""
        model = cls(unknown_reward=unknown_reward)
        p = Path(path)
        if not p.exists():
            return model
        try:
            with open(str(p), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    # "action" is the recoverable encoding; tolerate the legacy
                    # "action_key" field for any older file.
                    raw_action = data["action"] if "action" in data else data.get("action_key")
                    key = (data["state_hash"], _decode_key(raw_action))
                    model._t[key] = (
                        data["next"],
                        float(data["reward"]),
                        bool(data["done"]),
                    )
        except Exception:
            pass  # best-effort load; start from whatever parsed
        return model


__all__ = ["ObservedTransitionModel"]
