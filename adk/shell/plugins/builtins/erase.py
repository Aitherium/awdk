"""
AitherErase Plugin for AitherShell
==================================

Personal-data removal from the shell (.PRODUCTS/.ERASE layer 6). A thin window
onto the Genesis router ``/api/v1/aither-erase/*`` -- the same surface the portal
panel uses -- called with YOUR bearer, so the owner and tenant are derived from
your account on the server and never from anything this client sends.

Usage:
    /erase profile                      — Show your removal profile (masked) + consent state
    /erase profile set --name N --email E [--phone P] [--address A] [--dob D] --residency US-CA
                                        — Create/replace your profile (flags repeat)
    /erase consent --sign "Full Name"   — Authorize AitherErase to act as your agent
    /erase consent --revoke             — Withdraw the authorization
    /erase scan [--limit N]             — Find brokers and open removal requests
    /erase tasks [--status STATUS]      — List your removal requests
    /erase report                       — Coverage report (confirmed vs still exposed)
    /erase report --export FILE         — Save the dated audit trail as JSON
    /erase pause | /erase resume        — Stop / restart new work on your profile

Aliases: /aither-erase
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

PREFIX = "/api/v1/aither-erase"
TASK_STATUSES = ("discovered", "drafted", "sent", "awaiting_reply", "confirmed",
                 "needs_verification", "rejected", "no_response", "relisted")


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    """Bearer only. The owner and tenant come from the authenticated caller on
    the server, never from a header this client chooses."""
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
        return "Not signed in — run `aither login` first (AitherErase acts for your account)."
    if status == 404 and isinstance(detail, str) and "profile" in detail:
        return "No removal profile yet — run `/erase profile set ...` first."
    if isinstance(detail, str):
        return f"Error {status}: {detail}"
    return f"Error {status}: {json.dumps(detail, default=str)}"


def _pop_flag(args: List[str], flag: str) -> Tuple[Optional[str], List[str]]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
        return None, args[:i]
    return None, args


def _pop_all(args: List[str], flag: str) -> Tuple[List[str], List[str]]:
    values: List[str] = []
    while flag in args:
        value, args = _pop_flag(args, flag)
        if value is None:
            break
        values.append(value)
    return values, args


class ErasePlugin(SlashCommand):
    name: str = "erase"
    aliases: List[str] = ["aither-erase"]
    description: str = "AitherErase — find data brokers holding your info and get it deleted"
    category: str = "productivity"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass whose __init__ does not carry a
        # subclass's class attrs onto the instance; without this the registry
        # registers the plugin under an EMPTY name (see durability.py).
        super().__init__(*args, **kwargs)
        self.name = "erase"
        self.aliases = ["aither-erase"]
        self.description = "AitherErase — find data brokers holding your info and get it deleted"
        self.category = "productivity"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower(), args[1:]
        handler = {
            "profile": self._profile,
            "consent": self._consent,
            "scan": self._scan,
            "tasks": self._tasks,
            "report": self._report,
            "pause": self._pause,
            "resume": self._resume,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    async def _profile(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if args and args[0] == "set":
            return await self._profile_set(args[1:], ctx)
        status, data = await _request(ctx, "GET", "/profile")
        if status != 200:
            return _render_error(status, data)
        consent = data.get("consent") or {}
        consent_line = (f"recorded {consent.get('at')}" if consent.get("recorded")
                        else "NOT recorded")
        lines = [f"Residency: {data.get('residency') or 'not set'}",
                 f"Consent:   {consent_line}",
                 f"State:     {'paused' if data.get('paused') else 'active'}",
                 f"Last scan: {data.get('last_scan_at') or 'never'}"]
        for key, values in (data.get("profile") or {}).items():
            if values:
                shown = ", ".join(values) if isinstance(values, list) else str(values)
                lines.append(f"  {key}: {shown}")
        return "\n".join(lines)

    async def _profile_set(self, args: List[str], ctx: Dict[str, Any]) -> str:
        names, args = _pop_all(args, "--name")
        emails, args = _pop_all(args, "--email")
        phones, args = _pop_all(args, "--phone")
        addresses, args = _pop_all(args, "--address")
        dob, args = _pop_flag(args, "--dob")
        residency, args = _pop_flag(args, "--residency")
        if not names or not emails or not residency:
            return ("Usage: /erase profile set --name N --email E [--phone P] "
                    "[--address A] [--dob D] --residency US-CA")
        body: Dict[str, Any] = {"full_names": names, "emails": emails, "phones": phones,
                                "addresses": addresses, "residency": residency.upper()}
        if dob:
            body["dob"] = dob
        status, data = await _request(ctx, "POST", "/profile", body)
        if status != 200:
            return _render_error(status, data)
        return ("Profile saved. Next: `/erase consent --sign \"Your Full Name\"` to "
                "authorize requests on your behalf.")

    async def _consent(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if "--revoke" in args:
            status, data = await _request(ctx, "POST", "/consent",
                                          {"authorize": False, "signature": "revoke"})
            if status != 200:
                return _render_error(status, data)
            return "Authorization revoked. No new removal requests will be filed."
        signature, _ = _pop_flag(args, "--sign")
        if not signature or len(signature.strip()) < 2:
            # Consent is the user's own act: never defaulted, never inferred.
            return "Usage: /erase consent --sign \"Your Full Name\"  (or --revoke)"
        status, data = await _request(ctx, "POST", "/consent",
                                      {"authorize": True, "signature": signature.strip()})
        if status != 200:
            return _render_error(status, data)
        return (f"Authorization recorded ({data.get('version')}).\n"
                f"{data.get('statement', '')}").rstrip()

    async def _scan(self, args: List[str], ctx: Dict[str, Any]) -> str:
        limit, _ = _pop_flag(args, "--limit")
        body: Dict[str, Any] = {}
        if limit:
            if not limit.isdigit() or int(limit) < 1:
                return "Usage: /erase scan [--limit N]  (N >= 1)"
            body["broker_limit"] = int(limit)
        status, data = await _request(ctx, "POST", "/scan", body)
        if status != 200:
            return _render_error(status, data)
        return (f"Scan complete: {data.get('created', 0)} new removal request(s) "
                f"across {data.get('directory_size', '?')} brokers "
                f"(directory {data.get('directory_version', '?')}).")

    async def _tasks(self, args: List[str], ctx: Dict[str, Any]) -> str:
        wanted, _ = _pop_flag(args, "--status")
        params = None
        if wanted:
            if wanted not in TASK_STATUSES:
                return f"Unknown status {wanted!r}; one of: {', '.join(TASK_STATUSES)}"
            params = {"status": wanted}
        status, data = await _request(ctx, "GET", "/tasks", params=params)
        if status != 200:
            return _render_error(status, data)
        tasks = data.get("tasks") or []
        if not tasks:
            return "No removal requests yet — run `/erase scan`."
        return "\n".join(f"  {t.get('status', '?'):<19} {t.get('broker_name') or t.get('broker')}"
                         for t in tasks)

    async def _report(self, args: List[str], ctx: Dict[str, Any]) -> str:
        out, _ = _pop_flag(args, "--export")
        if "--export" in args and not out:
            return "Usage: /erase report --export FILE"
        if out:
            status, data = await _request(ctx, "GET", "/report/export")
            if status != 200:
                return _render_error(status, data)
            path = Path(out).expanduser()
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            return f"Audit trail written to {path}"
        status, data = await _request(ctx, "GET", "/report")
        if status != 200:
            return _render_error(status, data)
        return (f"Brokers checked: {data.get('brokers_checked', 0)}\n"
                f"Confirmed removed: {data.get('confirmed', 0)}\n"
                f"Pending: {data.get('pending', 0)}   Rejected: {data.get('rejected', 0)}\n"
                f"Still exposed: {data.get('still_exposed', 0)} "
                "(data stays exposed until a broker confirms removal)")

    async def _pause(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "POST", "/pause", {})
        return "Paused: no new removal work will be filed." if status == 200 \
            else _render_error(status, data)

    async def _resume(self, args: List[str], ctx: Dict[str, Any]) -> str:
        status, data = await _request(ctx, "POST", "/resume", {})
        return "Resumed." if status == 200 else _render_error(status, data)
