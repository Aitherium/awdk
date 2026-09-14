"""CLI for llm_serving tools via argparse. JSON to stdout.

  python -m adk.toolpacks.llm_serving detect
  python -m adk.toolpacks.llm_serving resolve --model orchestrator
  python -m adk.toolpacks.llm_serving plan --model reasoner
  python -m adk.toolpacks.llm_serving apply --model orchestrator --dry-run
  python -m adk.toolpacks.llm_serving verify --model orchestrator --base-url http://localhost:8120

Exit codes: 0 healthy, 1 error, 2 degraded, 3 unknown, 4 wrong_model.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools


def _detect(a) -> dict:
    return tools.llm_detect_hardware()


def _resolve(a) -> dict:
    return tools.llm_resolve(model=a.model)


def _plan(a) -> dict:
    return tools.llm_plan_deployment(model=a.model)


def _apply(a) -> dict:
    return tools.llm_apply(model=a.model, dry_run=a.dry_run, install_vllm=a.install_vllm)


def _verify(a) -> dict:
    return tools.llm_verify(base_url=a.base_url, model=a.model, timeout_s=a.timeout)


def main() -> int:
    p = argparse.ArgumentParser(
        description="LLM serving — vLLM + fleet models, quant-optimized",
        prog="python -m adk.toolpacks.llm_serving",
    )
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("detect", help="which fleet models fit + optimal quant")
    d.set_defaults(handler=_detect)

    r = sub.add_parser("resolve", help="model role/id -> recipe + optimized quant")
    r.add_argument("--model", required=True, help="role or recipe id")
    r.set_defaults(handler=_resolve)

    pl = sub.add_parser("plan", help="render the vllm serve command")
    pl.add_argument("--model", required=True)
    pl.set_defaults(handler=_plan)

    a = sub.add_parser("apply", help="install vllm + launch the server")
    a.add_argument("--model", required=True)
    a.add_argument("--install-vllm", action="store_true", help="pip install -U vllm first")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(handler=_apply)

    v = sub.add_parser("verify", help="prove it with a real chat round-trip")
    v.add_argument("--base-url", required=True)
    v.add_argument("--model", default="", help="role or id (checks served name)")
    v.add_argument("--timeout", type=float, default=60.0)
    v.set_defaults(handler=_verify)

    args = p.parse_args()
    if not hasattr(args, "handler"):
        p.print_help()
        return 1
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2))
        if "error" in result:
            return 1
        return {"degraded": 2, "unknown": 3, "wrong_model": 4}.get(result.get("status"), 0)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
