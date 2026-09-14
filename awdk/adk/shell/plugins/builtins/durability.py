"""
Aither Durability Plugin for AitherShell
==========================================

Per-user encrypted GitHub backup + DR restore. Wraps AitherRecover's
/recover/user-backup/* and /recover/user-restore/* routes (mounted under
the SecurityCore compound service).

Usage:
    /durability status                 # Backup/restore engine status
    /durability backup [USER]          # Backup one user (default: me), encrypted, scope-diffed
    /durability backup all             # Backup ALL human users (admin/platform only)
    /durability restore USER [SCOPES]  # Restore a user's backup (decrypt only)
    /durability restore USER --write   # Restore + write back into source services
    /durability users                  # List backup-eligible users

Aliases: /backup
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _recover_url() -> str:
    """SecurityCore compound host — AitherRecover routes are under /recover."""
    return os.environ.get("AITHER_SECURITY_CORE_URL", "https://localhost:8115")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    # Internal services declare platform privilege explicitly.
    if os.environ.get("AITHER_INTERNAL_SECRET"):
        headers["X-Internal-Key"] = os.environ["AITHER_INTERNAL_SECRET"]
        headers["X-Caller-Type"] = "platform"
    return headers


async def _post(path: str, body: dict = None) -> dict:
    import httpx

    url = f"{_recover_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, json=body or {}, headers=_api_headers())
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


class DurabilityPlugin(SlashCommand):
    name: str = "durability"
    aliases: List[str] = ["backup"]
    description: str = "Aither Durability — per-user encrypted backup + DR restore"
    category: str = "infrastructure"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass; its __init__ does NOT propagate
        # a subclass's `name`/`aliases` class attrs to the instance. Set them
        # explicitly (the same fix secret.py uses at line 141) or the registry
        # registers the plugin under an EMPTY name and /durability 404s.
        super().__init__(*args, **kwargs)
        self.name = "durability"
        self.aliases = ["backup"]
        self.description = "Aither Durability — per-user encrypted backup + DR restore"
        self.category = "infrastructure"

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        rest = args[1:]

        handlers = {
            "status": self._status,
            "backup": self._backup,
            "restore": self._restore,
            "users": self._users,
        }
        handler = handlers.get(sub)
        if not handler:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest)

    async def _status(self, args: List[str]) -> str:
        """Check the backup engine status."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{_recover_url()}/recover/status", headers=_api_headers())
                return json.dumps(r.json(), indent=2)
        except Exception as e:
            return f"Error: {e}"

    async def _backup(self, args: List[str]) -> str:
        """Backup a user (default: me), or 'all' for every human user."""
        target = args[0].lower() if args else "me"
        if target == "all":
            result = await _post("/recover/user-backup/all", {})
        elif target == "me":
            # The route needs an explicit user_id; 'me' resolves server-side is
            # not supported, so tell the caller to pass their username.
            return ("Use /durability backup <username> to back up a specific user, "
                    "or /durability backup all for everyone (admin).")
        else:
            result = await _post(f"/recover/user-backup/{target}", {})
        return json.dumps(result, indent=2)

    async def _restore(self, args: List[str]) -> str:
        """Restore a user's backup. /durability restore USER [--write] [SCOPES...]"""
        if not args:
            return "Usage: /durability restore USER [--write] [scope,scope,...]"
        user = args[0]
        write_back = "--write" in args
        scope_arg = [a for a in args[1:] if not a.startswith("--")]
        scopes = scope_arg[0].split(",") if scope_arg else None
        body = (
            {"scopes": scopes, "write_back": write_back}
            if scopes
            else {"write_back": write_back}
        )
        result = await _post(f"/recover/user-restore/{user}", body)
        return json.dumps(result, indent=2)

    async def _users(self, args: List[str]) -> str:
        """List backup-eligible users (admin)."""
        # There's no list-users route on recover; report the capability honestly.
        return ("Backup-eligible users are resolved server-side by the /user-backup/all "
                "batch. Use /durability backup all to run it.")
