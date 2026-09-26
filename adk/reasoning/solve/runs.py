"""Hosted solve runs: start, look up, steer, cancel -- for a long-lived server.

A server (Genesis, the adk daemon) holds many :class:`~adk.reasoning.solve.SolveRun`
objects at once and addresses them by id from later requests. This module is that
bookkeeping, kept here so it is tested once and every host behaves the same:

* **Environments by id.** A host starts runs on a NAMED environment from
  :data:`ENVIRONMENTS` (``toy:counter``, ``toy:gridwalk``, ``debug:demo``) or one
  it registered with :func:`register_environment`. There is deliberately no way to
  pass a source tree or code through this API: the sandbox is a guard, not an
  isolation boundary (design risk 3), so a host exposes only environments it chose.
* **Always bounded.** :func:`bounded_budget` clamps every requested limit to the
  host's :class:`RunLimits` and always sets a wall-clock limit, so no run is
  unbounded whatever the caller asked for. ``max_concurrent`` caps live runs.
* **Steering and cancel** go through the run's
  :class:`adk.reasoning_session.ReasoningSession` (``run_id`` is its conversation id).
  State is per PROCESS: a run id unknown here is ``None`` / ``False``, and a host
  must answer 404 for it, never 200.

Importing this module does not import numpy; starting a run does.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from ._types import Budget, LoopConfig

_log = logging.getLogger(__name__)

__all__ = [
    "ENVIRONMENTS",
    "RunLimits",
    "RunCapacityError",
    "SolveRunRegistry",
    "bounded_budget",
    "get_run_registry",
    "make_environment",
    "register_environment",
]


def _toy_counter() -> Any:
    from .envs.toy import Counter1D

    return Counter1D()


def _toy_gridwalk() -> Any:
    from .envs.toy import GridWalk5

    return GridWalk5()


def _debug_demo() -> Any:
    from .envs.debug import FailingTestEnv

    return FailingTestEnv.demo()


#: Environment id -> zero-argument factory. Hosts add their own with
#: :func:`register_environment`; callers can only name, never supply, an environment.
ENVIRONMENTS: Dict[str, Callable[[], Any]] = {
    "toy:counter": _toy_counter,
    "toy:gridwalk": _toy_gridwalk,
    "debug:demo": _debug_demo,
}


def register_environment(env_id: str, factory: Callable[[], Any]) -> None:
    if not env_id or not callable(factory):
        raise ValueError("register_environment needs an id and a zero-argument factory")
    ENVIRONMENTS[env_id] = factory


def make_environment(env_id: str) -> Any:
    try:
        factory = ENVIRONMENTS[env_id]
    except KeyError:
        raise KeyError(
            "unknown environment %r; known: %s" % (env_id, sorted(ENVIRONMENTS))
        ) from None
    return factory()


class RunCapacityError(RuntimeError):
    """``max_concurrent`` runs are already live."""


@dataclass(frozen=True)
class RunLimits:
    """The host's ceiling on any one run, and on how many run at once."""

    max_llm_calls: int = 100
    max_actions: int = 500
    max_wall_s: float = 1800.0
    max_tokens: Optional[int] = None
    default_wall_s: float = 600.0
    max_concurrent: int = 4
    keep_finished: int = 64


def bounded_budget(requested: Optional[Mapping[str, Any]], limits: RunLimits) -> Budget:
    """A :class:`Budget` from a request dict, every limit clamped to ``limits``.

    Unknown keys are refused (a typo must not silently mean "unbounded")."""
    req = dict(requested or {})
    known = {f.name for f in dataclasses.fields(Budget)}
    bad = sorted(set(req) - known)
    if bad:
        raise ValueError("unknown budget field(s) %s; known: %s" % (bad, sorted(known)))
    base = Budget()

    def _clamp(name: str, cap: Optional[float], default: Any, cast: Callable[[Any], Any]) -> Any:
        v = req.get(name)
        v = default if v is None else cast(v)
        if v is not None and v <= 0:
            raise ValueError("budget.%s must be positive" % name)
        if cap is None:
            return v
        return cap if v is None else min(v, cap)

    return Budget(
        max_llm_calls=_clamp("max_llm_calls", limits.max_llm_calls, base.max_llm_calls, int),
        max_actions=_clamp("max_actions", limits.max_actions, limits.max_actions, int),
        max_wall_s=_clamp("max_wall_s", limits.max_wall_s, limits.default_wall_s, float),
        max_tokens=_clamp("max_tokens", limits.max_tokens, None, int),
        turn_s=_clamp("turn_s", 60.0, base.turn_s, float),
        llm_timeout_s=_clamp("llm_timeout_s", 300.0, base.llm_timeout_s, float),
    )


@dataclass
class _Entry:
    run: Any
    env: Any
    env_id: str
    owner: str
    goal: str
    created: float


class SolveRunRegistry:
    """The live and recently finished runs of THIS process."""

    def __init__(self, limits: Optional[RunLimits] = None) -> None:
        self.limits = limits or RunLimits()
        self._runs: Dict[str, _Entry] = {}

    # -- start -------------------------------------------------------------------
    def start(
        self,
        env: Any,
        model: Any,
        *,
        env_id: str = "",
        goal: str = "",
        budget: Optional[Mapping[str, Any]] = None,
        sase: bool = True,
        owner: str = "",
    ) -> Any:
        """Start a :class:`SolveRun` on the running event loop; returns it.

        ``env`` is an environment id (looked up in :data:`ENVIRONMENTS`) or an
        environment object the HOST built. Raises ``KeyError`` for an unknown id,
        ``ValueError`` for a bad budget, :class:`RunCapacityError` when full."""
        from ._run import SolveRun

        if self.live_count() >= self.limits.max_concurrent:
            raise RunCapacityError(
                "%d solve runs already live (limit %d)"
                % (self.live_count(), self.limits.max_concurrent)
            )
        cfg = LoopConfig(budget=bounded_budget(budget, self.limits), sase=bool(sase))
        if isinstance(env, str):
            env_id = env_id or env
            env = make_environment(env)
        run = SolveRun(env, model, goal=goal, config=cfg)
        self._runs[run.run_id] = _Entry(
            run=run,
            env=env,
            env_id=env_id or type(env).__name__,
            owner=owner,
            goal=goal,
            created=time.time(),
        )
        task = run.start()
        task.add_done_callback(lambda _t, e=env: _close(e))
        self._prune()
        return run

    # -- lookup / control ----------------------------------------------------------
    def get(self, run_id: str) -> Optional[Any]:
        e = self._runs.get(run_id)
        return e.run if e is not None else None

    def owner(self, run_id: str) -> Optional[str]:
        e = self._runs.get(run_id)
        return e.owner if e is not None else None

    def steer(self, run_id: str, message: str) -> bool:
        run = self.get(run_id)
        if run is None or run.result is not None:
            return False
        return run.steer(message)

    def cancel(self, run_id: str, reason: str = "cancelled") -> bool:
        run = self.get(run_id)
        if run is None:
            return False
        run.cancel(reason)
        return True

    def snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        e = self._runs.get(run_id)
        if e is None:
            return None
        run = e.run
        snap = run.snapshot()
        snap.update(
            {
                "env_id": e.env_id,
                "owner": e.owner,
                "goal": e.goal,
                "status": _status(run),
                "budget": dataclasses.asdict(run.config.budget),
            }
        )
        if run.result is not None:
            body = dataclasses.asdict(run.result)
            body.pop("stats", None)
            body["exit_code"] = run.result.exit_code
            snap["result"] = body
        return snap

    def list(self) -> List[Dict[str, Any]]:
        return [s for s in (self.snapshot(r) for r in list(self._runs)) if s is not None]

    def live_count(self) -> int:
        return sum(1 for e in self._runs.values() if e.run.result is None)

    def _prune(self) -> None:
        done = [
            k
            for k, e in sorted(self._runs.items(), key=lambda kv: kv[1].created)
            if e.run.result is not None
        ]
        for k in done[: max(0, len(done) - self.limits.keep_finished)]:
            self._runs.pop(k, None)


def _status(run: Any) -> str:
    if run.result is None:
        return "running"
    if run.result.finish_reason == "cancelled":
        return "cancelled"
    if run.result.finish_reason == "error":
        return "failed"
    return "completed"


def _close(env: Any) -> None:
    close = getattr(env, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the run's result
            _log.debug("env.close() failed; the run result stands", exc_info=True)


_REGISTRY: Optional[SolveRunRegistry] = None


def get_run_registry() -> SolveRunRegistry:
    """The process-wide registry (created with default :class:`RunLimits`)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SolveRunRegistry()
    return _REGISTRY
