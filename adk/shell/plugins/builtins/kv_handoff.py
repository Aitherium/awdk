"""
kv-handoff Plugin for AitherShell
=================================

Cross-model KV cache handoff from the shell (the kv-handoff CLI surface). A thin
window onto the platform router ``/api/v1/kv-handoff/*`` -- the same surface the
portal panel and the MCP tools use -- called with YOUR bearer. Read-only.

Usage:
    /kv-handoff                  -- eligible fleet pairs (same as /kv-handoff pairs)
    /kv-handoff pairs            -- eligible pairs + why the rest are blocked
    /kv-handoff verdict <pack>   -- PASS / REFUSED for one pack under the packs root

The capture / fit / measure pipeline itself is the library's CLI:
``python -m aither_kvcache.kvtransfer.{capture,fit,transfer}``.

Alias: /kvh
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

PREFIX = "/api/v1/kv-handoff"


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
    import httpx

    url = f"{_genesis_url(ctx)}{PREFIX}{path}"
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as client:
        resp = await client.get(url, params=params, headers=_headers())
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return resp.status_code, data


def _error(status: int, data: Any) -> str:
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if status == 401:
        return "Not signed in -- run `aither login` first."
    return f"Error {status}: {detail}"


def render_pairs(data: Dict[str, Any]) -> str:
    """Human-readable pairs report."""
    eligible = data.get("eligible") or []
    lines = [f"geometry measured {data.get('measured_at') or '?'}; "
             f"{len(data.get('resolved') or [])}/{len(data.get('models') or [])} models resolved",
             f"ELIGIBLE ({len(eligible)}):"]
    lines += [f"  {r['source']} -> {r['target']}" for r in eligible] or ["  (none)"]
    summary = data.get("blocked_summary") or {}
    if summary:
        lines.append(f"BLOCKED ({data.get('blocked_count', 0)}):")
        ordered = sorted(summary.items(), key=lambda kv: -kv[1])
        lines += [f"  {n:>3}  {label}" for label, n in ordered]
    unresolved = data.get("unresolved") or {}
    if unresolved:
        lines.append("UNRESOLVED (never counted as compatible):")
        lines += [f"  {k}: {str(v)[:90]}" for k, v in sorted(unresolved.items())]
    return "\n".join(lines)


def render_verdict(data: Dict[str, Any]) -> str:
    """Human-readable pack verdict."""
    acc = data.get("acceptance") or {}
    floors = data.get("floors") or {}
    lines = [f"{data.get('verdict')}  {data.get('pack')}  "
             f"({data.get('source')} -> {data.get('target')})"]
    if data.get("refused_by"):
        lines.append(f"  refused by {data['refused_by']}: {data.get('reason')}")
    if acc:
        lines.append(f"  top-1 agreement {acc.get('top1_agreement')} "
                     f"(floor {floors.get('min_top1_agreement')}, "
                     f"control {acc.get('top1_control')})")
        lines.append(f"  NLL delta {acc.get('nll_delta')} "
                     f"(ceiling {floors.get('max_nll_delta')})")
        lines.append(f"  positions {acc.get('n_positions')} "
                     f"(floor {floors.get('min_acceptance_positions')})")
    for k, v in (data.get("derived") or {}).items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


class KvHandoffPlugin(SlashCommand):
    name = "kv-handoff"
    description = "Cross-model KV cache handoff: eligible pairs and pack PASS/REFUSED verdicts"
    aliases = ["kvh"]

    def __init__(self):
        super().__init__(name="kv-handoff", description=self.description, aliases=["kvh"])

    def get_help(self) -> str:
        return __doc__ or ""

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        sub = args[0].lower() if args else "pairs"
        if sub in ("help", "-h", "--help"):
            return self.get_help()
        if sub == "pairs":
            status, data = await _get(ctx, "/pairs")
            return render_pairs(data) if status == 200 else _error(status, data)
        if sub == "verdict":
            if len(args) < 2:
                return "Usage: /kv-handoff verdict <pack-name>"
            status, data = await _get(ctx, "/packs/verdict", {"path": args[1]})
            return render_verdict(data) if status == 200 else _error(status, data)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
