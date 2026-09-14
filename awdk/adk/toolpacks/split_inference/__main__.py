"""CLI for split_inference tools via argparse.

Subcommands map 1:1 to split_* tools. JSON to stdout.

  python -m adk.toolpacks.split_inference topology
  python -m adk.toolpacks.split_inference resolve
  python -m adk.toolpacks.split_inference plan --recipe-id bonsai-27b-5090-dgx-rpc
  python -m adk.toolpacks.split_inference apply --recipe-id ... --stage build --dry-run
  python -m adk.toolpacks.split_inference verify --recipe-id ... --base-url http://localhost:8080

Exit codes: 0 healthy, 1 error, 2 degraded, 3 unknown, 4 local_only (NOT a split).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools


def _handle_topology(args) -> dict:
    backends = [b.strip() for b in (args.backends or "").split(",") if b.strip()]
    return tools.split_detect_topology(container=args.container, backends=backends)


def _handle_resolve(args) -> dict:
    return tools.split_resolve_recipe(recipe_id=args.recipe_id, container=args.container)


def _handle_plan(args) -> dict:
    return tools.split_plan_deployment(recipe_id=args.recipe_id, container=args.container)


def _handle_apply(args) -> dict:
    return tools.split_apply(
        recipe_id=args.recipe_id, stage=args.stage,
        container=args.container, dry_run=args.dry_run,
    )


def _handle_verify(args) -> dict:
    return tools.split_verify(
        recipe_id=args.recipe_id, base_url=args.base_url,
        container=args.container, timeout_s=args.timeout,
    )


def main() -> int:
    """Main CLI entry point."""
    p = argparse.ArgumentParser(
        description="Split inference — multi-node llama.cpp RPC model sharding",
        prog="python -m adk.toolpacks.split_inference",
    )
    p.add_argument("--container", default="", help="llama.cpp container name")
    sub = p.add_subparsers(dest="command", help="subcommand")

    t_p = sub.add_parser("topology", help="detect local devices + RPC backends")
    t_p.add_argument("--backends", default="", help="comma-separated host:port list")
    t_p.set_defaults(handler=_handle_topology)

    r_p = sub.add_parser("resolve", help="resolve a split recipe for this topology")
    r_p.add_argument("--recipe-id", default="", help="explicit recipe ID")
    r_p.set_defaults(handler=_handle_resolve)

    pl_p = sub.add_parser("plan", help="plan the build + start sequence (pure)")
    pl_p.add_argument("--recipe-id", required=True)
    pl_p.set_defaults(handler=_handle_plan)

    a_p = sub.add_parser("apply", help="execute a stage")
    a_p.add_argument("--recipe-id", required=True)
    a_p.add_argument("--stage", default="build", choices=["build", "main"])
    a_p.add_argument("--dry-run", action="store_true")
    a_p.set_defaults(handler=_handle_apply)

    v_p = sub.add_parser("verify", help="PROVE the split is real")
    v_p.add_argument("--recipe-id", default="")
    v_p.add_argument("--base-url", default="", help="main server URL for an inference probe")
    v_p.add_argument("--timeout", type=float, default=60.0)
    v_p.set_defaults(handler=_handle_verify)

    args = p.parse_args()
    if not hasattr(args, "handler"):
        p.print_help()
        return 1

    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2))
        if "error" in result:
            return 1
        status = result.get("status")
        # local_only gets its OWN code: the deployment "works" but is NOT a split.
        # Exiting 0 there is exactly how a silent fallback ships to production.
        return {"degraded": 2, "unknown": 3, "local_only": 4}.get(status, 0)
    except Exception as e:  # noqa: BLE001 — CLI must never traceback at the user
        print(json.dumps({"error": str(e), "fix": "check arguments and system state"}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
