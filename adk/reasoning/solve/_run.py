"""``solve()``, ``SolveRun`` and ``SolveLoop``: the async surface over the sync core.

The vendored :class:`~adk.reasoning.solve._vendor.loop.ReasoningLoop` is
synchronous (it ``exec``s model code and blocks on ``env.act``). A
:class:`SolveRun` runs it on a dedicated worker thread and bridges back to the
caller's event loop for three things only:

* model calls (``_bridge.SyncModel`` -> ``run_coroutine_threadsafe``);
* events, published with ``call_soon_threadsafe(session.emit, ...)``;
* the final :class:`SolveResult`, delivered to an ``asyncio.Future``.

Steering and cancel go through the :class:`adk.reasoning_session.ReasoningSession`
(``steer`` queues a message that the next model call drains; ``cancel`` sets the
Governor's token, noticed at the next act / model call / traced line).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import threading
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from ._control import CancelToken, GovernedEnv, Governor, Stopped
from ._types import Hypothesis, LoopConfig, SolveResult

_log = logging.getLogger(__name__)

__all__ = ["solve", "SolveRun", "SolveLoop", "build_core_loop"]

_STATUS = {
    "verified": "active",
    "consistent": "pending",
    "unsupported": "pending",
    "refuted": "refuted",
}


class _Hooks:
    """The environment's optional hooks, with the goal text added to ``primer``."""

    def __init__(self, env: Any, goal: str) -> None:
        self._env = env
        self._goal = goal

    def __getattr__(self, name: str) -> Any:
        if name == "primer" and self._goal:
            base = getattr(self._env, "primer", None)

            def primer() -> str:
                head = str(base() or "") if base is not None else ""
                return (head + "\n" if head else "") + "GOAL: " + self._goal

            return primer
        return getattr(self._env, name)


def build_core_loop(
    env: Any,
    llm: Any,
    config: Optional[LoopConfig] = None,
    *,
    memory: Any = None,
    goal: str = "",
    episode_id: str = "episode",
    governor: Optional[Governor] = None,
    sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Any:
    """A vendored ``ReasoningLoop`` configured from a public :class:`LoopConfig`.

    ``llm`` is anything with the h30 ``chat`` protocol (see ``_bridge.as_llm``).
    ``env`` is used as-is for acting; hooks are read from ``env`` (plus ``goal``).
    """
    from ._vendor.loop import LoopConfig as CoreConfig
    from ._vendor.loop import ReasoningLoop
    from ._vendor.prism import BY_ID

    cfg = config or LoopConfig()
    log_path = None
    if cfg.run_dir:
        log_path = os.path.join(cfg.run_dir, "%s.jsonl" % episode_id)
    core = CoreConfig(
        mode="sase" if cfg.sase else "plain",
        prism=cfg.prism,
        max_calls=cfg.budget.max_llm_calls,
        turn_s=cfg.budget.turn_s,
        max_out=cfg.max_tokens,
        wm_tokens=cfg.wm_tokens,
        temperature=cfg.temperature,
        log_path=log_path,
        grounded=cfg.grounded,
    )
    if cfg.core:
        core = dataclasses.replace(core, **cfg.core)
    raw = getattr(env, "_env", env)
    hooks = _Hooks(raw, goal) if goal else raw
    if cfg.planning:
        from ._daydream import PlanningLoop

        loop = PlanningLoop(
            env,
            llm,
            core,
            backend=memory,
            hooks=hooks,
            episode_id=episode_id,
            sink=sink,
            governor=governor,
            daydream=cfg.daydream,
            daydream_s=cfg.daydream_s,
            plan_states=cfg.plan_states,
        )
    else:
        loop = ReasoningLoop(
            env,
            llm,
            core,
            backend=memory,
            hooks=hooks,
            episode_id=episode_id,
            sink=sink,
            governor=governor,
        )
    if cfg.strategies and loop.prism is not None:
        unknown = [s for s in cfg.strategies if s not in BY_ID]
        if unknown:
            raise ValueError("unknown strategies %s; known: %s" % (unknown, sorted(BY_ID)))
        loop.prism.allowed = [s for s in loop.prism.allowed if s in cfg.strategies]
        first = loop.prism.best(initial=True)
        if first is not None:
            loop.prism.active = BY_ID[first]
    return loop


def _hypotheses(loop: Any) -> List[Hypothesis]:
    out: List[Hypothesis] = []
    for h in list(loop.hyps.active.values()) + [h for h in loop.hyps.refuted if h.kind != "claim"]:
        key = (h.source or h.name).encode("utf-8")
        out.append(
            Hypothesis(
                id=hashlib.sha256(key).hexdigest()[:16],
                name=h.name,
                kind=h.kind,
                source=h.source,
                support=int(h.support),
                status=_STATUS.get(h.status, "pending"),
                detail=h.status,
            )
        )
    return out


def _won(env: Any, genv: GovernedEnv) -> bool:
    obs = genv.last_obs
    info = getattr(obs, "info", None) or {}
    if "won" in info:
        return bool(info["won"])
    try:
        done = bool(env.done())
    except Exception:  # noqa: BLE001
        done = False
    return done and bool(getattr(obs, "level_up", False))


def _run_sync(
    env: Any,
    llm: Any,
    cfg: LoopConfig,
    *,
    memory: Any,
    goal: str,
    episode_id: str,
    governor: Governor,
    sink: Optional[Callable[[Dict[str, Any]], None]],
) -> SolveResult:
    """Build and play one episode on THIS thread; never raises."""
    genv = GovernedEnv(env, governor)
    loop: Any = None
    reason: Optional[str] = None
    error: Optional[str] = None
    trace: List[str] = []

    def _sink(rec: Dict[str, Any]) -> None:
        if rec.get("event") == "rotate":
            trace.append(str(rec.get("to")))
        if sink is not None:
            sink(rec)

    try:
        if memory is None and cfg.run_dir:
            from .memory import FileMemory

            memory = FileMemory(os.path.join(cfg.run_dir, "memory"))
        loop = build_core_loop(
            genv,
            llm,
            cfg,
            memory=memory,
            goal=goal,
            episode_id=episode_id,
            governor=governor,
            sink=_sink,
        )
        if memory is not None:
            from .hypotheses import load_refuted

            loop.stats["hyps_refuted_loaded"] = load_refuted(loop.hyps, memory)
        if loop.prism is not None:
            trace.append(loop.prism.active.id)
        loop.run()
    except Stopped as s:  # the core's own ``finally`` already saved PRISM scores
        reason = s.reason
    except Exception as exc:  # noqa: BLE001 - reported as finish_reason "error"
        reason, error = "error", "%s: %s" % (type(exc).__name__, str(exc)[:300])
    finally:
        if loop is not None:
            loop.close_log()
            if memory is not None:
                try:  # semantic memory outlives the run; a failed save is reported, not raised
                    from .hypotheses import save_hypotheses

                    save_hypotheses(loop.hyps, memory, episode_id)
                except Exception as exc:  # noqa: BLE001
                    loop.stats["hyps_save_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])

    stats = loop.summary() if loop is not None else {}
    won = reason is None and _won(env, genv)
    if reason is None:
        if won:
            reason = "won"
        elif _safe_done(env):
            reason = "done"
        elif stats.get("llm_errors", 0) and (loop.endpoint_dead or stats.get("llm_calls", 0) == 0):
            reason = "llm_error"
        elif stats.get("llm_calls", 0) >= cfg.budget.max_llm_calls:
            reason = "budget:llm_calls"
        else:
            reason = "done"
    level_actions = list(genv.level_actions)
    if level_actions and level_actions[-1] == 0:
        level_actions.pop()
    lg = loop.learner.ledger if loop is not None else None
    return SolveResult(
        finish_reason=reason,
        won=won,
        levels=int(stats.get("levels", 0)),
        level_actions=level_actions,
        actions=governor.actions,
        turns=int(stats.get("turns", 0)),
        llm_calls=int(stats.get("llm_calls", 0)),
        tokens=governor.tokens(),
        wall_s=round(governor.wall_s(), 3),
        hypotheses=_hypotheses(loop) if loop is not None else [],
        calibration=(lg.calibration() if lg is not None and lg.made else None),
        strategy_trace=trace,
        log_path=getattr(loop.cfg, "log_path", None) if loop else None,
        error=error,
        stats=stats,
    )


def _safe_done(env: Any) -> bool:
    try:
        return bool(env.done())
    except Exception:  # noqa: BLE001
        return False


class SolveRun:
    """One steerable, cancellable run. ``start()`` then ``await join()``."""

    def __init__(
        self,
        env: Any,
        model: Any,
        *,
        goal: str = "",
        config: Optional[LoopConfig] = None,
        memory: Any = None,
        session: Any = None,
    ) -> None:
        self.env = env
        self.model = model
        self.goal = goal
        self.config = config or LoopConfig()
        self.memory = memory
        if session is None:
            from adk.reasoning_session import get_session_manager

            self.run_id = "solve-" + uuid.uuid4().hex[:12]
            session = get_session_manager().get_or_create(self.run_id)
        else:
            self.run_id = str(getattr(session, "conversation_id", "solve-" + uuid.uuid4().hex[:12]))
        self.session = session
        self.token = CancelToken()
        self.governor = Governor(self.config.budget, self.token)
        self.result: Optional[SolveResult] = None
        self.thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._done: Optional[asyncio.Future] = None
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> "asyncio.Task[SolveResult]":
        if self._task is not None:
            return self._task
        self._loop = asyncio.get_running_loop()
        self._done = self._loop.create_future()
        self.session.mark_running()
        self._emit(
            {
                "event": "start",
                "run_id": self.run_id,
                "budget": dataclasses.asdict(self.config.budget),
                "sase": self.config.sase,
            }
        )
        self.thread = threading.Thread(
            target=self._worker, name="solve-" + self.run_id, daemon=True
        )
        self.thread.start()
        self._task = self._loop.create_task(self._await())
        return self._task

    async def _await(self) -> SolveResult:
        assert self._done is not None
        return await asyncio.shield(self._done)

    def _worker(self) -> None:
        from ._bridge import as_llm

        llm = as_llm(
            self.model,
            loop=self._loop,
            governor=self.governor,
            timeout_s=self.config.budget.llm_timeout_s,
            steering=self.session.pop_steering,
            on_steer=lambda m: self.session.emit({"event": "steer", "message": m}),
        )
        res = _run_sync(
            self.env,
            llm,
            self.config,
            memory=self.memory,
            goal=self.goal,
            episode_id=self.run_id,
            governor=self.governor,
            sink=self._emit,
        )
        self.result = res
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._finish, res)
            except RuntimeError:
                _log.debug("caller loop closed before the result could be posted", exc_info=True)

    def _finish(self, res: SolveResult) -> None:
        self.session.emit(
            {
                "event": "finish",
                "finish_reason": res.finish_reason,
                "won": res.won,
                "levels": res.levels,
                "actions": res.actions,
                "turns": res.turns,
                "llm_calls": res.llm_calls,
            }
        )
        self.session.result = res
        if res.finish_reason == "cancelled":
            self.session.status = "cancelled"
        else:
            self.session.mark_done(error=res.error if res.finish_reason == "error" else None)
        if self._done is not None and not self._done.done():
            self._done.set_result(res)

    def _emit(self, rec: Dict[str, Any]) -> None:
        loop = self._loop
        ev = json.loads(json.dumps(rec, default=str))
        if loop is None:
            return
        if _running_loop() is loop:  # already on the caller's loop
            self.session.emit(ev)
            return
        try:
            loop.call_soon_threadsafe(self.session.emit, ev)
        except RuntimeError:  # the caller's loop is closed
            _log.debug("caller loop closed; event dropped", exc_info=True)

    # -- control ---------------------------------------------------------------
    def steer(self, message: str) -> bool:
        """Queue a message for the next model call. Call on the caller's loop."""
        return bool(self.session.steer(message))

    def cancel(self, reason: str = "cancelled") -> None:
        """Stop at the next check; ``join()`` still returns a SolveResult."""
        self.token.cancel(reason)

    async def join(self, timeout: Optional[float] = None) -> SolveResult:
        task = self.start() if self._task is None else self._task
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    def events(self) -> AsyncIterator[Dict[str, Any]]:
        return self.session.observe(backfill=True)

    def snapshot(self) -> Dict[str, Any]:
        g = self.governor
        return {
            "run_id": self.run_id,
            "running": bool(self.thread and self.thread.is_alive()),
            "actions": g.actions,
            "llm_calls": g.llm_calls,
            "tokens": g.tokens(),
            "wall_s": round(g.wall_s(), 3),
            "cancelled": self.token.cancelled,
            "finish_reason": self.result.finish_reason if self.result else None,
        }


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


async def solve(
    env: Any,
    model: Any,
    *,
    goal: str = "",
    config: Optional[LoopConfig] = None,
    memory: Any = None,
    session: Any = None,
) -> SolveResult:
    """Play ``env`` with ``model`` (an ``adk.core.model.ModelBackend``) until it is
    won, stopped or out of budget. Model calls run on THIS event loop."""
    run = SolveRun(env, model, goal=goal, config=config, memory=memory, session=session)
    run.start()
    return await run.join()


class SolveLoop:
    """:class:`adk.core.agent.AgentLoop` that runs ``solve`` on an environment.

    ``env`` is an Environment, or a callable ``prompt -> Environment``. The model
    is ``model=`` or the agent's. ``AgentResult.output`` is the SolveResult as JSON.
    """

    def __init__(
        self,
        env: Any,
        *,
        config: Optional[LoopConfig] = None,
        memory: Any = None,
        model: Any = None,
    ) -> None:
        self.env = env
        self.config = config
        self.memory = memory
        self.model = model

    async def run(self, agent: Any, prompt: str) -> Any:
        from adk.core.agent import AgentResult

        env = self.env
        if callable(env) and not hasattr(env, "act"):
            env = env(prompt)
        model = self.model or getattr(agent, "model", None)
        res = await solve(env, model, goal=prompt or "", config=self.config, memory=self.memory)
        body = dataclasses.asdict(res)
        body.pop("stats", None)
        return AgentResult(
            output=json.dumps(body, default=str),
            messages=[],
            tool_calls=[],
            steps=res.turns,
            finish_reason=res.finish_reason,
        )
