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


def _goal(env: SkillTaskEnv) -> str:
    return ("Make the working directory satisfy the task, then act(1). Use sh()/read()/write(); "
            "there is no grid.")


async def _solve(env: SkillTaskEnv, backend: Any, max_calls: int, wall_s: float,
                 run_dir: Optional[str], mode: str = "plain") -> Any:
    from adk.reasoning.solve import Budget, LoopConfig, solve

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

    backend = MicroSchedulerBackend(base_url=scheduler_url, model=model, local_only=True,
                                    allow_cross_model=False, source="adk.skilltasks")
    served = backend.resolve_model()
    if served != model:
        raise RunRefused("scheduler resolved %r, not the requested %r" % (served, model))
    env = SkillTaskEnv(task_dir)
    t0 = time.time()
    try:
        result = asyncio.run(_solve(env, backend, max_calls, wall_s, run_dir, mode))
        final = env.final()
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
        "tool_calls": env.tool_calls,
        "final_reward": final.reward,
        "final_tests_passed": sum(1 for v in final.tests.values() if v),
        "final_tests_total": len(final.tests),
        "outcome_failed": final.outcome_failed,
        "wall_s": round(time.time() - t0, 1),
        "error": getattr(result, "error", None),
        "log_path": getattr(result, "log_path", None),
    }
    out["served_model_ok"] = (stats.get("cross_model_replies", 0) == 0
                              and stats.get("local_only") is True and stats.get("llm_calls", 0) > 0)
    return out


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
