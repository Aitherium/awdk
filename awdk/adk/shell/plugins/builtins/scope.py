"""
Scope Plugin for AitherShell
==============================

Launch AitherScope — interactive codebase and system visualization,
dead code detection, dependency analysis, and code reorganization.

Usage:
    /scope                    — Open AitherScope in browser (AitherVeil)
    /scope desktop            — Open AitherScope in AitherDesktop window
    /scope graph [path]       — Fetch full dependency graph (terminal)
    /scope dead [path]        — Find dead code (terminal)
    /scope metrics [path]     — Show codebase metrics (terminal)
    /scope system             — Full system graph (configs, Docker, identity, cognitive)
    /scope infra              — Infrastructure view (Docker, layers, services)
    /scope cognitive          — Cognitive architecture map
    /scope identity           — Agent identity graph
    /scope health             — Check CodeGraph + Scope service health
    /scope reindex [path]     — Force re-index a codebase path

Aliases: /aitherscope, /codeview
"""

import json
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify
import os
import subprocess
import sys
import webbrowser
from typing import Any, Dict, List

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _genesis_url() -> str:
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


def _veil_url() -> str:
    return os.environ.get("AITHER_VEIL_URL", "http://localhost:3000")


def _api_headers() -> Dict[str, str]:
    """Auth + project scope headers."""
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        profile = AuthStore.get_active_profile() if hasattr(AuthStore, "get_active_profile") else None
        if profile and profile.get("tenant_id"):
            headers["X-Tenant-ID"] = profile["tenant_id"]
    # Project context
    project = os.environ.get("AITHER_PROJECT", "")
    if project:
        headers["X-Project-Name"] = project
    return headers


class ScopePlugin(SlashCommand):
    name: str = "scope"
    aliases: List[str] = ["aitherscope", "codeview"]
    description: str = "Launch AitherScope — codebase visualization and analysis"
    category: str = "development"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='scope',
            description='Launch AitherScope — codebase visualization and analysis',
            aliases=['aitherscope', 'codeview'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return await self._open_browser(args, ctx)

        sub = args[0].lower()
        dispatch = {
            "desktop": self._open_desktop,
            "browser": self._open_browser,
            "graph": self._graph,
            "dead": self._dead_code,
            "metrics": self._metrics,
            "health": self._health,
            "reindex": self._reindex,
            "system": self._system,
            "infra": self._system_infra,
            "cognitive": self._system_cognitive,
            "identity": self._system_identity,
            "help": self._help,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)

        # Default: treat arg as a path for graph view
        return await self._open_browser(args, ctx)

    def get_help(self) -> str:
        return """🔬 **AitherScope** — Codebase & System Visualization

| Command | Description |
|---------|-------------|
| `/scope` | Open AitherScope in browser |
| `/scope desktop` | Open in AitherDesktop window |
| `/scope graph [path]` | Fetch dependency graph |
| `/scope dead [path]` | Find dead / unreachable code |
| `/scope metrics [path]` | Codebase metrics summary |
| `/scope system` | Full system graph (all subsystems) |
| `/scope infra` | Infrastructure view (Docker, layers) |
| `/scope cognitive` | Cognitive architecture map |
| `/scope identity` | Agent identity graph |
| `/scope health` | CodeGraph + Scope service health |
| `/scope reindex [path]` | Force re-index codebase |

**Tip:** System views show configs, identities, Docker, boot phases, and cognitive architecture.
Use `/scope system` for the full picture or a specific view like `/scope infra`.
"""

    # ─── Browser launch ───────────────────────────────────────────────

    async def _open_browser(self, args: List[str], ctx: Dict[str, Any]) -> str:
        path = args[0] if args else ""
        project = os.environ.get("AITHER_PROJECT", "")

        url = f"{_veil_url()}/console/scope"
        params = []
        if project:
            params.append(f"project={project}")
        if path:
            params.append(f"path={path}")
        if params:
            url += "?" + "&".join(params)

        try:
            webbrowser.open(url)
            return f"🔬 Opening AitherScope in browser...\n  URL: {url}"
        except Exception as e:
            return f"⚠️ Could not open browser: {e}\n  URL: {url}"

    # ─── Desktop launch ──────────────────────────────────────────────

    async def _open_desktop(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Open AitherScope as a window in AitherDesktop."""
        import httpx

        project = os.environ.get("AITHER_PROJECT", "")
        path = args[0] if args else ""

        # POST to Veil's desktop window API to open scope window
        try:
            async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as c:
                r = await c.post(
                    f"{_veil_url()}/api/desktop/open-window",
                    json={
                        "widgetId": "scope",
                        "title": f"AitherScope{' — ' + project if project else ''}",
                        "props": {"initialPath": path or "AitherOS"},
                    },
                    headers=_api_headers(),
                )
                if r.status_code == 200:
                    return f"🖥️ Opened AitherScope in AitherDesktop"
                else:
                    # Fallback to browser
                    return await self._open_browser(args, ctx)
        except Exception:
            # Fallback: open in browser
            return await self._open_browser(args, ctx)

    # ─── Graph (terminal) ────────────────────────────────────────────

    async def _graph(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx
        path = args[0] if args else "AitherOS"

        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/scope/graph/full",
                    params={"root": path},
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"⚠️ Scope API error: {r.status_code} {r.text[:200]}"

                data = r.json()
                nodes = data.get("nodes", [])
                edges = data.get("edges", [])

                lines = [f"📊 **Dependency Graph** — `{path}`\n"]
                lines.append(f"  Nodes: {len(nodes)}  |  Edges: {len(edges)}")

                # Top 15 most-connected nodes
                connections: Dict[str, int] = {}
                for e in edges:
                    connections[e.get("source", "")] = connections.get(e.get("source", ""), 0) + 1
                    connections[e.get("target", "")] = connections.get(e.get("target", ""), 0) + 1

                top = sorted(connections.items(), key=lambda x: -x[1])[:15]
                if top:
                    lines.append("\n  **Most connected:**")
                    for name, count in top:
                        short = name.split("/")[-1] if "/" in name else name
                        lines.append(f"    {short}: {count} connections")

                lines.append(f"\n  View interactive graph: `/scope` or `/scope desktop`")
                return "\n".join(lines)

        except Exception as e:
            return f"⚠️ Could not fetch graph: {e}"

    # ─── Dead code ───────────────────────────────────────────────────

    async def _dead_code(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx
        path = args[0] if args else ""

        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/scope/dead-code",
                    params={"path": path} if path else {},
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"⚠️ Dead code API error: {r.status_code}"

                data = r.json()
                items = data if isinstance(data, list) else data.get("dead_code", data.get("items", []))

                if not items:
                    return "✅ No dead code detected!"

                lines = [f"💀 **Dead Code Report** — {len(items)} items\n"]
                for item in items[:20]:
                    name = item.get("name", item.get("symbol", "?"))
                    file = item.get("file", item.get("path", ""))
                    conf = item.get("confidence", 0)
                    short_file = file.split("/")[-1] if "/" in file else file
                    lines.append(f"  ⚠️ `{name}` in {short_file} (confidence: {conf:.0%})")

                if len(items) > 20:
                    lines.append(f"\n  ... and {len(items) - 20} more. Use `/scope` for the full interactive view.")

                return "\n".join(lines)

        except Exception as e:
            return f"⚠️ Could not fetch dead code: {e}"

    # ─── Metrics ─────────────────────────────────────────────────────

    async def _metrics(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
                r = await c.get(
                    f"{_genesis_url()}/scope/codebase-metrics",
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"⚠️ Metrics API error: {r.status_code}"

                data = r.json()
                lines = ["📈 **Codebase Metrics**\n"]

                for key in ["total_files", "total_lines", "total_functions", "total_classes",
                            "languages", "avg_complexity", "test_coverage"]:
                    val = data.get(key)
                    if val is not None:
                        label = key.replace("_", " ").title()
                        if isinstance(val, dict):
                            lines.append(f"  **{label}:**")
                            for k, v in list(val.items())[:10]:
                                lines.append(f"    {k}: {v}")
                        elif isinstance(val, float):
                            lines.append(f"  **{label}:** {val:.1f}")
                        else:
                            lines.append(f"  **{label}:** {val}")

                return "\n".join(lines)

        except Exception as e:
            return f"⚠️ Could not fetch metrics: {e}"

    # ─── Health ──────────────────────────────────────────────────────

    async def _health(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx
        lines = ["🏥 **AitherScope Health**\n"]

        checks = [
            ("Genesis Scope", f"{_genesis_url()}/scope/graph/health"),
            ("CodeGraph Stats", f"{_genesis_url()}/scope/codegraph-stats"),
        ]

        async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as c:
            for name, url in checks:
                try:
                    r = await c.get(url, headers=_api_headers())
                    if r.status_code == 200:
                        lines.append(f"  ✅ {name}: OK")
                    else:
                        lines.append(f"  ⚠️ {name}: HTTP {r.status_code}")
                except Exception as e:
                    lines.append(f"  ❌ {name}: {e}")

        return "\n".join(lines)

    # ─── Reindex ─────────────────────────────────────────────────────

    async def _reindex(self, args: List[str], ctx: Dict[str, Any]) -> str:
        import httpx
        path = args[0] if args else os.environ.get("AITHER_PROJECT_PATH", "")

        if not path:
            return "Usage: `/scope reindex <path>`\n  Or set AITHER_PROJECT_PATH via `/project switch`."

        try:
            async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
                r = await c.post(
                    f"{_genesis_url()}/scope/reindex",
                    json={"path": path, "force": True},
                    headers=_api_headers(),
                )
                if r.status_code == 200:
                    data = r.json()
                    return f"✅ Reindex complete for `{path}`\n  {json.dumps(data, indent=2)[:500]}"
                else:
                    return f"⚠️ Reindex failed: {r.status_code} {r.text[:200]}"

        except Exception as e:
            return f"⚠️ Could not reindex: {e}"

    # ─── System graph ──────────────────────────────────────────────

    async def _fetch_system(self, flags: Dict[str, Any]) -> str:
        """Fetch the system graph from POST /scope/system."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
                r = await c.post(
                    f"{_genesis_url()}/scope/system",
                    json=flags,
                    headers=_api_headers(),
                )
                if r.status_code != 200:
                    return f"⚠️ System scope API error: {r.status_code} {r.text[:200]}"

                data = r.json()
                nodes = data.get("nodes", [])
                connections = data.get("connections", [])

                # Group nodes by type
                type_counts: Dict[str, int] = {}
                for n in nodes:
                    t = n.get("type", "unknown")
                    type_counts[t] = type_counts.get(t, 0) + 1

                lines = [f"🔬 **System Graph** — {len(nodes)} nodes, {len(connections)} connections\n"]

                if type_counts:
                    lines.append("  **Node types:**")
                    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                        lines.append(f"    {t}: {count}")

                # Top connected
                conn_count: Dict[str, int] = {}
                for c in connections:
                    src = c.get("source", c.get("from", ""))
                    tgt = c.get("target", c.get("to", ""))
                    conn_count[src] = conn_count.get(src, 0) + 1
                    conn_count[tgt] = conn_count.get(tgt, 0) + 1

                top = sorted(conn_count.items(), key=lambda x: -x[1])[:10]
                if top:
                    lines.append("\n  **Most connected:**")
                    for name, count in top:
                        short = name.split("/")[-1] if "/" in name else name
                        lines.append(f"    {short}: {count}")

                lines.append(f"\n  🌐 Interactive view: `/scope` then select Infrastructure/Cognitive/Identity")
                return "\n".join(lines)

        except Exception as e:
            return f"⚠️ Could not fetch system graph: {e}"

    async def _system(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Full system graph with all subsystems."""
        return await self._fetch_system({"max_nodes": 3000})

    async def _system_infra(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Infrastructure-focused system graph."""
        return await self._fetch_system({
            "include_docker": True, "include_layers": True, "include_services": True,
            "include_configs": False, "include_identities": False, "include_personas": False,
            "include_agent_cards": False, "include_cognitive": False, "max_nodes": 2000,
        })

    async def _system_cognitive(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Cognitive architecture graph."""
        return await self._fetch_system({
            "include_cognitive": True, "include_layers": True, "include_services": True,
            "include_configs": False, "include_identities": False, "include_personas": False,
            "include_agent_cards": False, "include_docker": False, "max_nodes": 1000,
        })

    async def _system_identity(self, args: List[str], ctx: Dict[str, Any]) -> str:
        """Agent identity graph."""
        return await self._fetch_system({
            "include_identities": True, "include_personas": True, "include_agent_cards": True,
            "include_services": True, "include_configs": False, "include_docker": False,
            "include_layers": False, "include_cognitive": False, "max_nodes": 1000,
        })

    # ─── Help ────────────────────────────────────────────────────────

    async def _help(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()
