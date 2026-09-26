"""Budget accounting and cancellation for one solve run (stdlib only).

The Governor is the single place a run is stopped from outside the core. It is
called from the worker thread only: at the head of each turn and on every traced
line of model code (the vendored ``SEAM(adk)`` hooks), before each ``act()``
(:class:`GovernedEnv`) and before each model call (``_bridge``).

A stop is :class:`Stopped`, a ``BaseException``: the vendored core catches
``Exception`` around model code, model calls and hypothesis replay, and the
sandbox rewrites ``except:`` / ``except BaseException`` in model code to
``except Exception`` -- so nothing between the check and :func:`adk.reasoning.solve.solve`
can swallow it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

from ._types import Action, Budget, Obs

__all__ = ["Stopped", "CancelToken", "Governor", "GovernedEnv"]


class Stopped(BaseException):
    """The run must stop now; ``reason`` becomes ``SolveResult.finish_reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """Thread-safe one-way cancel flag."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self.reason = "cancelled"

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = reason or "cancelled"
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()


def _selftest_break() -> bool:
    """``SOLVE_SELFTEST_BREAK=1`` turns every check into a no-op (the break arm
    the budget tests must fail under)."""
    return os.environ.get("SOLVE_SELFTEST_BREAK", "") == "1"


class Governor:
    """Counts what a run spends and raises :class:`Stopped` past a limit."""

    def __init__(self, budget: Budget, token: Optional[CancelToken] = None) -> None:
        self.budget = budget
        self.token = token or CancelToken()
        self.t0 = time.monotonic()
        self.llm_calls = 0
        self.llm_errors = 0
        self.actions = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_tokens = False
        self._broken = _selftest_break()

    # -- checks ---------------------------------------------------------------
    def check(self) -> None:
        """Cancel, wall clock and token budget. Cheap: runs on traced lines."""
        if self._broken:
            return
        if self.token.cancelled:
            raise Stopped("cancelled")
        b = self.budget
        if b.max_wall_s is not None and time.monotonic() - self.t0 > b.max_wall_s:
            raise Stopped("budget:wall")
        if b.max_tokens is not None and self.prompt_tokens + self.completion_tokens >= b.max_tokens:
            raise Stopped("budget:tokens")

    def before_llm(self) -> None:
        self.check()
        if not self._broken and self.llm_calls >= self.budget.max_llm_calls:
            raise Stopped("budget:llm_calls")

    def before_act(self) -> None:
        self.check()
        if (
            not self._broken
            and self.budget.max_actions is not None
            and self.actions >= self.budget.max_actions
        ):
            raise Stopped("budget:actions")

    # -- accounting -----------------------------------------------------------
    def charge_llm(self, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None:
        self.llm_calls += 1
        self.prompt_tokens += int(prompt_tokens)
        self.completion_tokens += int(completion_tokens)
        self.estimated_tokens = self.estimated_tokens or bool(estimated)

    def charge_action(self) -> None:
        self.actions += 1

    def wall_s(self) -> float:
        return time.monotonic() - self.t0

    def tokens(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt_tokens,
            "completion": self.completion_tokens,
            "total": self.prompt_tokens + self.completion_tokens,
            "estimated": self.estimated_tokens,
        }


class GovernedEnv:
    """An Environment whose ``act`` is charged to a :class:`Governor`.

    Only the four protocol methods are exposed: the loop reads the optional hooks
    from the ORIGINAL environment (``ReasoningLoop(hooks=env)``), so a hook that
    is absent stays absent.
    """

    def __init__(self, env: Any, governor: Governor) -> None:
        self._env = env
        self._gov = governor
        self.last_obs: Optional[Obs] = None
        self.level_actions: List[int] = []

    def observe(self) -> Any:
        obs = self._env.observe()
        self.last_obs = obs
        return obs

    def act(self, action: Action, source: str = "model") -> Any:
        self._gov.before_act()
        obs = self._env.act(action, source=source)
        self._gov.charge_action()
        if not self.level_actions:
            self.level_actions.append(0)
        self.level_actions[-1] += 1
        if getattr(obs, "level_up", False):
            self.level_actions.append(0)
        self.last_obs = obs
        return obs

    def available_actions(self) -> List[int]:
        return self._env.available_actions()

    def done(self) -> bool:
        return self._env.done()
