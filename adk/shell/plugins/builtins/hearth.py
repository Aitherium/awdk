"""
Hearth Plugin for AitherShell
=============================

Talk to your running ``adk home serve`` from the shell -- a window onto the one
Hearth process over its loopback ``local`` channel, never a second agent.

Usage:
    /hearth <text>             send <text>; prints the replies and any approval card
    /hearth yes <nonce>        answer an approval card (or: no <nonce>)
    /hearth say <text>         send <text> verbatim (e.g. a message that starts
                               with "receipts" or "help")
    /hearth receipts [n]       the last n receipts (default 10) + the verify verdict
    /hearth cloud <verb>       your PLATFORM Hearth on the tool gateway (hearth_* tools):
                               status | reminders | due | receipts [n] | yes|no <code>
                               | remind <when> -- <text>

The token and the port are read from ``<home>/local.token``, which the running
serve writes once it listens. Start the server with ``adk home serve``. When nothing
is serving, the platform verbs (status, reminders, due, receipts, yes, no, remind)
are answered by your platform Hearth instead (see ``adk.home.gateway_hearth``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def _say(text: str) -> str:
    from adk.home.local_client import LocalClient, render_replies

    return render_replies(LocalClient().say(text))


def _receipts(n: int) -> str:
    from adk.home.local_client import LocalClient, render_receipts

    return render_receipts(LocalClient().receipts(n))


def _cloud(args: List[str]) -> str:
    from adk.home.gateway_hearth import run_verb

    return run_verb(args)


def hearth_command(args: List[str]) -> str:
    """Run one /hearth invocation synchronously and return what to print."""
    from adk.home.config import HomeError
    from adk.home.gateway_hearth import VERBS

    if not args or args[0] in ("help", "-h", "--help"):
        return __doc__ or "hearth"
    if args[0] == "cloud":
        return _cloud(args[1:])
    try:
        if args[0] == "receipts" and len(args) <= 2:
            raw = args[1] if len(args) == 2 else "10"
            if not raw.isdigit():
                return "hearth: usage: /hearth receipts [n]"
            return _receipts(int(raw))
        words = args[1:] if args[0] == "say" else args
        text = " ".join(words).strip()
        if not text:
            return "hearth: nothing to say"
        return _say(text)
    except HomeError as exc:            # LocalClientError included: unreachable, 401, 413
        if args[0] in VERBS:
            # No local Hearth answered: the same verb on the platform Hearth, labelled so
            # nobody mistakes which home replied.
            return f"(local Hearth unavailable: {exc})\nplatform Hearth:\n{_cloud(list(args))}"
        return f"hearth: {exc}\n  your platform Hearth: /hearth cloud status"


class HearthPlugin(SlashCommand):
    name: str = "hearth"
    aliases: List[str] = []
    description: str = "Your Hearth: the running adk home serve, else the platform Hearth"

    def __init__(self) -> None:
        super().__init__(name="hearth", aliases=[],
                         description="Your Hearth: the running adk home serve, else the "
                                     "platform Hearth")

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        # The client is synchronous and one turn can take minutes: a worker thread
        # keeps the shell's loop responsive.
        return await asyncio.to_thread(hearth_command, list(args))
