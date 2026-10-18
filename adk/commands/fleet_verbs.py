"""The owner's fleet verbs from ``adk``: gpu, fleet, and the inference toggle.

A thin shell over the AitherOS repo's ``AitherOS/dev/tools/fleet_verbs.py`` -- the ONE
implementation awdesk's Fleet window, the ``aither`` CLI, awnode's MCP tools and AitherZero
run too -- so the verbs cannot come to mean different things per surface. awdk never
imports AitherOS code (the wheel boundary); it runs the tool as a subprocess from the
checkout ``adk.commands.fleet_host.find_tool`` finds.

    adk inference on|dgx|off|status [--execute] [--spark] [--json]
    adk gpu status   [--json]
    adk gpu sleep    [--execute] [--json]        # = inference dgx
    adk gpu wake     [--execute] [--force] [--json]   # = inference on
    adk fleet sleep  [--execute] [--json]        # `adk fleet` otherwise manages AGENTS;
    adk fleet wake   [--execute] [--json]        # sleep/wake/park/critical/profile are the
    adk fleet park   [--execute] [--json]        # machine's
    adk fleet critical [--execute] [--json]
    adk fleet profile business|platform|critical|full [--execute] [--json]

``inference <mode>`` is the toggle: ``on`` = the desktop GPU's lanes home, ``dgx`` = the
desktop GPU serves nothing and the lanes run on the DGX, ``off`` = every LLM lane refuses at
once, ``status`` = one tiny completion per lane decides the mode (never a unit's state).
``fleet sleep`` frees the PC for a game by turning its WSL VM off -- every hosted API goes
dark with it; ``fleet profile business`` keeps the business set up instead.

Every mutating verb is a DRY RUN unless ``--execute``. Exit codes pass through: 0 ok ·
1 refused/failed/drift · 2 could not judge.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, List

TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_verbs.py"
GPU_VERBS = ("status", "sleep", "wake")
FLEET_VERBS = ("sleep", "wake", "park", "critical", "profile")
INFERENCE_VERBS = ("on", "dgx", "off", "status")
PROFILE_TIERS = ("business", "platform", "critical", "full")


def build_argv(noun: str, verb: str, tool: Path, *, execute: bool = False,
               force: bool = False, as_json: bool = False, tier: str = "",
               spark: bool = False) -> List[str]:
    """The exact subprocess argv: ``adk gpu sleep --execute`` -> ``fleet_verbs.py gpu sleep
    --execute``; ``adk gpu status`` -> ``fleet_verbs.py status`` (the verb set's status);
    ``adk inference dgx`` -> ``fleet_verbs.py inference dgx``; ``adk fleet profile business``
    -> ``fleet_verbs.py fleet profile business``."""
    if noun == "gpu" and verb not in GPU_VERBS:
        raise ValueError(f"adk gpu {verb}: one of {', '.join(GPU_VERBS)}")
    if noun == "fleet" and verb not in FLEET_VERBS:
        raise ValueError(f"adk fleet {verb}: one of {', '.join(FLEET_VERBS)}")
    if noun == "inference" and verb not in INFERENCE_VERBS:
        raise ValueError(f"adk inference {verb}: one of {', '.join(INFERENCE_VERBS)}")
    if noun not in ("gpu", "fleet", "inference"):
        raise ValueError(f"unknown noun {noun!r}")
    if noun == "fleet" and verb == "profile":
        if tier not in PROFILE_TIERS:
            raise ValueError(f"adk fleet profile {tier or '?'}: one of "
                             f"{', '.join(PROFILE_TIERS)}")
        words = ["fleet", "profile", tier]
    elif noun == "inference":
        words = ["inference", verb]
    else:
        words = ["status"] if verb == "status" else [noun, verb]
    argv = [sys.executable, str(tool), *words]
    if execute and verb != "status":
        argv.append("--execute")
    if force and verb != "status":
        argv.append("--force")
    if spark and noun == "inference" and verb == "off":
        argv.append("--spark")
    if as_json:
        argv.append("--json")
    return argv


def run_verb(noun: str, verb: str, *, execute: bool = False, force: bool = False,
             as_json: bool = False, tier: str = "", spark: bool = False) -> int:
    from adk.commands.fleet_host import find_tool

    tool = find_tool(rel=TOOL_REL)
    if tool is None:
        print("fleet_verbs.py not found: run from the AitherOS repo or set AITHEROS_ROOT",
              file=sys.stderr)
        return 2
    try:
        argv = build_argv(noun, verb, tool, execute=execute, force=force, as_json=as_json,
                          tier=tier, spark=spark)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return subprocess.run(argv, cwd=str(tool.parents[3]), timeout=7800,
                              check=False).returncode
    except subprocess.TimeoutExpired:
        print("fleet_verbs.py timed out", file=sys.stderr)
        return 2


def cmd_gpu(args: Any) -> int:
    return run_verb("gpu", getattr(args, "gpu_command", None) or "status",
                    execute=getattr(args, "execute", False), force=getattr(args, "force", False),
                    as_json=getattr(args, "json", False))


def cmd_fleet_verb(args: Any) -> int:
    return run_verb("fleet", args.fleet_command, execute=getattr(args, "execute", False),
                    as_json=getattr(args, "json", False), tier=getattr(args, "tier", "") or "")


def cmd_inference(args: Any) -> int:
    return run_verb("inference", getattr(args, "inference_command", None) or "status",
                    execute=getattr(args, "execute", False),
                    as_json=getattr(args, "json", False), spark=getattr(args, "spark", False))


def _flags(p: Any, verb: str, force: bool = False, spark: bool = False) -> None:
    p.add_argument("--json", action="store_true")
    if verb != "status":
        p.add_argument("--execute", action="store_true", help="actually run (default: dry run)")
    if force:
        p.add_argument("--force", action="store_true",
                       help="wake the GPU even while a game runs")
    if spark:
        p.add_argument("--spark", action="store_true",
                       help="also stop the DGX pool (reload cost)")


def register_gpu_parser(sub: Any) -> None:
    """``adk gpu status|sleep|wake`` (top level)."""
    p = sub.add_parser("gpu", help="the desktop GPU: status | sleep (= inference dgx) | wake "
                                   "(= inference on) -- fleet_verbs.py, dry-run default")
    gsub = p.add_subparsers(dest="gpu_command")
    for verb in GPU_VERBS:
        _flags(gsub.add_parser(verb, help=f"gpu {verb}"), verb, force=(verb == "wake"))


def register_inference_parser(sub: Any) -> None:
    """``adk inference on|dgx|off|status`` (top level) -- the one inference toggle."""
    p = sub.add_parser("inference", help="the inference toggle: on | dgx | off | status "
                                         "(status proves the mode with a real completion per "
                                         "lane) -- fleet_verbs.py, dry-run default")
    isub = p.add_subparsers(dest="inference_command")
    helps = {"on": "desktop GPU lanes home, proven", "dgx": "desktop GPU serves nothing; "
             "lanes on the DGX, proven", "off": "every LLM lane refuses at once",
             "status": "measure the mode (one tiny completion per lane)"}
    for verb in INFERENCE_VERBS:
        _flags(isub.add_parser(verb, help=helps[verb]), verb, spark=(verb == "off"))


def register_fleet_verbs(fleet_sub: Any) -> None:
    """``adk fleet sleep|wake|park|critical|profile`` on the EXISTING ``adk fleet``
    subparsers (the agent-fleet verbs create/list/rm/status/connect-local are untouched)."""
    for verb in FLEET_VERBS:
        p = fleet_sub.add_parser(verb, help=f"machine fleet {verb} (fleet_verbs.py; "
                                            "dry-run default)")
        if verb == "profile":
            p.add_argument("tier", choices=PROFILE_TIERS)
        _flags(p, verb)
