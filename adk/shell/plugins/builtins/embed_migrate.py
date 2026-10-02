"""
embed-migrate Plugin for AitherShell
====================================

Switching a fleet's embedding model without losing recall, from the shell (the
embed-migrate CLI surface). A thin window onto the platform router
``/api/v1/embed-migrate/*`` -- the same surface the portal panel and the MCP
tools use -- called with YOUR bearer. Read-only: nothing here starts a migration
run or retires an embedder.

Usage:
    /embed-migrate               -- per-collection progress (same as /embed-migrate status)
    /embed-migrate plan          -- which collections migrate, and to what
    /embed-migrate status        -- per-collection progress + recall verdict
    /embed-migrate retire-gate   -- is the old embedder safe to retire: READY / NOT READY + blockers

The migration run itself is a separate operator tool on the fleet host; this plugin
only reports its state.

Alias: /emig
"""

from __future__ import annotations

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

PREFIX = "/api/v1/embed-migrate"

# The whole surface. Every entry is a GET; there is deliberately no run / retire verb.
ROUTES: Dict[str, str] = {
    "plan": "/plan",
    "status": "/status",
    "retire-gate": "/retire-gate",
}


def _genesis_url(ctx: Optional[Dict[str, Any]] = None) -> str:
    env = os.environ.get("AITHER_GENESIS_URL")
    if env:
        return env.rstrip("/")
    cfg = (ctx or {}).get("config")
    url = getattr(cfg, "url", "") if cfg is not None else ""
    return (url or "https://localhost:8001").rstrip("/")


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(ctx: Optional[Dict[str, Any]], path: str,
               params: Optional[dict] = None) -> Tuple[int, Any]:
    """GET one router path. Never raises: an unreachable platform is status 0 + a reason."""
    import httpx

    url = f"{_genesis_url(ctx)}{PREFIX}{path}"
    try:
        async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as client:
            resp = await client.get(url, params=params, headers=_headers())
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable"
        reason = f"platform unreachable: {type(exc).__name__}: {exc}"
        return 0, {"ok": False, "reason": reason[:200]}
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in -- run `aither login` first."
    if status == 0:
        reason = data.get("reason") if isinstance(data, dict) else data
        return f"Could not judge: {reason}"
    return f"Error {status}: {detail}"


def _refusal(data: Any) -> Optional[str]:
    """The line to print when the engine could not answer, else None.

    A 200 that carries ``ok: false`` is the engine saying it could not judge
    (stores unreachable, no state yet). It is never rendered as an empty success.
    """
    if not isinstance(data, dict):
        return f"Could not judge: unexpected response: {str(data)[:120]}"
    if data.get("ok") is False:
        reason = data.get("reason") or data.get("error") or data.get("detail") or "no reason given"
        return f"Could not judge: {reason}"
    return None


def _rows(collections: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """Collections as (key, row), whether the router sent a list or a keyed mapping."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(collections, dict):
        for key, row in sorted(collections.items()):
            out.append((str(key), row if isinstance(row, dict) else {}))
    elif isinstance(collections, list):
        for row in collections:
            if not isinstance(row, dict):
                continue
            key = row.get("key") or "/".join(
                str(p) for p in (row.get("store"), row.get("source") or row.get("name")) if p)
            out.append((str(key or "?"), row))
    return out


def _space(value: Any) -> str:
    """An embedding space as text: a bare name or a {name, model, dim} record."""
    if isinstance(value, dict):
        name = value.get("name") or value.get("model") or "?"
        return f"{name} ({value['dim']}-d)" if value.get("dim") else str(name)
    return str(value) if value else "?"


def render_plan(data: Dict[str, Any]) -> str:
    """Human-readable migration plan."""
    refused = _refusal(data)
    if refused:
        return refused
    rows = _rows(data.get("collections"))
    lines = [f"{_space(data.get('source'))} -> {_space(data.get('target'))}"]
    migrate = [(k, r) for k, r in rows if r.get("action") == "migrate"]
    empty = [(k, r) for k, r in rows if r.get("action") == "empty"]
    other = [(k, r) for k, r in rows if r.get("action") not in ("migrate", "empty")]
    lines.append(f"MIGRATE ({len(migrate)}):")
    for key, r in migrate:
        have = r.get("target_points")
        lines.append(f"  {key} -> {r.get('target')}  {r.get('points')} points"
                     f" ({'target not created' if have is None else f'{have} in target'})")
    if not migrate:
        lines.append("  (none)")
    if empty:
        lines.append(f"EMPTY ({len(empty)}): create the empty target only")
        lines += [f"  {key} -> {r.get('target')}" for key, r in empty]
    if other:
        lines.append(f"SKIPPED ({len(other)}):")
        lines += [f"  {key}  {r.get('action')}: {r.get('reason') or '?'}" for key, r in other]
    unreachable = data.get("stores_unreachable") or data.get("errors") or {}
    if isinstance(unreachable, dict) and unreachable:
        lines.append("UNREACHABLE (not planned, never counted as migrated):")
        lines += [f"  {k}: {str(v)[:90]}" for k, v in sorted(unreachable.items())]
    return "\n".join(lines)


def _verdict(row: Dict[str, Any]) -> str:
    """Recall verdict for one collection. Unknown stays unknown -- never a pass."""
    explicit = row.get("verdict") or row.get("recall_verdict")
    if explicit:
        return str(explicit)
    verify = row.get("verify")
    if isinstance(verify, dict) and "ok" in verify:
        if verify.get("ok") is True:
            return "PASS"
        reasons = verify.get("reasons") or []
        return "FAIL" + (f" ({'; '.join(str(x) for x in reasons)[:120]})" if reasons else "")
    return "UNVERIFIED"


def render_status(data: Dict[str, Any]) -> str:
    """Human-readable per-collection progress and recall verdict."""
    refused = _refusal(data)
    if refused:
        return refused
    rows = _rows(data.get("collections"))
    lines = [f"{_space(data.get('source'))} -> {_space(data.get('target'))}; "
             f"retire-ready at {data.get('retire_ready_at') or 'never'}",
             f"COLLECTIONS ({len(rows)}):"]
    for key, r in rows:
        pending = r.get("pending")
        line = (f"  {key} -> {r.get('target')}  "
                f"pending {'?' if pending is None else pending}/{r.get('source_points', '?')}  "
                f"embedded {r.get('embedded_total') or 0}  {_verdict(r)}")
        if r.get("verified_at"):
            line += f" at {r['verified_at']}"
        lines.append(line)
        if r.get("skipped") or r.get("preview_fallback"):
            lines.append(f"      skipped {r.get('skipped') or 0}, "
                         f"preview-fallback {r.get('preview_fallback') or 0}")
        if r.get("error"):
            lines.append(f"      error: {str(r['error'])[:160]}")
    if not rows:
        lines.append("  (none: no migration state recorded yet)")
    return "\n".join(lines)


def render_retire_gate(data: Dict[str, Any]) -> str:
    """Human-readable retire gate. READY only on a literal ``ready: true`` with no blockers."""
    refused = _refusal(data)
    if refused:
        return "NOT READY  " + refused
    blockers = [str(b) for b in (data.get("blockers") or [])]
    ready = data.get("ready") is True and not blockers
    lines = [f"{'READY' if ready else 'NOT READY'}  retire {_space(data.get('source'))} "
             f"(target {_space(data.get('target'))})"]
    if blockers:
        lines.append(f"BLOCKERS ({len(blockers)}):")
        lines += [f"  {b[:200]}" for b in blockers]
    elif not ready:
        lines.append("  the gate did not report ready; no blocker list was returned")
    if data.get("checked_at"):
        lines.append(f"  checked at {data['checked_at']}")
    return "\n".join(lines)


_RENDER = {
    "plan": render_plan,
    "status": render_status,
    "retire-gate": render_retire_gate,
}


class EmbedMigratePlugin(SlashCommand):
    name = "embed-migrate"
    description = "Embedding model migration: plan, per-collection status and the retire gate"
    aliases = ["emig"]

    def __init__(self):
        super().__init__(name="embed-migrate", description=self.description, aliases=["emig"])

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = args[0].lower() if args else "status"
        if sub in ("help", "-h", "--help"):
            return self.get_help()
        if sub in ("retire_gate", "gate"):
            sub = "retire-gate"
        if sub not in ROUTES:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        status, data = await _get(ctx, ROUTES[sub])
        return _RENDER[sub](data) if status == 200 else _error(status, data)
