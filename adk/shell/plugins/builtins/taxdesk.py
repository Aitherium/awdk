"""
TaxDesk Plugin for AitherShell
==============================

Personal tax ingestion and 1040 preparation from the shell. A thin window onto the
Genesis router ``/taxdesk/*`` -- the same surface the portal panel uses -- called
with YOUR bearer, so the router's entitlement gate (pro tier or a TaxDesk
subscription), per (tenant, user) data scope, metering and audit apply exactly as
they do in the portal. DRAFT for CPA review -- not a tax preparer.

Usage:
    /tax status                          — Am I entitled?
    /tax stats                           — Documents, transactions, pending review
    /tax docs [all|parsed|error]         — List ingested documents
    /tax ingest FILE... [--account NAME] — Ingest statements (CSV/OFX/XLSX/PDF)
    /tax w2 FILE                         — Import a W-2 PDF
    /tax 1099 FILE [--form auto|nec|int|div|misc|k|b]
    /tax 1098 FILE                       — Import a Form 1098 PDF
    /tax broker FILE [--method fifo|lifo|specific]
    /tax receipt IMAGE                   — Scan a receipt (local vision only)
    /tax build                           — Deduplicate and build the ledger
    /tax summary [YEAR]                  — Monthly and account summaries
    /tax categorize [--no-llm]           — Categorize uncategorized transactions
    /tax review [LIMIT]                  — Transactions needing review
    /tax confirm TXN_ID [--class C] [--category C] [--notes TEXT]
    /tax approve [--min 0.95]            — Bulk-approve high-confidence transactions
    /tax rule PATTERN TAX_CLASS [--category C] [--note TEXT]
    /tax categories                      — Valid tax classes / categories
    /tax 1040 [--status single|mfj|mfs|hoh|qw] [--year 2025]
    /tax schedule-c                      — Schedule C worksheet
    /tax package [--status S] [--year Y] — Generate the CPA report package
    /tax export OUT.xlsx [--year Y]      — Download the ledger workbook

Aliases: /taxdesk
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand

try:
    from adk._tls import tls_verify
except ImportError:  # pragma: no cover - older adk without the TLS helper
    def tls_verify():  # type: ignore[no-redef]
        return True

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore

PREFIX = "/taxdesk"
_TIMEOUT = 300  # ingest/categorize/package can take minutes on local models


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    """Bearer only. The tenant comes from the authenticated caller on the server,
    never from a header this client chooses."""
    headers: Dict[str, str] = {}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(ctx: Optional[Dict[str, Any]], method: str, path: str,
                   params: Optional[dict] = None,
                   files: Optional[list] = None,
                   raw: bool = False) -> Tuple[int, Any]:
    import httpx

    url = f"{_genesis_url(ctx)}{PREFIX}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=tls_verify()) as client:
        resp = await client.request(method, url, params=params, files=files,
                                    headers=_headers())
    if raw and resp.status_code == 200:
        return resp.status_code, resp.content
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _render_error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in — run `aither login` first (TaxDesk needs your account)."
    if status in (402, 403) and isinstance(detail, dict) \
            and detail.get("status") == "not_entitled":
        return (f"{detail.get('message', 'Upgrade required')} "
                f"(you are on '{detail.get('current_tier', '?')}'): "
                f"{detail.get('upgrade_url', '')}").strip()
    if isinstance(detail, dict) and detail.get("error"):
        return f"Error {status}: {detail['error']}"
    if isinstance(detail, str):
        return f"Error {status}: {detail}"
    return f"Error {status}: {json.dumps(detail, default=str)}"


def _pop_flag(args: List[str], flag: str, default: str) -> Tuple[str, List[str]]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
        return default, args[:i]
    return default, args


def _pop_switch(args: List[str], flag: str) -> Tuple[bool, List[str]]:
    if flag in args:
        return True, [a for a in args if a != flag]
    return False, args


def _file_part(field: str, path_arg: str) -> Tuple[Optional[tuple], str]:
    """A multipart part for `path_arg`, or (None, reason)."""
    p = Path(path_arg).expanduser()
    if not p.is_file():
        return None, f"No such file: {p}"
    return (field, (p.name, p.read_bytes())), p.name


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


class TaxDeskPlugin(SlashCommand):
    name: str = "tax"
    aliases: List[str] = ["taxdesk"]
    description: str = "TaxDesk — tax ingestion, ledger and 1040 worksheets (draft for CPA)"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass whose __init__ does not carry a
        # subclass's class attrs onto the instance; without this the registry
        # registers the plugin under an EMPTY name (see durability.py).
        super().__init__(*args, **kwargs)
        self.name = "tax"
        self.aliases = ["taxdesk"]
        self.description = "TaxDesk — tax ingestion, ledger and 1040 worksheets (draft for CPA)"
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower(), args[1:]
        handler = {
            "status": self._status,
            "stats": self._simple("GET", "/stats"),
            "docs": self._docs,
            "ingest": self._ingest,
            "w2": self._upload("/import-w2", "W-2"),
            "1099": self._import_1099,
            "1098": self._upload("/import-1098", "1098"),
            "broker": self._broker,
            "receipt": self._upload("/scan-receipt", "receipt"),
            "build": self._simple("POST", "/build-ledger"),
            "summary": self._summary,
            "categorize": self._categorize,
            "review": self._review,
            "confirm": self._confirm,
            "approve": self._approve,
            "rule": self._rule,
            "categories": self._simple("GET", "/categories"),
            "1040": self._worksheet("GET", "/worksheet-1040"),
            "schedule-c": self._simple("GET", "/worksheet-schedule-c"),
            "package": self._worksheet("POST", "/report-package"),
            "export": self._export,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    # ── generic shapes ───────────────────────────────────────────────────────

    def _simple(self, method: str, path: str):
        async def handler(args: List[str], ctx: Dict[str, Any]) -> str:
            status, data = await _request(ctx, method, path)
            return _dump(data) if status == 200 else _render_error(status, data)
        return handler

    def _upload(self, path: str, what: str):
        async def handler(args: List[str], ctx: Dict[str, Any]) -> str:
            if not args:
                return f"Usage: /tax {path.rsplit('-', 1)[-1].lstrip('/')} FILE ({what})"
            part, why = _file_part("file", args[0])
            if part is None:
                return why
            status, data = await _request(ctx, "POST", path, files=[part])
            return _dump(data) if status == 200 else _render_error(status, data)
        return handler

    def _worksheet(self, method: str, path: str):
        async def handler(args: List[str], ctx: Dict[str, Any]) -> str:
            filing, args = _pop_flag(args, "--status", "single")
            year, args = _pop_flag(args, "--year", "2025")
            status, data = await _request(ctx, method, path,
                                          params={"filing_status": filing,
                                                  "tax_year": year})
            return _dump(data) if status == 200 else _render_error(status, data)
        return handler

    # ── subcommands ──────────────────────────────────────────────────────────

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/check-entitlement")
        if status != 200:
            return _render_error(status, data)
        verdict = "entitled" if data.get("entitled") else "NOT entitled"
        line = f"TaxDesk: {verdict} (tier '{data.get('current_tier')}')"
        if not data.get("entitled") and data.get("reason"):
            line += f" — {data['reason']} {data.get('upgrade_url', '')}".rstrip()
        return line

    async def _docs(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/list-documents",
                                      params={"status": args[0] if args else "all"})
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _ingest(self, args: List[str], ctx: Dict[str, Any]) -> str:
        account, args = _pop_flag(args, "--account", "")
        if not args:
            return "Usage: /tax ingest FILE... [--account NAME]"
        parts = []
        for a in args:
            part, why = _file_part("files", a)
            if part is None:
                return why
            parts.append(part)
        status, data = await _request(ctx, "POST", "/ingest-batch",
                                      params={"account_name": account}, files=parts)
        if status != 200:
            return _render_error(status, data)
        lines = [f"Ingested {data.get('ingested', 0)}, failed {data.get('failed', 0)}"]
        for f in data.get("files") or []:
            tail = f.get("error", "") if f.get("status") != "ok" else ""
            lines.append(f"  [{f.get('status')}] {f.get('filename')} {tail}".rstrip())
        return "\n".join(lines)

    async def _import_1099(self, args: List[str], ctx: Dict[str, Any]) -> str:
        form, args = _pop_flag(args, "--form", "auto")
        if not args:
            return "Usage: /tax 1099 FILE [--form auto|nec|int|div|misc|k|b]"
        part, why = _file_part("file", args[0])
        if part is None:
            return why
        status, data = await _request(ctx, "POST", "/import-1099",
                                      params={"form_type": form}, files=[part])
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _broker(self, args: List[str], ctx: Dict[str, Any]) -> str:
        method, args = _pop_flag(args, "--method", "fifo")
        if not args:
            return "Usage: /tax broker FILE [--method fifo|lifo|specific]"
        part, why = _file_part("file", args[0])
        if part is None:
            return why
        status, data = await _request(ctx, "POST", "/import-broker-csv",
                                      params={"method": method}, files=[part])
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _summary(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/ledger-summary",
                                      params={"year": args[0] if args else 0})
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _categorize(self, args: List[str], ctx: Dict[str, Any]) -> str:
        no_llm, args = _pop_switch(args, "--no-llm")
        status, data = await _request(ctx, "POST", "/categorize",
                                      params={"use_llm": str(not no_llm).lower()})
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _review(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/review-queue",
                                      params={"limit": args[0] if args else 50})
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _confirm(self, args: List[str], ctx: Dict[str, Any]) -> str:
        tax_class, args = _pop_flag(args, "--class", "")
        category, args = _pop_flag(args, "--category", "")
        notes, args = _pop_flag(args, "--notes", "")
        if not args:
            return "Usage: /tax confirm TXN_ID [--class C] [--category C] [--notes TEXT]"
        params = {"txn_id": args[0]}
        for k, v in (("tax_class", tax_class), ("expense_category", category),
                     ("notes", notes)):
            if v:
                params[k] = v
        status, data = await _request(ctx, "POST", "/review-txn", params=params)
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _approve(self, args: List[str], ctx: Dict[str, Any]) -> str:
        minimum, args = _pop_flag(args, "--min", "0.95")
        status, data = await _request(ctx, "POST", "/bulk-approve",
                                      params={"min_confidence": minimum})
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _rule(self, args: List[str], ctx: Dict[str, Any]) -> str:
        category, args = _pop_flag(args, "--category", "")
        note, args = _pop_flag(args, "--note", "")
        if len(args) < 2:
            return "Usage: /tax rule PATTERN TAX_CLASS [--category C] [--note TEXT]"
        status, data = await _request(ctx, "POST", "/add-merchant-rule", params={
            "pattern": args[0], "tax_class": args[1],
            "expense_category": category, "note": note,
        })
        return _dump(data) if status == 200 else _render_error(status, data)

    async def _export(self, args: List[str], ctx: Dict[str, Any]) -> str:
        year, args = _pop_flag(args, "--year", "2025")
        if not args:
            return "Usage: /tax export OUT.xlsx [--year Y]"
        out = Path(args[0]).expanduser()
        status, data = await _request(ctx, "POST", "/export-ledger-xlsx",
                                      params={"tax_year": year}, raw=True)
        if status != 200:
            return _render_error(status, data)
        out.write_bytes(data)
        return f"Wrote {out} ({len(data)} bytes)"
