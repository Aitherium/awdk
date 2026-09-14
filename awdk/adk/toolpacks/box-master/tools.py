"""Box Master tools — discovery, activation, and learning.

Design rules:
  * Pure HTTP (httpx.AsyncClient). No SDK, no browser OAuth.
  * All calls trust the internal CA via tls_verify(); never disable TLS checks.
  * Fail soft with actionable guidance — a missing gateway or endpoint is a
    status message, not an exception into the agent loop.
  * Configuration from environment only (AITHER_MCP_URL, etc.). Never
    hardcoded, never logged.

Transports:

  1. GATEWAY — https://mcp.aitherium.com (or AITHER_MCP_URL) provides
     /discover (search cards) and /discover/detail (full tool schema).
     Calls carry Authorization: Bearer <token> if AITHER_MCP_KEY is set.

  2. WORLD-MODEL — :8210 (internal, /observe endpoint) to feed agent
     outcomes for next-turn recall. Calls use the internal CA.

  3. LOCAL — system_report() / fs_map() query the box directly.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Any

import httpx

from adk._tls import tls_verify
from adk.graph_memory import GraphMemory

logger = logging.getLogger("box_master_pack")

_TIMEOUT = 30.0
_GATEWAY_URL = (
    os.getenv("AITHER_MCP_URL", "https://mcp.aitherium.com").rstrip("/")
)
_GATEWAY_KEY = os.getenv("AITHER_MCP_KEY", "")
_WORLD_MODEL_URL = os.getenv("AITHER_WORLD_MODEL_URL", "http://localhost:8210")


# ── Utilities ───────────────────────────────────────────────────────


def _auth_headers() -> dict[str, str]:
    """Return auth headers for gateway calls."""
    if not _GATEWAY_KEY:
        return {}
    return {"Authorization": f"Bearer {_GATEWAY_KEY}"}


async def _get(
    url: str,
    params: dict | None = None,
    use_gateway_auth: bool = True,
    timeout: float = _TIMEOUT,
) -> dict:
    """One guarded HTTP GET call. Returns {"ok": True, "data": ...} or fail-soft
    {"ok": False, "error": ..., "fix": ...}.
    """
    try:
        headers = _auth_headers() if use_gateway_auth else {}
        async with httpx.AsyncClient(
            timeout=timeout, verify=tls_verify()
        ) as client:
            resp = await client.get(
                url, headers=headers, params=params or {}
            )
        if resp.status_code == 200:
            return {"ok": True, "data": resp.json()}
        if resp.status_code == 401:
            return {
                "ok": False,
                "error": "authentication required",
                "fix": "set AITHER_MCP_KEY in environment",
            }
        if resp.status_code == 404:
            return {
                "ok": False,
                "error": f"endpoint not found: {url}",
                "detail": f"HTTP {resp.status_code}",
            }
        return {
            "ok": False,
            "error": f"gateway returned HTTP {resp.status_code}",
            "detail": resp.text[:200],
        }
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "error": "gateway unreachable",
            "detail": str(exc)[:100],
            "fix": f"check AITHER_MCP_URL (currently {_GATEWAY_URL})",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "gateway call failed",
            "detail": str(exc)[:100],
        }


async def _post(
    url: str,
    json_body: dict,
    use_gateway_auth: bool = False,
    timeout: float = _TIMEOUT,
) -> dict:
    """One guarded HTTP POST call. Returns {"ok": True} or fail-soft error."""
    try:
        headers = (_auth_headers() if use_gateway_auth else {})
        headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(
            timeout=timeout, verify=tls_verify()
        ) as client:
            resp = await client.post(
                url, headers=headers, json=json_body
            )
        if resp.status_code in (200, 201, 202):
            return {"ok": True, "status": resp.status_code}
        return {
            "ok": False,
            "error": f"POST returned HTTP {resp.status_code}",
            "detail": resp.text[:200],
        }
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "error": "endpoint unreachable",
            "detail": str(exc)[:100],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "POST failed",
            "detail": str(exc)[:100],
        }


# ── Tool 1: explore ────────────────────────────────────────────────


_DISCOVER_DOMAINS = ("tools", "agents", "fs", "code", "skills", "any")


async def explore(query: str, domain: str = "any", k: int = 8) -> dict:
    """Search the gateway /discover endpoint for compact cards matching query.

    query: natural-language question or name, e.g. 'code search',
           'deploy workers', 'vector search', 'memory'.
    domain: which catalog to search — one of 'tools', 'agents', 'fs',
            'code', 'skills', or 'any' (all). Defaults to 'any'.
            (Non-internal callers only ever receive 'tools' cards; the other
            domains are entitlement-gated server-side.)
    k: maximum results to return (default 8).

    Returns {"ok": True, "results": [...]} with compact cards
    or {"ok": False, "error": ..., "fix": ...} on failure.
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    q = query.strip()
    if k < 1 or k > 100:
        k = 8
    if domain not in _DISCOVER_DOMAINS:
        domain = "any"
    url = f"{_GATEWAY_URL}/discover"
    params = {"q": q, "domain": domain, "k": k}
    result = await _get(url, params=params)
    if not result.get("ok"):
        return result
    data = result.get("data") or {}
    cards = data.get("results") or []
    return {
        "ok": True,
        "query": q,
        "domain": domain,
        "count": len(cards),
        "results": cards,
    }


# ── Tool 2: activate ───────────────────────────────────────────────


async def activate(ref: str, domain: str = "tools") -> dict:
    """Fetch full details for one discovered item and make it callable.

    Calls /discover/detail to get the item's full detail — for a tool, its
    OpenAI-compatible schema so you can call it immediately via
    MCPBridge.call_tool(); for code/agents/fs, the full record.

    ref: the reference from an explore() result card, e.g. 'cf_dns_list'.
    domain: the item's domain — one of 'tools', 'agents', 'fs', 'code',
            'skills'. Must match the card's `domain`. Default 'tools'.

    Returns {"ok": True, "schema": {...}} or {"ok": False, "error": ...}.
    """
    if not ref or not ref.strip():
        return {"error": "ref is required (tool reference from explore)"}
    ref = ref.strip()
    if domain not in ("tools", "agents", "fs", "code", "skills"):
        domain = "tools"
    url = f"{_GATEWAY_URL}/discover/detail"
    params = {"ref": ref, "domain": domain}
    result = await _get(url, params=params)
    if not result.get("ok"):
        return result
    schema = result.get("data") or {}
    tool_name = schema.get("name", ref)
    description = schema.get("description", "")
    return {
        "ok": True,
        "tool": tool_name,
        "description": description,
        "schema": schema,
        "note": (
            "use MCPBridge.call_tool(tool, arguments) "
            "to invoke it immediately"
        ),
    }


# ── Tool 3: system_report ──────────────────────────────────────────


async def system_report() -> dict:
    """Introspect the box: hardware, OS, memory, network, filesystems.

    Returns a dict with platform info, CPU count, memory (total/available),
    disk space for key mount points, and network interfaces.
    """
    try:
        uname = platform.uname()
        mem_info = (
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if hasattr(os, "sysconf")
            else None
        )
        report: dict[str, Any] = {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count() or 0,
            "hostname": uname.nodename,
            "pythonver": platform.python_version(),
            "uname": {
                "system": uname.system,
                "node": uname.nodename,
                "release": uname.release,
                "version": uname.version,
                "machine": uname.machine,
            },
        }
        if mem_info:
            report["memory_bytes"] = mem_info
        mount_points = ["/", "/tmp", "/home"] if uname.system != "Windows" \
            else ["C:\\", "D:\\"]
        for mp in mount_points:
            try:
                stat = shutil.disk_usage(mp)
                report[f"disk_{mp.replace('/', '_')}"] = {
                    "total": stat.total,
                    "used": stat.used,
                    "free": stat.free,
                }
            except Exception:
                pass
        return {"ok": True, "report": report}
    except Exception as exc:
        return {"ok": False, "error": f"introspection failed: {exc}"}


# ── Tool 4: fs_map ─────────────────────────────────────────────────


async def fs_map(root: str = ".", depth: int = 3) -> dict:
    """Walk a filesystem tree to depth levels. Returns structure for
    understanding code layout before exploring codebases.

    root: path to start from (default ".").
    depth: max directory levels to traverse (default 3).

    Returns {"ok": True, "tree": {...}} with the directory structure.
    """
    if depth < 1:
        depth = 1
    if depth > 10:
        depth = 10
    try:
        root_path = Path(root).resolve()
        if not root_path.exists():
            return {
                "ok": False,
                "error": f"path does not exist: {root}",
            }
        tree = {}
        _walk_tree(root_path, tree, depth)
        return {
            "ok": True,
            "root": str(root_path),
            "depth": depth,
            "tree": tree,
        }
    except Exception as exc:
        return {"ok": False, "error": f"fs_map failed: {exc}"}


def _walk_tree(path: Path, node: dict, depth: int) -> None:
    """Recursively build a tree dict of filesystem structure."""
    if depth < 1:
        return
    try:
        items = sorted(path.iterdir())
    except (PermissionError, OSError):
        return
    for item in items:
        if item.name.startswith("."):
            continue
        if item.is_dir():
            node[item.name] = {}
            _walk_tree(item, node[item.name], depth - 1)
        else:
            node[item.name] = "file"


# ── Tool 5: learn ──────────────────────────────────────────────────


async def learn(
    query: str, ref: str = "", outcome: str = ""
) -> dict:
    """Feed agent discoveries and outcomes to the world-model and local
    GraphMemory for next-turn recall.

    Captures what you discovered (via explore/activate), learned, or built.
    Writes to:
      1. Local GraphMemory (survives in session, searchable via recall)
      2. World-model /observe endpoint (:8210) for fleet-wide learning

    query: what you were trying to solve, e.g. 'deploy cloudflare workers'.
    ref: tool or concept ref that helped, e.g. 'cf_worker_deploy'.
    outcome: what you learned or built, e.g. 'deployed to workers.dev'.

    Returns {"ok": True} or {"ok": False, "error": ...}.
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    q = query.strip()
    r = (ref or "").strip()
    o = (outcome or "").strip()
    tasks = []
    graph = GraphMemory(agent_name="box-master")
    if r and o:
        tasks.append(graph.remember(q, "resolved_by", r))
        tasks.append(graph.remember(r, "resolved", o))
    elif o:
        tasks.append(graph.remember(q, "outcome", o))
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        logger.debug("graph remember failed: %s", exc)
    observe_payload = {
        "agent": "box-master",
        "query": q,
        "ref": r,
        "outcome": o,
        "timestamp": __import__("time").time(),
    }
    result = await _post(
        f"{_WORLD_MODEL_URL}/observe",
        observe_payload,
        use_gateway_auth=False,
    )
    if not result.get("ok"):
        return {
            "ok": True,
            "learned_locally": True,
            "note": "local GraphMemory updated; world-model unreachable",
        }
    return {
        "ok": True,
        "learned_locally": True,
        "learned_fleet_wide": True,
    }
