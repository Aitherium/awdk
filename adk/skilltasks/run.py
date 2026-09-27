"""Run the reasoning loop on one compiled skill task with a LOCAL model.

    python -m adk.skilltasks run <task_dir> --model gemma4-12b [--max-calls 12]

Needs the ``reason`` extra (``adk.reasoning.solve``) and the MicroScheduler backend;
without them it exits 2 (could not run), never 0.

Served-model assertion, three layers:
1. the requested model must be in :data:`LOCAL_MODELS` -- a cloud name is refused
   before any call is made;
2. the backend sends ``metadata.local_only`` and refuses any reply whose route says a
   different model served it (``CrossModelRouteError``);
3. after the run, ``cross_model_replies`` must be 0 and every call must have been
   answered, or the result is reported as not judged (exit 2).

The score is the frozen verifier on the final workspace (``SkillTaskEnv.final``), not
the loop's own ``won`` flag.

Modes: ``plain`` / ``sase`` play :class:`~adk.skilltasks.env.SkillTaskEnv` (one SUBMIT
action, free tools). ``terminal-plain`` / ``terminal-sase`` play
:class:`~adk.skilltasks.terminal.SkillTaskTerminalEnv`, where every command, read, write,
test run and change hypothesis is a booked action behind ``permits()``. In the terminal
modes a fourth served-model layer checks EVERY reply: the model that answered must equal
the one requested, or the call fails (``CrossModelRouteError``) and is counted.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .env import SkillTaskEnv

__all__ = ["LOCAL_MODELS", "run_task", "RunRefused"]

#: Locally served models. Anything else is refused before a call is made.
LOCAL_MODELS = (
    "gemma4-12b", "gemma4-reasoning", "gemma4-e4b",
    "deepseek-v4-flash-pool", "pool-284b", "v4-flash-pool",
    "bonsai2-27b", "bonsai2-27b-5090", "bonsai2-5090", "bonsai2", "bonsai2-fast",
    "ternary-bonsai-2-27b",
)


class RunRefused(RuntimeError):
    pass


def _goal(env: Any) -> str:
    if hasattr(env, "try_change"):
        return ("Make the working directory satisfy the task and its Must-do tests, keep every "
                "Must-avoid, then submit().")
    return ("Make the working directory satisfy the task, then act(1). Use sh()/read()/write(); "
            "there is no grid.")


def _strict_backend(scheduler_url: Optional[str], model: str) -> Any:
    """A MicroScheduler backend that asserts on EVERY reply that the served model is the
    requested one (the base class only checks when a route record is present)."""
    from adk.core.backends.microscheduler import (
        CrossModelRouteError,
        MicroSchedulerBackend,
        SchedulerUnavailableError,
    )

    class _AssertServed(MicroSchedulerBackend):
        served_checked = 0
        served_mismatch = 0
        queue_waits = 0
        queue_wait_s = 0.0

        def chat(self, messages: Any, max_tokens: int = 1000, temperature: float = 0.4,
                 extra: Optional[Dict[str, Any]] = None) -> Any:
            # A single-slot pool busy with another run (the ARC eval) answers 502 "Backend
            # ... timed out" before our turn comes, and a restarting scheduler refuses the
            # connection: both are a QUEUE, not a dead endpoint. Wait and retry (never
            # counted as a call) for up to QUEUE_WAIT_S.
            t0 = time.time()
            while True:
                t_try = time.time()
                try:
                    reply = super().chat(messages, max_tokens, temperature, extra)
                    break
                except SchedulerUnavailableError as exc:
                    transient = _transient(exc)
                    if not transient or time.time() - t0 > QUEUE_WAIT_S:
                        raise
                    self.errors -= 1  # a queue wait is not a model error
                    self.queue_waits += 1
                    time.sleep(20.0)
            self.queue_wait_s += t_try - t0
            if str(reply.model) != model:
                self.served_mismatch += 1
                self.cross_model_replies += 1
                raise CrossModelRouteError(model, str(reply.model))
            self.served_checked += 1
            return reply

        def stats(self) -> Dict[str, Any]:
            out = super().stats()
            out.update(served_checked=self.served_checked, served_mismatch=self.served_mismatch,
                       queue_waits=self.queue_waits, queue_wait_s=round(self.queue_wait_s, 1))
            return out

    return _AssertServed(base_url=scheduler_url, model=model, local_only=True,
                         allow_cross_model=False, source="adk.skilltasks")


MODES = ("plain", "sase", "terminal-plain", "terminal-sase")

#: How long one model call may wait for a busy single-slot pool before it counts as failed.
QUEUE_WAIT_S = 3600.0

#: Scheduler failures that mean "not now", not "dead": the pool slot is held by another
#: run (a 5xx whose upstream timed out), or the scheduler itself is restarting under us
#: (measured: the fleet rolled it mid-run and three refused calls ended the run). A
#: client-side ReadTimeout is NOT one: that is a genuinely slow call, and resending its
#: whole prompt for an hour would only queue it again.
_REFUSED = ("ConnectError", "RemoteProtocolError", "ConnectTimeout", "EOF occurred")


def _transient(exc: Any) -> bool:
    msg = str(exc)
    if "ReadTimeout" in msg:
        return False
    if int(getattr(exc, "status", 0) or 0) in (502, 503, 504):
        return "timed out" in msg or "unavailable" in msg.lower()
    return "unreachable" in msg and any(m in msg for m in _REFUSED)


def terminal_config(mode: str, max_calls: int, wall_s: float, run_dir: Optional[str]) -> Any:
    """The loop configuration of a terminal mode. ``terminal-sase`` = SASE turns, intent
    budgets, PRISM over the terminal strategies, the grounded command -> effect evidence
    table and the prediction ledger; ``terminal-plain`` = the same tools and budget with
    all of that off (the h30 ``plain`` ablation)."""
    from adk.reasoning.solve import Budget, LoopConfig

    sase = mode == "terminal-sase"
    return LoopConfig(budget=Budget(max_llm_calls=max_calls, max_actions=80, max_wall_s=wall_s,
                                    turn_s=90.0, llm_timeout_s=QUEUE_WAIT_S + 900.0),
                      sase=sase, prism=sase, grounded=sase, planning=False, daydream=False,
                      max_tokens=3000, run_dir=run_dir,
                      core={"stuck_actions": 10, "stuck_turns": 2, "turn_actions": 12,
                            "ctx_tokens": 16384, "grounding_chars": 1500,
                            "print_cap": 6000})


async def _solve(env: Any, backend: Any, max_calls: int, wall_s: float,
                 run_dir: Optional[str], mode: str = "plain") -> Any:
    from adk.reasoning.solve import Budget, LoopConfig, solve

    if mode.startswith("terminal-"):
        cfg = terminal_config(mode, max_calls, wall_s, run_dir)
        return await solve(env, backend, goal=_goal(env), config=cfg)

    # "plain" = one python block per reply, no PRISM rotation. The sase/PRISM arms are
    # built for grid games: their auto "handoff" arms act without the model and a turn
    # with no act() reads as no progress (measured on dns-policy-inplace, 2026-09-26).
    cfg = LoopConfig(budget=Budget(max_llm_calls=max_calls, max_actions=env.max_submits + 1,
                                   max_wall_s=wall_s, turn_s=60.0, llm_timeout_s=240.0),
                     sase=(mode == "sase"), prism=(mode == "sase"),
                     planning=False, daydream=False, max_tokens=1600, run_dir=run_dir)
    return await solve(env, backend, goal=_goal(env), config=cfg)


def run_task(task_dir: Path, *, model: str, max_calls: int = 12, wall_s: float = 900.0,
             scheduler_url: Optional[str] = None, run_dir: Optional[str] = None,
             mode: str = "plain") -> Dict[str, Any]:
    if model not in LOCAL_MODELS:
        raise RunRefused("model %r is not a locally served model %s" % (model, list(LOCAL_MODELS)))
    try:
        from adk.core.backends.microscheduler import MicroSchedulerBackend
        import adk.reasoning.solve  # noqa: F401
    except ImportError as exc:
        raise RunRefused("the reasoning loop is not installed here (%s); install awdk with the "
                         "reason extra from a build that ships adk.reasoning.solve" % exc) from exc

    if mode not in MODES:
        raise RunRefused("mode %r is not one of %s" % (mode, list(MODES)))
    terminal = mode.startswith("terminal-")
    if terminal:
        backend = _strict_backend(scheduler_url, model)
    else:
        backend = MicroSchedulerBackend(base_url=scheduler_url, model=model, local_only=True,
                                        allow_cross_model=False, source="adk.skilltasks")
    served = backend.resolve_model()
    if served != model:
        raise RunRefused("scheduler resolved %r, not the requested %r" % (served, model))
    if terminal:
        from .terminal import SkillTaskTerminalEnv

        env: Any = SkillTaskTerminalEnv(task_dir)
    else:
        env = SkillTaskEnv(task_dir)
    t0 = time.time()
    extra: Dict[str, Any] = {}
    try:
        result = asyncio.run(_solve(env, backend, max_calls, wall_s, run_dir, mode))
        final = env.final()
        if terminal:
            extra = _terminal_report(env, result, final)
    finally:
        env.close()
    stats = backend.stats()
    out = {
        "task": env.task.id,
        "model_requested": model,
        "model_served": served,
        "mode": mode,
        "backend": stats,
        "finish_reason": getattr(result, "finish_reason", None),
        "loop_won": bool(getattr(result, "won", False)),
        "llm_calls": getattr(result, "llm_calls", None),
        "submits": env.submits,
        "tool_calls": getattr(env, "tool_calls", None),
        "final_reward": final.reward,
        "final_tests_passed": sum(1 for v in final.tests.values() if v),
        "final_tests_total": len(final.tests),
        "outcome_failed": final.outcome_failed,
        "wall_s": round(time.time() - t0, 1),
        "error": getattr(result, "error", None),
        "log_path": getattr(result, "log_path", None),
    }
    out.update(extra)
    out["served_model_ok"] = (stats.get("cross_model_replies", 0) == 0
                              and stats.get("local_only") is True and stats.get("llm_calls", 0) > 0)
    if terminal:  # every answered call was checked, and the loop saw only the requested model
        seen = set((extra.get("served_models") or {}))
        out["served_model_ok"] = (out["served_model_ok"]
                                  and stats.get("served_checked") == stats.get("llm_calls")
                                  and seen <= {model})
    return out


def _terminal_report(env: Any, result: Any, final: Any) -> Dict[str, Any]:
    loop_stats = getattr(result, "stats", {}) or {}
    trace = env.trace()
    fams: Dict[str, int] = {}
    for s in trace:
        fam = str(s["family"]).split(":", 1)[0].split(" ", 1)[0]
        fams[fam] = fams.get(fam, 0) + 1
    return {
        "actions": len(trace),
        "action_families": fams,
        "denials": env.denials,
        "fence_repairs": env.fence_repairs,
        "rollback_failures": env.rollback_failures,
        "solved_by": env.solved_by(final_passed=True) if final.reward == 1.0 else None,
        "strategy_trace": list(getattr(result, "strategy_trace", []) or []),
        "changes": {n: c["verified"] for n, c in env.changes.items()},
        "predictions": loop_stats.get("predictions"),
        "served_models": loop_stats.get("served_models"),
        "rubric_secondary": {k: "%d/%d" % (v["met"], v["total"])
                             for k, v in env.rubric_report(final).items()},
    }


def main_run(args: Any) -> int:
    try:
        out = run_task(Path(args.task), model=args.model, max_calls=args.max_calls,
                       wall_s=args.wall_s, scheduler_url=args.scheduler_url, run_dir=args.run_dir,
                       mode=args.mode)
    except RunRefused as exc:
        print(json.dumps({"error": str(exc), "exit_code": 2}))
        return 2
    print(json.dumps(out, default=str))
    if not out["served_model_ok"]:
        return 2
    return 0 if out["final_reward"] == 1.0 else 1
