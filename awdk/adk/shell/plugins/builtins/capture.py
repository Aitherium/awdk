"""
AitherCapture Plugin for AitherShell
=====================================

Capture your AI coding sessions across Claude Code, Codex, Copilot, Cursor and
Antigravity, then export a clean ShareGPT JSONL dataset you own.

Usage:
    /capture status                       Show connected tools + stats
    /capture tools                        List supported/detected tools
    /capture sessions [--since 30] [--tools claude-code,cursor]
    /capture export [--tools ...] [--since 30] [--no-scrub] [--no-dedup]
    /capture pool optin [--tier 3]        Opt in to pooling for credits
    /capture pool optout                  Revoke pooling consent
    /capture pool credits                 View earned credits / discount

Aliases: /cap
"""

import json
from adk._tls import tls_verify
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore

_PREFIX = "/api/v1/aither-capture"


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


async def _api_get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{_PREFIX}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=120, verify=tls_verify()) as c:
        resp = await c.post(f"{_genesis_url()}{_PREFIX}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


def _opt(args: List[str], flag: str, default: Optional[str] = None) -> Optional[str]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


class CapturePlugin(SlashCommand):
    name: str = "capture"
    aliases: List[str] = ["cap"]
    description: str = "AitherCapture — capture coding sessions, export clean ShareGPT datasets"
    category: str = "data"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='capture',
            description='AitherCapture — capture coding sessions, export clean ShareGPT datasets',
            aliases=['cap'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()
        sub = args[0].lower()
        dispatch = {
            "status": self._status,
            "tools": self._tools,
            "sessions": self._sessions,
            "export": self._export,
            "pool": self._pool,
            "connect": self._connect,
            "disconnect": self._disconnect,
            "sync": self._sync,
            "help": self._help_cmd,
        }
        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    def get_help(self) -> str:
        return """**AitherCapture** — Turn your coding sessions into a dataset you own

| Command | Description |
|---------|-------------|
| `/capture status` | Connected tools + capture stats |
| `/capture tools` | List supported / detected tools |
| `/capture connect [--tools ...] [--interval 600] [--no-hook]` | Enable persistent capture (watcher + Claude hook) |
| `/capture disconnect` | Disable persistent capture |
| `/capture sync [--tools ...]` | Capture new sessions right now |
| `/capture sessions [--since 30] [--tools claude-code,cursor]` | List captured sessions |
| `/capture export [--tools ...] [--since 30] [--no-scrub] [--no-dedup]` | Build clean ShareGPT JSONL |
| `/capture pool optin [--tier 3]` | Opt in to pooling for credits |
| `/capture pool optout` | Revoke pooling consent |
| `/capture pool credits` | View earned credits / discount |
"""

    async def _help_cmd(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        data = await _api_get("/status")
        tools = data.get("tools", [])
        connected = [t["display_name"] for t in tools if t.get("connected")]
        crawl = data.get("crawl", {})
        return (
            f"**AitherCapture Status**\n"
            f"  Connected tools: {', '.join(connected) or '(none detected)'}\n"
            f"  Captured sessions: {crawl.get('total_sessions', 0)}\n"
            f"  Export dir: `{data.get('export_dir', '?')}`"
        )

    async def _tools(self, args: List[str], ctx: Dict[str, Any]) -> str:
        data = await _api_get("/tools")
        lines = ["**Supported Tools**"]
        for t in data.get("tools", []):
            mark = "✓" if t.get("connected") else "·"
            lines.append(f"  {mark} {t['display_name']} ({t['tool']})")
        return "\n".join(lines)

    async def _sessions(self, args: List[str], ctx: Dict[str, Any]) -> str:
        params: Dict[str, Any] = {"since_days": int(_opt(args, "--since", "30"))}
        tools = _opt(args, "--tools")
        if tools:
            params["tools"] = tools
        data = await _api_get("/sessions", params)
        sessions = data.get("sessions", [])
        if not sessions:
            return "No captured sessions found. Code in a supported tool, then try again."
        lines = [f"**Captured Sessions** ({data.get('count', len(sessions))})"]
        for s in sessions[:25]:
            stats = s.get("stats", {})
            lines.append(
                f"  [{s.get('source', '?')}] {s.get('session_id', '?')[:12]} — "
                f"{stats.get('total_messages', 0)} msgs, {stats.get('total_tool_uses', 0)} tools"
            )
        return "\n".join(lines)

    async def _export(self, args: List[str], ctx: Dict[str, Any]) -> str:
        body: Dict[str, Any] = {
            "since_days": int(_opt(args, "--since", "30")),
            "scrub_pii": "--no-scrub" not in args,
            "dedup": "--no-dedup" not in args,
            "include_thinking": "--thinking" in args,
        }
        tools = _opt(args, "--tools")
        if tools:
            body["tools"] = tools.split(",")
        data = await _api_post("/export", body)
        if "error" in data:
            return f"Export failed: {data['error']}"
        return (
            f"**Export complete** — `{data.get('export_id', '?')}`\n"
            f"  Examples: {data.get('example_count', 0)}\n"
            f"  Sessions: {data.get('sessions_included', 0)}\n"
            f"  Duplicates removed: {data.get('dedup_removed', 0)}\n"
            f"  PII redactions: {data.get('pii_redactions', 0)}\n"
            f"  File: `{data.get('path', '?')}`\n"
            f"  Download: GET {_PREFIX}/export/{data.get('export_id', '?')}/download"
        )

    async def _connect(self, args: List[str], ctx: Dict[str, Any]) -> str:
        body: Dict[str, Any] = {
            "interval_seconds": int(_opt(args, "--interval", "600")),
            "install_claude_hook": "--no-hook" not in args,
        }
        tools = _opt(args, "--tools")
        if tools:
            body["tools"] = tools.split(",")
        data = await _api_post("/install", body)
        if "error" in data:
            return f"Connect failed: {data['error']}"
        hook = data.get("claude_hook", {})
        return (
            f"**Capture connected** — watcher running\n"
            f"  Tools: {', '.join(data.get('tools', []))}\n"
            f"  Claude hook: {hook.get('reason', 'n/a')}\n"
            f"  Interval: {data.get('poller', {}).get('interval_seconds', '?')}s\n"
            f"New sessions are now captured automatically. Run `/capture export` anytime."
        )

    async def _disconnect(self, args: List[str], ctx: Dict[str, Any]) -> str:
        data = await _api_post("/uninstall", {})
        return f"Capture disconnected. Watcher stopped, Claude hook removed: {data.get('claude_hook_removed', False)}"

    async def _sync(self, args: List[str], ctx: Dict[str, Any]) -> str:
        body: Dict[str, Any] = {"since_days": int(_opt(args, "--since", "2"))}
        tools = _opt(args, "--tools")
        if tools:
            body["tools"] = tools.split(",")
        data = await _api_post("/sync", body)
        if "error" in data:
            return f"Sync failed: {data['error']}"
        return f"Synced — {data.get('ingested', 0)} new session(s) captured."

    async def _pool(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /capture pool optin|optout|credits"
        action = args[0].lower()
        if action == "optin":
            tier = int(_opt(args[1:], "--tier", "3"))
            data = await _api_post("/pooling/optin", {
                "consent": True, "acknowledge_anonymization": True, "tier": tier,
            })
            return f"Pooling enabled (tier {tier}). Anonymized sessions now earn credits.\n```json\n{json.dumps(data, indent=2)}\n```"
        if action == "optout":
            data = await _api_post("/pooling/optout", {})
            return f"Pooling disabled. No future sessions will be contributed.\n```json\n{json.dumps(data, indent=2)}\n```"
        if action == "credits":
            data = await _api_get("/pooling/credits")
            disc = data.get("discount", {})
            return (
                f"**Pooling Status**\n"
                f"  Enabled: {data.get('pooling_enabled', False)}\n"
                f"  Discount: {disc.get('discount_pct', 0)}% ({disc.get('tier_label', '?')})\n"
                f"  Contributions: {data.get('contributions', {}).get('total', 0)}"
            )
        return f"Unknown pool action: {action}"
