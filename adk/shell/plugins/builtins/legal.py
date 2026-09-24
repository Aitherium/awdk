"""
Themis Legal Plugin for AitherShell
===================================

Contract clause-risk review from the shell. A thin window onto the Genesis router
``/api/v1/themis-legal/*`` -- the same surface the portal panel uses -- called with
YOUR bearer, so the router's pro-tier gate and per (tenant, user) document store
apply exactly as they do in the portal. NOT legal advice.

Usage:
    /legal status                         — Am I entitled? (plan tier vs min tier)
    /legal analyze FILE [--party NAME]    — Flag predatory / risky clauses in a text file
    /legal docs                           — List stored documents
    /legal add FILE [--type TYPE]         — Store a text document (contract by default)
    /legal search QUERY...                — Keyword search stored documents
    /legal rm DOC_ID                      — Delete a stored document
    /legal letter SITUATION :: OUTCOME [--tone TONE]
                                          — Draft a negotiation letter

Aliases: /themis
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

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

PREFIX = "/api/v1/themis-legal"
_MAX_TEXT = 200_000  # the router's own request-body cap


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
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(ctx: Optional[Dict[str, Any]], method: str, path: str,
                   body: Optional[dict] = None,
                   params: Optional[dict] = None) -> Tuple[int, Any]:
    import httpx

    url = f"{_genesis_url(ctx)}{PREFIX}{path}"
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as client:
        resp = await client.request(method, url, json=body, params=params, headers=_headers())
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _render_error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in — run `aither login` first (Themis Legal needs your account)."
    if status == 403 and isinstance(detail, dict) and detail.get("status") == "not_entitled":
        return (f"{detail.get('message', 'Upgrade required')} "
                f"(you are on '{detail.get('current_tier', '?')}', needs "
                f"'{detail.get('min_tier', 'pro')}'): {detail.get('upgrade_url', '')}").strip()
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


def _read_text(path_arg: str) -> Tuple[Optional[str], str]:
    p = Path(path_arg).expanduser()
    if not p.is_file():
        return None, f"No such file: {p}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None, f"{p} is empty"
    if len(text) > _MAX_TEXT:
        return None, f"{p} is {len(text)} characters; the limit is {_MAX_TEXT}"
    return text, p.name


class LegalPlugin(SlashCommand):
    name: str = "legal"
    aliases: List[str] = ["themis"]
    description: str = "Themis Legal — contract clause-risk review (not legal advice)"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass whose __init__ does not carry a
        # subclass's class attrs onto the instance; without this the registry
        # registers the plugin under an EMPTY name (see durability.py).
        super().__init__(*args, **kwargs)
        self.name = "legal"
        self.aliases = ["themis"]
        self.description = "Themis Legal — contract clause-risk review (not legal advice)"
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower(), args[1:]
        handler = {
            "status": self._status,
            "analyze": self._analyze,
            "docs": self._docs,
            "add": self._add,
            "search": self._search,
            "rm": self._rm,
            "letter": self._letter,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    async def _status(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/check-entitlement")
        if status != 200:
            return _render_error(status, data)
        verdict = "entitled" if data.get("entitled") else "NOT entitled"
        return (f"Themis Legal: {verdict} (tier '{data.get('current_tier')}', "
                f"needs '{data.get('min_tier')}')")

    async def _analyze(self, args: List[str], ctx: Dict[str, Any]) -> str:
        party, args = _pop_flag(args, "--party", "reviewer")
        if not args:
            return "Usage: /legal analyze FILE [--party NAME]"
        text, why = _read_text(args[0])
        if text is None:
            return why
        status, data = await _request(ctx, "POST", "/analyze", {"text": text, "party": party})
        if status != 200:
            return _render_error(status, data)
        counts = data.get("counts") or {}
        lines = [f"{why}: {counts.get('TOTAL', 0)} finding(s) — "
                 + ", ".join(f"{k} {counts.get(k, 0)}"
                             for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW"))]
        for f in data.get("findings") or []:
            lines.append(f"  [{f.get('severity')}] {f.get('title')}: {f.get('risk', '')}")
        lines.append("NOT legal advice — have counsel review binding terms.")
        return "\n".join(lines)

    async def _docs(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "GET", "/documents")
        if status != 200:
            return _render_error(status, data)
        docs = data.get("documents") or []
        if not docs:
            return "No stored documents."
        return "\n".join(f"  {d.get('doc_id') or '?'}  {d.get('filename', '')}"
                         for d in docs)

    async def _add(self, args: List[str], ctx: Dict[str, Any]) -> str:
        doc_type, args = _pop_flag(args, "--type", "contract")
        if not args:
            return "Usage: /legal add FILE [--type TYPE]"
        text, filename = _read_text(args[0])
        if text is None:
            return filename
        status, data = await _request(ctx, "POST", "/documents",
                                      {"text": text, "filename": filename,
                                       "doc_type": doc_type})
        if status != 200:
            return _render_error(status, data)
        return json.dumps(data, indent=2, default=str)

    async def _search(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /legal search QUERY..."
        status, data = await _request(ctx, "GET", "/search",
                                      params={"q": " ".join(args), "top_k": 5})
        if status != 200:
            return _render_error(status, data)
        return json.dumps(data, indent=2, default=str)

    async def _rm(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /legal rm DOC_ID"
        status, data = await _request(ctx, "DELETE", f"/documents/{quote(args[0], safe='')}")
        if status != 200:
            return _render_error(status, data)
        return f"Deleted {args[0]}"

    async def _letter(self, args: List[str], ctx: Dict[str, Any]) -> str:
        tone, args = _pop_flag(args, "--tone", "firm_but_professional")
        situation, sep, outcome = " ".join(args).partition("::")
        if not sep or not situation.strip() or not outcome.strip():
            return "Usage: /legal letter SITUATION :: OUTCOME [--tone TONE]"
        status, data = await _request(ctx, "POST", "/draft-letter",
                                      {"situation": situation.strip(),
                                       "desired_outcome": outcome.strip(), "tone": tone})
        if status != 200:
            return _render_error(status, data)
        return str(data.get("letter") or json.dumps(data, indent=2, default=str))
