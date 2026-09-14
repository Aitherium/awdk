"""
Knowledge RAG Plugin for AitherShell
======================================

Manage knowledge bases, ingest documents, query, and audit document health.

Usage:
    /knowledge create "My Docs" --source ./docs/
    /knowledge list
    /knowledge ingest my-docs ./new-file.pdf
    /knowledge query my-docs "how does auth work?"
    /knowledge audit my-docs
    /knowledge audit my-docs --dead-docs
    /knowledge watch my-docs --start
    /knowledge watch my-docs --stop
    /knowledge graph my-docs --format json

Aliases: /kb, /rag
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
    return os.environ.get("AITHER_GENESIS_URL", "http://localhost:8100")


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
    return "\n".join(f"  {l}" for l in lines)


class KnowledgePlugin(SlashCommand):
    name: str = "knowledge"
    aliases: List[str] = ["kb", "rag"]
    description: str = "Knowledge RAG — document ingestion, querying, auditing, and auto-update"
    category: str = "data"

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='knowledge',
            description='Knowledge RAG — document ingestion, querying, auditing, and auto-update',
            aliases=['kb', 'rag'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        sub = args[0].lower()
        dispatch = {
            "create": self._create,
            "list": self._list,
            "ls": self._list,
            "ingest": self._ingest,
            "query": self._query,
            "search": self._search,
            "audit": self._audit,
            "watch": self._watch,
            "graph": self._graph,
            "delete": self._delete,
            "help": self._help_cmd,
        }

        handler = dispatch.get(sub)
        if handler:
            return await handler(args[1:], ctx)
        return f"Unknown subcommand: {sub}\n\n{self.get_help()}"

    def get_help(self) -> str:
        return """**Knowledge RAG** — Document Knowledge Base Management

| Command | Description |
|---------|-------------|
| `/knowledge create "name" --source ./path` | Create a knowledge base |
| `/knowledge list` | List all knowledge bases |
| `/knowledge ingest <base> <file>` | Ingest a document |
| `/knowledge query <base> "question"` | RAG query (retrieval + generation) |
| `/knowledge search <base> "query"` | Semantic search (no generation) |
| `/knowledge audit <base>` | Run freshness audit |
| `/knowledge audit <base> --dead-docs` | Show stale documents |
| `/knowledge watch <base> --start` | Enable auto-update |
| `/knowledge watch <base> --stop` | Disable auto-update |
| `/knowledge graph <base>` | Show document relationship graph |
| `/knowledge delete <base>` | Delete a knowledge base |
"""

    async def _help_cmd(self, args: List[str], ctx: Dict[str, Any]) -> str:
        return self.get_help()

    async def _create(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /knowledge create \"name\" [--source path] [--type local|git|s3]"
        name = args[0]
        source = ""
        source_type = "local"
        i = 1
        while i < len(args):
            if args[i] == "--source" and i + 1 < len(args):
                source = args[i + 1]
                i += 2
            elif args[i] == "--type" and i + 1 < len(args):
                source_type = args[i + 1]
                i += 2
            else:
                i += 1

        result = await _api_post("/api/knowledge-rag/bases", {
            "name": name, "source_path": source, "source_type": source_type,
        })
        base = result.get("base", {})
        return (
            f"Created knowledge base: **{base.get('name', name)}**\n"
            f"  ID: `{base.get('base_id', '?')}`\n"
            f"  Source: {base.get('source_path', '(none)')}\n"
            f"  Docs: {base.get('doc_count', 0)}"
        )

    async def _list(self, args: List[str], ctx: Dict[str, Any]) -> str:
        result = await _api_get("/api/knowledge-rag/bases")
        bases = result.get("bases", [])
        if not bases:
            return "No knowledge bases found. Create one with `/knowledge create`."
        rows = [
            {"id": b.get("base_id", "?")[:8], "name": b.get("name", "?"),
             "docs": b.get("doc_count", 0), "watcher": "on" if b.get("watcher_active") else "off"}
            for b in bases
        ]
        return f"**Knowledge Bases** ({len(bases)})\n{_format_table(rows, ['id', 'name', 'docs', 'watcher'])}"

    async def _ingest(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 2:
            return "Usage: /knowledge ingest <base_id> <file_path_or_dir>"
        base_id, path = args[0], args[1]
        if os.path.isdir(path):
            result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/sync")
            return f"Synced directory: {result.get('docs_processed', 0)} documents processed"
        result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/ingest/url", {"url": path})
        return f"Ingested: {result.get('status', 'unknown')} (doc_id: {result.get('doc_id', '?')})"

    async def _query(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 2:
            return "Usage: /knowledge query <base_id> \"question\""
        base_id = args[0]
        question = " ".join(args[1:])
        result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/query", {
            "query": question, "mode": "mix",
        })
        answer = result.get("answer", "No answer found.")
        sources = result.get("source_docs", [])
        out = f"**Answer:**\n{answer}"
        if sources:
            out += f"\n\n**Sources:** {', '.join(str(s)[:40] for s in sources[:5])}"
        return out

    async def _search(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if len(args) < 2:
            return "Usage: /knowledge search <base_id> \"query\""
        base_id = args[0]
        query = " ".join(args[1:])
        result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/search", {
            "query": query, "limit": 10,
        })
        results = result.get("results", [])
        if not results:
            return "No results found."
        lines = [f"**Search Results** ({len(results)})"]
        for i, r in enumerate(results[:10], 1):
            lines.append(f"  {i}. {str(r)[:80]}")
        return "\n".join(lines)

    async def _audit(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /knowledge audit <base_id> [--dead-docs]"
        base_id = args[0]

        if "--dead-docs" in args:
            result = await _api_get(f"/api/knowledge-rag/bases/{base_id}/dead-docs")
            docs = result.get("dead_docs", [])
            if not docs:
                return "No stale documents found."
            rows = [{"name": d.get("file_name", "?"), "freshness": d.get("freshness", 0)} for d in docs]
            return f"**Stale Documents** ({len(docs)})\n{_format_table(rows, ['name', 'freshness'])}"

        result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/audit/run")
        dist = result.get("grade_distribution", {})
        return (
            f"**Audit Report** — {result.get('total_documents', 0)} documents\n"
            f"  Average Freshness: {result.get('avg_freshness', 0):.2f}\n"
            f"  Grades: A={dist.get('A', 0)} B={dist.get('B', 0)} C={dist.get('C', 0)} "
            f"D={dist.get('D', 0)} F={dist.get('F', 0)}\n"
            f"  Dead: {result.get('dead_doc_count', 0)} | Orphans: {result.get('orphan_count', 0)} | "
            f"Duplicates: {result.get('duplicate_count', 0)} | Broken Links: {result.get('broken_link_count', 0)}"
        )

    async def _watch(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /knowledge watch <base_id> --start|--stop"
        base_id = args[0]
        if "--stop" in args:
            result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/watcher/stop")
            return f"Watcher stopped for `{base_id}`"
        result = await _api_post(f"/api/knowledge-rag/bases/{base_id}/watcher/start")
        watcher = result.get("watcher", {})
        return (
            f"Watcher started for `{base_id}`\n"
            f"  State: {watcher.get('state', '?')}\n"
            f"  Type: {watcher.get('source_type', '?')}\n"
            f"  Interval: {watcher.get('poll_interval', 0)}s"
        )

    async def _graph(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /knowledge graph <base_id> [--format json]"
        base_id = args[0]
        result = await _api_get(f"/api/knowledge-rag/bases/{base_id}/graph")
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])

        if "--format" in args and "json" in args:
            return f"```json\n{json.dumps(result, indent=2)}\n```"

        orphans = [n for n in nodes if n.get("is_orphan")]
        grades = {}
        for n in nodes:
            g = n.get("grade", "?")
            grades[g] = grades.get(g, 0) + 1
        return (
            f"**Knowledge Graph** — {len(nodes)} nodes, {len(edges)} edges\n"
            f"  Grades: {', '.join(f'{k}={v}' for k, v in sorted(grades.items()))}\n"
            f"  Orphans: {len(orphans)}"
        )

    async def _delete(self, args: List[str], ctx: Dict[str, Any]) -> str:
        if not args:
            return "Usage: /knowledge delete <base_id>"
        base_id = args[0]
        result = await _api_delete(f"/api/knowledge-rag/bases/{base_id}")
        return f"Deleted knowledge base `{base_id}`"
