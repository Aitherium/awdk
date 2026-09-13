"""``adk workflow mirror`` -- stream Claude Code Workflow journals into expeditions.

Usage:
  adk workflow mirror [--once] [--root PATH] [--gateway URL] [--interval SECONDS]
  adk workflow status <run_id>
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _mirror_once(args) -> dict:
    from adk.workflow_mirror import scan_and_mirror

    root = Path(args.root) if getattr(args, "root", None) else None
    return scan_and_mirror(root=root, gateway_url=args.gateway)


def cmd_workflow(args) -> int:
    sub = getattr(args, "workflow_command", None)
    if sub == "mirror":
        return cmd_workflow_mirror(args)
    if sub == "status":
        return cmd_workflow_status(args)
    print("usage: adk workflow mirror [--once] | adk workflow status <run_id>")
    return 2


def cmd_workflow_mirror(args) -> int:
    once = getattr(args, "once", False)
    interval = max(5, int(getattr(args, "interval", 15) or 15))
    if once:
        summary = _mirror_once(args)
        print(json.dumps(summary, indent=2))
        return 1 if summary.get("errors") else 0
    logger.info("workflow mirror: polling every %ss (Ctrl-C to stop)", interval)
    try:
        while True:
            summary = _mirror_once(args)
            if summary.get("events") or summary.get("errors"):
                logger.info("workflow mirror: %s", json.dumps(summary))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_workflow_status(args) -> int:
    from adk.workflow_mirror import DEFAULT_GATEWAY, SOURCE, GatewayClient, MirrorError

    client = GatewayClient(getattr(args, "gateway", None) or DEFAULT_GATEWAY)
    try:
        res = client.call("expedition_mirror_status", {"run_id": args.run_id, "source": SOURCE})
    except MirrorError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(res, indent=2))
    return 0


def add_workflow_parser(sub) -> None:
    """Register ``adk workflow ...`` on the top-level subparsers."""
    p = sub.add_parser("workflow", help="Claude Code Workflow runs mirrored as expeditions")
    wsub = p.add_subparsers(dest="workflow_command")
    m = wsub.add_parser("mirror", help="Stream workflow journals into the expedition mirror")
    m.add_argument("--once", action="store_true", help="One pass, then exit")
    m.add_argument("--root", help="Projects root, a session dir, or a workflows dir")
    m.add_argument("--gateway", default="http://127.0.0.1:8182", help="MCP gateway URL")
    m.add_argument("--interval", type=int, default=15, help="Seconds between passes")
    s = wsub.add_parser("status", help="Show the mirrored expedition for a run")
    s.add_argument("run_id")
    s.add_argument("--gateway", default="http://127.0.0.1:8182", help="MCP gateway URL")
