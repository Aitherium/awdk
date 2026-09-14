"""CLI commands for provenance graph management: `aiter graph <command>`.

Commands operate on the local spool when possible (offline), and fall back to
the platform when necessary. Output is human-readable by default; --json returns
machine-parseable JSON for scripting.

Usage:
    adk graph status                 Show spool stats + platform health
    adk graph drain [--limit N]      Force drain pending entries
    adk graph claim "..." --source X Record a claim from the shell
    adk graph ground "..."           Check platform's grounding of a claim
    adk graph context <task>         Bounded subgraph for an agent
    adk graph leaves [--limit N]     Frontier nodes (unexplored)
    adk graph lineage <node_id>      Ancestry path
    adk graph runs [--limit N]       Recent local runs
    adk graph show <node_id>         One node with its edges
    adk graph purge [--older-than D] Delete sent spool entries
"""

from __future__ import annotations

import asyncio
import json as _json
import sys
from typing import Optional

from adk.config import Config
from adk.selfgraph.publisher import Publisher, drain_sync, resolve_base_url
from adk.selfgraph.recorder import record_run
from adk.selfgraph.schema import ProvEdgeType, ProvNodeType, make_node_id
from adk.selfgraph.spool import Spool


def _json_safe(obj) -> str:
    """Pretty-print JSON safely."""
    try:
        return _json.dumps(obj, indent=2, default=str)
    except (TypeError, ValueError):
        return _json.dumps({"error": str(obj)}, indent=2)


def _print_result(data, as_json: bool = False) -> None:
    """Print a result object (dict or list) as JSON or formatted text."""
    if as_json:
        print(_json_safe(data))
    else:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, (int, float)):
                    print(f"  {key}: {value}")
                elif isinstance(value, str):
                    if "\n" in str(value):
                        print(f"  {key}:")
                        for line in str(value).split("\n"):
                            print(f"    {line}")
                    else:
                        print(f"  {key}: {value}")
                else:
                    print(f"  {key}: {value}")
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, dict):
                    print(f"  [{i}]:")
                    for k, v in item.items():
                        print(f"    {k}: {v}")
                else:
                    print(f"  [{i}]: {item}")
        else:
            print(f"  {data}")


def cmd_graph_status(args) -> int:
    """Show spool stats and platform reachability."""
    as_json = getattr(args, "json", False)
    spool = Spool()

    # Local spool stats
    stats = spool.stats()
    spool.close()

    # Platform reachability (async)
    config = Config.from_env()
    publisher = Publisher(config=config)

    async def _check_health():
        return await publisher.health()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reachable = loop.run_until_complete(_check_health())
        loop.close()
    except Exception:
        reachable = False

    try:
        asyncio.run(publisher.aclose())
    except RuntimeError:
        pass

    result = {
        "spool": {
            "pending": stats.get("pending", 0),
            "sent": stats.get("sent", 0),
            "failed": stats.get("failed", 0),
            "oldest_pending_age_s": stats.get("oldest_pending_age_s", 0),
            "db_bytes": stats.get("db_bytes", 0),
        },
        "platform": {
            "reachable": reachable,
            "base_url": resolve_base_url(config),
        },
    }

    if as_json:
        print(_json_safe(result))
    else:
        print("Spool Status:")
        print(f"  pending: {result['spool']['pending']}")
        print(f"  sent: {result['spool']['sent']}")
        print(f"  failed: {result['spool']['failed']}")
        if result["spool"]["oldest_pending_age_s"] > 0:
            print(f"  oldest pending: {result['spool']['oldest_pending_age_s']:.1f}s ago")
        print(f"  database: {result['spool']['db_bytes'] / 1024:.1f} KB")
        print()
        print("Platform:")
        status = "reachable" if result["platform"]["reachable"] else "unreachable"
        print(f"  {status}")
        print(f"  {result['platform']['base_url']}")

    return 0


def cmd_graph_drain(args) -> int:
    """Force a spool drain."""
    limit = getattr(args, "limit", 100)
    as_json = getattr(args, "json", False)

    outcome = drain_sync(limit=limit)

    result = outcome.to_dict()

    if as_json:
        print(_json_safe(result))
    else:
        print("Drain Outcome:")
        print(f"  ok: {result['ok']}")
        print(f"  sent: {result['sent']}")
        print(f"  failed: {result['failed']}")
        print(f"  skipped: {result['skipped']}")
        if result.get("unreachable"):
            print(f"  unreachable: true (will retry)")
        if result.get("errors"):
            print("  errors:")
            for err in result["errors"]:
                print(f"    - {err}")

    return 0 if result["ok"] else 1


def cmd_graph_claim(args) -> int:
    """Record a claim from the shell."""
    statement = getattr(args, "statement", "")
    sources = getattr(args, "source", []) or []
    inference = getattr(args, "inference", False)
    derived_from = getattr(args, "derived_from", []) or []
    as_json = getattr(args, "json", False)

    if not statement:
        print("Error: claim text is required")
        return 2

    # Validate: require sources or inference
    if not sources and not inference:
        print("Error: claim must have --source or --inference")
        return 2

    # Validate: inference requires --derived-from
    if inference and not derived_from:
        print("Error: inference=True requires --derived-from")
        return 2

    # Record the claim
    with record_run(
        agent_id="cli", tenant_id="", objective="shell claim recording"
    ) as run:
        try:
            claim_node = run.claim(
                statement,
                sources=tuple(sources),
                inference=inference,
                derived_from=tuple(derived_from),
            )
            run.flush()

            result = {
                "node_id": claim_node.id,
                "node_type": claim_node.node_type.value,
                "name": claim_node.name,
                "created_at": claim_node.created_at,
            }

            if as_json:
                print(_json_safe(result))
            else:
                print("Claim recorded:")
                print(f"  id: {result['node_id']}")
                print(f"  type: {result['node_type']}")
                print(f"  name: {result['name']}")

            return 0
        except ValueError as e:
            print(f"Error: {e}")
            return 2


def cmd_graph_ground(args) -> int:
    """Ask the platform whether a claim is grounded (supported)."""
    statement = getattr(args, "statement", "")
    as_json = getattr(args, "json", False)

    if not statement:
        print("Error: statement is required")
        return 2

    config = Config.from_env()
    publisher = Publisher(config=config)

    async def _ground():
        return await publisher.ground(statement=statement)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_ground())
        loop.close()
    except Exception as e:
        if as_json:
            print(_json_safe({"error": str(e)}))
        else:
            print(f"Error: {e}")
        return 1

    try:
        asyncio.run(publisher.aclose())
    except RuntimeError:
        pass

    if not result:
        print("Platform unreachable")
        return 1

    if as_json:
        print(_json_safe(result))
    else:
        supported = result.get("supported", False)
        reason = result.get("reason", "")
        evidence = result.get("required_evidence", [])

        status = "SUPPORTED" if supported else "UNSUPPORTED"
        print(f"Status: {status}")
        if reason:
            print(f"Reason: {reason}")
        if evidence:
            print("Required Evidence:")
            for ev in evidence:
                print(f"  - {ev}")

    return 0 if result.get("supported") else 1


def cmd_graph_context(args) -> int:
    """Fetch bounded context for a task."""
    task = getattr(args, "task", "")
    hops = getattr(args, "hops", 2)
    budget = getattr(args, "budget", 4000)
    as_json = getattr(args, "json", False)

    if not task:
        print("Error: task description is required")
        return 2

    config = Config.from_env()
    publisher = Publisher(config=config)

    async def _context():
        return await publisher.context(task=task, hops=hops, token_budget=budget)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_context())
        loop.close()
    except Exception as e:
        if as_json:
            print(_json_safe({"error": str(e)}))
        else:
            print(f"Error: {e}")
        return 1

    try:
        asyncio.run(publisher.aclose())
    except RuntimeError:
        pass

    if not result:
        print("Platform unreachable")
        return 1

    if as_json:
        print(_json_safe(result))
    else:
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])
        print(f"Context ({len(nodes)} nodes, {len(edges)} edges):")
        for node in nodes[:10]:  # Limit display
            print(f"  [{node.get('node_type')}] {node.get('name')}")
        if len(nodes) > 10:
            print(f"  ... and {len(nodes) - 10} more")

    return 0


def cmd_graph_leaves(args) -> int:
    """Fetch leaf/frontier nodes."""
    limit = getattr(args, "limit", 50)
    as_json = getattr(args, "json", False)

    config = Config.from_env()
    publisher = Publisher(config=config)

    async def _leaves():
        return await publisher.leaves(limit=limit)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(_leaves())
        loop.close()
    except Exception as e:
        if as_json:
            print(_json_safe({"error": str(e)}))
        else:
            print(f"Error: {e}")
        return 1

    try:
        asyncio.run(publisher.aclose())
    except RuntimeError:
        pass

    if as_json:
        print(_json_safe(result))
    else:
        print(f"Leaf Nodes ({len(result)}):")
        for leaf in result:
            ntype = leaf.get("node_type", "?")
            name = leaf.get("name", "unnamed")
            print(f"  [{ntype}] {name}")

    return 0


def cmd_graph_lineage(args) -> int:
    """Fetch ancestry path for a node."""
    node_id = getattr(args, "node_id", "")
    as_json = getattr(args, "json", False)

    if not node_id:
        print("Error: node_id is required")
        return 2

    # For now, we return local edges leading to this node (ancestry)
    spool = Spool()
    edges = spool.local_edges(target_id=node_id, limit=100)
    spool.close()

    result = [e.to_dict() for e in edges]

    if as_json:
        print(_json_safe(result))
    else:
        print(f"Lineage for {node_id} ({len(result)} edges):")
        for edge in result:
            src = edge.get("source_id", "?")
            pred = edge.get("edge_type", "?")
            print(f"  {src} --{pred}--> {edge.get('target_id', '?')}")

    return 0


def cmd_graph_runs(args) -> int:
    """List recent runs from the local spool."""
    limit = getattr(args, "limit", 50)
    as_json = getattr(args, "json", False)

    spool = Spool()
    nodes = spool.local_nodes(
        node_type=ProvNodeType.RUN, limit=limit
    )
    spool.close()

    result = [n.to_dict() for n in nodes]

    if as_json:
        print(_json_safe(result))
    else:
        print(f"Recent Runs ({len(result)}):")
        for node in nodes:
            run_id = node.name
            created = node.created_at
            status = node.properties.get("status", "unknown")
            print(f"  {run_id} ({status}) @ {created}")

    return 0


def cmd_graph_show(args) -> int:
    """Show a node and its edges."""
    node_id = getattr(args, "node_id", "")
    as_json = getattr(args, "json", False)

    if not node_id:
        print("Error: node_id is required")
        return 2

    spool = Spool()
    nodes = spool.local_nodes(limit=10000)  # Query all
    edges_in = spool.local_edges(target_id=node_id, limit=1000)
    edges_out = spool.local_edges(source_id=node_id, limit=1000)
    spool.close()

    # Find the node
    node = next((n for n in nodes if n.id == node_id), None)
    if not node:
        print(f"Node not found: {node_id}")
        return 1

    result = {
        "node": node.to_dict(),
        "edges_in": [e.to_dict() for e in edges_in],
        "edges_out": [e.to_dict() for e in edges_out],
    }

    if as_json:
        print(_json_safe(result))
    else:
        print(f"Node: {node.name}")
        print(f"  id: {node.id}")
        print(f"  type: {node.node_type.value}")
        print(f"  created: {node.created_at}")
        print()
        print(f"Incoming Edges ({len(edges_in)}):")
        for edge in edges_in:
            src = edge.source_id[:20]
            print(f"  {src} -{edge.edge_type.value}-> {node_id}")
        print()
        print(f"Outgoing Edges ({len(edges_out)}):")
        for edge in edges_out:
            tgt = edge.target_id[:20]
            print(f"  {node_id} -{edge.edge_type.value}-> {tgt}")

    return 0


def cmd_graph_purge(args) -> int:
    """Purge old sent entries from the spool."""
    older_than_days = getattr(args, "older_than", 7)
    as_json = getattr(args, "json", False)

    older_than_s = older_than_days * 86400
    spool = Spool()
    deleted = spool.purge_sent(older_than_s=older_than_s)
    spool.close()

    result = {
        "deleted": deleted,
        "older_than_days": older_than_days,
    }

    if as_json:
        print(_json_safe(result))
    else:
        print(f"Purged {deleted} entries older than {older_than_days} days")

    return 0


def cmd_graph(args) -> int:
    """Dispatch graph sub-commands."""
    graph_cmd = getattr(args, "graph_command", None)

    if graph_cmd == "status":
        return cmd_graph_status(args)
    elif graph_cmd == "drain":
        return cmd_graph_drain(args)
    elif graph_cmd == "claim":
        return cmd_graph_claim(args)
    elif graph_cmd == "ground":
        return cmd_graph_ground(args)
    elif graph_cmd == "context":
        return cmd_graph_context(args)
    elif graph_cmd == "leaves":
        return cmd_graph_leaves(args)
    elif graph_cmd == "lineage":
        return cmd_graph_lineage(args)
    elif graph_cmd == "runs":
        return cmd_graph_runs(args)
    elif graph_cmd == "show":
        return cmd_graph_show(args)
    elif graph_cmd == "purge":
        return cmd_graph_purge(args)
    else:
        print("Usage: adk graph [status|drain|claim|ground|context|leaves|lineage|runs|show|purge]")
        print()
        print("  status              Show spool stats + platform health")
        print("  drain [--limit N]   Force drain pending entries")
        print("  claim <stmt>        Record a claim with --source URI [--source URI ...]")
        print("  ground <stmt>       Check platform grounding")
        print("  context <task>      Bounded subgraph for task")
        print("  leaves [--limit N]  Frontier nodes")
        print("  lineage <node_id>   Ancestry path")
        print("  runs [--limit N]    Recent local runs")
        print("  show <node_id>      One node with edges")
        print("  purge               Delete old sent entries")
        print()
        print("All commands support --json for scripting.")
        return 1
