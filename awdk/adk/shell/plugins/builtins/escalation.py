"""
Escalation Plugin for AitherShell
===================================

Manage escalations requiring human approval.

Usage:
    /escalations                        -- List pending escalations
    /escalations pending                -- List pending (same as above)
    /escalations history                -- Show resolved escalations
    /escalations approve <id>           -- Approve a pending escalation
    /escalations deny <id>              -- Deny a pending escalation
    /escalations detail <id>            -- Show escalation details

Aliases: /esc
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def _genesis_url() -> str:
    return os.environ.get(
        "AITHER_GENESIS_URL", "http://localhost:8001"
    )


class EscalationCommand(SlashCommand):
    name = "escalations"
    description = "Manage human-approval escalations"
    aliases = ["esc"]

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='escalations',
            description='Manage human-approval escalations',
            aliases=['esc'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        import httpx

        base = _genesis_url()
        sub = args[0] if args else "pending"

        if sub in ("pending", "list"):
            return await self._list_pending(base)
        elif sub == "history":
            return await self._list_history(base)
        elif sub == "approve" and len(args) >= 2:
            return await self._resolve(base, args[1], "approve")
        elif sub == "deny" and len(args) >= 2:
            return await self._resolve(base, args[1], "deny")
        elif sub == "detail" and len(args) >= 2:
            return await self._detail(base, args[1])
        else:
            return (
                "Usage:\n"
                "  /esc pending          List pending escalations\n"
                "  /esc history          Show resolved\n"
                "  /esc approve <id>     Approve escalation\n"
                "  /esc deny <id>        Deny escalation\n"
                "  /esc detail <id>      Show details"
            )

    async def _list_pending(self, base: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/escalations/pending")
            data = r.json()
        items = data.get("escalations", [])
        if not items:
            return "No pending escalations."
        lines = ["Pending Escalations:", ""]
        for e in items:
            lines.append(
                f"  {e.get('id', '?')[:16]}  "
                f"{e.get('action_type', '?'):30s}  "
                f"agent={e.get('agent', '?')}"
            )
            if e.get("reason"):
                lines.append(f"    Reason: {e['reason'][:80]}")
        return "\n".join(lines)

    async def _list_history(self, base: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/escalations/history?limit=20")
            data = r.json()
        items = data.get("escalations", [])
        if not items:
            return "No escalation history."
        lines = ["Escalation History:", ""]
        for e in items:
            status = e.get("status", e.get("decision", "?"))
            lines.append(
                f"  {e.get('id', '?')[:16]}  "
                f"{status:10s}  "
                f"{e.get('action_type', '?'):30s}  "
                f"by={e.get('resolved_by', '?')}"
            )
        return "\n".join(lines)

    async def _resolve(self, base: str, esc_id: str, decision: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{base}/escalations/{esc_id}/{decision}",
                json={"resolved_by": "shell-admin"},
            )
            if r.status_code == 404:
                return f"Escalation {esc_id} not found."
            data = r.json()
        return f"Escalation {esc_id} {decision}d."

    async def _detail(self, base: str, esc_id: str) -> str:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{base}/escalations/{esc_id}")
            if r.status_code == 404:
                return f"Escalation {esc_id} not found."
            data = r.json()
        return json.dumps(data, indent=2, default=str)


class AuditCommand(SlashCommand):
    name = "audit"
    description = "Query agent activity audit trail"
    aliases = ["timeline"]

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='audit',
            description='Query agent activity audit trail',
            aliases=['timeline'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        import httpx

        base = _genesis_url()

        if not args:
            return (
                "Usage:\n"
                "  /audit <agent_id>             "
                "Agent activity timeline\n"
                "  /audit <agent_id> --since 2026-05-25  "
                "With date filter"
            )

        agent_id = args[0]
        since = ""
        for i, a in enumerate(args):
            if a == "--since" and i + 1 < len(args):
                since = args[i + 1]

        params = {"agent_id": agent_id, "limit": "50"}
        if since:
            params["since"] = since

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{base}/platform/audit/agent-timeline",
                params=params,
            )
            data = r.json()

        records = data.get("records", [])
        if not records:
            return f"No audit records for agent '{agent_id}'."

        lines = [
            f"Agent Timeline: {agent_id} "
            f"({data.get('total', 0)} records)",
            "",
        ]
        for rec in records[:30]:
            action = rec.get("action", "?")
            outcome = rec.get("outcome", "")
            source = rec.get("source", "")
            detail = rec.get("details", "")[:60]
            lines.append(
                f"  [{source:12s}] {action:25s} "
                f"{outcome:10s} {detail}"
            )

        return "\n".join(lines)
