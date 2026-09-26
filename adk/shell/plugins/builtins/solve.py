"""
Solve Plugin for AitherShell
============================

Run the general reasoning loop (``adk solve``) once, bounded, from the shell.

Usage:
    /solve                         toy environment, default limits
    /solve arc ls20                an ARC-AGI-3 game (needs the arc extra)
    /solve toy --max-calls 10      limits: --max-calls --max-actions --wall-s
    /solve arc ls20 --tier reasoning | --backend <preset> [--model M]

Distinct from /arc, which watches and steers the fleet's ARC solver; /solve runs
the loop HERE, as a child ``python -m adk.cli solve --json`` process, and prints
its summary. Limits are clamped (60 calls, 500 actions, 900 s).
"""

from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_FLAGS = {
    "--max-calls": "max_calls",
    "--max-actions": "max_actions",
    "--wall-s": "wall_s",
    "--tier": "tier",
    "--backend": "backend",
    "--model": "model",
    "--game": "game",
    "--env-dir": "env_dir",
    "--run-dir": "run_dir",
}


def parse_solve_args(args: List[str]) -> Dict[str, Any]:
    """`/solve [toy|arc] [game] [--flag value …]` -> run_bounded_solve args."""
    out: Dict[str, Any] = {}
    positional: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _FLAGS:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % a)
            out[_FLAGS[a]] = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            raise ValueError("unknown flag %s" % a)
        positional.append(a)
        i += 1
    if positional:
        out["env"] = positional[0]
    if len(positional) > 1:
        out["game"] = positional[1]
    return out


class SolvePlugin(SlashCommand):
    name: str = "solve"
    aliases: List[str] = []
    description: str = "Run the reasoning loop once, bounded (adk solve)"
    category: str = "labs"

    def __init__(self) -> None:
        # Explicit for the same reason as ArcPlugin: the dataclass base would
        # otherwise register this instance under the empty string.
        super().__init__(
            name="solve", description="Run the reasoning loop once, bounded (adk solve)", aliases=[]
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if args and args[0] in ("help", "-h", "--help"):
            return self.get_help()
        try:
            parsed = parse_solve_args(args)
        except ValueError as exc:
            return "solve: %s\n%s" % (exc, self.get_help())
        import asyncio

        from adk.reasoning.solve._shell import render_summary, run_bounded_solve

        summary = await asyncio.to_thread(run_bounded_solve, parsed)
        return render_summary(summary)

    def get_help(self) -> str:
        return __doc__ or "solve"
