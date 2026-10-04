"""CLI for node_bootstrap tools via argparse.

Subcommands map 1:1 to node_* tools. Output is JSON to stdout. Exit 0 on success,
1 on error (when output dict contains "error" key).

The engine-tournament subcommands (parity, bench, tournament) use the checker
contract instead: 0 pass/ok, 1 violation (a candidate FAILed parity), 2 could not judge.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools

_EXIT_KEY = "_exit_code"


def _handle_detect(args) -> dict:
    """Handle detect subcommand."""
    return tools.node_detect_hardware(verbose=args.verbose)


def _handle_resolve(args) -> dict:
    """Handle resolve subcommand."""
    return tools.node_resolve_recipe(
        prefer_backend=args.prefer_backend,
        recipe_id=args.recipe_id,
    )


def _handle_plan(args) -> dict:
    """Handle plan subcommand."""
    return tools.node_plan_deployment(
        recipe_id=args.recipe_id,
        node_ip=args.node_ip,
        ssh_user=args.ssh_user,
    )


def _handle_apply(args) -> dict:
    """Handle apply subcommand."""
    return tools.node_apply(
        recipe_id=args.recipe_id,
        node_ip=args.node_ip,
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        dry_run=args.dry_run,
    )


def _handle_enroll(args) -> dict:
    """Handle enroll subcommand."""
    return tools.node_enroll(
        control_plane_url=args.control_plane_url,
        token=args.token,
    )


def _handle_register(args) -> dict:
    """Handle register subcommand."""
    models = []
    if args.models:
        # Parse comma-separated model list
        models = [m.strip() for m in args.models.split(",") if m.strip()]

    return tools.node_register_backend(
        genesis_url=args.genesis_url,
        base_url=args.base_url,
        backend_type=args.backend_type,
        models=models,
        preferred=args.preferred,
    )


def _handle_verify(args) -> dict:
    """Handle verify subcommand."""
    return tools.node_verify(
        base_url=args.base_url,
        backend_type=args.backend_type,
        model=args.model,
        timeout_s=args.timeout,
        recipe_id=args.recipe_id,
    )


def _conn(args):
    from . import tournament as t

    return t.Conn(token_env=args.token_env, ca_bundle=args.ca_bundle,
                  insecure=args.insecure, timeout=args.timeout)


def _tolerances(pairs) -> dict:
    out = {}
    for item in pairs or []:
        key, sep, val = item.partition("=")
        if not sep:
            raise ValueError(f"--tolerance expects KEY=VALUE, got {item!r}")
        out[key.strip()] = float(val)
    return out


def _id_map(pairs, flag: str) -> dict:
    out = {}
    for item in pairs or []:
        key, sep, val = item.partition("=")
        if not sep or not key.strip() or not val.strip():
            raise ValueError(f"{flag} expects ID=VALUE, got {item!r}")
        if key.strip() in out:
            raise ValueError(f"{flag}: id {key.strip()!r} given more than once")
        out[key.strip()] = val.strip()
    return out


def _prompts(args):
    from . import tournament as t

    return t.load_prompts(args.prompts) if args.prompts else None


def _handle_parity(args) -> dict:
    """Handle parity subcommand: one candidate engine against a reference."""
    from . import tournament as t

    res = t.parity(
        args.reference, args.candidate, args.model, _prompts(args),
        max_tokens=args.max_tokens, top_logprobs=args.top_logprobs,
        tolerances=_tolerances(args.tolerance),
        require_logprobs=not args.no_require_logprobs, conn=_conn(args),
    ).to_dict()
    res[_EXIT_KEY] = res["exit_code"]
    return res


def _handle_bench(args) -> dict:
    """Handle bench subcommand: streaming TTFT + decode tok/s, optional cold start."""
    from . import tournament as t

    conn = _conn(args)
    out = {}
    if args.launch:
        out["cold_start"] = t.cold_start(args.launch, args.url, args.model,
                                         timeout=args.cold_start_timeout, conn=conn)
    out["bench"] = t.bench(args.url, args.model, runs=args.runs,
                           max_tokens=args.max_tokens, conn=conn)
    if args.launch and args.stop:
        try:
            t.run_action(args.stop)
        except Exception as e:  # noqa: BLE001
            out["stop_error"] = str(e)
    ok = out["bench"]["ok"] and (not args.launch or out["cold_start"]["ok"])
    out[_EXIT_KEY] = 0 if ok else 2
    return out


def _handle_tournament(args) -> dict:
    """Handle tournament subcommand: parity gate, then speed; writes the verdict file."""
    from . import tournament as t

    if args.self_test:
        code = t.self_test(verbose=True)
        return {"self_test": "pass" if code == 0 else "fail", _EXIT_KEY: code}
    entrants = _id_map(args.entrants, "entrant")
    if not entrants or not args.reference or not args.model:
        return {"error": "tournament needs ID=URL entrants, --reference and --model",
                _EXIT_KEY: 2}
    launch = _id_map(args.launch, "--launch")
    stop = _id_map(args.stop, "--stop")
    unknown = sorted((set(launch) | set(stop)) - set(entrants))
    if unknown:
        return {"error": f"--launch/--stop name unknown entrants: {unknown}", _EXIT_KEY: 2}
    rows = []
    for rid, url in entrants.items():
        row = {"recipe_id": rid, "url": url}
        if rid in launch:
            row["launch"] = launch[rid]
        if rid in stop:
            row["stop"] = stop[rid]
        rows.append(row)
    report = t.run_tournament(
        rows, args.reference, args.model, prompts=_prompts(args), runs=args.runs,
        max_tokens=args.max_tokens, top_logprobs=args.top_logprobs,
        tolerances=_tolerances(args.tolerance),
        require_logprobs=not args.no_require_logprobs, conn=_conn(args),
        out_path=args.out or None, write=not args.no_write,
        cold_start_timeout=args.cold_start_timeout,
    )
    report[_EXIT_KEY] = report["exit_code"]
    return report


def _add_conn_args(p) -> None:
    p.add_argument("--token-env", default="",
                   help="NAME of an env var holding a bearer token (never the token)")
    p.add_argument("--ca-bundle", default="",
                   help="CA bundle for https endpoints (default: system store)")
    p.add_argument("--insecure", action="store_true",
                   help="disable TLS verification (prints a warning; avoid)")
    p.add_argument("--timeout", type=float, default=120.0, help="per-request timeout (s)")


def _add_parity_args(p) -> None:
    p.add_argument("--prompts", default="", help="JSONL prompt file (default: built-in)")
    p.add_argument("--max-tokens", type=int, default=64, help="tokens per parity answer")
    p.add_argument("--top-logprobs", type=int, default=5, help="top-k logprobs to compare")
    p.add_argument("--no-require-logprobs", action="store_true",
                   help="judge on text metrics alone when logprobs are unavailable")
    p.add_argument("--tolerance", action="append", default=[], metavar="KEY=VALUE",
                   help="override a parity tolerance (repeatable)")


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Node bootstrap — hardware-aware inference deployment",
        prog="python -m adk.toolpacks.node_bootstrap",
    )
    subparsers = parser.add_subparsers(dest="command", help="subcommand")

    # detect
    detect_p = subparsers.add_parser("detect", help="detect system hardware")
    detect_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="verbose output"
    )
    detect_p.set_defaults(handler=_handle_detect)

    # resolve
    resolve_p = subparsers.add_parser("resolve", help="resolve recipe for hardware")
    resolve_p.add_argument(
        "--prefer-backend",
        default="auto",
        help="prefer backend (auto, ollama, etc.)",
    )
    resolve_p.add_argument(
        "--recipe-id",
        default="",
        help="explicit recipe ID (overrides auto-detection)",
    )
    resolve_p.set_defaults(handler=_handle_resolve)

    # plan
    plan_p = subparsers.add_parser("plan", help="plan deployment")
    plan_p.add_argument(
        "--recipe-id",
        required=True,
        help="recipe ID to deploy",
    )
    plan_p.add_argument(
        "--node-ip",
        default="",
        help="target node IP (remote mode)",
    )
    plan_p.add_argument(
        "--ssh-user",
        default="",
        help="SSH user (remote mode)",
    )
    plan_p.set_defaults(handler=_handle_plan)

    # apply
    apply_p = subparsers.add_parser("apply", help="apply deployment")
    apply_p.add_argument(
        "--recipe-id",
        required=True,
        help="recipe ID to deploy",
    )
    apply_p.add_argument(
        "--node-ip",
        default="",
        help="target node IP (remote mode)",
    )
    apply_p.add_argument(
        "--ssh-user",
        default="",
        help="SSH user (remote mode)",
    )
    apply_p.add_argument(
        "--ssh-key",
        default="",
        help="SSH private key path (remote mode)",
    )
    apply_p.add_argument(
        "--dry-run",
        action="store_true",
        help="show commands without executing",
    )
    apply_p.set_defaults(handler=_handle_apply)

    # enroll
    enroll_p = subparsers.add_parser("enroll", help="enroll with control plane")
    enroll_p.add_argument(
        "--control-plane-url",
        default="",
        help="control plane URL (env: AITHER_CONTROL_PLANE_URL)",
    )
    enroll_p.add_argument(
        "--token",
        default="",
        help="authentication token (env: AITHER_AUTH_TOKEN)",
    )
    enroll_p.set_defaults(handler=_handle_enroll)

    # register
    register_p = subparsers.add_parser("register", help="register backend")
    register_p.add_argument(
        "--genesis-url",
        default="",
        help="Genesis URL (env: AITHER_GENESIS_URL)",
    )
    register_p.add_argument(
        "--base-url",
        required=True,
        help="backend service URL",
    )
    register_p.add_argument(
        "--backend-type",
        required=True,
        help="backend type (vllm, ollama, etc.)",
    )
    register_p.add_argument(
        "--models",
        default="",
        help="comma-separated model list",
    )
    register_p.add_argument(
        "--preferred",
        action="store_true",
        help="mark as preferred backend",
    )
    register_p.set_defaults(handler=_handle_register)

    # verify
    verify_p = subparsers.add_parser("verify", help="verify backend health")
    verify_p.add_argument(
        "--base-url",
        required=True,
        help="backend service URL",
    )
    verify_p.add_argument(
        "--backend-type",
        default="",
        help="backend type (vllm, ollama, etc.); defaults to the recipe's",
    )
    verify_p.add_argument(
        "--recipe-id",
        default="",
        help="recipe whose declared health/completion paths to use",
    )
    verify_p.add_argument(
        "--model",
        default="",
        help="model name to test",
    )
    verify_p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="timeout in seconds",
    )
    verify_p.set_defaults(handler=_handle_verify)

    # parity
    parity_p = subparsers.add_parser(
        "parity", help="quality-parity gate: candidate engine vs reference, same model")
    parity_p.add_argument("--reference", required=True, help="reference endpoint URL")
    parity_p.add_argument("--candidate", required=True, help="candidate endpoint URL")
    parity_p.add_argument("--model", required=True, help="model id served by both")
    _add_parity_args(parity_p)
    _add_conn_args(parity_p)
    parity_p.set_defaults(handler=_handle_parity)

    # bench
    bench_p = subparsers.add_parser("bench", help="streaming TTFT + decode tok/s")
    bench_p.add_argument("--url", required=True, help="endpoint URL")
    bench_p.add_argument("--model", required=True, help="model id")
    bench_p.add_argument("--runs", type=int, default=5, help="measured runs")
    bench_p.add_argument("--max-tokens", type=int, default=128, help="tokens per run")
    bench_p.add_argument("--launch", default="", help="command that starts the engine "
                         "(measures cold start to first coherent completion)")
    bench_p.add_argument("--stop", default="", help="command that stops the engine")
    bench_p.add_argument("--cold-start-timeout", type=float, default=600.0)
    _add_conn_args(bench_p)
    bench_p.set_defaults(handler=_handle_bench)

    # tournament
    tour_p = subparsers.add_parser(
        "tournament", help="rank engines serving one model: parity gate, then speed")
    tour_p.add_argument("entrants", nargs="*", metavar="ID=URL",
                        help="recipe id and its endpoint URL")
    tour_p.add_argument("--reference", default="", help="entrant id used as reference")
    tour_p.add_argument("--model", default="", help="model id served by every entrant")
    tour_p.add_argument("--runs", type=int, default=5, help="bench runs per entrant")
    tour_p.add_argument("--launch", action="append", default=[], metavar="ID=CMD",
                        help="cold-start command for an entrant (repeatable)")
    tour_p.add_argument("--stop", action="append", default=[], metavar="ID=CMD",
                        help="stop command for an entrant, run right after that entrant "
                             "is measured (repeatable)")
    tour_p.add_argument("--cold-start-timeout", type=float, default=600.0)
    tour_p.add_argument("--out", default="",
                        help="verdict file (default: $AITHER_TOURNAMENT_FILE or "
                             "~/.aither/node-bootstrap/tournament.json)")
    tour_p.add_argument("--no-write", action="store_true", help="do not write the file")
    tour_p.add_argument("--self-test", action="store_true",
                        help="prove every verdict path against in-process fake engines")
    _add_parity_args(tour_p)
    _add_conn_args(tour_p)
    tour_p.set_defaults(handler=_handle_tournament)

    args = parser.parse_args()

    if not hasattr(args, "handler"):
        parser.print_help()
        return 1

    try:
        result = args.handler(args)
        exit_code = result.pop(_EXIT_KEY, None)
        print(json.dumps(result, indent=2))
        if exit_code is not None:
            return int(exit_code)
        return 0 if "error" not in result else 1
    except Exception as e:
        error_result = {"error": str(e), "fix": "check arguments and system state"}
        print(json.dumps(error_result, indent=2))
        # The tournament subcommands follow the checker contract: an exception means
        # the run could not judge, which is 2, never a pass and never a violation.
        if getattr(args, "command", "") in ("parity", "bench", "tournament"):
            return 2
        return 1


if __name__ == "__main__":
    sys.exit(main())
