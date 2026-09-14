"""Provenance graph recording and publishing for AitherOS.

This module provides a local-first, durable recording system for agent runs.
Agents write typed, source-backed findings to a local SQLite spool that drains
into the shared platform graph, so knowledge survives the session and the node.

Key design:
  - **Offline-first**: a run is never blocked, lost, or slowed by platform
    availability. All recording is to a local durable spool.
  - **Cryptographically stable**: an update recorded offline on a laptop must
    produce byte-identical node/edge IDs to one recorded in the fleet, or the
    same finding forks into two nodes on platform ingest. The hashing is a
    compatibility surface, not an implementation detail.
  - **Source-backed claims**: it is impossible to casually record an unsourced
    claim. The API enforces that every claim either cites a source, is marked as
    inference (with explicit derivation), or is supported by another claim.

Primary API::

    from adk.selfgraph import record_run

    with record_run(agent_id="myagent", objective="find answers") as run:
        run.claim("it is raining", sources=["https://weather.example.com"])
        run.artifact("analysis.pdf", version=1, path="/tmp/analysis.pdf")
        run.entity("Alice", entity_type="person", aliases=["Ms. Smith"])

Convenience functions for library code::

    from adk.selfgraph import record_claim, current_run

    record_claim("the sky is blue", sources=["photo.jpg"])
    if current_run():
        current_run().tool_call("tool_name", ok=True, duration_ms=150)

Environment control::
  - `AITHER_SELFGRAPH=0` disables recording entirely (all functions become no-ops).
  - `AITHER_SELFGRAPH_DB` overrides the spool database path.
  - `AITHER_SELFGRAPH_URL` overrides the platform graph plane URL.

Drain the spool manually or via a background drainer::

    from adk.selfgraph import Spool, Publisher, drain_sync
    import asyncio

    # Sync drain
    outcome = drain_sync(Spool())

    # Async drain
    async with Publisher() as pub:
        outcome = await pub.drain()
"""

from .drainer import BackgroundDrainer, drain_sync
from .publisher import Publisher, PublishOutcome
from .recorder import (
    RunRecorder,
    current_run,
    new_run_id,
    record_artifact,
    record_claim,
    record_open_question,
    record_run,
    record_tool_call,
)
from .schema import (
    GraphUpdate,
    ProvEdge,
    ProvEdgeType,
    ProvNode,
    ProvNodeType,
    check_invariants,
    make_edge_id,
    make_node_id,
)
from .spool import Spool, SpoolEntry

__all__ = [
    # Recorder API
    "RunRecorder",
    "record_run",
    "current_run",
    "new_run_id",
    "record_claim",
    "record_artifact",
    "record_tool_call",
    "record_open_question",
    # Spool
    "Spool",
    "SpoolEntry",
    # Publisher
    "Publisher",
    "PublishOutcome",
    # Drainer
    "BackgroundDrainer",
    "drain_sync",
    # Schema
    "ProvNode",
    "ProvEdge",
    "ProvNodeType",
    "ProvEdgeType",
    "GraphUpdate",
    "check_invariants",
    "make_node_id",
    "make_edge_id",
]
