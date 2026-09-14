"""
Admin Plugin for AitherShell
=============================

RBAC-gated admin commands: invite management, capacity, user management.

Usage:
    /invite <email>                         — Send alpha invite
    /invite <email> --tier team             — Team-tier invite
    /invite <email> --note "From Discord"   — Invite with note
    /invite list                            — Show active invites
    /invite revoke <invite_id>              — Revoke an invite
    /admin capacity                         — Show alpha capacity
    /admin users                            — List registered users
    /admin users <email> --role developer   — Assign role

Aliases: /inv
"""

import json
import os
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _identity_url() -> str:
    return os.environ.get(
        "AITHER_IDENTITY_URL",
        os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"),
    )


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _check_admin() -> Optional[str]:
    """Return error message if current user is not admin, else None."""
    if not AuthStore:
        return "Auth module not available. Run `aither setup` first."
    user = AuthStore.get_active_user()
    if not user:
        return "Not logged in. Run `aither setup` to authenticate."
    roles = user.get("roles", [])
    if "admin" not in roles:
        return "Insufficient permissions (requires admin role)."
    return None


# ═══════════════════════════════════════════════════════════════════════
# /invite — Invite management
# ═══════════════════════════════════════════════════════════════════════

class InvitePlugin(SlashCommand):
    name: str = "invite"
    aliases: List[str] = ["inv"]
    description: str = "Manage platform invites (admin only)"
    category: str = "admin"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='invite',
            description='Manage platform invites (admin only)',
            aliases=['inv'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        err = _check_admin()
        if err:
            return err

        if not args:
            return (
                "Usage:\n"
                "  /invite <email>              — Send alpha invite\n"
                "  /invite <email> --tier team   — Team-tier invite\n"
                "  /invite <email> --note <msg>  — Invite with note\n"
                "  /invite list                  — Show active invites\n"
                "  /invite revoke <id>           — Revoke an invite"
            )

        sub = args[0].lower()
        if sub == "list":
            return await self._list(args[1:], ctx)
        elif sub == "revoke":
            return await self._revoke(args[1:], ctx)
        elif sub == "help":
            return await self.run([], ctx)
        else:
            return await self._send(args, ctx)

    async def _send(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        email = args[0]
        tier = "alpha"
        note = ""

        i = 1
        while i < len(args):
            if args[i] == "--tier" and i + 1 < len(args):
                tier = args[i + 1]
                i += 2
            elif args[i] == "--note" and i + 1 < len(args):
                note = args[i + 1]
                i += 2
            elif args[i] == "--no-email":
                i += 1
            else:
                i += 1

        body = {
            "email": email,
            "tier": tier,
            "note": note,
            "send_email": True,
        }

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.post("/admin/invites", json=body)

        if resp.status_code == 403:
            return "Insufficient permissions (requires admin role)."
        if resp.status_code != 200:
            return f"Failed to create invite: {resp.status_code} — {resp.text[:300]}"

        data = resp.json()
        inv = data.get("invite", {})
        code = inv.get("code", "?")
        invite_id = inv.get("id", "?")
        email_sent = inv.get("email_sent", False)
        expires = inv.get("expires_at", "?")

        lines = ["**Invite Created**"]
        lines.append(f"  ID: `{invite_id}`")
        lines.append(f"  Code: `{code}`")
        lines.append(f"  Email: {inv.get('email', email)}")
        lines.append(f"  Tier: {inv.get('tier', tier)}")
        lines.append(f"  Expires: {expires}")
        if email_sent:
            lines.append(f"  Email sent to {email}")
        else:
            lines.append(f"  Email not sent — share the code manually")
        if note:
            lines.append(f"  Note: {note}")
        return "\n".join(lines)

    async def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        params: Dict[str, Any] = {}
        if "--all" in args:
            params["include_consumed"] = "true"

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/admin/invites", params=params)

        if resp.status_code == 403:
            return "Insufficient permissions (requires admin role)."
        if resp.status_code != 200:
            return f"Failed to fetch invites: {resp.status_code}"

        data = resp.json()
        invites = data.get("invites", [])
        if not invites:
            return "No active invites."

        lines = [f"**{len(invites)} Invite(s)**\n"]
        for inv in invites:
            status = "revoked" if inv.get("revoked") else (
                "consumed" if not inv.get("consumable") else "active"
            )
            email = inv.get("email", "—")
            lines.append(
                f"  `{inv['id']}` — {email}  [{inv.get('tier', '?')}]  "
                f"{inv.get('uses', 0)}/{inv.get('max_uses', 1)} uses  "
                f"**{status}**  expires {inv.get('expires_at', '?')[:10]}"
            )
        return "\n".join(lines)

    async def _revoke(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        if not args:
            return "Usage: `/invite revoke <invite_id>`"

        invite_id = args[0]
        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.delete(f"/admin/invites/{invite_id}")

        if resp.status_code == 403:
            return "Insufficient permissions (requires admin role)."
        if resp.status_code == 404:
            return f"Invite `{invite_id}` not found."
        if resp.status_code != 200:
            return f"Failed to revoke: {resp.status_code}"

        return f"Invite `{invite_id}` revoked."


# ═══════════════════════════════════════════════════════════════════════
# /admin — General admin commands
# ═══════════════════════════════════════════════════════════════════════

class AdminPlugin(SlashCommand):
    name: str = "admin"
    aliases: List[str] = []
    description: str = "Admin commands: capacity, users"
    category: str = "admin"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='admin',
            description='Admin commands: capacity, users',
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        err = _check_admin()
        if err:
            return err

        if not args:
            return (
                "Usage:\n"
                "  /admin capacity               — Show alpha capacity\n"
                "  /admin users                   — List registered users\n"
                "  /admin users <email> --role <r> — Assign role"
            )

        sub = args[0].lower()
        if sub == "capacity":
            return await self._capacity(args[1:], ctx)
        elif sub == "users":
            return await self._users(args[1:], ctx)
        else:
            return f"Unknown admin command: {sub}. Try `/admin` for help."

    async def _capacity(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/auth/alpha-capacity")

        if resp.status_code != 200:
            return f"Failed to fetch capacity: {resp.status_code}"

        data = resp.json()
        current = data.get("current", "?")
        limit = data.get("limit", 0)
        remaining = data.get("remaining", "?")
        status = data.get("status_reason", "?")
        invite_only = data.get("invite_only_registration", False)

        lines = ["**Alpha Capacity**"]
        if limit == 0:
            lines.append(f"  Users: {current} (unlimited)")
        else:
            lines.append(f"  Users: {current}/{limit} ({remaining} remaining)")
        lines.append(f"  Status: {status}")
        if invite_only:
            lines.append(f"  Mode: invite-only")
        lines.append(f"  Message: {data.get('status_message', '')}")
        return "\n".join(lines)

    async def _users(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        # If email/username given with --role, assign role
        if len(args) >= 1 and "--role" in args:
            return await self._assign_role(args, ctx)

        # List users
        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.get("/identity/users", params={"user_type": "human"})

        if resp.status_code != 200:
            return f"Failed to fetch users: {resp.status_code}"

        data = resp.json()
        users = data if isinstance(data, list) else data.get("users", [])
        if not users:
            return "No registered users."

        lines = [f"**{len(users)} Registered User(s)**\n"]
        for u in users[:50]:
            roles = ", ".join(u.get("roles", [])) or "none"
            lines.append(
                f"  {u.get('username', '?')} — {u.get('email', '')}  "
                f"roles=[{roles}]"
            )
        if len(users) > 50:
            lines.append(f"  ... and {len(users) - 50} more")
        return "\n".join(lines)

    async def _assign_role(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        user_ref = args[0]
        role = None
        i = 1
        while i < len(args):
            if args[i] == "--role" and i + 1 < len(args):
                role = args[i + 1]
                i += 2
            else:
                i += 1

        if not role:
            return "Usage: `/admin users <email> --role <role>`"

        async with httpx.AsyncClient(
            base_url=_identity_url(), headers=_headers(), timeout=15
        ) as c:
            resp = await c.post(
                f"/identity/users/{user_ref}/roles",
                json={"roles": [role]},
            )

        if resp.status_code != 200:
            return f"Failed to assign role: {resp.status_code} — {resp.text[:200]}"

        return f"Role `{role}` assigned to `{user_ref}`."
