"""
Addon Management Plugin for AitherShell
=========================================

Manage self-hosted service addons (Qdrant, Knowledge-RAG, CodeGraph, etc.).

Usage:
    /addon list
    /addon enable qdrant
    /addon disable qdrant
    /addon status [addon_id]
    /addon logs qdrant --lines 50
    /addon update

Aliases: /addons
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


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


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
    async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
        resp = await c.post(f"{_genesis_url()}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_delete(path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
        resp = await c.delete(f"{_genesis_url()}{path}", headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


def _format_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(f"  {l}" for l in lines)


class AddonPlugin(SlashCommand):
    name: str = "addon"
    aliases: List[str] = ["addons"]
    description: str = "Self-hosted addon management — enable, disable, status, logs"
    category: str = "infrastructure"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='addon',
            description='Self-hosted addon management — enable, disable, status, logs',
            aliases=['addons'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        dispatch = {
            "list": self._list,
            "ls": self._list,
            "enable": self._enable,
            "disable": self._disable,
            "status": self._status,
            "logs": self._logs,
            "update": self._update,
            "catalog": self._catalog,
            "help": self._help_cmd,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    def get_help(self) -> str:
        return """**Addon Management** -- Self-hosted service addons

| Command | Description |
|---------|-------------|
| `/addon list` | Show available addons + status |
| `/addon enable <id>` | Pull image, start container, register |
| `/addon disable <id>` | Stop container, deregister |
| `/addon status [id]` | Health + metrics |
| `/addon logs <id>` | Tail container logs |
| `/addon update` | Pull latest images for all addons |
| `/addon catalog` | Browse addon catalog from portal |
"""

    async def _help_cmd(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()

    async def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """List available addons with their current status."""
        try:
            result = await _api_get("/v1/addons/catalog")
            addons = result.get("addons", [])
        except Exception:
            addons = []

        if not addons:
            return "No addons available. Is Genesis running?"

        rows = [
            {
                "id": a.get("id", "?"),
                "type": a.get("type", "?"),
                "port": str(a.get("default_port", "")),
                "plan": a.get("requires_plan", "free"),
                "pack": a.get("pack_id", "-"),
            }
            for a in addons
        ]
        return f"**Available Addons** ({len(addons)})\n{_format_table(rows, ['id', 'type', 'port', 'plan', 'pack'])}"

    async def _enable(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /addon enable <addon_id> [--endpoint url]"
        addon_id = args[0]
        endpoint = ""
        if "--endpoint" in args:
            idx = args.index("--endpoint")
            if idx + 1 < len(args):
                endpoint = args[idx + 1]

        body: Dict[str, Any] = {"addon_id": addon_id}
        if endpoint:
            body["endpoint"] = endpoint

        try:
            # Try via AddonManager locally first
            from adk.addon_manager import AddonManager
            import asyncio
            mgr = AddonManager()
            config = {"endpoint": endpoint} if endpoint else {}
            inst = await mgr.enable(addon_id, config=config)
            return (
                f"Addon **{addon_id}** enabled\n"
                f"  Status: {inst.status}\n"
                f"  Endpoint: {inst.endpoint}\n"
                f"  Health: {'OK' if inst.health_ok else 'checking...'}"
            )
        except ImportError:
            pass
        except Exception as e:
            return f"Failed to enable {addon_id}: {e}"

        return f"AddonManager not available. Install awdk to manage addons locally."

    async def _disable(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /addon disable <addon_id>"
        addon_id = args[0]
        try:
            from adk.addon_manager import AddonManager
            mgr = AddonManager()
            await mgr.disable(addon_id)
            return f"Addon **{addon_id}** disabled"
        except ImportError:
            return "AddonManager not available. Install awdk to manage addons locally."
        except Exception as e:
            return f"Failed to disable {addon_id}: {e}"

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        addon_id = args[0] if args else None
        try:
            from adk.addon_manager import AddonManager
            mgr = AddonManager()
            instances = await mgr.status(addon_id)
            if not instances:
                return "No addons enabled." if not addon_id else f"Addon {addon_id} not found."
            lines = []
            for inst in instances:
                health = "OK" if inst.health_ok else "FAIL"
                lines.append(
                    f"**{inst.addon_id}** — {inst.status} ({health})\n"
                    f"  Type: {inst.addon_type} | Endpoint: {inst.endpoint}"
                )
            return "\n\n".join(lines)
        except ImportError:
            return "AddonManager not available. Install awdk."
        except Exception as e:
            return f"Error: {e}"

    async def _logs(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /addon logs <addon_id> [--lines N]"
        addon_id = args[0]
        lines = 50
        if "--lines" in args:
            idx = args.index("--lines")
            if idx + 1 < len(args):
                try:
                    lines = int(args[idx + 1])
                except ValueError:
                    pass

        try:
            from adk.addon_manager import _load_state
            from adk.addon_docker import container_logs
            state = _load_state()
            addon_state = state.get(addon_id, {})
            cid = addon_state.get("container_id", "")
            if not cid:
                return f"No container found for {addon_id}"
            output = container_logs(cid, tail=lines)
            return f"**Logs: {addon_id}** (last {lines} lines)\n```\n{output}\n```"
        except ImportError:
            return "docker package not available. Install with: pip install docker"
        except Exception as e:
            return f"Error: {e}"

    async def _update(self, args: List[str], ctx: Dict[str, Any]) -> str:
        try:
            from adk.addon_manager import _load_state, load_addon_manifest
            from adk.addon_docker import pull_image
            state = _load_state()
            if not state:
                return "No addons enabled."
            updated = []
            for addon_id in state:
                manifest = load_addon_manifest(addon_id)
                if not manifest or manifest.get("type") != "docker":
                    continue
                image = manifest.get("image", "")
                if image:
                    pull_image(image)
                    updated.append(addon_id)
            if updated:
                return f"Updated images for: {', '.join(updated)}"
            return "No docker addons to update."
        except ImportError:
            return "docker package not available."
        except Exception as e:
            return f"Error: {e}"

    async def _catalog(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Browse addon catalog from portal."""
        try:
            result = await _api_get("/v1/addons/catalog")
            addons = result.get("addons", [])
            if not addons:
                return "No addons in catalog."
            rows = [
                {
                    "id": a.get("id", "?"),
                    "name": a.get("name", "?"),
                    "plan": a.get("requires_plan", "free"),
                    "image": a.get("image", "-")[:40],
                }
                for a in addons
            ]
            return f"**Addon Catalog** ({len(addons)})\n{_format_table(rows, ['id', 'name', 'plan', 'image'])}"
        except Exception as e:
            return f"Cannot reach portal: {e}"
