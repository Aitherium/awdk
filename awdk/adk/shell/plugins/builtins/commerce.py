"""
Stripe Commerce Plugin for AitherShell
=======================================

Manage storefronts, products, checkout, subscriptions, and revenue.
Works with both hosted (portal.aitherium.com) and self-hosted deployments.

Usage:
    /commerce status              # Stripe Connect status
    /commerce onboard EMAIL       # Start Stripe onboarding
    /commerce dashboard           # Open Stripe dashboard
    /commerce products            # List products
    /commerce products create NAME PRICE_CENTS [--recurring month|year]
    /commerce checkout PRICE_ID [--qty N] [--email EMAIL]
    /commerce payments [--limit N]
    /commerce refund PAYMENT_ID [--amount CENTS]
    /commerce subscriptions [--status active|canceled|all]
    /commerce subscriptions cancel SUB_ID [--now]
    /commerce revenue [--period current_month|last_30d]
    /commerce payouts [--limit N]
    /commerce storefront          # View storefront config
    /commerce coupons             # List coupons
    /commerce coupons create NAME --percent N | --amount CENTS

Aliases: /store
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


def _fmt_currency(amount: float, currency: str = "usd") -> str:
    return f"${amount:,.2f} {currency.upper()}"


def _fmt_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(f"  {l}" for l in lines)


def _parse_flag(args: List[str], flag: str, default: str = "") -> str:
    """Extract --flag value from args list."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return default


BASE = "/api/v1/commerce"


class CommercePlugin(SlashCommand):
    name: str = "commerce"
    aliases: List[str] = ["store"]
    description: str = "Stripe Commerce — storefronts, products, checkout, revenue"
    category: str = "business"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='commerce',
            description='Stripe Commerce — storefronts, products, checkout, revenue',
            aliases=['store'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        rest = args[1:]

        handlers = {
            "status": self._status,
            "onboard": self._onboard,
            "dashboard": self._dashboard,
            "products": self._products,
            "checkout": self._checkout,
            "payments": self._payments,
            "refund": self._refund,
            "subscriptions": self._subscriptions,
            "revenue": self._revenue,
            "payouts": self._payouts,
            "storefront": self._storefront,
            "coupons": self._coupons,
            "template": self._template,
            "help": lambda _: self.get_help(),
        }

        handler = handlers.get(sub)
        if handler:
            return await handler(rest)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    async def _status(self, args: List[str]) -> str:
        data = await _get(f"{BASE}/connect/status")
        if not data.get("connected"):
            return "Not connected to Stripe.\nRun: /commerce onboard YOUR_EMAIL"
        lines = [
            f"Connected: {data.get('account_id', 'N/A')}",
            f"Charges enabled: {data.get('charges_enabled', False)}",
            f"Payouts enabled: {data.get('payouts_enabled', False)}",
            f"Details submitted: {data.get('details_submitted', False)}",
        ]
        due = data.get("requirements_currently_due", [])
        if due:
            lines.append(f"Requirements due: {', '.join(due)}")
        return "\n".join(lines)

    async def _onboard(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce onboard EMAIL [--country US]"
        email = args[0]
        country = _parse_flag(args, "--country", "US")
        data = await _post(f"{BASE}/connect/onboard", {"email": email, "country": country})
        url = data.get("onboarding_url", "")
        return f"Onboarding URL: {url}\nOpen this link to complete Stripe verification."

    async def _dashboard(self, args: List[str]) -> str:
        data = await _get(f"{BASE}/connect/dashboard")
        return f"Stripe Dashboard: {data.get('url', 'N/A')}"

    async def _products(self, args: List[str]) -> str:
        if args and args[0] == "create":
            return await self._products_create(args[1:])
        data = await _get(f"{BASE}/products")
        products = data.get("products", [])
        if not products:
            return "No products. Create one: /commerce products create NAME PRICE_CENTS"
        rows = []
        for p in products:
            price = p.get("prices", [{}])[0] if p.get("prices") else {}
            rows.append({
                "name": p["name"],
                "price": _fmt_currency((price.get("unit_amount", 0) or 0) / 100),
                "type": "recurring" if price.get("recurring") else "one-time",
                "active": "yes" if p.get("active") else "no",
            })
        return f"Products ({len(products)}):\n{_fmt_table(rows, ['name', 'price', 'type', 'active'])}"

    async def _products_create(self, args: List[str]) -> str:
        if len(args) < 2:
            return "Usage: /commerce products create NAME PRICE_CENTS [--recurring month|year]"
        name = args[0]
        price_cents = int(args[1])
        recurring = _parse_flag(args, "--recurring", "")
        data = await _post(f"{BASE}/products", {
            "name": name, "price_cents": price_cents, "recurring_interval": recurring,
        })
        return f"Created: {data.get('name', name)} (product: {data.get('product_id')}, price: {data.get('price_id')})"

    async def _checkout(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce checkout PRICE_ID [--qty N] [--email EMAIL]"
        price_id = args[0]
        qty = int(_parse_flag(args, "--qty", "1"))
        email = _parse_flag(args, "--email", "")
        data = await _post(f"{BASE}/checkout", {
            "line_items": [{"price_id": price_id, "quantity": qty}],
            "customer_email": email,
        })
        return f"Checkout URL: {data.get('checkout_url', 'N/A')}\nTotal: {_fmt_currency(data.get('total_amount', 0) / 100)}"

    async def _payments(self, args: List[str]) -> str:
        limit = int(_parse_flag(args, "--limit", "10"))
        data = await _get(f"{BASE}/payments", {"limit": limit})
        payments = data.get("payments", [])
        if not payments:
            return "No payments yet."
        rows = [{"id": p["id"][:16], "amount": _fmt_currency(p["amount"]),
                 "status": p["status"], "email": p.get("customer_email", "")} for p in payments]
        return f"Payments ({len(payments)}):\n{_fmt_table(rows, ['id', 'amount', 'status', 'email'])}"

    async def _refund(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce refund PAYMENT_ID [--amount CENTS]"
        payment_id = args[0]
        amount = int(_parse_flag(args, "--amount", "0"))
        data = await _post(f"{BASE}/refund", {"payment_id": payment_id, "amount_cents": amount})
        return f"Refund {data.get('refund_id', 'N/A')}: {data.get('status', 'N/A')} — {_fmt_currency(data.get('amount_refunded', 0))}"

    async def _subscriptions(self, args: List[str]) -> str:
        if args and args[0] == "cancel":
            return await self._subscriptions_cancel(args[1:])
        status = _parse_flag(args, "--status", "active")
        data = await _get(f"{BASE}/subscriptions", {"status": status})
        subs = data.get("subscriptions", [])
        if not subs:
            return "No subscriptions."
        rows = [{"id": s["id"][:16], "amount": _fmt_currency(s.get("amount", 0)),
                 "interval": s.get("interval", ""), "status": s["status"]} for s in subs]
        return f"Subscriptions ({len(subs)}):\n{_fmt_table(rows, ['id', 'amount', 'interval', 'status'])}"

    async def _subscriptions_cancel(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce subscriptions cancel SUB_ID [--now]"
        sub_id = args[0]
        at_end = "--now" not in args
        data = await _post(f"{BASE}/subscriptions/cancel", {
            "subscription_id": sub_id, "at_period_end": at_end,
        })
        return f"Subscription {data.get('subscription_id', sub_id)}: {data.get('status', 'N/A')}"

    async def _revenue(self, args: List[str]) -> str:
        period = _parse_flag(args, "--period", "current_month")
        data = await _get(f"{BASE}/revenue", {"period": period})
        return (
            f"Revenue ({period}):\n"
            f"  Available: {_fmt_currency(data.get('available', 0))}\n"
            f"  Pending:   {_fmt_currency(data.get('pending', 0))}\n"
            f"  Total:     {_fmt_currency(data.get('gross_revenue', 0))}"
        )

    async def _payouts(self, args: List[str]) -> str:
        limit = int(_parse_flag(args, "--limit", "5"))
        data = await _get(f"{BASE}/payouts", {"limit": limit})
        payouts = data.get("payouts", [])
        if not payouts:
            return "No payouts yet."
        rows = [{"amount": _fmt_currency(p["amount"]), "status": p["status"],
                 "currency": p.get("currency", "usd")} for p in payouts]
        return f"Payouts ({len(payouts)}):\n{_fmt_table(rows, ['amount', 'status', 'currency'])}"

    async def _storefront(self, args: List[str]) -> str:
        data = await _get(f"{BASE}/storefront")
        return (
            f"Storefront Config:\n"
            f"  Name:      {data.get('store_name', '(not set)')}\n"
            f"  Color:     {data.get('accent_color', '#6366f1')}\n"
            f"  Tax:       {data.get('tax_behavior', 'unspecified')}\n"
            f"  Fee (bps): {data.get('platform_fee_bps', 0)}"
        )

    async def _coupons(self, args: List[str]) -> str:
        if args and args[0] == "create":
            return await self._coupons_create(args[1:])
        data = await _get(f"{BASE}/coupons")
        coupons = data.get("coupons", [])
        if not coupons:
            return "No coupons. Create one: /commerce coupons create NAME --percent 20"
        rows = [{"name": c["name"],
                 "discount": f"{c['percent_off']}%" if c.get("percent_off") else _fmt_currency(c.get("amount_off", 0)),
                 "duration": c["duration"], "used": str(c.get("times_redeemed", 0))} for c in coupons]
        return f"Coupons ({len(coupons)}):\n{_fmt_table(rows, ['name', 'discount', 'duration', 'used'])}"

    async def _coupons_create(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce coupons create NAME --percent 20 | --amount 500"
        name = args[0]
        percent = float(_parse_flag(args, "--percent", "0"))
        amount = int(_parse_flag(args, "--amount", "0"))
        duration = _parse_flag(args, "--duration", "once")
        data = await _post(f"{BASE}/coupons", {
            "name": name, "percent_off": percent, "amount_off_cents": amount, "duration": duration,
        })
        return f"Created coupon: {data.get('coupon_id', 'N/A')} — {data.get('name', name)}"

    async def _template(self, args: List[str]) -> str:
        if not args:
            return "Usage: /commerce template list | /commerce template use <id>"
        action = args[0].lower()
        if action == "list":
            data = await _get("/api/templates")
            templates = data.get("templates", [])
            if not templates:
                return "No templates available."
            lines = ["Available Product Templates:", ""]
            for t in templates:
                sf = " [storefront]" if t.get("storefrontEnabled") else ""
                lines.append(f"  {t['id']:<12} {t['name']}{sf}")
            return "\n".join(lines)
        if action == "use" and len(args) > 1:
            template_id = args[1]
            data = await _get(f"/api/templates/{template_id}")
            panels = ", ".join(data.get("panels", []))
            return (
                f"Template: {data['name']}\n"
                f"  Panels: {panels}\n"
                f"  Storefront: {data.get('storefrontEnabled', False)}\n"
                f"  Theme: {data.get('theme', 'default')}\n\n"
                "Apply with: /storefront init --template "
                f"{template_id} --name 'Your App'"
            )
        return "Usage: /commerce template list | /commerce template use <id>"

    def get_help(self) -> str:
        return """Stripe Commerce — manage your storefront

  /commerce status                  Check Stripe Connect status
  /commerce onboard EMAIL           Start Stripe onboarding
  /commerce dashboard               Open Stripe dashboard
  /commerce products                List products
  /commerce products create N P     Create product (name, price in cents)
  /commerce checkout PRICE_ID       Create checkout session
  /commerce payments                List payments
  /commerce refund PAYMENT_ID       Issue refund
  /commerce subscriptions           List subscriptions
  /commerce subscriptions cancel ID Cancel subscription
  /commerce revenue                 Revenue summary
  /commerce payouts                 List payouts
  /commerce storefront              View storefront config
  /commerce coupons                 List coupons
  /commerce coupons create N        Create coupon
  /commerce template list           List product templates
  /commerce template use ID         Show template details"""
