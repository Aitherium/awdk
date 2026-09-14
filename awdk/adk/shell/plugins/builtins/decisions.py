"""`/decide` — decision cards from the shell.

    /decide list [status]              pending cards (default: open)
    /decide show <id>                  one card in full
    /decide answer <id> <choice> [note]
    /decide ask <headline> [options...] raise a new card
    /decide cancel <id> [note]
    /decide count                      how many are waiting

Aliases: /decision, /decisions

🚨 THIS FILE WAS INERT UNTIL 2026-08-19, and of everything in the builtins
directory it was the worst one to lose. It was written as a class with a
`register_commands(cli_group)` static method — a shape the shell's plugin loader
does not look for. The loader registers `SlashCommand` SUBCLASSES; nothing calls
`register_commands`, and `cli.py::_register_commands` is an unrelated function
that takes an argparse subparser. So `/decide` did not exist, and the failure was
silent: the module imported cleanly, the class was found, and no exception was
ever raised.

That matters because a decision card is how an agent reaches a human. The
decision-card contract already has a rule for the same class of defect —
`escalate_to_human` once wrote a log line and returned `"logged_locally"`,
raising nothing and telling nobody — and this was that defect one layer up: the
surface a human uses to ANSWER was missing while the store, the daemon and the
router were all correct.

The work is still done by `adk.decisions.cli`; this only makes the command
reachable.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.decisions.cli import main as decisions_cli
except ImportError:  # pragma: no cover
    decisions_cli = None  # type: ignore


class DecisionsPlugin(SlashCommand):
    """Decision cards — the surface a human answers on."""

    name = "decide"
    description = "Manage decision cards (human-in-the-loop approvals)"
    aliases = ["decision", "decisions"]

    def __init__(self) -> None:
        # Explicit. The dataclass base assigns `self.name = ""`, which shadows
        # the class attribute above and registers the plugin under the empty
        # string — where the next plugin to do the same overwrites it.
        super().__init__(
            name="decide",
            description="Manage decision cards (human-in-the-loop approvals)",
            aliases=["decision", "decisions"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if decisions_cli is None:
            return ("adk.decisions.cli is unavailable, so no card can be listed "
                    "or answered from here. Reporting that rather than printing "
                    "an empty list: 'no decisions' and 'I cannot see the "
                    "decisions' are different answers, and only one of them "
                    "means nobody is waiting on you.")

        sub = args[0].lower() if args else "list"
        rest = args[1:]

        if sub in ("help", "-h", "--help"):
            return __doc__

        argv = self._argv(sub, rest)
        if isinstance(argv, str):
            return argv

        # The underlying CLI writes to stdout and reads sys.argv. Capture both
        # so the shell can render the result instead of it escaping to the
        # terminal, and always restore argv — leaving it rewritten breaks the
        # next command in the session in a way that is hard to attribute.
        saved = sys.argv
        buf = io.StringIO()
        try:
            sys.argv = argv
            with redirect_stdout(buf):
                decisions_cli()
        except SystemExit:
            # argparse exits on --help and on a usage error. That is the CLI
            # answering, not a crash.
            pass
        except Exception as exc:  # noqa: BLE001
            return f"{buf.getvalue()}\n/decide {sub} failed: {exc}"
        finally:
            sys.argv = saved
        return buf.getvalue().strip() or "ok"

    @staticmethod
    def _argv(sub: str, rest: List[str]):
        """Translate a slash invocation into the CLI's argv, or a usage string."""
        if sub in ("list", "ls"):
            return ["adk", "decide", "list", "--status", rest[0] if rest else "open"]
        if sub in ("count", "n"):
            return ["adk", "decide", "count"]
        if sub == "show":
            if not rest:
                return "Usage: /decide show <id>"
            return ["adk", "decide", "show", rest[0]]
        if sub == "answer":
            if len(rest) < 2:
                return "Usage: /decide answer <id> <choice> [note]"
            argv = ["adk", "decide", "answer", rest[0], "--choice", rest[1]]
            if len(rest) > 2:
                argv += ["--note", " ".join(rest[2:])]
            return argv
        if sub == "ask":
            if not rest:
                return "Usage: /decide ask <headline> [option ...]"
            return ["adk", "decide", "ask", *rest]
        if sub == "cancel":
            if not rest:
                return "Usage: /decide cancel <id> [note]"
            argv = ["adk", "decide", "cancel", rest[0]]
            if len(rest) > 1:
                argv += ["--note", " ".join(rest[1:])]
            return argv
        return f"Unknown subcommand {sub!r}\n\n{__doc__}"
