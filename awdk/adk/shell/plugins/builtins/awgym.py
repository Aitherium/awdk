"""
AitherGym Plugin for AitherShell
=================================

The ARC-AGI-3 training gym from the shell: the world model's health and
training progress, the game pool, and the solver-run ledger.

Usage:
    /awgym                     gym + world-model status
    /awgym games               the game pool
    /awgym runs                recent solver-run ledger rows
    /awgym burst [steps]       one throttled LeWM train burst (default 100)

Aliases: /gym

Talks to the gym through the genesis /gym proxy (the same surface the MCP
tools and the portal panel dial), so the shell, the tools and the panel can
never disagree about what the gym reports. Reads work without auth; the
burst is a write and carries the fleet internal key (X-Internal-Token — the
genesis proxy forwards ONLY that spelling, measured 2026-08-30).
"""

import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

# The gym is container-hosted; genesis proxies /gym verbatim. From inside the
# fleet this name resolves; from a dev shell use AITHER_GYM_URL to point at a
# gateway/edge that reaches it.
DEFAULT_URL = "https://aitheros-genesis:8001/gym"


def _gym_url() -> str:
    return os.environ.get("AITHER_GYM_URL", DEFAULT_URL).rstrip("/")


def _internal_token() -> str:
    return os.environ.get("AITHER_INTERNAL_SECRET", "")


async def _get(path: str) -> Any:
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_gym_url()}{path}")
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: Dict[str, Any]) -> Any:
    import httpx
    headers = {}
    if _internal_token():
        headers["X-Internal-Token"] = _internal_token()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_gym_url()}{path}", json=body, headers=headers)
        r.raise_for_status()
        return r.json()


class AwgymPlugin(SlashCommand):
    name: str = "awgym"
    aliases: List[str] = ["gym"]
    description: str = "The ARC-AGI-3 training gym — world model, games, solver runs"
    category: str = "labs"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='awgym',
            description='The ARC-AGI-3 training gym — world model, games, solver runs',
            aliases=['gym'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = (args[0].lower() if args else "status")
        try:
            if sub in ("status", ""):
                return await self._status()
            if sub == "games":
                return await self._games()
            if sub == "runs":
                return await self._runs()
            if sub == "burst":
                steps = int(args[1]) if len(args) > 1 and args[1].isdigit() else 100
                return await self._burst(steps)
            return __doc__
        except Exception as e:
            # Name the URL — "connection refused" with no host is the single
            # most useless shell error, and the gym moves between the fleet
            # name and an edge URL depending on where the shell is running.
            return f"Cannot reach the gym at {_gym_url()}: {e}"

    async def _status(self) -> str:
        wm = await _get("/wm/health")
        bits = [f"device {wm.get('device', '?')}"]
        if wm.get("train_steps") is not None:
            bits.append(f"train steps {wm['train_steps']:,}")
        if wm.get("recon") is not None:
            bits.append(f"recon {wm['recon']:.4f}")
        bits.append("checkpoint " + ("saved" if wm.get("checkpoint_exists") else "missing"))
        return "AitherGym — " + " · ".join(bits)

    async def _games(self) -> str:
        data = await _get("/games")
        games = data.get("games") or []
        lines = [f"**{len(games)} games**"]
        for g in games[:12]:
            tags = ",".join(g.get("tags") or [])
            lines.append(f"`{g['game_id']}`" + (f" ({tags})" if tags else ""))
        return "\n".join(lines)

    async def _runs(self) -> str:
        data = await _get("/score")
        runs = data.get("runs") or (data if isinstance(data, list) else [])
        if not runs:
            return "No solver runs yet."
        lines = ["recent runs:"]
        for r in runs[-6:][::-1]:
            surprise = r.get("mean_surprise")
            s = f"{surprise:.2f}" if surprise is not None else "—"
            lines.append(
                f"`{(r.get('game_id') or '?')[:14]}` sim={r.get('simulator', '?')} "
                f"steps={r.get('steps', 0)} surprise={s}")
        return "\n".join(lines)

    async def _burst(self, steps: int) -> str:
        out = await _post("/train", {"steps": steps})
        burst = out.get("burst") or {}
        if burst.get("step") is not None:
            return (f"burst done — step {burst['step']:,}, "
                    f"loss {burst.get('loss', 0):.1f}, "
                    f"recon {burst.get('mse', 0):.1f}")
        return f"burst returned: {out}"
