"""CLI for node_bootstrap tools via argparse.

Subcommands map 1:1 to node_* tools. Output is JSON to stdout. Exit 0 on success,
1 on error (when output dict contains "error" key).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools


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
    )


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
        default="vllm",
        help="backend type (vllm, ollama, etc.)",
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

    args = parser.parse_args()

    if not hasattr(args, "handler"):
        parser.print_help()
        return 1

    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2))
        return 0 if "error" not in result else 1
    except Exception as e:
        error_result = {"error": str(e), "fix": "check arguments and system state"}
        print(json.dumps(error_result, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
