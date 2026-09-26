"""``adk kb`` and ``adk embed`` — read back what ``adk ingest`` stored, and call
the canonical embeddings provider directly.

``adk ingest`` writes chunks into the agent's local GraphMemory; before these
verbs there was no CLI path to query or list them, nor to see which embeddings
rung the SDK resolved to.

    adk kb query "how do refunds work" [--agent NAME] [--limit N] [--json]
    adk kb list [--agent NAME] [--limit N] [--json]
    adk embed "some text" ["more text" ...] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Dict, List, Optional


def register(sub: Any) -> None:
    """Add the ``kb`` and ``embed`` sub-commands to an argparse subparsers object.

    ``adk/cloud_memory.py`` registers ``kb`` and ``embed`` on the same subparsers
    first, and ``main()`` dispatches both names to cloud_memory. Registering them
    a second time raised ``conflicting subparser: kb`` while the parser was being
    BUILT, so every ``adk`` command -- ``adk --version``, ``adk up`` -- died
    before parsing. A name already present is left to its first owner.
    """
    taken = set(getattr(sub, "choices", None) or ())
    if "kb" in taken and "embed" in taken:
        return
    if "kb" in taken or "embed" in taken:
        raise RuntimeError("adk kb/embed half-registered by another module; "
                           "one owner must register both")
    kb_p = sub.add_parser("kb", help="Query or list the agent's local knowledge graph")
    kb_sub = kb_p.add_subparsers(dest="kb_action")
    q = kb_sub.add_parser("query", help="Hybrid (keyword + semantic) search")
    q.add_argument("text", help="What to search for")
    q.add_argument("--agent", default="default", help="Agent name for the graph")
    q.add_argument("--limit", type=int, default=5)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    ls = kb_sub.add_parser("list", help="Graph stats and the most recently ingested chunks")
    ls.add_argument("--agent", default="default", help="Agent name for the graph")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--json", action="store_true", help="Machine-readable output")

    em_p = sub.add_parser("embed", help="Embed text via the canonical adk embeddings provider")
    em_p.add_argument("texts", nargs="+", help="One or more strings to embed")
    em_p.add_argument("--json", action="store_true", help="Print the full vectors as JSON")


def _open_graph(agent: str, graph: Optional[Any] = None) -> Any:
    if graph is not None:
        return graph
    from adk.graph_memory import GraphMemory

    return GraphMemory(agent_name=agent)


def _node_row(node: Any) -> Dict[str, Any]:
    meta = getattr(node, "metadata", {}) or {}
    return {
        "id": node.id,
        "label": node.label,
        "type": node.node_type,
        "source": meta.get("source", ""),
        "content": (node.content or "")[:240],
    }


async def kb_query(text: str, agent: str = "default", limit: int = 5,
                   graph: Optional[Any] = None) -> List[Dict[str, Any]]:
    g = _open_graph(agent, graph)
    return [_node_row(n) for n in await g.search(text, limit=limit)]


async def kb_list(agent: str = "default", limit: int = 20,
                  graph: Optional[Any] = None) -> Dict[str, Any]:
    g = _open_graph(agent, graph)
    stats = await g.get_stats()
    recent: List[Dict[str, Any]] = []
    with g._connect() as conn:  # noqa: SLF001 — same-package read of the node table
        rows = conn.execute(
            "SELECT id FROM nodes WHERE label LIKE 'chunk:%' "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    for (nid,) in rows:
        node = await g.get_node(nid)
        if node:
            recent.append(_node_row(node))
    return {"stats": stats, "chunks": recent}


async def embed(texts: List[str], provider: Optional[Any] = None) -> Dict[str, Any]:
    if provider is None:
        from adk.embeddings import get_provider

        provider = get_provider()
    vecs, dim = await provider.embed_texts(list(texts))
    return {"provider": provider.describe(), "dim": dim, "vectors": vecs}


def cmd_kb(args: argparse.Namespace) -> int:
    action = getattr(args, "kb_action", None)
    if action == "query":
        rows = asyncio.run(kb_query(args.text, agent=args.agent, limit=args.limit))
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
            return 0
        if not rows:
            print("No matches.")
            return 0
        for i, r in enumerate(rows, 1):
            src = f"  [{r['source']}]" if r["source"] else ""
            print(f"{i}. {r['label']}{src}")
            print("   " + r["content"].replace("\n", " ")[:200])
        return 0
    if action == "list":
        out = asyncio.run(kb_list(agent=args.agent, limit=args.limit))
        if args.json:
            print(json.dumps(out, indent=2, default=str))
            return 0
        s = out["stats"]
        print(f"Graph: {s.get('db_path')}  (agent={s.get('agent')})")
        print(f"Nodes: {s.get('nodes')}  embedded: {s.get('embedded')}  edges: {s.get('edges')}")
        for r in out["chunks"]:
            print(f"  {r['label']}  {r['source']}")
        return 0
    print("usage: adk kb {query,list} ...")
    return 2


def cmd_embed(args: argparse.Namespace) -> int:
    out = asyncio.run(embed(args.texts))
    if args.json:
        print(json.dumps(out, default=str))
        return 0
    p = out["provider"]
    print(f"backend={p.get('backend')} model={p.get('model')} dim={out['dim']} "
          f"degraded={p.get('degraded')} url={p.get('url') or '-'}")
    for t, v in zip(args.texts, out["vectors"]):
        head = ", ".join(f"{x:.4f}" for x in (v or [])[:4])
        print(f"  {t[:40]!r}: [{head}{', ...' if v else ''}] ({len(v or [])}-d)")
    # An empty vector means the backend cannot produce the index dimension.
    return 0 if all(out["vectors"]) else 1
