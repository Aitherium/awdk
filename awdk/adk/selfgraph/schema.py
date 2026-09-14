"""Typed provenance schema — the ADK mirror of the platform's graph plane.

This is a deliberate, dependency-free mirror of
``AitherOS/lib/graph/provenance/schema.py``. The ADK ships to PyPI and runs on
machines that have never seen the platform, so it cannot import from ``lib.*``;
but a run recorded offline on a customer's laptop must produce **byte-identical
node and edge identifiers** to one recorded inside the fleet, or the same finding
would fork into two nodes the moment the spool drains.

The hashing in :func:`make_node_id` and :func:`make_edge_id` is therefore a
compatibility surface, not an implementation detail. ``tests/test_selfgraph_
schema_parity.py`` asserts the enum members and the derived ids agree with the
platform copy whenever both trees are present — a mirror without a differential
test is just a fork that has not been noticed yet.

Stdlib only. No httpx, no pydantic, no numpy.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


class ProvNodeType(str, Enum):
    """Epistemic node types. Must stay in lockstep with the platform enum."""

    OBJECTIVE = "objective"
    PLAN = "plan"
    RUN = "run"
    AGENT = "agent"
    EXPERIMENT = "experiment"
    CLAIM = "claim"
    SOURCE = "source"
    ENTITY = "entity"
    ARTIFACT = "artifact"
    RUBRIC = "rubric"
    EVALUATION = "evaluation"
    OPEN_QUESTION = "open_question"
    CONFLICT = "conflict"


class ProvEdgeType(str, Enum):
    """Predicates. Must stay in lockstep with the platform enum."""

    PRODUCED = "produced"
    SUPPORTS = "supports"
    REFUTES = "refutes"
    CITES = "cites"
    EVALUATES = "evaluates"
    REVISES = "revises"
    SUPERSEDES = "supersedes"
    DEPENDS_ON = "depends_on"
    PARENT_OF = "parent_of"
    RESOLVED_TO = "resolved_to"
    ABOUT = "about"
    MODIFIED = "modified"
    PLANNED_BY = "planned_by"
    ASSIGNED_TO = "assigned_to"
    DERIVED_FROM = "derived_from"
    ANSWERS = "answers"


def _slug(value: str, limit: int = 96) -> str:
    out = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return out[:limit] or "unnamed"


def make_node_id(node_type: ProvNodeType, name: str, tenant_id: str = "") -> str:
    """Deterministic, tenant-scoped node id. Identical algorithm to the platform."""
    digest = hashlib.sha256(
        f"{tenant_id}\x00{node_type.value}\x00{(name or '').strip().lower()}".encode()
    ).hexdigest()[:16]
    return f"{node_type.value}:{_slug(name, 48)}:{digest}"


def make_edge_id(
    source_id: str,
    predicate: ProvEdgeType,
    target_id: str,
    source_doc: str = "",
) -> str:
    """Stable, citable edge id. Identical algorithm to the platform."""
    digest = hashlib.sha256(
        f"{source_id}\x00{predicate.value}\x00{target_id}\x00{source_doc}".encode()
    ).hexdigest()[:24]
    return f"e:{digest}"


@dataclass
class ProvNode:
    """A node awaiting publication (or already published)."""

    id: str
    node_type: ProvNodeType
    name: str
    tenant_id: str = ""
    workspace_id: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    agent_id: str = ""
    version: int = 1
    superseded_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION

    @property
    def is_inference(self) -> bool:
        return bool(self.properties.get("inference"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "node_type": self.node_type.value,
            "name": self.name,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "properties": dict(self.properties),
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "version": self.version,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProvNode":
        return cls(
            id=str(data["id"]),
            node_type=ProvNodeType(data["node_type"]),
            name=str(data.get("name", "")),
            tenant_id=str(data.get("tenant_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            properties=dict(data.get("properties") or {}),
            run_id=str(data.get("run_id", "")),
            agent_id=str(data.get("agent_id", "")),
            version=int(data.get("version", 1)),
            superseded_by=data.get("superseded_by"),
            created_at=float(data.get("created_at", time.time())),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class ProvEdge:
    """A directed, provenance-carrying edge."""

    source_id: str
    target_id: str
    edge_type: ProvEdgeType
    id: str = ""
    tenant_id: str = ""
    workspace_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    source_doc: str = ""
    confidence: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_edge_id(
                self.source_id, self.edge_type, self.target_id, self.source_doc
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type.value,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "source_doc": self.source_doc,
            "confidence": self.confidence,
            "properties": dict(self.properties),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProvEdge":
        return cls(
            id=str(data.get("id", "")),
            source_id=str(data["source_id"]),
            target_id=str(data["target_id"]),
            edge_type=ProvEdgeType(data["edge_type"]),
            tenant_id=str(data.get("tenant_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            run_id=str(data.get("run_id", "")),
            agent_id=str(data.get("agent_id", "")),
            source_doc=str(data.get("source_doc", "")),
            confidence=float(data.get("confidence", 1.0)),
            properties=dict(data.get("properties") or {}),
            created_at=float(data.get("created_at", time.time())),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class GraphUpdate:
    """One atomic batch of writes produced by one ADK run."""

    nodes: List[ProvNode] = field(default_factory=list)
    edges: List[ProvEdge] = field(default_factory=list)
    run_id: str = ""
    agent_id: str = ""
    tenant_id: str = ""
    workspace_id: str = ""
    objective_id: str = ""

    def stamp(self) -> "GraphUpdate":
        """Push the update's identity down onto every element."""
        for node in self.nodes:
            node.run_id = node.run_id or self.run_id
            node.agent_id = node.agent_id or self.agent_id
            node.tenant_id = node.tenant_id or self.tenant_id
            node.workspace_id = node.workspace_id or self.workspace_id
        for edge in self.edges:
            edge.run_id = edge.run_id or self.run_id
            edge.agent_id = edge.agent_id or self.agent_id
            edge.tenant_id = edge.tenant_id or self.tenant_id
            edge.workspace_id = edge.workspace_id or self.workspace_id
            if not edge.id:
                edge.id = make_edge_id(
                    edge.source_id, edge.edge_type, edge.target_id, edge.source_doc
                )
        return self

    def is_empty(self) -> bool:
        return not self.nodes and not self.edges

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "objective_id": self.objective_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GraphUpdate":
        return cls(
            nodes=[ProvNode.from_dict(n) for n in data.get("nodes") or []],
            edges=[ProvEdge.from_dict(e) for e in data.get("edges") or []],
            run_id=str(data.get("run_id", "")),
            agent_id=str(data.get("agent_id", "")),
            tenant_id=str(data.get("tenant_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            objective_id=str(data.get("objective_id", "")),
        )


def check_invariants(update: GraphUpdate) -> List[str]:
    """Local pre-flight of the four write invariants.

    The platform re-validates on ingest and is the authority; this runs first so a
    malformed update is caught at the point it was *created* — while the agent that
    made it is still around to fix it — rather than as an opaque 4xx an hour later
    when the spool drains.

    Returns a list of human-readable violations; empty means the update is clean.
    """
    problems: List[str] = []
    update.stamp()
    ids = {n.id for n in update.nodes}
    out_by_source: Dict[str, List[ProvEdge]] = {}
    for edge in update.edges:
        out_by_source.setdefault(edge.source_id, []).append(edge)

    if not update.run_id:
        problems.append("update has no run_id (unattributable)")
    if not update.agent_id:
        problems.append("update has no agent_id (unattributable)")

    for node in update.nodes:
        if node.node_type is ProvNodeType.CLAIM:
            edges = out_by_source.get(node.id, [])
            if node.is_inference:
                if not any(e.edge_type is ProvEdgeType.DERIVED_FROM for e in edges):
                    problems.append(
                        f"claim {node.id} is marked inference but names nothing it was "
                        "derived from"
                    )
            else:
                cited = any(e.edge_type is ProvEdgeType.CITES for e in edges)
                supported = any(
                    e.edge_type is ProvEdgeType.SUPPORTS and e.target_id == node.id
                    for e in update.edges
                )
                if not cited and not supported:
                    problems.append(
                        f"claim {node.id} has no source and is not marked inference"
                    )
        elif node.node_type is ProvNodeType.ARTIFACT:
            if not node.run_id:
                problems.append(f"artifact {node.id} has no authoring run")
            if not isinstance(node.version, int) or node.version < 1:
                problems.append(f"artifact {node.id} has an invalid version")
        elif node.node_type is ProvNodeType.EVALUATION:
            rubric_id = str(node.properties.get("rubric_id") or "")
            if not rubric_id:
                problems.append(f"evaluation {node.id} does not name a rubric")

    for edge in update.edges:
        if not edge.source_id or not edge.target_id:
            problems.append(f"edge {edge.id} has an empty endpoint")
        if not (edge.run_id or edge.source_doc):
            problems.append(f"edge {edge.id} has neither run_id nor source_doc")
        if not 0.0 <= edge.confidence <= 1.0:
            problems.append(f"edge {edge.id} confidence {edge.confidence} outside 0..1")
        if edge.edge_type is ProvEdgeType.SUPERSEDES and edge.target_id not in ids:
            # Only checkable locally when the target is in the same update; the
            # platform does the authoritative check against stored history.
            pass
    return problems
