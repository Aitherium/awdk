"""
AitherARC Plugin for AitherShell
=================================

Watch and steer the ARC-AGI-3 agent from the shell.

Usage:
    /arc                     what she is doing right now
    /arc watch               the board, as text
    /arc say <text>          say something in the room
    /arc vote ACTION3        suggest her next move
    /arc vote 38 30          suggest a click at that cell (ACTION6)
    /arc actions             the action space the server accepts

Aliases: /aitherarc

Talks to the ARC playground directly (:8198 / arc.aitherium.com) rather than
through a gateway: the playground IS the data plane, owns its own rate limits,
and is public by design, so a second front door would only be a second place for
the contract to drift.
"""

import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

# http, not https. Measured 2026-08-07: this service answers HTTP only on 8198
# (standalone uvicorn, no ssl_keyfile), and an https upgrade is a dropped
# connection that reads as the service being DOWN.
DEFAULT_URL = "http://localhost:8198"


def _arc_url() -> str:
    return os.environ.get("AITHER_ARC_URL", DEFAULT_URL).rstrip("/")


async def _get(path: str) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{_arc_url()}{path}")
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: Dict[str, Any]) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{_arc_url()}{path}", json=body)
        return r.json()


# ARC's board palette as terminal blocks. Index 0 is the background and stays
# dim so the grid reads as a lit thing rather than a wall of colour.
_BLOCKS = "·▁▂▃▄▅▆▇█▓▒░◆◇○●"


class ArcPlugin(SlashCommand):
    name: str = "arc"
    aliases: List[str] = ["aitherarc"]
    description: str = "Watch and steer the ARC-AGI-3 agent — board, room, votes"
    category: str = "labs"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same. Without this,
        # /arc resolves to nothing (measured 2026-08-30: the registry
        # loaded the class, checked the CLASS attr, and registered the
        # instance under '' — arc was absent from every command list).
        super().__init__(
            name='arc',
            description='Watch and steer the ARC-AGI-3 agent — board, room, votes',
            aliases=['aitherarc'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = (args[0].lower() if args else "status")
        try:
            if sub in ("status", ""):
                return await self._status()
            if sub == "watch":
                return await self._watch()
            if sub == "say":
                return await self._say(args[1:])
            if sub == "vote":
                return await self._vote(args[1:])
            if sub == "actions":
                return await self._actions()
            return self.get_help() if hasattr(self, "get_help") else __doc__
        except Exception as e:
            # Name the URL. "connection refused" with no host is the single most
            # useless shell error, and this service moves between localhost and
            # arc.aitherium.com depending on where the shell is running.
            return f"Cannot reach ARC at {_arc_url()}: {e}"

    async def _status(self) -> str:
        snap = await _get("/api/snapshot")
        st = snap.get("state") or {}
        run = snap.get("run") or {}
        state = run.get("state") or "unknown"
        bits = [f"**{state}**"]
        if st.get("game"):
            bits.append(f"game `{st['game']}`")
        if st.get("turn") is not None:
            bits.append(f"turn {st['turn']}")
        if st.get("score") is not None:
            bits.append(f"level {st['score']}")
        if st.get("viewers"):
            bits.append(f"{st['viewers']} watching")
        return "AitherARC — " + " · ".join(bits)

    async def _watch(self) -> str:
        snap = await _get("/api/snapshot")
        frame = ((snap.get("state") or {}).get("frame"))
        if not isinstance(frame, list) or not frame:
            return "No frame yet — she may be between runs."
        grid = frame[0] if isinstance(frame[0], list) and isinstance(frame[0][0], list) else frame
        # Sample every other row/col: a 64x64 grid is unreadable at 1 char per
        # cell in a normal terminal, and a wrapped board is worse than none.
        rows = []
        for y in range(0, len(grid), 2):
            row = grid[y]
            rows.append("".join(_BLOCKS[(row[x] or 0) % len(_BLOCKS)]
                                for x in range(0, len(row), 2)))
        return "```\n" + "\n".join(rows) + "\n```"

    async def _say(self, rest: List[str]) -> str:
        text = " ".join(rest).strip()
        if not text:
            return "Say what? `/arc say nice one`"
        nick = os.environ.get("AITHER_NICK") or os.environ.get("USER") or "shell"
        res = await _post("/api/steer", {"kind": "msg", "nick": nick,
                                         "name": nick, "text": text})
        if res.get("ok"):
            return f"→ #playground as `{nick}`"
        # Report the refusal. It is rate limited per IP, and a silent success
        # message would be indistinguishable from a message that landed.
        return f"Not sent: {res.get('error', 'refused')}"

    async def _vote(self, rest: List[str]) -> str:
        if not rest:
            return "Vote for what? `/arc vote ACTION3` or `/arc vote 38 30`"
        body: Dict[str, Any]
        if len(rest) >= 2 and rest[0].isdigit() and rest[1].isdigit():
            body = {"kind": "vote", "action": "ACTION6",
                    "x": int(rest[0]), "y": int(rest[1])}
            label = f"ACTION6({rest[0]},{rest[1]})"
        else:
            label = rest[0].upper()
            body = {"kind": "vote", "action": label}
        res = await _post("/api/steer", body)
        if res.get("ok"):
            tally = ", ".join(f"{k} {v}" for k, v in sorted(res.get("votes", {}).items()))
            return f"voted {label} — now: {tally or 'first vote of the window'}"
        return f"Not counted: {res.get('error', 'refused')}"

    async def _actions(self) -> str:
        """Ask the SERVER what it accepts rather than printing a local list.

        A hardcoded copy is how ACTION7 stayed unreachable from the web app: the
        client offered ACTION1..ACTION5 while the server accepted seven, and an
        action a UI never offers produces no error to notice."""
        summary = await _get("/api/steer/summary")
        actions = summary.get("valid_actions")
        if not actions:
            return ("This ARC build does not serve `valid_actions` yet — upgrade it "
                    "rather than guessing the action space here.")
        return ("Accepted: " + ", ".join(actions)
                + "\n(ACTION6 needs x y; RESET discards the attempt)")

    def get_help(self) -> str:
        return __doc__ or "AitherARC"
