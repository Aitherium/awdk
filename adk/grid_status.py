"""AitherGrid node status -- one probe, two surfaces.

``adk grid status`` prints it; the ADK node server exposes it as
``GET /grid/status`` (behind the server's global auth middleware) so a panel or
a peer can read node health without shelling out to the CLI.

Nodes come from ``~/.aither/config.json``::

    grid_nodes = {"reasoning": {"host", "port", "model"},
                  "cluster":   [{"host", "port", "model"}, ...]}

falling back to the flat ``reasoning_url`` / ``cluster_url`` keys.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_GRID_PORT = 8121

Probe = Callable[[str, int], Dict[str, Any]]


def probe_node(host: str, port: int, timeout: float = 5.0) -> Dict[str, Any]:
    """Probe one grid node: /health, then the OpenAI-compatible /v1/models.

    Returns ``{"state": "healthy"|"no_api"|"unreachable", "models": [...]}``.
    """
    headers = {"User-Agent": "AitherADK/1.0"}
    try:
        with urlopen(Request(f"http://{host}:{port}/health", headers=headers), timeout=timeout):
            pass
    except Exception as exc:  # noqa: BLE001 -- any failure = unreachable
        return {"state": "unreachable", "models": [], "error": type(exc).__name__}
    try:
        req = Request(f"http://{host}:{port}/v1/models", headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read())
                models = [m.get("id", "") for m in data.get("data", [])]
                return {"state": "healthy", "models": models}
    except Exception as exc:  # noqa: BLE001 -- up, but no /v1 (missing --api-oai)
        return {"state": "no_api", "models": [], "error": type(exc).__name__}
    return {"state": "no_api", "models": []}


def _url_node(url: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    return {"host": host, "port": parsed.port or DEFAULT_GRID_PORT}


def configured_nodes(saved: Dict[str, Any], grid_nodes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every configured grid node as ``{"role", "host", "port", "model"}``."""
    nodes: List[Dict[str, Any]] = []
    r_node = grid_nodes.get("reasoning")
    if r_node and r_node.get("host"):
        nodes.append({
            "role": "reasoning", "host": r_node["host"],
            "port": int(r_node.get("port", DEFAULT_GRID_PORT)), "model": r_node.get("model", ""),
        })
    for node in grid_nodes.get("cluster", []) or []:
        if node.get("host"):
            nodes.append({
                "role": "cluster", "host": node["host"],
                "port": int(node.get("port", DEFAULT_GRID_PORT)), "model": node.get("model", ""),
            })
    if nodes:
        return nodes
    for role, key in (("reasoning", "reasoning_url"), ("cluster", "cluster_url")):
        parsed = _url_node(saved.get(key, "") or "")
        if parsed:
            nodes.append({"role": role, "model": "", **parsed})
    return nodes


def collect_grid_status(
    saved: Dict[str, Any],
    grid_nodes: Dict[str, Any],
    target_host: Optional[str] = None,
    probe: Optional[Probe] = None,
) -> Dict[str, Any]:
    """Probe every configured node (or only ``target_host``) and summarise."""
    probe = probe or probe_node
    results: List[Dict[str, Any]] = []
    for node in configured_nodes(saved, grid_nodes):
        if target_host is not None and node["host"] != target_host:
            continue
        results.append({**node, **probe(node["host"], node["port"])})
    healthy = sum(1 for n in results if n.get("state") == "healthy")
    return {
        "configured": bool(results),
        "total": len(results),
        "healthy": healthy,
        "nodes": results,
    }
