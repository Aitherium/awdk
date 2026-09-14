"""MCTS environment protocol.

A domain plugs into the generic engine by implementing
:class:`MCTSEnvironment`. The engine never imports anything domain-specific;
all behavior comes through this interface. Ported from AitherOS'
``UnifiedMCTS`` with zero external couplings.

The protocol is generic over the *state* type ``S`` and the *action* type
``A``. It is :func:`runtime_checkable`, so ``isinstance(env, MCTSEnvironment)``
does a structural check for the five required methods.

Reward contract: :meth:`step` returns ``(observation, reward, done)`` and
:meth:`evaluate` returns a heuristic value in ``[0.0, 1.0]``. Keeping rewards
in this range is what lets the engine blend rollout reward with the leaf
heuristic without re-normalizing.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

S = TypeVar("S")
A = TypeVar("A")


@runtime_checkable
class MCTSEnvironment(Protocol[S, A]):
    """Protocol for domain-specific MCTS environments.

    Any domain (planning, tool selection, games, expeditions) implements
    this interface to plug into the generic engine.
    """

    def get_state_hash(self) -> int:
        """Hash of the current state (for deduplication / transition keys)."""
        ...

    def get_actions(self) -> list[A]:
        """Available actions from the current state."""
        ...

    def step(self, action: A) -> tuple[Any, float, bool]:
        """Execute ``action``. Returns ``(observation, reward, done)``."""
        ...

    def evaluate(self) -> float:
        """Heuristic value of the current state, in ``[0.0, 1.0]``."""
        ...

    def clone(self) -> "MCTSEnvironment[S, A]":
        """Deep copy for simulation branches (must not alias mutable state)."""
        ...


__all__ = ["MCTSEnvironment", "S", "A"]
