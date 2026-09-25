"""
AitherGrid Plugin for AitherShell
=================================

A thin slash surface over ``adk grid`` -- the plugin owns no grid logic; it
parses the arguments with the CLI's own grid parser and hands them to
``adk.cli.cmd_grid``, so the shell and the CLI can never drift.

Usage:
    /grid                      -- topology + health (same as /grid status)
    /grid add cluster <ip>     -- add a node
    /grid remove <ip>          -- remove a node
    /grid test [ip]            -- probe nodes
    /grid sync | pull          -- workspace sync
    /grid ls | enroll | deregister <id>   -- mesh registry
    /grid activate <key>       -- install a Grid Pro/Enterprise license

Alias: /aithergrid
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def grid_parser() -> argparse.ArgumentParser:
    """A parser carrying exactly the CLI's ``grid`` subcommand tree."""
    from adk import cli

    parser = argparse.ArgumentParser(prog="adk")
    cli._register_grid_parser(parser.add_subparsers(dest="command"))
    return parser


def _run_grid(args: List[str]) -> int:
    """Parse ``adk grid <args>`` with the CLI's own grid parser and run cmd_grid."""
    from adk import cli

    try:
        ns = grid_parser().parse_args(["grid", *args])
    except SystemExit as exc:  # argparse already printed the usage error
        return int(exc.code or 0)
    return int(cli.cmd_grid(ns) or 0)


class GridPlugin(SlashCommand):
    name = "grid"
    description = "AitherGrid: distributed inference nodes (status, add, test, activate ...)"
    aliases = ["aithergrid"]

    def __init__(self):
        super().__init__(
            name="grid",
            description="AitherGrid: distributed inference nodes (status, add, test, activate ...)",
            aliases=["aithergrid"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        # cmd_grid is synchronous and some branches call asyncio.run(), which
        # cannot run inside the shell's event loop -- give it a worker thread.
        rc = await asyncio.to_thread(_run_grid, list(args))
        if rc == 0:
            return ""  # cmd_grid printed its own output
        return f"adk grid exited {rc}"
