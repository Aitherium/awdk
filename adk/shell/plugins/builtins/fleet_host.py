"""
Fleet Host Plugin for AitherShell (awsh)
=========================================

The awnix fleet host (the WSL2 distro that runs the podman fleet) from the shell
prompt. Same engine as ``adk fleet-host``: every verb shells to the AitherOS
repo's ``AitherOS/dev/tools/fleet_host.py``, so the prompt, the CLI, awdesk and
awnode's MCP tools report one verdict.

Usage:
    /fleethost                     status (read-only; never boots the distro)
    /fleethost start|stop|restart  DRY RUN: prints the exact commands
    /fleethost reattach            DRY RUN: re-attach the fleet data disk
    /fleethost migrate [mode]      the migrate-fleet-to-awnix playbook in -DryRun
    /fleethost <verb> --execute    actually run it

Aliases: /fh, /fleet-host
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_VERBS = ("status", "start", "stop", "restart", "reattach", "migrate")
_MODES = ("preflight", "rehearse", "cutover")


def build_args(args: List[str]) -> List[str]:
    """Slash-command words -> fleet_host.py argv (after the script path)."""
    words = [a for a in args if not a.startswith("--")]
    flags = [a for a in args if a in ("--execute", "--force", "--terminate", "--json")]
    verb = words[0].lower() if words else "status"
    if verb not in _VERBS:
        raise ValueError(f"unknown verb {verb!r} (one of {', '.join(_VERBS)})")
    out = [verb]
    if verb == "migrate" and len(words) > 1 and words[1].lower() in _MODES:
        out += ["--mode", words[1].lower()]
    if verb == "status":
        flags = [f for f in flags if f == "--json"]
    return out + flags


class FleetHostPlugin(SlashCommand):
    name = "fleethost"
    description = (
        "awnix fleet host: status, start/stop/restart, reattach data, migrate (dry-run default)"
    )
    aliases = ["fh", "fleet-host"]

    def __init__(self):
        super().__init__(
            name="fleethost", description=self.description, aliases=["fh", "fleet-host"]
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if args and args[0].lower() == "help":
            return __doc__
        try:
            argv = build_args(args)
        except ValueError as exc:
            return f"{exc}\n{__doc__}"
        from adk.commands.fleet_host import find_tool

        tool = find_tool()
        if tool is None:
            return "fleet_host.py not found: set AITHEROS_ROOT to the AitherOS checkout."
        try:
            # Interactive REPL: one command, output streamed to the terminal.
            r = subprocess.run(  # blocking-ok: single-user REPL, no concurrent turns
                [sys.executable, str(tool), *argv],
                timeout=7800,
                check=False,
                cwd=str(tool.parents[3]),
            )
        except subprocess.TimeoutExpired:
            return "fleet_host.py timed out"
        if r.returncode == 2:
            return "could not judge (exit 2): wsl.exe missing, wedged or timed out"
        return "" if r.returncode == 0 else f"exit {r.returncode}"
