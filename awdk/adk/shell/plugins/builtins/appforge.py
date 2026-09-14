"""
AppForge Plugin for AitherShell
================================

Watch, create, and steer AppForge pipelines from the terminal.

Usage:
    /appforge                          - List active projects
    /appforge create <brief>           - Create new project from brief
    /appforge watch <id>               - Live-stream pipeline events (SSE)
    /appforge status <id>              - One-shot status check
    /appforge feedback <id> <text>     - Submit feedback on preview
    /appforge cancel <id>              - Cancel a running project
    /appforge files <id>               - Download generated code as ZIP

Aliases: /forge, /af
"""

import asyncio
from adk._tls import tls_verify
import json
import sys
import time
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


# ============================================================================
# ANSI helpers
# ============================================================================

class _C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"


PHASE_COLORS = {
    "analyze": _C.CYAN,
    "design": _C.MAGENTA,
    "build": _C.YELLOW,
    "assets": _C.MAGENTA,
    "preview": _C.GREEN,
}

EVENT_ICONS = {
    "phase_start": f"{_C.BLUE}>>>{_C.RESET}",
    "phase_done": f"{_C.GREEN} * {_C.RESET}",
    "phase_skip": f"{_C.GRAY} - {_C.RESET}",
    "llm_call": f"{_C.YELLOW} ~ {_C.RESET}",
    "swarm_start": f"{_C.MAGENTA} # {_C.RESET}",
    "error": f"{_C.RED} ! {_C.RESET}",
    "done": f"{_C.GREEN} @ {_C.RESET}",
}


def _status_color(status: str) -> str:
    if status in ("live", "awaiting_feedback"):
        return _C.GREEN
    if status in ("failed", "cancelled"):
        return _C.RED
    if status in ("building", "generating_assets"):
        return _C.YELLOW
    if status in ("analyzing", "designing"):
        return _C.CYAN
    return _C.WHITE


# ============================================================================
# Command implementation
# ============================================================================

class AppForgePlugin:
    """AppForge slash command handler."""

    def __init__(self, genesis_url: str = "https://localhost:8001"):
        self.genesis_url = genesis_url.rstrip("/")

    async def _get(self, path: str) -> dict:
        import httpx
        async with httpx.AsyncClient(verify=tls_verify(), timeout=15.0) as c:
            r = await c.get(f"{self.genesis_url}{path}", headers=_api_headers())
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, data: dict) -> dict:
        import httpx
        async with httpx.AsyncClient(verify=tls_verify(), timeout=30.0) as c:
            r = await c.post(f"{self.genesis_url}{path}", json=data, headers=_api_headers())
            r.raise_for_status()
            return r.json()

    async def list_projects(self) -> str:
        data = await self._get("/appforge/list")
        if not data:
            return f"{_C.DIM}No AppForge projects.{_C.RESET}"
        lines = [f"{_C.BOLD}AppForge Projects{_C.RESET}", ""]
        for p in data:
            sc = _status_color(p["status"])
            lines.append(
                f"  {_C.BOLD}{p['id']}{_C.RESET}  {sc}{p['status']:20s}{_C.RESET}  {p.get('brief','')[:60]}"
            )
        return "\n".join(lines)

    async def create(self, brief: str) -> str:
        data = await self._post("/appforge/create", {"brief": brief})
        pid = data["id"]
        return (
            f"{_C.GREEN}Project created:{_C.RESET} {_C.BOLD}{pid}{_C.RESET}\n"
            f"  Brief: {brief[:80]}\n"
            f"  Status: {data['status']}\n\n"
            f"  {_C.DIM}Watch with: /appforge watch {pid}{_C.RESET}"
        )

    async def status(self, project_id: str) -> str:
        data = await self._get(f"/appforge/{project_id}")
        sc = _status_color(data["status"])
        lines = [
            f"{_C.BOLD}AppForge {project_id}{_C.RESET}",
            f"  Status:  {sc}{data['status']}{_C.RESET}",
            f"  Type:    {data.get('app_type', '?')}",
            f"  Brief:   {data.get('brief', '')[:80]}",
        ]
        if data.get("preview_url"):
            lines.append(f"  Preview: {_C.BLUE}{data['preview_url']}{_C.RESET}")
        if data.get("error"):
            lines.append(f"  Error:   {_C.RED}{data['error'][:120]}{_C.RESET}")
        arch = data.get("architecture")
        if arch and arch.get("file_layout"):
            lines.append(f"  Files:   {len(arch['file_layout'])} planned")
        return "\n".join(lines)

    async def watch(self, project_id: str) -> str:
        """Live-stream SSE events to terminal."""
        import httpx

        print(f"\n{_C.BOLD}Watching AppForge {project_id}{_C.RESET}")
        print(f"{_C.DIM}Press Ctrl+C to stop{_C.RESET}\n")

        url = f"{self.genesis_url}/appforge/{project_id}/stream"
        try:
            async with httpx.AsyncClient(verify=tls_verify(), timeout=None) as client:
                async with client.stream("GET", url, headers=_api_headers()) as resp:
                    resp.raise_for_status()
                    event_type = ""
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            raw = line[5:].strip()
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            self._render_event(event_type, data)

                            if event_type == "done":
                                return f"\n{_C.GREEN}Pipeline complete.{_C.RESET}"
        except KeyboardInterrupt:
            return f"\n{_C.DIM}Stopped watching.{_C.RESET}"
        except Exception as e:
            return f"{_C.RED}Stream error: {e}{_C.RESET}"

        return ""

    def _render_event(self, event_type: str, data: dict):
        """Render a single SSE event to the terminal."""
        icon = EVENT_ICONS.get(event_type, f"{_C.GRAY} . {_C.RESET}")
        ts = data.get("ts", "")
        if ts:
            try:
                from datetime import datetime
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts = t.strftime("%H:%M:%S")
            except Exception:
                ts = ts[-8:]

        if event_type in ("phase_start", "phase_done", "phase_skip", "llm_call", "swarm_start"):
            phase = data.get("phase", "")
            pc = PHASE_COLORS.get(phase, _C.WHITE)
            detail = data.get("detail", event_type)
            agent = data.get("agent", "")
            agent_str = f" {_C.BLUE}[{agent}]{_C.RESET}" if agent else ""
            print(f"  {icon} {_C.DIM}{ts}{_C.RESET} {pc}{detail}{_C.RESET}{agent_str}")

        elif event_type == "status":
            status = data.get("status", "?")
            sc = _status_color(status)
            files = data.get("files_count", 0)
            extra = f"  ({files} files)" if files else ""
            print(f"  {_C.BOLD}--- {sc}{status}{_C.RESET}{extra} ---")

        elif event_type == "forge":
            sessions = data.get("active_sessions", [])
            if sessions:
                agents = ", ".join(
                    f"{s['agent']}({s['status']},{int(s['elapsed'])}s)"
                    for s in sessions
                )
                print(f"  {_C.GRAY}    agents: {agents}{_C.RESET}")

        elif event_type == "done":
            status = data.get("status", "?")
            preview = data.get("preview_url", "")
            files = data.get("files", 0)
            print(f"\n  {_C.GREEN}{_C.BOLD}DONE{_C.RESET} status={status} files={files}")
            if preview:
                print(f"  {_C.BLUE}Preview: {preview}{_C.RESET}")

        elif event_type == "error":
            print(f"  {icon} {_C.RED}{data.get('detail', data.get('error', '?'))}{_C.RESET}")

    async def submit_feedback(self, project_id: str, text: str) -> str:
        data = await self._post(f"/appforge/{project_id}/feedback", {"feedback": text})
        return f"{_C.GREEN}Feedback submitted.{_C.RESET} Status: {data.get('status', '?')}"

    async def cancel(self, project_id: str) -> str:
        data = await self._post(f"/appforge/{project_id}/cancel", {})
        return f"{_C.YELLOW}Cancelled.{_C.RESET}"


# ============================================================================
# Plugin registration
# ============================================================================

_plugin: Optional[AppForgePlugin] = None


def _get_plugin() -> AppForgePlugin:
    global _plugin
    if _plugin is None:
        _plugin = AppForgePlugin()
    return _plugin


async def _handle(args: str, **kwargs) -> str:
    """Main command dispatcher."""
    plugin = _get_plugin()
    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if not subcmd or subcmd == "list":
        return await plugin.list_projects()
    elif subcmd == "create":
        if not rest:
            return f"{_C.RED}Usage: /appforge create <brief>{_C.RESET}"
        return await plugin.create(rest)
    elif subcmd == "watch":
        if not rest:
            return f"{_C.RED}Usage: /appforge watch <project_id>{_C.RESET}"
        return await plugin.watch(rest.split()[0])
    elif subcmd == "status":
        if not rest:
            return f"{_C.RED}Usage: /appforge status <project_id>{_C.RESET}"
        return await plugin.status(rest.split()[0])
    elif subcmd == "feedback":
        parts2 = rest.split(maxsplit=1)
        if len(parts2) < 2:
            return f"{_C.RED}Usage: /appforge feedback <project_id> <text>{_C.RESET}"
        return await plugin.submit_feedback(parts2[0], parts2[1])
    elif subcmd == "cancel":
        if not rest:
            return f"{_C.RED}Usage: /appforge cancel <project_id>{_C.RESET}"
        return await plugin.cancel(rest.split()[0])
    else:
        return (
            f"{_C.YELLOW}Unknown subcommand: {subcmd}{_C.RESET}\n"
            f"Usage: /appforge [list|create|watch|status|feedback|cancel]"
        )


# Register as a slash command.
#
# This was `SlashCommand(name=..., handler=_handle, ...)` — a `handler=` kwarg
# the dataclass has never had, so constructing it raised TypeError at IMPORT,
# the loader swallowed it at DEBUG, and /appforge simply did not exist. The
# loader looks for SlashCommand SUBCLASSES; a list of instances named COMMANDS
# is read by nothing.
class AppForgeCommand(SlashCommand):
    """`/appforge` — AppForge pipeline control."""

    name = "appforge"
    description = "AppForge pipeline control"
    aliases = ["forge", "af"]

    def __init__(self) -> None:
        super().__init__(
            name="appforge",
            description="AppForge pipeline control",
            aliases=["forge", "af"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        return await _handle(" ".join(args))
