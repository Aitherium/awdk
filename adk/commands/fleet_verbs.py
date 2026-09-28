"""The owner's fleet verbs from ``adk``: gpu sleep|wake|status, fleet sleep|wake|critical.

A thin shell over the AitherOS repo's ``AitherOS/dev/tools/fleet_verbs.py`` -- the ONE
implementation awdesk's Fleet window, the ``aither`` CLI, awnode's MCP tools and AitherZero
run too -- so the verbs cannot come to mean different things per surface. awdk never
imports AitherOS code (the wheel boundary); it runs the tool as a subprocess from the
checkout ``adk.commands.fleet_host.find_tool`` finds.

    adk gpu status   [--json]
    adk gpu sleep    [--execute] [--json]
    adk gpu wake     [--execute] [--force] [--json]
    adk fleet sleep  [--execute] [--json]        # `adk fleet` otherwise manages AGENTS;
    adk fleet wake   [--execute] [--json]        # sleep/wake/critical are the machine's
    adk fleet critical [--execute] [--json]

Every mutating verb is a DRY RUN unless ``--execute``. gpu wake refuses when awnix reports
"GPU access blocked" (a maintenance restart is the fix) and while a game runs (``--force``).
Exit codes pass through: 0 ok · 1 refused/failed · 2 could not judge.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, List

TOOL_REL = Path("AitherOS") / "dev" / "tools" / "fleet_verbs.py"
GPU_VERBS = ("status", "sleep", "wake")
FLEET_VERBS = ("sleep", "wake", "critical")


def build_argv(noun: str, verb: str, tool: Path, *, execute: bool = False,
               force: bool = False, as_json: bool = False) -> List[str]:
    """The exact subprocess argv: ``adk gpu sleep --execute`` -> ``fleet_verbs.py gpu sleep
    --execute``; ``adk gpu status`` -> ``fleet_verbs.py status`` (the verb set's status)."""
    if noun == "gpu" and verb not in GPU_VERBS:
        raise ValueError(f"adk gpu {verb}: one of {', '.join(GPU_VERBS)}")
    if noun == "fleet" and verb not in FLEET_VERBS:
        raise ValueError(f"adk fleet {verb}: one of {', '.join(FLEET_VERBS)}")
    words = ["status"] if verb == "status" else [noun, verb]
    argv = [sys.executable, str(tool), *words]
    if execute and verb != "status":
        argv.append("--execute")
    if force and verb != "status":
        argv.append("--force")
    if as_json:
        argv.append("--json")
    return argv


def run_verb(noun: str, verb: str, *, execute: bool = False, force: bool = False,
             as_json: bool = False) -> int:
    from adk.commands.fleet_host import find_tool

    tool = find_tool(rel=TOOL_REL)
    if tool is None:
        print("fleet_verbs.py not found: run from the AitherOS repo or set AITHEROS_ROOT",
              file=sys.stderr)
        return 2
    try:
        argv = build_argv(noun, verb, tool, execute=execute, force=force, as_json=as_json)
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
                    as_json=getattr(args, "json", False))


def _flags(p: Any, verb: str, force: bool = False) -> None:
    p.add_argument("--json", action="store_true")
    if verb != "status":
        p.add_argument("--execute", action="store_true", help="actually run (default: dry run)")
    if force:
        p.add_argument("--force", action="store_true",
                       help="wake the GPU even while a game runs")


def register_gpu_parser(sub: Any) -> None:
    """``adk gpu status|sleep|wake`` (top level)."""
    p = sub.add_parser("gpu", help="the 5090: status | sleep (game on) | wake (game off) -- "
                                   "fleet_verbs.py, dry-run default")
    gsub = p.add_subparsers(dest="gpu_command")
    for verb in GPU_VERBS:
        _flags(gsub.add_parser(verb, help=f"gpu {verb}"), verb, force=(verb == "wake"))


def register_fleet_verbs(fleet_sub: Any) -> None:
    """``adk fleet sleep|wake|critical`` on the EXISTING ``adk fleet`` subparsers (the
    agent-fleet verbs create/list/rm/status/connect-local are untouched)."""
    for verb in FLEET_VERBS:
        _flags(fleet_sub.add_parser(verb, help=f"machine fleet {verb} (fleet_verbs.py; "
                                               "dry-run default)"), verb)
