"""adk.reasoning.solve -- the general reasoning loop (design: docs/reasoning-loop-design.md).

A model plays an :class:`Environment` by writing Python: each turn it states a
situation, writes ``predict()`` / ``goal()`` hypotheses that must replay over ALL
recorded transitions, and acts with predictions the ledger scores. The core is
the h30 prototype vendored at a pinned sha (``_vendor/``, provenance in
``_provenance.PINNED_SOURCE``); this package adds the ModelBackend bridge,
budgets, cancellation, steering and events.

    from adk.reasoning.solve import solve, LoopConfig, Budget
    result = await solve(env, model, config=LoopConfig(budget=Budget(max_llm_calls=20)))

Importing this package never imports numpy: ``solve`` / ``SolveRun`` /
``SolveLoop`` load on first access (PEP 562), and only building a loop needs the
``reason`` extra. ``InMemory`` / ``FileMemory`` live in
``adk.reasoning.solve.memory``.
"""

from __future__ import annotations

from typing import Any

from ._types import (
    Action,
    Budget,
    Environment,
    Hypothesis,
    LoopConfig,
    Memory,
    Obs,
    SolveResult,
    Strategy,
)

__all__ = [
    "solve",
    "SolveRun",
    "SolveLoop",
    "SolveResult",
    "LoopConfig",
    "Budget",
    "Environment",
    "Obs",
    "Action",
    "Strategy",
    "Hypothesis",
    "Memory",
]

_LAZY = {"solve": "_run", "SolveRun": "_run", "SolveLoop": "_run"}


def __getattr__(name: str) -> Any:
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError("module %r has no attribute %r" % (__name__, name))
    import importlib

    value = getattr(importlib.import_module("." + mod, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list:
    return sorted(set(globals()) | set(__all__))
