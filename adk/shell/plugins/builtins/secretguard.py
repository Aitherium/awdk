"""
SecretGuard Plugin for AitherShell
==================================

Git secret detection and history purge from the shell. A thin window onto the
Genesis router ``/api/v1/secretguard/*`` -- the same surface the portal panel
uses -- called with YOUR bearer. It is a platform-operator surface: the
router answers 401 to an anonymous caller and 403 to anyone who is not a platform
operator, a workspace admin included. This plugin never sends a tenant id.

Usage:
    /secretguard scan [--tree|--history] [--depth N] [--repo PATH] [--config PATH]
                                          — Scan for leaked secrets (default: working tree)
    /secretguard allowlist [--repo PATH]  — List .gitleaks.toml allowlist entries
    /secretguard allow path|regex VALUE [--repo PATH]
                                          — Add an allowlist entry
    /secretguard purge FILE... [--repo PATH] [--execute]
                                          — Remove files from git history (dry run
                                            unless --execute is given)
    /secretguard hook [pre-commit|pre-push|both] [--repo PATH] [--force]
                                          — Install a gitleaks git hook

Aliases: /sg
"""

import json
import os
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

PREFIX = "/api/v1/secretguard"
# A full-history gitleaks scan or a filter-repo rewrite can run for minutes; the
# router's own subprocess timeout is 600 s.
_TIMEOUT_S = 620


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    """Bearer only -- authorization is decided server-side from the caller."""
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
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, verify=tls_verify()) as client:
        resp = await client.request(method, url, json=body, params=params, headers=_headers())
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _render_error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in -- run `aither login` first (SecretGuard needs your account)."
    if status == 403:
        return ("SecretGuard is a platform-operator surface; this account is not a "
                "platform operator.")
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


def _mask(match: str) -> str:
    """First 6 characters only -- a terminal is not the place to re-print a credential."""
    if not match:
        return ""
    return "*" * len(match) if len(match) <= 6 else match[:6] + "..."


def _payload(data: Any) -> Any:
    return data.get("data", data) if isinstance(data, dict) else data


class SecretguardPlugin(SlashCommand):
    name: str = "secretguard"
    aliases: List[str] = ["sg"]
    description: str = "SecretGuard — git secret scan, allowlist and history purge"
    category: str = "security"

    def __init__(self, *args: Any, **kwargs: Any):
        # The base SlashCommand is a dataclass whose __init__ does not carry a
        # subclass's class attrs onto the instance; without this the registry
        # registers the plugin under an EMPTY name (see durability.py).
        super().__init__(*args, **kwargs)
        self.name = "secretguard"
        self.aliases = ["sg"]
        self.description = "SecretGuard — git secret scan, allowlist and history purge"
        self.category = "security"

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help"):
            return self.get_help()
        sub, rest = args[0].lower(), args[1:]
        handler = {
            "scan": self._scan,
            "allowlist": self._allowlist,
            "allow": self._allow,
            "purge": self._purge,
            "hook": self._hook,
        }.get(sub)
        if handler is None:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(rest, ctx)

    async def _scan(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo, args = _pop_flag(args, "--repo", "")
        config, args = _pop_flag(args, "--config", "")
        depth_s, args = _pop_flag(args, "--depth", "0")
        history, args = _pop_switch(args, "--history")
        _tree, args = _pop_switch(args, "--tree")
        try:
            depth = max(0, int(depth_s))
        except ValueError:
            return f"--depth must be a number, got {depth_s!r}"
        body = {"mode": "history" if history else "tree", "depth": depth,
                "repo_path": repo, "config_path": config}
        status, data = await _request(ctx, "POST", "/scan", body)
        if status != 200:
            return _render_error(status, data)
        result = _payload(data)
        if isinstance(result, dict) and result.get("error"):
            return f"Scan failed: {result['error']}"
        leaks = result.get("leaks") or []
        lines = [f"{result.get('count', len(leaks))} leak(s) -- mode {result.get('scan_mode')}"]
        for leak in leaks:
            commit = str(leak.get("commit") or "")[:10]
            masked = _mask(str(leak.get("match") or ""))
            lines.append(f"  [{leak.get('rule')}] {leak.get('file')}:{leak.get('line')} "
                         f"{commit} {masked}")
        if leaks:
            lines.append("Rotate every exposed credential -- purging history does not un-leak it.")
        return "\n".join(lines)

    async def _allowlist(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo, _ = _pop_flag(args, "--repo", "")
        params = {"repo_path": repo} if repo else None
        status, data = await _request(ctx, "GET", "/allowlist", params=params)
        if status != 200:
            return _render_error(status, data)
        result = _payload(data)
        if isinstance(result, dict) and result.get("error"):
            return f"Allowlist read failed: {result['error']}"
        lines = [f"config: {result.get('config_path') or '(none -- adding an entry creates one)'}"]
        for key in ("paths", "regexes", "commits"):
            values = result.get(key) or []
            lines.append(f"{key}: {len(values)}")
            lines.extend(f"  {v}" for v in values)
        return "\n".join(lines)

    async def _allow(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo, args = _pop_flag(args, "--repo", "")
        if len(args) < 2 or args[0] not in ("path", "regex"):
            return "Usage: /secretguard allow path|regex VALUE [--repo PATH]"
        body = {"entry_type": args[0], "value": " ".join(args[1:]), "repo_path": repo}
        status, data = await _request(ctx, "POST", "/allowlist", body)
        if status != 200:
            return _render_error(status, data)
        result = _payload(data)
        if isinstance(result, dict) and result.get("error"):
            return f"Allowlist add failed: {result['error']}"
        return f"{result.get('status', 'ok')}: {args[0]} {body['value']}"

    async def _purge(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo, args = _pop_flag(args, "--repo", "")
        execute, args = _pop_switch(args, "--execute")
        if not args:
            return "Usage: /secretguard purge FILE... [--repo PATH] [--execute]"
        body = {"paths": args, "repo_path": repo, "dry_run": not execute}
        status, data = await _request(ctx, "POST", "/purge", body)
        if status != 200:
            return _render_error(status, data)
        result = _payload(data)
        if isinstance(result, dict) and result.get("error"):
            return f"Purge failed: {result['error']}"
        text = json.dumps(result, indent=2, default=str)
        if not execute:
            return text + "\nDry run only -- re-run with --execute to rewrite history."
        return text + "\nHistory rewritten: force-push, and rotate every exposed credential."

    async def _hook(self, args: List[str], ctx: Dict[str, Any]) -> str:
        repo, args = _pop_flag(args, "--repo", "")
        force, args = _pop_switch(args, "--force")
        kind = args[0] if args else "pre-commit"
        if kind not in ("pre-commit", "pre-push", "both"):
            return "Usage: /secretguard hook [pre-commit|pre-push|both] [--repo PATH] [--force]"
        body = {"hook": kind, "repo_path": repo, "force": force}
        status, data = await _request(ctx, "POST", "/hooks", body)
        if status != 200:
            return _render_error(status, data)
        result = _payload(data)
        if isinstance(result, dict) and result.get("error"):
            return f"Hook install failed: {result['error']}"
        return json.dumps(result, indent=2, default=str)
