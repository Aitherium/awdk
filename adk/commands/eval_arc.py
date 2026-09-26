"""``adk eval arc`` -- the ARC-AGI-3 suite (RHAE) over games x seeds with a policy.

    adk eval arc --env-dir PATH --games ls20,ft09 --seeds 1 --cap 200 --out runs/
    adk eval arc --policy llm --backend microscheduler --games ls20 --cap 30
    adk eval arc --policy solve --budget 20 --games ls20
    adk eval arc --self-test

Policies: ``random`` (uniform over the available actions, no explorer -- the
RHAE floor's policy), ``llm`` (the per-step model policy that proves the model
path) and ``solve`` (the reasoning loop, one run per episode).

Seeds 10-69 are the held-out band and are refused before anything is built.

Exit codes: 0 mean RHAE (first seed, the competition headline) above the random
floor; 1 at or below it (or a failed ``--self-test``); 2 cannot judge -- engine
or games missing, a refused seed or unknown game, zero rows, or the model
backend dead on every row.
"""

from __future__ import annotations

import sys
from typing import Any, List, Mapping, Optional, Union

from adk.commands._model_profile import (
    BackendDeadError,
    SyncChat,
    add_model_args,
    backend_stats,
    build_backend,
    resolve_label,
)

POLICIES = ("random", "llm", "solve")

#: Test seams: an environment factory ``(game, seed) -> Environment`` and the
#: baselines map. None = the real engine over ``--env-dir``.
MAKE_ENV: Optional[Any] = None
BASELINES: Optional[Mapping[str, List[int]]] = None


def register_parser(eval_sub: Any) -> None:
    """Add ``arc`` to the ``adk eval`` subparsers."""
    p = eval_sub.add_parser(
        "arc", help="ARC-AGI-3 suite: a policy over games x seeds, scored by RHAE"
    )
    p.add_argument(
        "--env-dir",
        default=None,
        help="Games directory (<game>/<version>/metadata.json); default $ADK_ARC_ENV_DIR",
    )
    p.add_argument("--games", default="all", help="all, or a comma list (e.g. ls20,ft09)")
    p.add_argument(
        "--seeds",
        default="1",
        metavar="N|a,b",
        help="Seed count N (seeds 0..N-1) or an explicit comma list; 10-69 refused",
    )
    p.add_argument(
        "--policy",
        default="random",
        choices=POLICIES,
        help="random (no explorer), llm (per-step model), solve (reasoning loop)",
    )
    p.add_argument(
        "--cap",
        type=int,
        default=400,
        metavar="ACTIONS",
        help="Scored-action cap per episode (default: 400)",
    )
    p.add_argument("--cap-s", type=float, default=None, help="Wall-clock cap per episode")
    p.add_argument("--out", default=None, help="Directory for <run>.jsonl + <run>.summary.json")
    add_model_args(p)
    p.add_argument(
        "--budget",
        type=int,
        default=40,
        metavar="N",
        help="--policy solve: max model calls per episode (default: 40)",
    )
    p.add_argument(
        "--mode", default="sase", choices=["plain", "sase"], help="--policy solve: loop mode"
    )
    p.add_argument("--json", action="store_true", help="Print the summary as JSON")
    p.add_argument(
        "--live-url",
        default=None,
        metavar="URL",
        help="Stream the run to the ARC Theater (default $ADK_ARC_LIVE_URL; 'off' "
        "disables); token from $ADK_ARC_LIVE_TOKEN or $ARC_THEATER_INGEST_TOKEN",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="RHAE scorer self-test (its break arm must fail); needs no games or model",
    )


def parse_seeds(text: Union[str, int]) -> Union[int, List[int]]:
    """``"3"`` -> 3 (seeds 0..2); ``"0,1,70"`` -> [0, 1, 70]."""
    s = str(text).strip()
    if "," in s:
        return [int(x) for x in s.split(",") if x.strip()]
    return int(s)


def _solve_policy(model: Any, budget: int, mode: str, live: Any = None) -> Any:
    import asyncio

    from adk.reasoning.solve import Budget, LoopConfig, solve

    cfg = LoopConfig(budget=Budget(max_llm_calls=budget), sase=mode == "sase")

    def episode(env: Any, ctx: Any) -> Mapping[str, Any]:
        session = None
        if live is not None:  # tee the loop's turns/hypotheses to the Theater
            from adk.evalharness.arc_agi3.live import LoopTee, new_session

            session = new_session(LoopTee(live, ctx.game))
        res = asyncio.run(solve(env, model, config=cfg, session=session))
        if res.exit_code == 2:
            raise BackendDeadError(res.error or res.finish_reason)
        tokens = res.tokens or {}
        return {
            "llm_calls": res.llm_calls,
            "finish_reason": res.finish_reason,
            "prompt_tokens": int(tokens.get("prompt", 0) or 0),
            "completion_tokens": int(tokens.get("completion", 0) or 0),
            "hyps_active": sum(1 for h in res.hypotheses if h.status == "active"),
            "hyps_refuted": sum(1 for h in res.hypotheses if h.status == "refuted"),
        }

    episode.__name__ = "solve"
    return episode


def cmd_eval_arc(args: Any) -> int:
    import json

    from adk.evalharness.arc_agi3 import rhae
    from adk.evalharness.arc_agi3.env_arc import ArcUnavailableError
    from adk.evalharness.arc_agi3.suite import random_policy, resolve_seeds, run_suite

    if args.self_test:
        return rhae.self_test()
    as_json = bool(getattr(args, "json", False))
    say = (lambda *a: print(*a, file=sys.stderr)) if as_json else print
    try:
        seeds = resolve_seeds(parse_seeds(args.seeds))  # held-out band refused HERE
        if args.cap <= 0:
            raise ValueError("--cap must be positive")
    except (TypeError, ValueError) as exc:
        say("refused: %s" % exc)
        return 2

    model: Any = None
    live: Any = None
    if args.policy == "random":
        policy = random_policy
    else:
        try:
            model = build_backend(args)
            say("model: %s" % resolve_label(model))
        except Exception as exc:  # noqa: BLE001 - a dead backend cannot be judged
            say("cannot judge: model backend dead: %s" % exc)
            return 2
        if args.policy == "llm":
            from adk.evalharness.arc_agi3.llm_policy import llm_policy

            chat = model if callable(getattr(model, "chat", None)) else SyncChat(model)
            policy = llm_policy(chat)
        else:
            from adk.evalharness.arc_agi3 import live as live_mod

            live = live_mod.from_args(
                getattr(args, "live_url", None),
                model=resolve_label(model),
                mode=args.mode,
                policy="solve",
                games=[args.games],
                say=say,
            )
            policy = _solve_policy(model, args.budget, args.mode, live)
    make_env = MAKE_ENV
    if live is None and args.policy != "solve":
        from adk.evalharness.arc_agi3 import live as live_mod

        live = live_mod.from_args(
            getattr(args, "live_url", None),
            model=resolve_label(model) if model is not None else "",
            mode=args.policy,
            policy=args.policy,
            games=[args.games],
            say=say,
        )
    if live is not None:
        policy, make_env = live_mod.instrument_suite(policy, MAKE_ENV, live, env_dir=args.env_dir)
    try:
        res = run_suite(
            policy,
            args.games,
            seeds=seeds,
            cap_actions=args.cap,
            cap_s=args.cap_s,
            env_dir=args.env_dir,
            out=args.out,
            policy_name=args.policy,
            make_env=make_env,
            baselines=BASELINES,
        )
    except ArcUnavailableError as exc:
        say("cannot judge: %s" % exc)
        return 2
    except ValueError as exc:
        say("refused: %s" % exc)
        return 2
    finally:
        if live is not None:
            live_mod.finish(live, say=say)
    for r in res.rows:
        say(
            "  %-6s seed=%-3d levels=%d actions=%-4d rhae=%.4f  %s"
            % (
                r["game"],
                r["seed"],
                r["levels"],
                r["actions"],
                r["local_rhae"],
                r["abandon_reason"] or "",
            )
        )
    s = res.summary
    say(
        "rows=%d games=%d  mean RHAE (first seed, headline)=%.5f  (max over seeds=%.5f)  "
        "floor=%.5f"
        % (
            s["rows"],
            s["games"],
            s["mean_rhae_first_seed"],
            s["mean_rhae_max_over_seeds"],
            rhae.RANDOM_FLOOR,
        )
    )
    stats = backend_stats(model) if model is not None else None
    if stats:
        say("model stats: %s" % json.dumps(stats, default=str))
    if res.rows_path:
        say("wrote %s and %s" % (res.rows_path, res.summary_path))
    if res.exit_code == 2:
        say(
            "cannot judge: %s"
            % ("no rows" if not res.rows else "the model backend was dead on every row")
        )
    if as_json:
        print(
            json.dumps(
                dict(
                    s,
                    exit_code=res.exit_code,
                    rows_path=str(res.rows_path) if res.rows_path else None,
                ),
                default=str,
            )
        )
    return res.exit_code
