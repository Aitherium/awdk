"""
Untether Plugin for AitherShell
===============================

Read your mirrored CRM (the Untether canonical store) from the shell. A thin window
onto the gateway MCP tools ``ut_stats`` / ``ut_query`` / ``ut_timeline`` -- the same
tools agents use -- called with YOUR bearer. The tenant is always the one the gateway
derives from that bearer; this plugin never sends a ``tenant_id``.

Usage:
    /untether stats                              — Record counts per entity type
    /untether query TYPE [--source S] [--limit N] [--offset N]
                                                 — List records of one entity type
    /untether timeline SOURCE CLIENT_ID          — Everything linked to one client

TYPE is one of: client, session, invoice, payment, contract, gallery, comm, document.
Gateway: $AITHER_GATEWAY_URL (default https://mcp.aitherium.com; the local fleet
gateway is http://127.0.0.1:8182).

Aliases: /ut
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand

ENTITY_TYPES = ("client", "session", "invoice", "payment", "contract",
                "gallery", "comm", "document")
_MAX_LIMIT = 1000


def _gateway_client():
    """A GatewayMCPClient on $AITHER_GATEWAY_URL with the resolved bearer."""
    from adk.client._gateway_mcp import GatewayMCPClient

    return GatewayMCPClient(gateway_url=os.environ.get("AITHER_GATEWAY_URL", ""))


async def _call(name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any]:
    """(ok, payload). One tools/call; every failure is a message, never a raise."""
    try:
        client = _gateway_client()
    except ImportError as exc:  # pragma: no cover - adk always ships the client
        return False, f"gateway client unavailable: {exc}"
    if not getattr(client, "api_key", ""):
        return False, ("Not signed in — run `aither login` (or mint "
                       "~/.aither/session-bearer) so the gateway knows your tenant.")
    if not await client.ping():
        return False, f"Gateway unreachable at {client.gateway_url}/mcp"
    result = await client.call_tool(name, arguments)
    if result.get("error"):
        return False, f"{name}: {result.get('message') or result['error']}"
    text = result.get("text", "")
    try:
        data = json.loads(text)
    except ValueError:
        return True, text
    if isinstance(data, dict) and data.get("error"):
        return False, f"{name}: {data['error']}"
    return True, data


def _pop_flag(args: List[str], flag: str, default: str) -> Tuple[str, List[str]]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
        return default, args[:i]
    return default, args


def _int_flag(args: List[str], flag: str, default: int) -> Tuple[Optional[int], List[str]]:
    raw, rest = _pop_flag(args, flag, str(default))
    try:
        return int(raw), rest
    except ValueError:
        return None, rest


def _render_records(records: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for r in records:
        label = r.get("name") or r.get("title") or r.get("number") or ""
        lines.append(f"  {r.get('entity_type', '?'):<9} {r.get('source', '?')}:"
                     f"{r.get('source_id', '?')}  {label}".rstrip())
    return lines


class UntetherPlugin(SlashCommand):
    name: str = "untether"
    aliases: List[str] = ["ut"]
    description: str = "Untether — read your mirrored CRM (stats, query, timeline)"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # SlashCommand is a dataclass whose __init__ does not carry a subclass's class
        # attrs onto the instance; without this the registry registers an EMPTY name.
        super().__init__(*args, **kwargs)
        self.name = "untether"
        self.aliases = ["ut"]
        self.description = "Untether — read your mirrored CRM (stats, query, timeline)"
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower(), args[1:]
        handler = {
            "stats": self._stats,
            "query": self._query,
            "timeline": self._timeline,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest)

    async def _stats(self, args: List[str]) -> str:
        ok, data = await _call("ut_stats", {})
        if not ok:
            return str(data)
        if not isinstance(data, dict):
            return str(data)
        counts = {k: v for k, v in data.items()
                  if k in ENTITY_TYPES and isinstance(v, int)}
        lines = [f"Untether CRM (tenant {data.get('tenant_id', '?')}): "
                 f"{data.get('total', sum(counts.values()))} record(s)"]
        lines += [f"  {k:<9} {v}" for k, v in sorted(counts.items())]
        return "\n".join(lines)

    async def _query(self, args: List[str]) -> str:
        source, args = _pop_flag(args, "--source", "")
        limit, args = _int_flag(args, "--limit", 50)
        offset, args = _int_flag(args, "--offset", 0)
        if limit is None or offset is None:
            return "--limit and --offset take integers"
        if not args or args[0].lower() not in ENTITY_TYPES:
            return ("Usage: /untether query TYPE [--source S] [--limit N] [--offset N]\n"
                    f"TYPE is one of: {', '.join(ENTITY_TYPES)}")
        arguments: Dict[str, Any] = {
            "entity_type": args[0].lower(),
            "limit": max(1, min(limit, _MAX_LIMIT)),
            "offset": max(0, offset),
        }
        if source:
            arguments["source"] = source
        ok, data = await _call("ut_query", arguments)
        if not ok:
            return str(data)
        if not isinstance(data, dict):
            return str(data)
        records = data.get("records") or []
        if not records:
            return f"No {arguments['entity_type']} records."
        return "\n".join([f"{data.get('count', len(records))} "
                          f"{arguments['entity_type']} record(s):"]
                         + _render_records(records))

    async def _timeline(self, args: List[str]) -> str:
        if len(args) < 2:
            return "Usage: /untether timeline SOURCE CLIENT_ID"
        ok, data = await _call("ut_timeline", {"source": args[0],
                                               "client_source_id": args[1]})
        if not ok:
            return str(data)
        if not isinstance(data, dict):
            return str(data)
        records = data.get("records") or []
        if not records:
            return f"No records linked to {args[0]}:{args[1]}."
        return "\n".join([f"{len(records)} record(s) linked to {args[0]}:{args[1]}:"]
                         + _render_records(records))
