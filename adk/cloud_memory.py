"""``adk embed`` / ``adk kb`` / ``adk memory`` — thin clients over the tenant platform.

``adk ingest`` writes a LOCAL knowledge graph; these verbs talk to the tenant's
Genesis instead (through ``genesis_api_base()`` — the portal's ``/api/genesis``
proxy unless ``AITHER_API_URL`` names Genesis directly) with
``Authorization: Bearer $AITHER_API_KEY``. The tenant is always derived by the
server from that key; these commands never send a ``tenant_id``.

    adk embed "some text" [--dim 768] [--modality text] [--json]
    adk kb ingest <file|-> [--doc-id ID] [--doc-type T]
    adk kb query "what do we know about X" [--json]
    adk memory remember "fact" [--category C]
    adk memory recall "query" [--category C] [--json]

Routes: ``POST /embeddings/embed``; ``kb ingest`` -> ``POST /external/ingest`` and
``kb query`` -> ``POST /external/graph/query`` (ONE store: ingest always appends the
tenant's durable ``ingested.jsonl`` and query reads the graph then that file);
``POST /external/memory/remember`` / ``POST /external/memory/recall`` (one store:
``memories.jsonl`` + the graph). The two pairs must never be crossed — a verb that
writes one store and reads another returns "(no matches)" forever.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

Poster = Callable[[str, Dict[str, Any]], Tuple[int, Any]]

MAX_INGEST_CHARS = 50_000  # one document per request; split larger inputs

KB_INGEST_ROUTE = "/external/ingest"
KB_QUERY_ROUTE = "/external/graph/query"


def _base() -> str:
    from adk.control_plane import genesis_api_base

    return genesis_api_base()


def _headers() -> Dict[str, str]:
    key = os.getenv("AITHER_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _http_post(path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
    import httpx

    r = httpx.post(f"{_base()}{path}", json=body, headers=_headers(), timeout=60.0)
    try:
        payload: Any = r.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is reported as text
        payload = r.text[:500]
    return r.status_code, payload


def _call(path: str, body: Dict[str, Any], poster: Optional[Poster]) -> Tuple[int, Any]:
    if not os.getenv("AITHER_API_KEY", "").strip():
        print("adk: AITHER_API_KEY is not set — run `adk enroll` or export the key.",
              file=sys.stderr)
        return 0, None
    try:
        return (poster or _http_post)(path, body)
    except Exception as exc:  # noqa: BLE001 — surface the transport error, exit non-zero
        print(f"adk: request to {path} failed: {exc}", file=sys.stderr)
        return 0, None


def _await(coro: Any) -> Any:
    """Await a coroutine from sync verb code, even when a loop is already running.

    `adk embed` is dispatched from the sync CLI, but adk is also imported by the MCP
    server and the harness daemon, which call verbs from inside a loop; asyncio.run
    refuses there, so the coroutine gets its own loop in a one-shot worker thread."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _local_embedding(text: str, modality: str = "text") -> Optional[List[float]]:
    """Embed ``text`` with the LOCAL provider, or None when only the tenant can answer.

    Asked FIRST, before ``_call``: ``_call`` refuses without ``AITHER_API_KEY``, and a
    sovereign box that never enrolled must still embed.

    ``embed_texts`` is called BEFORE ``describe()`` on purpose. ``describe()`` reports
    the URL a PREVIOUS resolution pinned; a fresh provider has ``_url=""``, so a
    describe-first check reads "no local endpoint" forever (measured 2026-10-27)."""
    if modality not in ("text", "code"):
        return None                    # graph_state / agent_state are tenant-only
    from adk.embeddings import get_provider

    try:
        prov = get_provider()
        vecs, _dim = await prov.embed_texts([text])
        info = prov.describe()
    except Exception:  # noqa: BLE001 - any local failure falls through to the tenant
        return None
    if info.get("backend") not in ("vllm", "microscheduler", "cpu"):
        return None        # gateway is metered, hash is not a model, none is no vector
    if (info.get("space") == "aither-code-embed") != (modality == "code"):
        return None        # each space serves exactly its own modality
    vec = (vecs or [[]])[0]
    if not vec or len(vec) != int(info.get("dim") or 0):
        return None        # never label a vector with a width it does not have
    return list(vec)


def _fail(status: int, payload: Any) -> int:
    if status:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        print(f"adk: server returned {status}: {detail}", file=sys.stderr)
    return 1


def add_cloud_memory_parsers(sub: Any) -> None:
    """Register ``embed``, ``kb`` and ``memory`` on an ``add_subparsers`` result."""
    emb = sub.add_parser("embed", help="Embed text with the tenant platform's embedding engine")
    emb.add_argument("text", nargs="+", help="Text to embed")
    emb.add_argument("--dim", type=int, default=768, help="MRL dimension (64-3072, default 768)")
    emb.add_argument("--modality", default="text",
                     choices=["text", "code", "graph_state", "agent_state"])
    emb.add_argument("--json", action="store_true", help="Print the full JSON response")

    kb = sub.add_parser("kb", help="Tenant knowledge base on the platform: ingest, query")
    kbs = kb.add_subparsers(dest="kb_command")
    ing = kbs.add_parser("ingest", help="Embed a document and store it in the tenant KB")
    ing.add_argument("source", help="File path, or - for stdin")
    ing.add_argument("--doc-id", default="", help="Document id (default: the file name)")
    ing.add_argument("--doc-type", default="document", help="Document type metadata")
    q = kbs.add_parser("query", help="Search the tenant knowledge base")
    q.add_argument("query", nargs="+")
    q.add_argument("--category", default="")
    q.add_argument("--json", action="store_true")

    mem = sub.add_parser("memory", help="Tenant memory on the platform: remember, recall")
    ms = mem.add_subparsers(dest="memory_command")
    rem = ms.add_parser("remember", help="Store a memory in the tenant graph")
    rem.add_argument("content", nargs="+")
    rem.add_argument("--category", default="general")
    rec = ms.add_parser("recall", help="Recall memories from the tenant graph")
    rec.add_argument("query", nargs="+")
    rec.add_argument("--category", default="")
    rec.add_argument("--json", action="store_true")


def cmd_embed(args: argparse.Namespace, poster: Optional[Poster] = None) -> int:
    text = " ".join(args.text)
    vec = _await(_local_embedding(text, getattr(args, "modality", "text")))
    if vec:
        if getattr(args, "json", False):
            print(json.dumps({"embedding": vec, "modality": args.modality,
                              "dim": len(vec), "backend": "local"}))
        else:
            head = ", ".join(f"{v:.4f}" for v in vec[:6])
            more = ", ..." if len(vec) > 6 else ""
            print(f"dim={len(vec)} modality={args.modality} [{head}{more}]")
        return 0
    body = {"text": text, "modality": args.modality, "dim": int(args.dim)}
    status, payload = _call("/embeddings/embed", body, poster)
    if status != 200 or not isinstance(payload, dict):
        return _fail(status, payload)
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        vec = payload.get("embedding") or []
        head = ", ".join(f"{v:.4f}" for v in vec[:6])
        print(f"dim={len(vec)} modality={payload.get('modality', args.modality)} [{head}{', ...' if len(vec) > 6 else ''}]")
    return 0


def _read_source(source: str) -> Tuple[str, str]:
    if source == "-":
        return sys.stdin.read(), "stdin"
    p = Path(source)
    return p.read_text(encoding="utf-8", errors="replace"), p.name


def _print_memories(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload))
        return
    mems = payload.get("memories") or []
    if not mems:
        print("(no matches)")
    for m in mems:
        text = str(m.get("content") or m.get("text") or m)[:200].replace("\n", " ")
        print(f"- {text}")


def _print_results(payload: Dict[str, Any], category: str, as_json: bool) -> None:
    results = payload.get("results") or []
    if category:
        results = [r for r in results if isinstance(r, dict)
                   and category in (r.get("type"), r.get("category"))]
    if as_json:
        print(json.dumps({**payload, "results": results}))
        return
    if not results:
        print("(no matches)")
    for r in results:
        if not isinstance(r, dict):
            print(f"- {str(r)[:200]}")
            continue
        text = str(r.get("snippet") or r.get("content") or "")[:200].replace("\n", " ")
        src = r.get("source") or ""
        print(f"- {text}" + (f"  [{src}]" if src else ""))


def cmd_kb(args: argparse.Namespace, poster: Optional[Poster] = None) -> int:
    verb = getattr(args, "kb_command", None)
    if verb == "ingest":
        try:
            text, name = _read_source(args.source)
        except OSError as exc:
            print(f"adk: cannot read {args.source}: {exc}", file=sys.stderr)
            return 1
        if not text.strip():
            print("adk: nothing to ingest (empty input)", file=sys.stderr)
            return 1
        if len(text) > MAX_INGEST_CHARS:
            print(f"adk: input is {len(text)} chars; the platform accepts at most "
                  f"{MAX_INGEST_CHARS} per document — split it first.", file=sys.stderr)
            return 1
        doc_id = args.doc_id or name
        body = {"content": text, "content_type": args.doc_type, "source_name": doc_id,
                "metadata": {"doc_id": doc_id, "source": "adk kb ingest"}}
        status, payload = _call(KB_INGEST_ROUTE, body, poster)
        if status != 200 or not isinstance(payload, dict):
            return _fail(status, payload)
        if payload.get("status") != "ingested":
            print(f"adk: ingest not stored: {payload}", file=sys.stderr)
            return 1
        print(f"ingested {payload.get('source') or doc_id} (node {payload.get('node_id', '')})")
        return 0
    if verb == "query":
        body = {"query": " ".join(args.query), "max_results": 10}
        status, payload = _call(KB_QUERY_ROUTE, body, poster)
        if status != 200 or not isinstance(payload, dict):
            return _fail(status, payload)
        _print_results(payload, args.category, args.json)
        return 0
    print("usage: adk kb {ingest,query} ...", file=sys.stderr)
    return 2


def cmd_memory(args: argparse.Namespace, poster: Optional[Poster] = None) -> int:
    verb = getattr(args, "memory_command", None)
    if verb == "remember":
        body = {"content": " ".join(args.content), "category": args.category}
        status, payload = _call("/external/memory/remember", body, poster)
        if status != 200 or not isinstance(payload, dict):
            return _fail(status, payload)
        print(f"remembered {payload.get('memory_id', '')}".strip())
        return 0
    if verb == "recall":
        body = {"query": " ".join(args.query), "category": args.category}
        status, payload = _call("/external/memory/recall", body, poster)
        if status != 200 or not isinstance(payload, dict):
            return _fail(status, payload)
        _print_memories(payload, args.json)
        return 0
    print("usage: adk memory {remember,recall} ...", file=sys.stderr)
    return 2


__all__ = ["add_cloud_memory_parsers", "cmd_embed", "cmd_kb", "cmd_memory"]
