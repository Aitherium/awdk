"""CLI for graphrag tools. JSON to stdout.

  python -m adk.toolpacks.graphrag detect
  python -m adk.toolpacks.graphrag plan --embedder text
  python -m adk.toolpacks.graphrag apply --embedder text --dry-run
  python -m adk.toolpacks.graphrag verify-embedder --embedder text --base-url https://localhost:8209
  python -m adk.toolpacks.graphrag ingest --path ./docs --agent research
  python -m adk.toolpacks.graphrag verify-retrieval --agent research --sentinel "topic"

Exit codes: 0 healthy, 1 error, 2 degraded/empty, 3 unknown, 4 wrong_dimension.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m adk.toolpacks.graphrag",
                                description="Embeddings + GraphRAG setup")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("detect").set_defaults(handler=lambda a: tools.rag_detect_hardware())

    r = sub.add_parser("resolve"); r.add_argument("--embedder", default="text")
    r.set_defaults(handler=lambda a: tools.rag_resolve_embedder(a.embedder))

    pl = sub.add_parser("plan"); pl.add_argument("--embedder", default="text")
    pl.set_defaults(handler=lambda a: tools.rag_plan_embedder(a.embedder))

    ap = sub.add_parser("apply"); ap.add_argument("--embedder", default="text")
    ap.add_argument("--install-vllm", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(handler=lambda a: tools.rag_apply_embedder(a.embedder, a.dry_run, a.install_vllm))

    ve = sub.add_parser("verify-embedder"); ve.add_argument("--base-url", required=True)
    ve.add_argument("--embedder", default="text"); ve.add_argument("--timeout", type=float, default=60.0)
    ve.set_defaults(handler=lambda a: tools.rag_verify_embedder(a.base_url, a.embedder, a.timeout))

    ig = sub.add_parser("ingest"); ig.add_argument("--path", required=True)
    ig.add_argument("--agent", default="default"); ig.add_argument("--chunk-size", type=int, default=0)
    ig.set_defaults(handler=lambda a: tools.rag_ingest(a.path, a.agent, a.chunk_size))

    vr = sub.add_parser("verify-retrieval"); vr.add_argument("--query-url", default="")
    vr.add_argument("--agent", default="default"); vr.add_argument("--sentinel", default="")
    vr.add_argument("--timeout", type=float, default=60.0)
    vr.set_defaults(handler=lambda a: tools.rag_verify_retrieval(a.query_url, a.agent, a.sentinel, a.timeout))

    args = p.parse_args()
    if not hasattr(args, "handler"):
        p.print_help()
        return 1
    try:
        result = args.handler(args)
        print(json.dumps(result, indent=2))
        if "error" in result:
            return 1
        return {"degraded": 2, "empty": 2, "unknown": 3, "wrong_dimension": 4}.get(
            result.get("status"), 0)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
