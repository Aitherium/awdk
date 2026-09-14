"""Optional model seams for the generic MCTS engine.

These three protocols are the *injected* couplings that replace the
AitherOS-specific machinery in the original ``UnifiedMCTS`` (world model,
routing-prior provider, learned value model). All are optional: with every
seam left ``None`` on :class:`~adk.reasoning.mcts.core.MCTSConfig`, the engine
runs the pure heuristic algorithm and behaves identically to the base port.

* :class:`TransitionModel` — model-based stepping. When present, the rollout
  advances state through the model instead of (only) cloning the environment.
  ``step`` MAY be sync or async; the engine's sync rollout path calls it
  synchronously (an async ``step`` is skipped there and falls back to the
  environment), and the async path awaits it.
* :class:`PolicyModel` — action priors for PUCT selection. ``prior`` returns a
  list aligned to ``actions`` that should sum to 1.
* :class:`ValueModel` — leaf value oracle consulted past a depth threshold.
  ``value`` returns a float in ``[0.0, 1.0]`` and may be sync or async.

All protocols are :func:`runtime_checkable`, so the engine can duck-type them
without importing concrete classes.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

S = TypeVar("S")
A = TypeVar("A")


@runtime_checkable
class TransitionModel(Protocol[S, A]):
    """Predicts ``(next_state, reward, done)`` for ``(state, action)``.

    Enables model-based simulation for domains that cannot be cheaply cloned
    (e.g. live games). Implementations may also expose ``record(...)`` to be
    populated online and ``is_uncertain(state, action)`` so callers can bias
    exploration toward unseen transitions — see
    :class:`~adk.reasoning.mcts.adapters.observed_transition.ObservedTransitionModel`.

    ``step`` may be synchronous or a coroutine.
    """

    def step(self, state: S, action: A) -> "tuple[S, float, bool] | Any":
        """Return ``(next_state, reward, done)`` (or an awaitable of it)."""
        ...


@runtime_checkable
class PolicyModel(Protocol[S, A]):
    """Produces action priors for PUCT selection."""

    def prior(self, state: S, actions: list[A]) -> list[float]:
        """Return a prior distribution over ``actions`` (should sum to 1)."""
        ...


@runtime_checkable
class ValueModel(Protocol[S]):
    """Estimates the value of a state, in ``[0.0, 1.0]``.

    ``value`` may be synchronous or a coroutine.
    """

    def value(self, state: S) -> float:
        """Return the estimated value of ``state`` in ``[0.0, 1.0]``."""
        ...


__all__ = ["TransitionModel", "PolicyModel", "ValueModel", "S", "A"]
