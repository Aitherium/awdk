"""
LyraWiki Plugin for AitherShell
================================

Front door to a tenant-isolated LyraWiki knowledge base (the "Karpathy LLM
Wiki": immutable raw sources -> LLM-curated pages -> schema of conventions).
Autonomously curated by Nemotron-Orchestrator-8B.

Usage:
    /wiki ask "how does auth work?"            RAG answer with citations
    /wiki ingest ./notes.md                    Ingest a file/dir into the wiki
    /wiki ingest --url https://example.com/x   Ingest a URL
    /wiki search "oauth"                        Hybrid search (no generation)
    /wiki curate                               Lint + consolidate (health pass)
    /wiki status                               Wiki stats / health
    /wiki pages                                List wiki pages
    /wiki projects                             List this tenant's wikis
    /wiki use --tenant acme --wiki handbook    Set active tenant/wiki for session

Scope: pass --tenant <id> and --wiki <name> on any command, or set defaults
with `/wiki use`, or via env LYRA_TENANT_ID / LYRA_WIKI_PROJECT.

Aliases: /lyra, /kb-wiki
"""

import os
from adk._tls import tls_verify
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

try:
    from adk.shell.auth import AuthStore
except ImportError:
    AuthStore = None  # type: ignore


def _wiki_url() -> str:
    return os.environ.get("LYRA_WIKI_URL", "http://localhost:8270").rstrip("/")


def _api_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if AuthStore:
        token = AuthStore.get_active_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _post(path: str, body: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=120, verify=tls_verify()) as c:
        resp = await c.post(f"{_wiki_url()}{path}", json=body or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, params: dict = None) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=30, verify=tls_verify()) as c:
        resp = await c.get(f"{_wiki_url()}{path}", params=params or {}, headers=_api_headers())
        resp.raise_for_status()
        return resp.json()


def _flag(args: List[str], name: str) -> Optional[str]:
    """Pull `--name value` out of args (non-destructive read)."""
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _strip_flags(args: List[str], flags_with_values: List[str]) -> List[str]:
    """Return positional args with the named value-flags removed."""
    out: List[str] = []
    i = 0
    while i < len(args):
        if args[i] in flags_with_values:
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


class WikiPlugin(SlashCommand):
    name: str = "wiki"
    aliases: List[str] = ["lyra", "kb-wiki"]
    description: str = "LyraWiki — ingest, ask, search, and curate a tenant-isolated LLM wiki"
    category: str = "data"

    # Session defaults (overridden by flags / env)
    _tenant: str = os.environ.get("LYRA_TENANT_ID", "default")
    _wiki: str = os.environ.get("LYRA_WIKI_PROJECT", "default")

    def __init__(self) -> None:
        # Explicit, because the dataclass base assigns
        # `self.name = ""` and shadows the class attribute above —
        # the instance then registers under the empty string and is
        # overwritten by the next plugin to do the same.
        super().__init__(
            name='wiki',
            description='LyraWiki — ingest, ask, search, and curate a tenant-isolated LLM wiki',
            aliases=['lyra', 'kb-wiki'],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args:
            return self.get_help()

        # Resolve scope from flags, fall back to session defaults
        tenant = _flag(args, "--tenant") or self._tenant
        wiki = _flag(args, "--wiki") or self._wiki
        rest = _strip_flags(args[1:], ["--tenant", "--wiki", "--url"])

        sub = args[0].lower()
        dispatch = {
            "ask": self._ask,
            "query": self._ask,
            "ingest": self._ingest,
            "search": self._search,
            "curate": self._curate,
            "status": self._status,
            "stats": self._status,
            "pages": self._pages,
            "projects": self._projects,
            "use": self._use,
            "help": self._help_cmd,
        }
        handler = dispatch.get(sub)
        if not handler:
            return f"Unknown subcommand: {sub}\n\n{self.get_help()}"
        try:
            return await handler(rest, args, tenant, wiki)
        except Exception as e:  # surface backend/transport errors cleanly
            return f"ERROR: {type(e).__name__}: {e}"

    def get_help(self) -> str:
        return """**LyraWiki** — tenant-isolated LLM knowledge base

| Command | Description |
|---------|-------------|
| `/wiki ask "question"` | Answer from the wiki, with citations |
| `/wiki ingest <file\\|dir>` / `--url <url>` | Add a source into the wiki |
| `/wiki search "query"` | Hybrid search (BM25 + vector + graph) |
| `/wiki curate` | Health pass: lint + consolidate |
| `/wiki status` | Wiki stats / health score |
| `/wiki pages` | List wiki pages |
| `/wiki projects` | List this tenant's wikis |
| `/wiki use --tenant <id> --wiki <name>` | Set active scope for the session |

Scope any command with `--tenant <id> --wiki <name>` (defaults from
`/wiki use` or env `LYRA_TENANT_ID` / `LYRA_WIKI_PROJECT`)."""

    async def _help_cmd(self, rest, raw, tenant, wiki) -> str:
        return self.get_help()

    async def _use(self, rest, raw, tenant, wiki) -> str:
        type(self)._tenant = tenant
        type(self)._wiki = wiki
        return f"Active wiki scope: tenant=`{tenant}` wiki=`{wiki}`"

    async def _ask(self, rest, raw, tenant, wiki) -> str:
        if not rest:
            return 'Usage: /wiki ask "your question"'
        question = " ".join(rest).strip('"')
        result = await _post("/query", {
            "question": question, "tenant_id": tenant, "project": wiki,
        })
        answer = result.get("answer", "No answer found.")
        citations = result.get("citations", [])
        confidence = result.get("confidence", 0.0)
        out = f"**Answer** (confidence {confidence:.2f}):\n{answer}"
        if citations:
            cites = ", ".join(
                c.get("page") or c.get("source") or str(c) for c in citations[:6]
            )
            out += f"\n\n**Citations:** {cites}"
        else:
            out += "\n\n_No citations — the wiki may not cover this yet. Try `/wiki ingest`._"
        return out

    async def _ingest(self, rest, raw, tenant, wiki) -> str:
        url = _flag(raw, "--url")
        if url:
            content, title = f"Source URL: {url}", url
            # LyraWiki enriches URLs server-side during ingest.
            result = await _post("/ingest", {
                "content": content, "title": title, "source_type": "article",
                "tenant_id": tenant, "project": wiki, "metadata": {"url": url},
            })
        else:
            if not rest:
                return "Usage: /wiki ingest <file|dir>  |  /wiki ingest --url <url>"
            path = rest[0]
            if not os.path.exists(path):
                return f"ERROR: path not found: {path}"
            if os.path.isdir(path):
                ingested, created = 0, 0
                for root, _dirs, files in os.walk(path):
                    for fn in files:
                        if not fn.lower().endswith((".md", ".txt", ".rst")):
                            continue
                        fp = os.path.join(root, fn)
                        try:
                            with open(fp, encoding="utf-8", errors="ignore") as fh:
                                body = fh.read()
                        except OSError:
                            continue
                        if not body.strip():
                            continue
                        r = await _post("/ingest", {
                            "content": body, "title": fn, "source_type": "note",
                            "tenant_id": tenant, "project": wiki,
                        })
                        ingested += 1
                        created += len(r.get("pages_created", []))
                return f"Ingested {ingested} file(s) into `{tenant}/{wiki}` — {created} page(s) created."
            with open(path, encoding="utf-8", errors="ignore") as fh:
                body = fh.read()
            result = await _post("/ingest", {
                "content": body, "title": os.path.basename(path), "source_type": "note",
                "tenant_id": tenant, "project": wiki,
            })
        created = result.get("pages_created", [])
        updated = result.get("pages_updated", [])
        return (
            f"Ingested into `{tenant}/{wiki}`.\n"
            f"  Created: {len(created)} page(s){' — ' + ', '.join(created[:5]) if created else ''}\n"
            f"  Updated: {len(updated)} page(s)"
        )

    async def _search(self, rest, raw, tenant, wiki) -> str:
        if not rest:
            return 'Usage: /wiki search "query"'
        query = " ".join(rest).strip('"')
        result = await _post("/query", {
            "question": query, "tenant_id": tenant, "project": wiki, "depth": "quick",
        })
        pages = result.get("pages_consulted", [])
        if not pages:
            return "No matching pages found."
        lines = [f"**Top pages** ({len(pages)}):"]
        lines += [f"  - {p}" for p in pages[:10]]
        return "\n".join(lines)

    async def _curate(self, rest, raw, tenant, wiki) -> str:
        lint = await _post("/lint", {"tenant_id": tenant, "project": wiki, "scope": "all"})
        cons = await _post("/consolidate", {"tenant_id": tenant, "project": wiki})
        issues = lint.get("issues", [])
        promoted = (
            len(cons.get("promoted_to_episodic", []))
            + len(cons.get("promoted_to_semantic", []))
            + len(cons.get("promoted_to_procedural", []))
        )
        return (
            f"**Curation pass — `{tenant}/{wiki}`**\n"
            f"  Health score: {lint.get('health_score', 0):.2f}\n"
            f"  Issues: {len(issues)} (fixed: {lint.get('fixed', 0)})\n"
            f"  Pages: {lint.get('total_pages', 0)} | Sources: {lint.get('total_sources', 0)}\n"
            f"  Promoted to durable knowledge: {promoted} | "
            f"Decayed: {cons.get('pages_decayed', 0)} | Archived: {len(cons.get('pages_archived', []))}"
        )

    async def _status(self, rest, raw, tenant, wiki) -> str:
        stats = await _get("/stats", {"project": f"{tenant}/{wiki}" if tenant != "default" else wiki})
        lines = [f"**Wiki status — `{tenant}/{wiki}`**"]
        for k, v in stats.items():
            if isinstance(v, (int, float, str)):
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    async def _pages(self, rest, raw, tenant, wiki) -> str:
        proj = f"{tenant}/{wiki}" if tenant != "default" else wiki
        result = await _get("/wiki/pages", {"project": proj})
        pages = result.get("pages", result if isinstance(result, list) else [])
        if not pages:
            return "No pages yet. Ingest a source with `/wiki ingest`."
        lines = [f"**Wiki pages** ({len(pages)}):"]
        for p in pages[:30]:
            slug = p.get("slug", p) if isinstance(p, dict) else p
            lines.append(f"  - {slug}")
        return "\n".join(lines)

    async def _projects(self, rest, raw, tenant, wiki) -> str:
        result = await _get("/projects", {"tenant_id": tenant})
        projects = result.get("projects", [])
        if not projects:
            return f"No wikis for tenant `{tenant}` yet."
        lines = [f"**Wikis for `{tenant}`** ({len(projects)}):"]
        lines += [f"  - {p}" for p in projects]
        return "\n".join(lines)
