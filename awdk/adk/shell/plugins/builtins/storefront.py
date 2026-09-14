"""
Storefront Plugin for AitherShell
==================================

Scaffold, deploy, and manage storefront workspaces from the CLI.

Usage:
    /storefront init [--template ID] [--name NAME]   # Scaffold new storefront
    /storefront deploy                                 # Deploy storefront to workspace
    /storefront products                               # List storefront products
    /storefront theme [PRESET]                         # View/switch theme preset
    /storefront status                                 # Storefront health check
    /storefront templates                              # List available templates

Aliases: /sf
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
        has_prof = hasattr(AuthStore, "get_active_profile")
        profile = AuthStore.get_active_profile() if has_prof else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    return headers


async def _get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=20, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=20, verify=tls_verify()) as c:
        resp = await c.post(f"{_genesis_url()}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


def _parse_flag(args: List[str], flag: str, default: str = "") -> str:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


class StorefrontPlugin(SlashCommand):
    name: str = "storefront"
    aliases: List[str] = ["sf"]
    description: str = "Storefront — scaffold, deploy, and manage storefronts"
    category: str = "business"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='storefront',
            description='Storefront — scaffold, deploy, and manage storefronts',
            aliases=['sf'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        rest = args[1:]

        handlers = {
            "init": self._init,
            "deploy": self._deploy,
            "products": self._products,
            "theme": self._theme,
            "status": self._status,
            "templates": self._templates,
            "help": lambda _: self.get_help(),
        }

        handler = handlers.get(sub)
        if handler:
            return await handler(rest)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    async def _templates(self, args: List[str]) -> str:
        data = await _get("/api/templates")
        templates = data.get("templates", [])
        if not templates:
            return "No templates available."
        lines = ["Available Templates:", ""]
        for t in templates:
            sf = " [storefront]" if t.get("storefrontEnabled") else ""
            lines.append(f"  {t['id']:<12} {t['name']:<25} {t.get('category', '')}{sf}")
            lines.append(f"  {'':12} {t.get('description', '')}")
            lines.append("")
        return "\n".join(lines)

    async def _init(self, args: List[str]) -> str:
        template_id = _parse_flag(args, "--template", "shop")
        name = _parse_flag(args, "--name", "")
        if not name:
            return "Usage: /storefront init --template shop --name 'My Store'"
        data = await _post(f"/api/templates/{template_id}/scaffold")
        scaffold = data.get("scaffold", {})
        return (
            f"Scaffold ready for '{name}' (template: {template_id}):\n"
            f"  Panels: {', '.join(scaffold.get('panels', []))}\n"
            f"  Storefront: {scaffold.get('storefront', False)}\n"
            f"  Landing Page: {scaffold.get('landing_page', False)}\n"
            f"  Theme: {scaffold.get('theme', 'default')}\n"
            f"  Addon Packs: {', '.join(scaffold.get('addon_packs', []))}\n\n"
            "Run `aither deploy` to deploy this workspace."
        )

    async def _deploy(self, args: List[str]) -> str:
        return (
            "Storefront deployment is handled by WorkspaceRuntime.\n\n"
            "From your workspace directory:\n"
            "  docker compose up -d\n\n"
            "Or via AitherOS:\n"
            "  .DEPLOYMENT/scripts/compose.sh aitheros -f docker-compose.yml up -d"
        )

    async def _products(self, args: List[str]) -> str:
        data = await _get("/api/store/products")
        products = data.get("products", [])
        if not products:
            return "No products in storefront. Add some via /commerce products create"
        lines = [f"Products ({len(products)}):"]
        for p in products:
            price = p.get("prices", [{}])[0] if p.get("prices") else {}
            amount = (price.get("unit_amount", 0) or 0) / 100
            lines.append(f"  {p['name']:<30} ${amount:,.2f}")
        return "\n".join(lines)

    async def _theme(self, args: List[str]) -> str:
        if args:
            preset = args[0]
            return (
                "Theme switching requires redeployment. "
                f"Update your .env:\n  STOREFRONT_THEME_PRESET={preset}"
            )
        data = await _get("/api/store/config")
        return (
            f"Current Storefront Config:\n"
            f"  Name:  {data.get('store_name', '(not set)')}\n"
            f"  Color: {data.get('accent_color', '#00D4FF')}\n"
            f"  Logo:  {data.get('logo_url', '(none)')}"
        )

    async def _status(self, args: List[str]) -> str:
        try:
            data = await _get("/api/store/config")
            products = await _get("/api/store/products")
            count = len(products.get("products", []))
            return (
                f"Storefront: Online\n"
                f"  Name:     {data.get('store_name', '(not set)')}\n"
                f"  Products: {count}\n"
                f"  Currency: {data.get('currency', 'usd')}"
            )
        except Exception as e:
            return f"Storefront: Offline or not configured\n  Error: {e}"

    def get_help(self) -> str:
        return """Storefront management

  /storefront templates               List available product templates
  /storefront init --template ID --name NAME  Scaffold a storefront workspace
  /storefront deploy                   Show deployment instructions
  /storefront products                 List storefront products
  /storefront theme [PRESET]           View/switch theme preset
  /storefront status                   Check storefront health"""
