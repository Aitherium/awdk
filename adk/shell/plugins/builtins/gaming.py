"""
GPU / Fleet verbs for AitherShell (awsh)
=========================================

The owner's verb set (2026-09-27: "all the app surfaces ... need to be updated to work
with awnix, like GPU sleep and fleet sleep"), from the shell prompt. Every verb shells to
the AitherOS repo's ``AitherOS/dev/tools/fleet_verbs.py`` -- the same implementation as
awdesk's Fleet window, ``adk gpu|fleet``, the ``aither`` CLI, awnode's MCP tools and
AitherZero.

Usage:
    /gpu sleep | /gaming            GPU sleep: gaming lock, posture gaming (lanes to the Spark),
                                    park every 5090 GPU unit
    /gpu wake  | /gaming resume     GPU wake: units back one at a time, posture + lanes home,
                                    lock released (refused while GPU access is blocked)
    /gpu status | /gaming status    distro, systemd, containers, GPU access, posture, lock, record
    /fleet sleep | wake | critical  the whole fleet (sleep recorded, wake health-gated)
    add --dry-run to print the steps without running them; --force wakes the GPU mid-game

A typed slash command IS the consent, so verbs run with --execute unless --dry-run.
Aliases: /gaming, /game (for /gpu)

Before 2026-09-27 /gaming ran `llm-quiesce.ps1 gaming` (= quiesce --deep): no posture switch,
no MicroScheduler re-route, no gaming lock -- and on this checkout the ps1 still named Debian.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_GPU_WORDS = {
    "sleep": "sleep", "off": "sleep", "stop": "sleep", "enter": "sleep", "free": "sleep",
    "quiet": "sleep",
    # the old /gaming words: off/stop = services off (sleep), on/start = services on (wake)
    "wake": "wake", "resume": "wake", "back": "wake", "start": "wake", "up": "wake", "on": "wake",
    "status": "status",
}
_FLEET_WORDS = {"sleep": "sleep", "down": "sleep", "wake": "wake", "up": "wake",
                "critical": "critical", "status": "status"}


def build_verb_args(noun: str, args: List[str]) -> List[str]:
    """Slash words -> fleet_verbs.py argv (after the script path). ValueError if unknown.

    ``/gpu`` alone is GPU sleep (the old ``/gaming`` meaning); ``/fleet`` alone is status."""
    words = [a.lower() for a in args if not a.startswith("--")]
    dry = "--dry-run" in args
    force = "--force" in args
    table = _GPU_WORDS if noun == "gpu" else _FLEET_WORDS
    word = words[0] if words else ("sleep" if noun == "gpu" else "status")
    verb = table.get(word)
    if verb is None:
        raise ValueError(f"/{noun} {word}: one of {', '.join(sorted(set(table.values())))}")
    if verb == "status":
        return ["status"]
    out = [noun, verb]
    if not dry:
        out.append("--execute")
    if force and noun == "gpu" and verb == "wake":
        out.append("--force")
    return out


def _run_tool(argv: List[str]) -> str:
    from adk.commands.fleet_host import find_tool
    from adk.commands.fleet_verbs import TOOL_REL

    tool = find_tool(rel=TOOL_REL)
    if tool is None:
        return "[x] fleet_verbs.py not found: set AITHEROS_ROOT to the AitherOS checkout."
    try:
        r = subprocess.run(  # blocking-ok: single-user REPL, output streams to the terminal
            [sys.executable, str(tool), *argv], timeout=7800, check=False,
            cwd=str(tool.parents[3]))
    except subprocess.TimeoutExpired:
        return "[!] fleet_verbs.py timed out"
    if r.returncode == 2:
        return "could not judge (exit 2): the fleet host did not answer"
    return "" if r.returncode == 0 else f"[exit {r.returncode}]"


class GamingModePlugin(SlashCommand):
    name = "gpu"
    description = "GPU sleep / wake / status (the owner's verbs; fleet_verbs.py)"
    aliases = ["gaming", "game"]

    def __init__(self):
        super().__init__(name="gpu", description=self.description, aliases=["gaming", "game"])

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if args and args[0].lower() == "help":
            return __doc__
        try:
            argv = build_verb_args("gpu", args)
        except ValueError as exc:
            return f"{exc}\n{__doc__}"
        return _run_tool(argv)


class FleetVerbsPlugin(SlashCommand):
    name = "fleet"
    description = "Fleet sleep / wake / critical / status (the owner's verbs; fleet_verbs.py)"
    aliases: List[str] = []

    def __init__(self):
        super().__init__(name="fleet", description=self.description, aliases=[])

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if args and args[0].lower() == "help":
            return __doc__
        try:
            argv = build_verb_args("fleet", args)
        except ValueError as exc:
            return f"{exc}\n{__doc__}"
        return _run_tool(argv)
