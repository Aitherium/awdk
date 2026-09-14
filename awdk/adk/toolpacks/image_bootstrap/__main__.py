"""CLI for image_bootstrap tools via argparse.

Subcommands map 1:1 to imagegen_* tools. Output is JSON to stdout. Exit 0 on
success, 1 on error (when the output dict contains an "error" key).

  python -m adk.toolpacks.image_bootstrap detect
  python -m adk.toolpacks.image_bootstrap resolve --prefer-engine sana
  python -m adk.toolpacks.image_bootstrap apply --recipe-id cuda-comfyui-12gb --dry-run
  python -m adk.toolpacks.image_bootstrap verify --base-url http://localhost:8188
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import tools


def _handle_detect(args) -> dict:
    return tools.imagegen_detect_hardware(verbose=args.verbose)


def _handle_resolve(args) -> dict:
    return tools.imagegen_resolve_recipe(
        prefer_engine=args.prefer_engine,
        recipe_id=args.recipe_id,
    )


def _handle_plan(args) -> dict:
    return tools.imagegen_plan_deployment(
        recipe_id=args.recipe_id,
        tenant=args.tenant,
        host_port=args.host_port,
    )


def _handle_apply(args) -> dict:
    return tools.imagegen_apply(
        recipe_id=args.recipe_id,
        tenant=args.tenant,
        host_port=args.host_port,
        dry_run=args.dry_run,
    )


def _handle_register(args) -> dict:
    models = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else []
    return tools.imagegen_register_backend(
        genesis_url=args.genesis_url,
        base_url=args.base_url,
        backend_type=args.backend_type,
        models=models,
        preferred=args.preferred,
    )


def _handle_verify(args) -> dict:
    return tools.imagegen_verify(
        base_url=args.base_url,
        backend_type=args.backend_type,
        timeout_s=args.timeout,
    )


def _handle_setup(args) -> dict:
    return tools.imagegen_setup(
        prefer_engine=args.prefer_engine,
        recipe_id=args.recipe_id,
        tenant=args.tenant,
        host_port=args.host_port,
        network=args.network,
        genesis_url=args.genesis_url,
        token=os.getenv("AITHER_TOKEN", ""),
        register=not args.no_register,
        dry_run=args.dry_run,
    )


def self_test() -> int:
    """Offline self-test: every recipe plans, every target renders, the
    refusal arm fires, and no arm is vacuous."""
    bad = 0

    def check(label, cond):
        nonlocal bad
        print(f"  {'ok' if cond else 'FAIL'}  {label}")
        if not cond:
            bad += 1

    from . import tools
    from .recipes import RECIPE_IDS

    # Offline: model-download resolution can reach the fleet vault and
    # HANG on an off-fleet box (measured >110s on the fleet box itself,
    # before the bounded resolver landed) -- stub it so the self-test
    # stays fast and deterministic. The production path is untouched.
    tools._resolve_model_downloads = lambda *_a, **_k: ([], "")

    check("webgpu-browser is registered", "webgpu-browser" in RECIPE_IDS)
    check("mesh-spark is registered", "mesh-spark" in RECIPE_IDS)

    for rid in RECIPE_IDS:
        plan = tools.imagegen_plan_deployment(rid)
        check(f"plan({rid})", "error" not in plan)

    p = tools.imagegen_plan_deployment("webgpu-browser")
    check("browser lane plans mode browser with a URL",
          p.get("mode") == "browser" and bool(p.get("browser_url")))

    q = tools.imagegen_plan_deployment("cuda-comfyui-12gb", target="podman-quadlet")
    check("quadlet target renders a .container unit",
          q.get("mode") == "podman-quadlet"
          and "[Container]" in q.get("quadlet_unit", ""))

    r = tools.imagegen_plan_deployment("cuda-comfyui-12gb", free_vram_gb=3.5)
    check("refusal arm fires when free VRAM is below the recipe's need",
          "refused" in r.get("error", ""))
    r2 = tools.imagegen_plan_deployment("cuda-comfyui-12gb")
    check("refusal arm stays silent when free VRAM is not judged",
          "error" not in r2)

    a = tools.imagegen_apply("webgpu-browser", dry_run=True)
    check("apply dry-run handles the browser lane",
          "error" not in a and a.get("mode") == "browser")
    b = tools.imagegen_apply("cuda-comfyui-12gb", target="podman-quadlet", dry_run=True)
    check("apply dry-run handles the quadlet target",
          "error" not in b and b.get("mode") == "podman-quadlet")

    print("self-test ok" if not bad else "self-test FAILED")
    return 1 if bad else 0


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Image bootstrap — hardware-aware image-generation deployment",
        prog="python -m adk.toolpacks.image_bootstrap",
    )
    parser.add_argument("--self-test", action="store_true",
                        help="offline self-test (no network, no side effects)")
    subparsers = parser.add_subparsers(dest="command", help="subcommand")

    detect_p = subparsers.add_parser("detect", help="detect hardware + VRAM capability band")
    detect_p.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    detect_p.set_defaults(handler=_handle_detect)

    resolve_p = subparsers.add_parser("resolve", help="resolve recipe for this hardware")
    resolve_p.add_argument(
        "--prefer-engine", default="auto",
        help="engine family tiebreaker (auto, comfyui, sana)",
    )
    resolve_p.add_argument(
        "--recipe-id", default="", help="explicit recipe ID (overrides auto-detection)"
    )
    resolve_p.set_defaults(handler=_handle_resolve)

    plan_p = subparsers.add_parser("plan", help="plan deployment (pure, no side effects)")
    plan_p.add_argument("--recipe-id", required=True, help="recipe ID to deploy")
    plan_p.add_argument("--tenant", default="", help="tenant for private model resolution")
    plan_p.add_argument("--host-port", type=int, default=0,
                        help="host port to publish (default: the container port)")
    plan_p.set_defaults(handler=_handle_plan)

    apply_p = subparsers.add_parser("apply", help="apply deployment")
    apply_p.add_argument("--recipe-id", required=True, help="recipe ID to deploy")
    apply_p.add_argument("--tenant", default="", help="tenant for private model resolution")
    apply_p.add_argument("--host-port", type=int, default=0,
                         help="host port to publish (avoids clashing with an existing ComfyUI)")
    apply_p.add_argument(
        "--dry-run", action="store_true", help="show commands without executing"
    )
    apply_p.set_defaults(handler=_handle_apply)

    register_p = subparsers.add_parser("register", help="register backend with Genesis")
    register_p.add_argument(
        "--genesis-url", default="", help="Genesis URL (env: AITHER_GENESIS_URL)"
    )
    register_p.add_argument("--base-url", required=True, help="backend service URL")
    register_p.add_argument(
        "--backend-type", required=True, help="backend type (comfyui, sana)"
    )
    register_p.add_argument("--models", default="", help="comma-separated model list")
    register_p.add_argument(
        "--preferred", action="store_true", help="mark as preferred backend"
    )
    register_p.set_defaults(handler=_handle_register)

    verify_p = subparsers.add_parser(
        "verify", help="verify backend is up AND has models loaded"
    )
    verify_p.add_argument("--base-url", required=True, help="backend service URL")
    verify_p.add_argument(
        "--backend-type", default="comfyui", help="backend type (comfyui, sana)"
    )
    verify_p.add_argument("--timeout", type=float, default=60.0, help="timeout in seconds")
    verify_p.set_defaults(handler=_handle_verify)

    # ONE COMMAND. Every step below already existed and worked; what did not exist
    # was a way to run them without being the person who wrote them -- detect,
    # resolve, plan and apply were separate verbs, each needing a --recipe-id you
    # could only get by reading the previous one's output. Measured against the real
    # ask: someone vibe-coded their own ComfyUI mod rather than find this.
    setup_p = subparsers.add_parser(
        "setup", help="ONE COMMAND: detect -> resolve -> apply -> verify -> register")
    setup_p.add_argument("--prefer-engine", default="auto",
                         help="tiebreaker: comfyui | sana (not a hard gate)")
    setup_p.add_argument("--recipe-id", default="",
                         help="skip resolution and use this recipe")
    setup_p.add_argument("--host-port", type=int, default=0,
                         help="host port to publish (avoids clashing with an existing engine)")
    setup_p.add_argument("--tenant", default="", help="tenant for private model resolution")
    setup_p.add_argument("--network", default="", help="container network to join")
    setup_p.add_argument("--genesis-url", default="", help="Genesis URL for registration")
    setup_p.add_argument("--no-register", action="store_true",
                         help="skip fleet registration (it is skipped anyway without a token)")
    setup_p.add_argument("--dry-run", action="store_true",
                         help="plan only; nothing is executed")
    setup_p.set_defaults(handler=_handle_setup)

    args = parser.parse_args()

    if getattr(args, "self_test", False):
        return self_test()

    if not hasattr(args, "handler"):
        parser.print_help()
        return 1

    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2))
        if "error" in result:
            return 1
        # A 'degraded' backend (up but zero models) must NOT exit 0 — that is the
        # whole failure mode this pack exists to catch, and CI would sail past it.
        # 'unknown' gets its OWN code: the backend is up but the capability probe
        # did not answer, so model state was never determined. Conflating that
        # with degraded would report a false download failure.
        if result.get("status") == "degraded":
            return 2
        if result.get("status") == "unknown":
            return 3
        return 0
    except Exception as e:  # noqa: BLE001 — CLI must never traceback at the user
        print(json.dumps({"error": str(e), "fix": "check arguments and system state"}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
