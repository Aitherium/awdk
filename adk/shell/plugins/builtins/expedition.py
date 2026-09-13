"""
Expedition Plugin for AitherShell
==================================

Drive multi-session project orchestration from the shell.

Usage:
    /expedition list [status]
    /expedition status <id>
    /expedition tasks <id>
    /expedition decisions <id>
    /expedition gates                      -- every open gate awaiting a human
    /expedition approve <gate_id> [note]
    /expedition reject <gate_id> [note]
    /expedition steer <id> <instruction>
    /expedition cancel <id>
    /expedition harnesses                  -- ratchet scoreboards + availability
    /expedition ratchet <harness> [rounds] -- start a keep-or-revert expedition

Aliases: /exp, /expeditions

Layer 6 of the .EXPEDITIONS 10-layer surface (owner: atlas).
"""

import asyncio
import os
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify
from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = (
            AuthStore.get_active_profile()
            if hasattr(AuthStore, "get_active_profile")
            else None
        )
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


async def _api_get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
        resp = await c.get(
            f"{_genesis_url()}{path}", params=params or {}, headers=_api_headers()
        )
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
        resp = await c.post(
            f"{_genesis_url()}{path}", json=body or {}, headers=_api_headers()
        )
        resp.raise_for_status()
        return resp.json()


def _format_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {
        c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in columns
    }
    lines = [" | ".join(c.ljust(widths[c]) for c in columns)]
    lines.append("-|-".join("-" * widths[c] for c in columns))
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(f"  {line}" for line in lines)


class ExpeditionPlugin(SlashCommand):
    name: str = "expedition"
    aliases: List[str] = ["exp", "expeditions"]
    description: str = (
        "Project orchestration — plan, execute, gate, steer, and ratchet"
    )
    category: str = "orchestration"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='expedition',
            description='Project orchestration — plan, execute, gate, steer, and ratchet',
            aliases=['exp', 'expeditions'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        dispatch = {
            "list": self._list,
            "ls": self._list,
            "status": self._status,
            "tasks": self._tasks,
            "decisions": self._decisions,
            "gates": self._gates,
            "approve": self._approve,
            "reject": self._reject,
            "steer": self._steer,
            "cancel": self._cancel,
            "harnesses": self._harnesses,
            "watch": self._watch,
            "open": self._open,
            "ratchet": self._ratchet,
            "help": lambda a, c: self.get_help(),
        }
        handler = dispatch.get(sub)
        if handler:
            result = handler(args[1:], ctx)
            return await result if hasattr(result, "__await__") else result
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    async def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        params = {"limit": 25}
        if args:
            params["status"] = args[0]
        try:
            result = await _api_get("/expedition/list", params)
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        rows = [
            {
                "id": str(e.get("id", "?"))[:12],
                "title": str(e.get("title", "?"))[:38],
                "status": e.get("status", "?"),
                "owner": e.get("owner", "-"),
            }
            for e in result.get("expeditions", [])
        ]
        return (
            f"**Expeditions** ({len(rows)})\n"
            f"{_format_table(rows, ['id', 'title', 'status', 'owner'])}"
        )

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /expedition status <id>"
        try:
            r = await _api_get(f"/expedition/{args[0]}/status")
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        lines = [f"**Expedition {args[0]}** — {r.get('status', '?')}"]
        for key in ("tasks_total", "tasks_complete", "open_gates", "cost_usd_used"):
            if key in r:
                lines.append(f"  {key}: {r[key]}")
        return "\n".join(lines)

    async def _watch(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Poll status until the expedition leaves a running state.

        Ported from `expedition_status.py`, which offered this and `open` and
        could never run: it imported `ShellContext` from `adk.shell`, a name
        that exists nowhere in the package, so the module raised at import and
        the loader swallowed it at DEBUG. Everything else that file did was
        already here, which is why only these two came across.
        """
        if not args:
            return "Usage: /expedition watch <id> [poll-seconds]"
        exp_id = args[0]
        try:
            interval = max(2, int(args[1])) if len(args) > 1 else 10
        except ValueError:
            return f"poll-seconds must be a number, got {args[1]!r}"
        # Bounded. An unbounded watch inside a shell is a command you cannot
        # get out of, and the caller can always run it again.
        polls = 60
        seen: List[str] = []
        for _ in range(polls):
            try:
                r = await _api_get(f"/expedition/{exp_id}/status")
            except Exception as e:  # noqa: BLE001
                seen.append(f"Cannot reach Genesis: {e}")
                return "\n".join(seen)
            state = r.get("status", "?")
            line = (f"{state}  tasks {r.get('tasks_complete', '?')}/"
                    f"{r.get('tasks_total', '?')}  gates {r.get('open_gates', '?')}")
            if not seen or seen[-1] != line:
                seen.append(line)
            if state in ("complete", "completed", "failed", "cancelled"):
                seen.append(f"finished: {state}")
                return "\n".join(seen)
            await asyncio.sleep(interval)
        seen.append(f"still running after {polls} polls — run "
                    f"`/expedition status {exp_id}` to check again")
        return "\n".join(seen)

    async def _open(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """The portal URL for an expedition."""
        if not args:
            return "Usage: /expedition open <id>"
        return f"https://api.aitherium.com/workspace/expeditions/{args[0]}"

    async def _tasks(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /expedition tasks <id>"
        try:
            r = await _api_get(f"/expedition/{args[0]}/tasks")
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        rows = [
            {
                "title": str(t.get("title", "?"))[:34],
                "agent": t.get("agent", "-"),
                "method": t.get("execution_method", "-"),
                "status": t.get("status", "-"),
            }
            for t in r.get("tasks", [])
        ]
        return (
            f"**Tasks** ({len(rows)})\n"
            f"{_format_table(rows, ['title', 'agent', 'method', 'status'])}"
        )

    async def _decisions(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /expedition decisions <id>"
        try:
            r = await _api_get(f"/expedition/{args[0]}/decisions")
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        rows = [
            {
                "category": d.get("category", "-"),
                "answer": str(d.get("answer", ""))[:30],
                "by": d.get("decided_by", "-"),
            }
            for d in r.get("decisions", [])
        ]
        return (
            f"**Decisions** ({len(rows)})\n"
            f"{_format_table(rows, ['category', 'answer', 'by'])}"
        )

    async def _gates(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Open gates across every expedition — the things waiting on YOU."""
        try:
            result = await _api_get("/expedition/list", {"limit": 50})
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        rows: List[Dict[str, Any]] = []
        for e in result.get("expeditions", []):
            eid = e.get("id")
            if not eid:
                continue
            try:
                st = await _api_get(f"/expedition/{eid}/status")
            except Exception:
                # One unreachable expedition must not hide the other gates.
                continue
            for g in st.get("open_gates", []) or []:
                rows.append({
                    "gate_id": str(g.get("id", "?"))[:12],
                    "expedition": str(e.get("title", eid))[:28],
                    "question": str(g.get("question", ""))[:40],
                })
        if not rows:
            return "No open gates."
        return (
            f"**Open gates** ({len(rows)}) — approve with /expedition approve <gate_id>\n"
            f"{_format_table(rows, ['gate_id', 'expedition', 'question'])}"
        )

    async def _respond(self, args: List[str], approved: bool) -> str:
        if not args:
            verb = "approve" if approved else "reject"
            return f"Usage: /expedition {verb} <gate_id> [note]"
        note = " ".join(args[1:]) if len(args) > 1 else ""
        try:
            r = await _api_post(
                f"/expedition/gates/{args[0]}/respond",
                {"approved": approved, "note": note},
            )
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        return f"Gate {args[0]}: {'APPROVED' if approved else 'REJECTED'} ({r.get('status', 'ok')})"

    async def _approve(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return await self._respond(args, True)

    async def _reject(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return await self._respond(args, False)

    async def _steer(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 2:
            return "Usage: /expedition steer <id> <instruction>"
        try:
            await _api_post(
                f"/expedition/{args[0]}/steer", {"instruction": " ".join(args[1:])}
            )
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        return f"Steering instruction sent to {args[0]}."

    async def _cancel(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /expedition cancel <id>"
        try:
            await _api_post(f"/expedition/{args[0]}/cancel")
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        return f"Expedition {args[0]} cancelled."

    def _harnesses(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Local read — the registry ships with the package, no Genesis needed."""
        try:
            from lib.research.harnesses import get_harness_registry
        except ImportError:
            return (
                "Harness registry unavailable in this environment "
                "(lib.research.harnesses not importable)."
            )
        rows = []
        for h in get_harness_registry().values():
            h.resolve_missing()
            rows.append({
                "name": h.name,
                "metric": h.metric_name,
                "goal": "lower" if h.minimize else "higher",
                "available": str(h.is_available()),
                "why_not": (h.missing_reason or "")[:34],
            })
        return (
            f"**Ratchet harnesses** ({len(rows)})\n"
            f"{_format_table(rows, ['name', 'metric', 'goal', 'available', 'why_not'])}"
        )

    async def _ratchet(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return (
                "Usage: /expedition ratchet <harness> [rounds]\n"
                "Run /expedition harnesses to see what can be scored."
            )
        harness = args[0]
        rounds = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        try:
            from lib.research.harnesses import get_harness
        except ImportError:
            return "Harness registry unavailable in this environment."
        h = get_harness(harness)
        if h is None:
            return f"Unknown harness '{harness}'. Try /expedition harnesses."
        h.resolve_missing()
        if not h.is_available():
            # Fail before creating an expedition that could never score anything.
            return f"Harness '{harness}' is unavailable: {h.missing_reason}"

        direction = "lower" if h.minimize else "higher"
        goal = (
            f"Move {h.metric_name} {direction} on {h.mutable_file.name}, scored by "
            f"`{h.eval_command}`, without breaking correctness."
        )
        try:
            created = await _api_post("/expedition/intake", {
                "title": f"Ratchet: {harness} ({h.metric_name})",
                "goal": goal,
                "sow": goal,
                "metadata": {
                    "execution_method": "ratchet",
                    "ratchet_harness": harness,
                    "ratchet_rounds": rounds,
                },
            })
        except Exception as e:
            return f"Cannot reach Genesis: {e}"
        eid = created.get("expedition_id") or created.get("id", "?")
        return (
            f"Ratchet expedition **{eid}** created for `{harness}` "
            f"({h.metric_name}, {direction} is better, {rounds} rounds).\n"
            f"It is in PLANNING — Atlas quotes it and a human gate must be "
            f"approved before any trial runs. Check: /expedition gates"
        )

    def get_help(self) -> str:
        return """**Expeditions** -- multi-session project orchestration

| Command | Description |
|---------|-------------|
| `/expedition list [status]` | List expeditions |
| `/expedition status <id>` | Phase progress, task counts, open gates |
| `/expedition tasks <id>` | Tasks with agent + execution method |
| `/expedition decisions <id>` | Recorded decisions and who made them |
| `/expedition gates` | Every open gate awaiting a human |
| `/expedition approve <gate_id> [note]` | Approve a gate |
| `/expedition reject <gate_id> [note]` | Reject a gate |
| `/expedition steer <id> <text>` | Mid-flight steering instruction |
| `/expedition cancel <id>` | Cancel a running expedition |
| `/expedition harnesses` | Ratchet scoreboards + availability |
| `/expedition ratchet <harness> [rounds]` | Start a keep-or-revert expedition |

A ratchet reverts any trial that does not beat the MEASURED baseline, and any
trial whose eval command exits non-zero — so it cannot trade correctness for
speed. A run that reverted everything worked correctly and found nothing.
"""
