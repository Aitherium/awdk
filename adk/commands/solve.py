"""``adk solve`` -- run the reasoning loop on a named Environment with a model backend.

    adk solve --env counter                         # toy 1-D counter, MicroScheduler model
    adk solve --env arc --game ls20 --env-dir PATH --mode sase --prism on --budget 40
    adk solve --env gridwalk --backend reasoning --json

Exit codes (``SolveResult.exit_code``): 0 won; 1 ran and did not win (budget,
cancel, done); 2 cannot judge -- the model never answered, the environment could
not be built (missing ``reason``/``arc`` extra, missing games directory), or a
held-out seed (10-69) was asked for. A dead backend is never 0.

Design: docs/reasoning-loop-design.md section 4.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import signal
import sys
from typing import Any, Callable, Dict

from adk.commands._model_profile import add_model_args, build_backend, resolve_label

_log = logging.getLogger(__name__)

#: ``--env`` name -> factory(args) -> Environment. Each imports its own deps.
ENVIRONMENTS: Dict[str, Callable[[Any], Any]] = {}


def _counter(args: Any) -> Any:
    from adk.reasoning.solve.envs.toy import Counter1D

    return Counter1D()


def _gridwalk(args: Any) -> Any:
    from adk.reasoning.solve.envs.toy import GridWalk5

    return GridWalk5()


def _arc(args: Any) -> Any:
    from adk.evalharness.arc_agi3.env_arc import ArcAgi3Environment, check_seed

    check_seed(int(args.seed))  # the held-out band is refused before anything loads
    if not args.game:
        raise ValueError("--env arc needs --game (e.g. ls20)")
    return ArcAgi3Environment(args.game, int(args.seed), env_dir=args.env_dir)


ENVIRONMENTS.update(counter=_counter, gridwalk=_gridwalk, arc=_arc)


def register_parser(sub: Any) -> None:
    """Add ``adk solve`` to the top-level subparsers."""
    p = sub.add_parser(
        "solve",
        help="Run the reasoning loop on an environment (toy or ARC-AGI-3) with a model backend",
    )
    p.add_argument(
        "--env",
        default="counter",
        choices=sorted(ENVIRONMENTS),
        help="Environment adapter (default: counter)",
    )
    p.add_argument("--game", default=None, help="--env arc: game id (e.g. ls20)")
    p.add_argument("--seed", type=int, default=0, help="--env arc: seed (10-69 is held out)")
    p.add_argument(
        "--env-dir", default=None, help="--env arc: games directory (default $ADK_ARC_ENV_DIR)"
    )
    add_model_args(p)
    p.add_argument(
        "--mode",
        default="sase",
        choices=["plain", "sase"],
        help="sase (4-phase, predictions) or plain (one code block per reply)",
    )
    p.add_argument(
        "--prism", default="on", choices=["on", "off"], help="PRISM strategy rotation (default: on)"
    )
    p.add_argument(
        "--budget", type=int, default=40, metavar="N", help="Max model calls (default: 40)"
    )
    p.add_argument("--max-actions", type=int, default=None, help="Max environment actions")
    p.add_argument("--wall-s", type=float, default=None, help="Max wall-clock seconds")
    p.add_argument("--goal", default="", help="Goal text appended to the environment primer")
    p.add_argument("--run-dir", default=None, help="Write the JSONL turn log here")
    p.add_argument("--json", action="store_true", help="Print the SolveResult as JSON")
    p.add_argument(
        "--live-url",
        default=None,
        metavar="URL",
        help="--env arc: stream the run to the ARC Theater (default $ADK_ARC_LIVE_URL; "
        "'off' disables); token from $ADK_ARC_LIVE_TOKEN or $ARC_THEATER_INGEST_TOKEN",
    )


def _config(args: Any) -> Any:
    from adk.reasoning.solve import Budget, LoopConfig

    if args.budget is not None and args.budget < 1:
        raise ValueError("--budget must be >= 1")
    budget = Budget(
        max_llm_calls=int(args.budget), max_actions=args.max_actions, max_wall_s=args.wall_s
    )
    return LoopConfig(
        budget=budget, sase=args.mode == "sase", prism=args.prism == "on", run_dir=args.run_dir
    )


async def _play(env: Any, model: Any, cfg: Any, goal: str, tee: Any = None) -> Any:
    from adk.reasoning.solve import SolveRun

    run = SolveRun(env, model, goal=goal, config=cfg)
    if tee is not None:
        from adk.evalharness.arc_agi3.live import tee_session

        tee_session(run.session, tee)
    loop = asyncio.get_running_loop()
    prev = None
    try:  # Ctrl-C cancels the run; the result still prints
        prev = signal.signal(
            signal.SIGINT, lambda *_: loop.call_soon_threadsafe(run.cancel, "sigint")
        )
    except (ValueError, OSError):  # not the main thread
        prev = None
    try:
        run.start()
        return await run.join()
    finally:
        if prev is not None:
            signal.signal(signal.SIGINT, prev)


def _baseline(args: Any) -> list:
    from adk.evalharness.arc_agi3 import rhae
    from adk.evalharness.arc_agi3.env_arc import resolve_env_dir

    return list(rhae.load_baselines(resolve_env_dir(args.env_dir)).get(args.game, []))


def _emit(obj: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, default=str))


def cmd_solve(args: Any) -> int:
    as_json = bool(getattr(args, "json", False))
    say = (lambda *a: print(*a, file=sys.stderr)) if as_json else print

    def cannot(stage: str, exc: BaseException) -> int:
        say("cannot judge (%s): %s" % (stage, exc))
        _emit({"exit_code": 2, "stage": stage, "error": str(exc)}, as_json)
        return 2

    try:
        cfg = _config(args)
        env = ENVIRONMENTS[args.env](args)
    except ImportError as exc:
        return cannot(
            "env",
            ImportError("%s -- install the extra: pip install 'aither-adk[reason,arc]'" % exc),
        )
    except Exception as exc:  # noqa: BLE001 - ArcUnavailableError, held-out seed, bad args
        return cannot("env", exc)
    try:
        model = build_backend(args)
        label = resolve_label(model)
    except Exception as exc:  # noqa: BLE001 - a dead backend is exit 2
        return cannot("backend", exc)
    b = cfg.budget
    say(
        "solve: env=%s mode=%s prism=%s model=%s budget(calls=%s actions=%s wall_s=%s)"
        % (args.env, args.mode, args.prism, label, b.max_llm_calls, b.max_actions, b.max_wall_s)
    )
    live = tee = None
    if args.env == "arc":
        from adk.evalharness.arc_agi3 import live as live_mod

        live = live_mod.from_args(
            getattr(args, "live_url", None),
            model=label,
            mode=args.mode,
            policy="solve",
            games=[args.game],
            say=say,
        )
        if live is not None:
            env = live_mod.LiveEnv(env, live, args.game, int(args.seed))
            tee = live_mod.LoopTee(live, args.game)
    try:
        res = asyncio.run(_play(env, model, cfg, args.goal, tee))
    finally:
        if live is not None:
            try:
                live.post(
                    live_mod.score_event(
                        args.game,
                        int(args.seed),
                        env.level_actions,
                        _baseline(args),
                        int(env.actions),
                    )
                )
            except Exception:  # noqa: BLE001 - the sink never breaks a run
                _log.debug("the live sink failed; the run continues without it", exc_info=True)
            live_mod.finish(live, say=say)
    out = dataclasses.asdict(res)
    out.update(exit_code=res.exit_code, model=label)
    if as_json:
        _emit(out, True)
    else:
        print(
            "finish=%s won=%s levels=%d level_actions=%s actions=%d turns=%d llm_calls=%d "
            "wall_s=%.1f"
            % (
                res.finish_reason,
                res.won,
                res.levels,
                res.level_actions,
                res.actions,
                res.turns,
                res.llm_calls,
                res.wall_s,
            )
        )
        active = [h.name for h in res.hypotheses if h.status == "active"]
        if active:
            print("active hypotheses: %s" % ", ".join(active))
        if res.error:
            print("error: %s" % res.error)
        if res.log_path:
            print("log: %s" % res.log_path)
    return res.exit_code
