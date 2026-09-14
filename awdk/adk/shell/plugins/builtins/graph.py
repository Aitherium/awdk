"""
AitherGraph Plugin for AitherShell
=====================================

Unified graph intelligence — code, knowledge, events, memory, freshness, research.

Usage:
    /graph search <query> [--domain code|knowledge|events|memory|all]
    /graph code search <query>
    /graph code trace <symbol_a> <symbol_b>
    /graph code roots [list|add|remove]
    /graph kb create <name> [--source <path>]
    /graph kb list
    /graph kb ingest <base_id> <file>
    /graph kb query <base_id> <query>
    /graph kb audit <base_id> [--dead-docs]
    /graph events trace <event_id>
    /graph events bottlenecks
    /graph memory query <query>
    /graph freshness [--domain ...] [--older-than 30]
    /graph watcher start <path>
    /graph watcher stop <id>
    /graph research "topic" [--effort deep_dive]
    /graph improve [--domain code|knowledge|all]
    /graph curate [--crystallize] [--decay] [--dedup]
    /graph discoveries [--limit 10]
    /graph stats

Aliases: /g
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
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")


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


async def _api_get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
        resp = await c.get(f"{_genesis_url()}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=60, verify=tls_verify()) as c:
        resp = await c.post(f"{_genesis_url()}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _api_delete(path: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=15, verify=tls_verify()) as c:
        resp = await c.delete(f"{_genesis_url()}{path}", headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


def _format_table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(f"  {line}" for line in lines)


BASE = "/api/graph"


class GraphPlugin(SlashCommand):
    name: str = "graph"
    aliases: List[str] = ["g"]
    description: str = "AitherGraph — unified intelligence graph (code, knowledge, events, memory, research)"
    category: str = "intelligence"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='graph',
            description='AitherGraph — unified intelligence graph (code, knowledge, events, memory, research)',
            aliases=['g'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        dispatch = {
            "search": self._search,
            "code": self._code,
            "kb": self._kb,
            "knowledge": self._kb,
            "events": self._events,
            "memory": self._memory,
            "freshness": self._freshness,
            "watcher": self._watcher,
            "watchers": self._watcher,
            "research": self._research,
            "improve": self._improve,
            "curate": self._curate,
            "discoveries": self._discoveries,
            "stats": self._stats,
            "domains": self._domains,
        }

        handler = dispatch.get(sub)
        if not handler:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        return await handler(args[1:], ctx)

    # ── Cross-domain ───────────────────────────────────────────────

    async def _search(self, args: List[str], ctx: dict) -> str:
        if not args:
            return "Usage: /graph search <query> [--domain <domain>]"
        domain = ""
        query_parts = []
        i = 0
        while i < len(args):
            if args[i] == "--domain" and i + 1 < len(args):
                domain = args[i + 1]
                i += 2
            else:
                query_parts.append(args[i])
                i += 1
        query = " ".join(query_parts)
        params = {"q": query, "limit": 10}
        if domain:
            params["domain"] = domain
        data = await _api_get(f"{BASE}/search", params)
        results = data.get("results", [])
        if not results:
            return f"No results for: {query}"
        rows = [{"domain": r.get("domain", ""), "relevance": f"{r.get('relevance', 0):.2f}", "content": r.get("content", "")[:80]} for r in results]
        return f"Search: {query} ({len(results)} results)\n\n{_format_table(rows, ['domain', 'relevance', 'content'])}"

    async def _stats(self, args: list, ctx: dict) -> str:
        data = await _api_get(f"{BASE}/stats")
        return json.dumps(data, indent=2)

    async def _domains(self, args: list, ctx: dict) -> str:
        data = await _api_get(f"{BASE}/domains")
        domains = data.get("domains", [])
        rows = [{"domain": d.get("domain", ""), "status": d.get("status", "")} for d in domains]
        return _format_table(rows, ["domain", "status"])

    # ── Code ───────────────────────────────────────────────────────

    async def _code(self, args: List[str], ctx: dict) -> str:
        if not args:
            return "Usage: /graph code <search|trace|roots|routes|affected-tests> ..."
        sub = args[0].lower()
        if sub == "search":
            q = " ".join(args[1:])
            if not q:
                return "Usage: /graph code search <query>"
            data = await _api_get(f"{BASE}/code/search", {"q": q, "limit": 10})
            results = data.get("results", [])
            if not results:
                return f"No code results for: {q}"
            rows = [{"name": r.get("name", ""), "type": r.get("type", ""), "file": r.get("file", "")} for r in results]
            return _format_table(rows, ["name", "type", "file"])
        elif sub == "trace":
            if len(args) < 3:
                return "Usage: /graph code trace <source> <target>"
            data = await _api_get(f"{BASE}/code/trace", {"source": args[1], "target": args[2]})
            return json.dumps(data, indent=2)
        elif sub == "roots":
            if len(args) > 1 and args[1].lower() == "add":
                if len(args) < 4:
                    return "Usage: /graph code roots add <label> <path>"
                data = await _api_post(f"{BASE}/code/roots", {"label": args[2], "path": args[3]})
                return f"Root added: {json.dumps(data)}"
            elif len(args) > 1 and args[1].lower() == "remove":
                if len(args) < 3:
                    return "Usage: /graph code roots remove <label>"
                data = await _api_delete(f"{BASE}/code/roots/{args[2]}")
                return f"Root removed: {json.dumps(data)}"
            else:
                data = await _api_get(f"{BASE}/code/roots")
                return json.dumps(data.get("roots", []), indent=2)
        elif sub == "routes":
            pattern = " ".join(args[1:])
            data = await _api_get(f"{BASE}/code/routes", {"pattern": pattern})
            return json.dumps(data, indent=2)
        elif sub in ("affected-tests", "tests"):
            if len(args) < 2:
                return "Usage: /graph code affected-tests <symbol>"
            data = await _api_get(f"{BASE}/code/affected-tests", {"symbol": args[1]})
            return json.dumps(data, indent=2)
        return f"Unknown code subcommand: {sub}"

    # ── Knowledge ──────────────────────────────────────────────────

    async def _kb(self, args: List[str], ctx: dict) -> str:
        if not args:
            return "Usage: /graph kb <create|list|ingest|query|audit> ..."
        sub = args[0].lower()
        if sub == "create":
            name = args[1] if len(args) > 1 else ""
            if not name:
                return "Usage: /graph kb create <name> [--source <path>]"
            source = ""
            if "--source" in args:
                idx = args.index("--source")
                source = args[idx + 1] if idx + 1 < len(args) else ""
            data = await _api_post(f"{BASE}/knowledge/bases", {"name": name, "source_path": source})
            return f"Created: {json.dumps(data)}"
        elif sub in ("list", "ls"):
            data = await _api_get(f"{BASE}/knowledge/bases")
            bases = data.get("bases", [])
            if not bases:
                return "No knowledge bases found."
            return json.dumps(bases, indent=2)
        elif sub == "ingest":
            if len(args) < 3:
                return "Usage: /graph kb ingest <base_id> <file_path>"
            data = await _api_post(f"{BASE}/knowledge/bases/{args[1]}/ingest", {"file_path": args[2]})
            return f"Ingested: {json.dumps(data)}"
        elif sub == "query":
            if len(args) < 3:
                return "Usage: /graph kb query <base_id> <query>"
            q = " ".join(args[2:])
            data = await _api_post(f"{BASE}/knowledge/bases/{args[1]}/query", params={"q": q})
            return json.dumps(data, indent=2)
        elif sub == "audit":
            if len(args) < 2:
                return "Usage: /graph kb audit <base_id> [--dead-docs]"
            if "--dead-docs" in args:
                data = await _api_get(f"{BASE}/knowledge/bases/{args[1]}/dead-docs")
            else:
                data = await _api_get(f"{BASE}/knowledge/bases/{args[1]}/audit")
            return json.dumps(data, indent=2)
        return f"Unknown kb subcommand: {sub}"

    # ── Events ─────────────────────────────────────────────────────

    async def _events(self, args: List[str], ctx: dict) -> str:
        if not args:
            return "Usage: /graph events <trace|bottlenecks|critical-path> ..."
        sub = args[0].lower()
        if sub == "trace":
            if len(args) < 2:
                return "Usage: /graph events trace <event_id>"
            data = await _api_get(f"{BASE}/events/root-causes/{args[1]}")
            return json.dumps(data, indent=2)
        elif sub == "bottlenecks":
            data = await _api_get(f"{BASE}/events/bottlenecks")
            return json.dumps(data, indent=2)
        elif sub in ("critical-path", "critical"):
            data = await _api_get(f"{BASE}/events/critical-path")
            return json.dumps(data, indent=2)
        elif sub in ("causal-chain", "chain"):
            if len(args) < 2:
                return "Usage: /graph events causal-chain <event_id>"
            data = await _api_get(f"{BASE}/events/causal-chain", {"event_id": args[1]})
            return json.dumps(data, indent=2)
        return f"Unknown events subcommand: {sub}"

    # ── Memory ─────────────────────────────────────────────────────

    async def _memory(self, args: List[str], ctx: dict) -> str:
        if not args:
            return "Usage: /graph memory <query|store> ..."
        sub = args[0].lower()
        if sub == "query":
            q = " ".join(args[1:])
            if not q:
                return "Usage: /graph memory query <query>"
            data = await _api_post(f"{BASE}/memory/query", {"query": q})
            return json.dumps(data, indent=2)
        elif sub == "store":
            content = " ".join(args[1:])
            if not content:
                return "Usage: /graph memory store <content>"
            data = await _api_post(f"{BASE}/memory/store", {"content": content})
            return f"Stored: {json.dumps(data)}"
        return f"Unknown memory subcommand: {sub}"

    # ── Freshness ──────────────────────────────────────────────────

    async def _freshness(self, args: List[str], ctx: dict) -> str:
        domain = "document"
        max_age = 30
        i = 0
        while i < len(args):
            if args[i] == "--domain" and i + 1 < len(args):
                domain = args[i + 1]
                i += 2
            elif args[i] == "--older-than" and i + 1 < len(args):
                max_age = int(args[i + 1])
                i += 2
            else:
                i += 1
        data = await _api_post(f"{BASE}/freshness/check", {"domain": domain, "max_age_days": max_age})
        return json.dumps(data, indent=2)

    # ── Watchers ───────────────────────────────────────────────────

    async def _watcher(self, args: List[str], ctx: dict) -> str:
        if not args:
            data = await _api_get(f"{BASE}/watchers/status")
            return json.dumps(data, indent=2)
        sub = args[0].lower()
        if sub == "start":
            if len(args) < 2:
                return "Usage: /graph watcher start <path>"
            data = await _api_post(f"{BASE}/watchers/start", {"path": args[1]})
            return f"Watcher started: {json.dumps(data)}"
        elif sub == "stop":
            if len(args) < 2:
                return "Usage: /graph watcher stop <watcher_id>"
            data = await _api_post(f"{BASE}/watchers/stop?watcher_id={args[1]}")
            return f"Watcher stopped: {json.dumps(data)}"
        elif sub == "status":
            data = await _api_get(f"{BASE}/watchers/status")
            return json.dumps(data, indent=2)
        return f"Unknown watcher subcommand: {sub}"

    # ── Research ───────────────────────────────────────────────────

    async def _research(self, args: List[str], ctx: dict) -> str:
        effort = "library_session"
        query_parts = []
        i = 0
        while i < len(args):
            if args[i] == "--effort" and i + 1 < len(args):
                effort = args[i + 1]
                i += 2
            elif args[i] == "--no-ingest":
                i += 1
            else:
                query_parts.append(args[i])
                i += 1
        query = " ".join(query_parts)
        if not query:
            return "Usage: /graph research <query> [--effort deep_dive]"
        data = await _api_post(f"{BASE}/research", {"query": query, "effort": effort})
        return json.dumps(data, indent=2)

    async def _improve(self, args: List[str], ctx: dict) -> str:
        domain = "all"
        if "--domain" in args:
            idx = args.index("--domain")
            domain = args[idx + 1] if idx + 1 < len(args) else "all"
        data = await _api_post(f"{BASE}/research/improve", {"domain": domain})
        return json.dumps(data, indent=2)

    async def _curate(self, args: List[str], ctx: dict) -> str:
        payload = {"crystallize": True, "decay": True, "dedup": True}
        if "--no-crystallize" in args:
            payload["crystallize"] = False
        if "--no-decay" in args:
            payload["decay"] = False
        if "--no-dedup" in args:
            payload["dedup"] = False
        data = await _api_post(f"{BASE}/research/curate", payload)
        return json.dumps(data, indent=2)

    async def _discoveries(self, args: List[str], ctx: dict) -> str:
        limit = 10
        if "--limit" in args:
            idx = args.index("--limit")
            limit = int(args[idx + 1]) if idx + 1 < len(args) else 10
        data = await _api_get(f"{BASE}/research/discoveries", {"limit": limit})
        return json.dumps(data, indent=2)

    def get_help(self) -> str:
        return """AitherGraph — Unified Intelligence Graph

  /graph search <query> [--domain ...]   Cross-domain search
  /graph code search <query>             Search code
  /graph code trace <src> <tgt>          Call path tracing
  /graph code roots [add|remove]         Manage indexed repos
  /graph code routes [pattern]           Search HTTP routes
  /graph code affected-tests <symbol>    Test impact analysis
  /graph kb create|list|ingest|query|audit  Knowledge bases
  /graph events trace|bottlenecks|critical-path  Event graph
  /graph memory query|store              Memory graph
  /graph freshness [--domain ...] [--older-than 30]
  /graph watcher start|stop|status       File watchers
  /graph research <query> [--effort ...]  Autonomous research
  /graph improve [--domain ...]          Self-improvement cycle
  /graph curate                          Knowledge curation
  /graph discoveries [--limit 10]        Recent findings
  /graph stats                           Backend statistics
  /graph domains                         Available domains"""
