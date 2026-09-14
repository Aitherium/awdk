"""User-facing recording API for provenance graph updates.

This is the ergonomic surface agents touch. All recording is local-first: a run
is never blocked or slowed by platform availability. Record-time invariant
checks ensure unsourced claims are impossible without explicit marking them
as inferences.

The primary entry point is record_run(), a context manager:

    from adk.selfgraph import record_run

    with record_run(agent_id="myagent", objective="find foo") as run:
        run.claim("foo is red", sources=["https://example.com/foo"])
        run.artifact("report.pdf", version=1)

Alternatively, the free-function shortcuts work when there is an active run:

    record_claim("the sky is blue")
    record_artifact("findings.json")

All recording is no-op safe when AITHER_SELFGRAPH=0, or when called outside
an active run context. Records flow through the local spool to the platform
via the Publisher's drain loop; network failures never block the agent.
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .schema import (
    GraphUpdate,
    ProvEdgeType,
    ProvNode,
    ProvNodeType,
    check_invariants,
    make_node_id,
)
from .spool import Spool

logger = logging.getLogger("adk.selfgraph.recorder")

# Global kill switch: AITHER_SELFGRAPH=0 disables all recording
_ENABLED = os.getenv("AITHER_SELFGRAPH", "").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)

# Current active run (per async context)
_current_run: contextvars.ContextVar[Optional[RunRecorder]] = contextvars.ContextVar(
    "adk.selfgraph.current_run", default=None
)


def new_run_id(prefix: str = "run") -> str:
    """Generate a new run id (uuid4 hex).

    Args:
        prefix: Prefix for readability. Defaults to "run".

    Returns:
        A string like "run-a1b2c3d4...".
    """
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def current_run() -> Optional[RunRecorder]:
    """Fetch the current active run, if any.

    Safe to call in any context (sync or async). Returns None when called
    outside a record_run() context or after the run is closed.

    Returns:
        The active RunRecorder, or None.
    """
    if not _ENABLED:
        return None
    return _current_run.get()


class RunRecorder:
    """Records a single agent run and its discoveries to the provenance graph.

    A run groups all claims, artifacts, and edges produced by one execution.
    It creates the RUN node and optionally an OBJECTIVE + AGENT nodes. All
    recorded nodes are buffered in memory and flushed to the spool atomically.

    The recorder enforces that unsourced claims are impossible (they require
    either sources, inference=True, or a SUPPORTS edge), making knowledge
    recording audit-friendly.

    Attributes:
        run_id: Unique run identifier.
        agent_id: The agent that created this run.
        tenant_id: The tenant (empty if on-prem).
        workspace_id: The workspace (empty if single-tenant).
        objective: Optional objective statement.
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        agent_id: str = "",
        tenant_id: str = "",
        workspace_id: str = "",
        objective: str = "",
        spool: Optional[Spool] = None,
        autoflush: int = 50,
    ) -> None:
        """Initialize a run recorder.

        Creates the RUN node and an AGENT node linking to it. If objective is
        given, also creates an OBJECTIVE node with a PLANNED_BY edge from the
        run. All nodes are buffered locally; call flush() to write them to
        the spool.

        Args:
            run_id: Run identifier. If empty, generates a new one.
            agent_id: Agent identifier (required for attribution).
            tenant_id: Tenant id (empty for on-prem).
            workspace_id: Workspace id (empty for single-tenant).
            objective: Optional objective statement. Creates a node if given.
            spool: Spool instance. If None, creates a default one.
            autoflush: Flush buffer when it exceeds N nodes. Defaults to 50.
        """
        self.run_id = run_id or new_run_id()
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.objective = objective
        self.spool = spool or Spool()
        self.autoflush = autoflush

        self._nodes: list[ProvNode] = []
        self._edges: list[Any] = []  # List of ProvEdge-compatible dicts
        self._status = "pending"
        self._metadata: dict[str, Any] = {}
        self._closed = False
        self._objective_id = ""

        # Create the RUN node
        run_node = ProvNode(
            id=make_node_id(ProvNodeType.RUN, self.run_id, self.tenant_id),
            node_type=ProvNodeType.RUN,
            name=self.run_id,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )
        self._nodes.append(run_node)

        # Create the AGENT node
        agent_node = ProvNode(
            id=make_node_id(ProvNodeType.AGENT, self.agent_id, self.tenant_id),
            node_type=ProvNodeType.AGENT,
            name=self.agent_id,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            agent_id=self.agent_id,
        )
        self._nodes.append(agent_node)

        # Link RUN -> AGENT
        from .schema import ProvEdge  # Import here to avoid circular deps
        agent_edge = ProvEdge(
            source_id=run_node.id,
            target_id=agent_node.id,
            edge_type=ProvEdgeType.ASSIGNED_TO,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )
        self._edges.append(agent_edge)

        # Create OBJECTIVE node if given
        if objective:
            objective_node = ProvNode(
                id=make_node_id(ProvNodeType.OBJECTIVE, objective, self.tenant_id),
                node_type=ProvNodeType.OBJECTIVE,
                name=objective,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                agent_id=self.agent_id,
            )
            self._nodes.append(objective_node)
            self._objective_id = objective_node.id

            # Link RUN -PLANNED_BY-> OBJECTIVE
            obj_edge = ProvEdge(
                source_id=run_node.id,
                target_id=objective_node.id,
                edge_type=ProvEdgeType.PLANNED_BY,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(obj_edge)

    def claim(
        self,
        statement: str,
        *,
        sources: tuple[str, ...] = (),
        inference: bool = False,
        about: tuple[str, ...] = (),
        confidence: float = 1.0,
        derived_from: tuple[str, ...] = (),
    ) -> ProvNode:
        """Record a claim with mandatory sourcing.

        Creates a CLAIM node and CITES edges to each source. Enforces that
        unsourced, non-inferred claims are impossible: if no sources and
        inference=False, raises ValueError.

        Strings in sources that look like URLs/paths are auto-converted to
        SOURCE nodes so the claim cannot accidentally cite nothing.

        Args:
            statement: The claim text.
            sources: Tuple of source URIs or node ids. Auto-converts URL-like
                strings to SOURCE nodes.
            inference: True if this claim is inferred (requires derived_from).
                Raises ValueError if inference=True but derived_from is empty.
            about: Tuple of node ids this claim is about (creates ABOUT edges).
            confidence: Confidence 0..1. Defaults to 1.0.
            derived_from: Tuple of node ids this was derived from (required if
                inference=True).

        Returns:
            The CLAIM node.

        Raises:
            ValueError: Unsourced non-inferred claim, or inference=True without
                derived_from.
        """
        if self._closed:
            raise ValueError("run is closed")

        if inference and not derived_from:
            raise ValueError(
                "claim marked inference=True but derived_from is empty; "
                "an inferred claim must name what it was derived from"
            )

        if not sources and not inference:
            raise ValueError(
                "claim has no sources and is not marked inference; "
                "an unsourced claim must cite something or be marked derived"
            )

        from .schema import ProvEdge

        claim_node = ProvNode(
            id=make_node_id(ProvNodeType.CLAIM, statement, self.tenant_id),
            node_type=ProvNodeType.CLAIM,
            name=statement,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties={"inference": inference, "confidence": confidence},
        )
        self._nodes.append(claim_node)

        # Create SOURCE nodes for URL-like strings in sources
        for source in sources:
            if source.startswith(("http://", "https://", "file://", "/")):
                source_node = ProvNode(
                    id=make_node_id(ProvNodeType.SOURCE, source, self.tenant_id),
                    node_type=ProvNodeType.SOURCE,
                    name=source,
                    tenant_id=self.tenant_id,
                    workspace_id=self.workspace_id,
                    agent_id=self.agent_id,
                )
                # Dedupe by id
                if not any(n.id == source_node.id for n in self._nodes):
                    self._nodes.append(source_node)
                target_id = source_node.id
            else:
                # Assume it's already a node id
                target_id = source

            # Create CITES edge
            edge = ProvEdge(
                source_id=claim_node.id,
                target_id=target_id,
                edge_type=ProvEdgeType.CITES,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(edge)

        # Create DERIVED_FROM edges
        for parent_id in derived_from:
            edge = ProvEdge(
                source_id=claim_node.id,
                target_id=parent_id,
                edge_type=ProvEdgeType.DERIVED_FROM,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(edge)

        # Create ABOUT edges
        for target_id in about:
            edge = ProvEdge(
                source_id=claim_node.id,
                target_id=target_id,
                edge_type=ProvEdgeType.ABOUT,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(edge)

        self._maybe_autoflush()
        return claim_node

    def source(self, uri: str, *, title: str = "") -> ProvNode:
        """Record a source (document, website, API, etc).

        Args:
            uri: The source URI (URL, file path, etc).
            title: Optional title or description.

        Returns:
            The SOURCE node.
        """
        if self._closed:
            raise ValueError("run is closed")

        props = {}
        if title:
            props["title"] = title

        node = ProvNode(
            id=make_node_id(ProvNodeType.SOURCE, uri, self.tenant_id),
            node_type=ProvNodeType.SOURCE,
            name=uri,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties=props,
        )
        self._nodes.append(node)
        self._maybe_autoflush()
        return node

    def artifact(
        self,
        name: str,
        *,
        version: int = 1,
        content_hash: str = "",
        path: str = "",
        revises: str = "",
    ) -> ProvNode:
        """Record an artifact (file, report, model, etc).

        Args:
            name: Artifact name or identifier.
            version: Version number. Defaults to 1.
            content_hash: Optional hash (sha256, etc).
            path: Optional file path.
            revises: Optional node id this revises (creates REVISES edge).

        Returns:
            The ARTIFACT node.
        """
        if self._closed:
            raise ValueError("run is closed")

        from .schema import ProvEdge

        props = {}
        if content_hash:
            props["content_hash"] = content_hash
        if path:
            props["path"] = path

        node = ProvNode(
            id=make_node_id(ProvNodeType.ARTIFACT, name, self.tenant_id),
            node_type=ProvNodeType.ARTIFACT,
            name=name,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            version=version,
            properties=props,
        )
        self._nodes.append(node)

        if revises:
            edge = ProvEdge(
                source_id=node.id,
                target_id=revises,
                edge_type=ProvEdgeType.REVISES,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(edge)

        self._maybe_autoflush()
        return node

    def evaluation(
        self,
        decision: str,
        *,
        rubric_id: str,
        target_id: str,
        reason: str = "",
        score: Optional[float] = None,
    ) -> ProvNode:
        """Record an evaluation against a rubric.

        Args:
            decision: The evaluation result (e.g., "pass", "fail").
            rubric_id: Node id of the rubric.
            target_id: Node id of what is being evaluated.
            reason: Optional explanation.
            score: Optional numeric score 0..1.

        Returns:
            The EVALUATION node.
        """
        if self._closed:
            raise ValueError("run is closed")

        from .schema import ProvEdge

        props: dict[str, Any] = {"rubric_id": rubric_id}
        if reason:
            props["reason"] = reason
        if score is not None:
            props["score"] = score

        node = ProvNode(
            id=make_node_id(ProvNodeType.EVALUATION, decision, self.tenant_id),
            node_type=ProvNodeType.EVALUATION,
            name=decision,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties=props,
        )
        self._nodes.append(node)

        # Link evaluation to target
        edge = ProvEdge(
            source_id=node.id,
            target_id=target_id,
            edge_type=ProvEdgeType.EVALUATES,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )
        self._edges.append(edge)

        self._maybe_autoflush()
        return node

    def rubric(self, name: str, *, criteria: dict[str, str]) -> ProvNode:
        """Record an evaluation rubric.

        Args:
            name: Rubric name.
            criteria: Dict mapping criterion names to descriptions.

        Returns:
            The RUBRIC node.
        """
        if self._closed:
            raise ValueError("run is closed")

        node = ProvNode(
            id=make_node_id(ProvNodeType.RUBRIC, name, self.tenant_id),
            node_type=ProvNodeType.RUBRIC,
            name=name,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties={"criteria": criteria},
        )
        self._nodes.append(node)
        self._maybe_autoflush()
        return node

    def open_question(self, text: str) -> ProvNode:
        """Record an open question or uncertainty.

        Args:
            text: The question.

        Returns:
            The OPEN_QUESTION node.
        """
        if self._closed:
            raise ValueError("run is closed")

        node = ProvNode(
            id=make_node_id(ProvNodeType.OPEN_QUESTION, text, self.tenant_id),
            node_type=ProvNodeType.OPEN_QUESTION,
            name=text,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )
        self._nodes.append(node)
        self._maybe_autoflush()
        return node

    def experiment(
        self,
        hypothesis: str,
        *,
        parent_id: str = "",
        metric_name: str = "",
        metric_value: Optional[float] = None,
        status: str = "pending",
        diff_ref: str = "",
    ) -> ProvNode:
        """Record an experiment.

        Args:
            hypothesis: The hypothesis being tested.
            parent_id: Optional parent experiment node id.
            metric_name: Optional metric being measured.
            metric_value: Optional observed value.
            status: Status (pending, running, complete, failed). Defaults to pending.
            diff_ref: Optional reference commit/version.

        Returns:
            The EXPERIMENT node.
        """
        if self._closed:
            raise ValueError("run is closed")

        from .schema import ProvEdge

        props: dict[str, Any] = {"status": status}
        if metric_name:
            props["metric_name"] = metric_name
        if metric_value is not None:
            props["metric_value"] = metric_value
        if diff_ref:
            props["diff_ref"] = diff_ref

        node = ProvNode(
            id=make_node_id(ProvNodeType.EXPERIMENT, hypothesis, self.tenant_id),
            node_type=ProvNodeType.EXPERIMENT,
            name=hypothesis,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties=props,
        )
        self._nodes.append(node)

        if parent_id:
            edge = ProvEdge(
                source_id=node.id,
                target_id=parent_id,
                edge_type=ProvEdgeType.PARENT_OF,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=self.agent_id,
            )
            self._edges.append(edge)

        self._maybe_autoflush()
        return node

    def entity(
        self,
        name: str,
        *,
        entity_type: str = "",
        description: str = "",
        aliases: tuple[str, ...] = (),
    ) -> ProvNode:
        """Record an entity (person, place, thing, concept).

        Args:
            name: Entity name.
            entity_type: Optional type (e.g., "person", "organization").
            description: Optional description.
            aliases: Optional tuple of alternate names.

        Returns:
            The ENTITY node.
        """
        if self._closed:
            raise ValueError("run is closed")

        props: dict[str, Any] = {}
        if entity_type:
            props["entity_type"] = entity_type
        if description:
            props["description"] = description
        if aliases:
            props["aliases"] = list(aliases)

        node = ProvNode(
            id=make_node_id(ProvNodeType.ENTITY, name, self.tenant_id),
            node_type=ProvNodeType.ENTITY,
            name=name,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties=props,
        )
        self._nodes.append(node)
        self._maybe_autoflush()
        return node

    def tool_call(
        self,
        tool_name: str,
        *,
        args_digest: str = "",
        ok: bool = True,
        duration_ms: float = 0.0,
    ) -> ProvNode:
        """Record a tool invocation.

        Recorded as an ARTIFACT node linked from the run (MODIFIED edge), so
        "which tools did this run use" is answerable later.

        Args:
            tool_name: Name of the tool.
            args_digest: Optional hash/digest of arguments.
            ok: Whether the call succeeded. Defaults to True.
            duration_ms: Execution time in milliseconds.

        Returns:
            The ARTIFACT node (tool call).
        """
        if self._closed:
            raise ValueError("run is closed")

        props: dict[str, Any] = {"tool_name": tool_name, "ok": ok}
        if args_digest:
            props["args_digest"] = args_digest
        if duration_ms:
            props["duration_ms"] = duration_ms

        node = ProvNode(
            id=make_node_id(ProvNodeType.ARTIFACT, tool_name, self.tenant_id),
            node_type=ProvNodeType.ARTIFACT,
            name=tool_name,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            properties=props,
        )
        self._nodes.append(node)

        # Link from run to tool call
        from .schema import ProvEdge

        run_node_id = make_node_id(ProvNodeType.RUN, self.run_id, self.tenant_id)
        edge = ProvEdge(
            source_id=run_node_id,
            target_id=node.id,
            edge_type=ProvEdgeType.MODIFIED,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )
        self._edges.append(edge)

        self._maybe_autoflush()
        return node

    def link(
        self,
        source_id: str,
        predicate: ProvEdgeType,
        target_id: str,
        *,
        source_doc: str = "",
        confidence: float = 1.0,
    ) -> Any:
        """Record a custom edge between two nodes.

        Args:
            source_id: Source node id.
            predicate: Edge type.
            target_id: Target node id.
            source_doc: Optional documentation/citation.
            confidence: Confidence 0..1. Defaults to 1.0.

        Returns:
            The ProvEdge instance.
        """
        if self._closed:
            raise ValueError("run is closed")

        from .schema import ProvEdge

        edge = ProvEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=predicate,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            source_doc=source_doc,
            confidence=confidence,
        )
        self._edges.append(edge)
        self._maybe_autoflush()
        return edge

    def flush(self) -> int:
        """Flush buffered nodes and edges to the spool.

        Runs check_invariants first. Any violations are logged at WARNING level
        without dropping data (the platform is the authority). Returns the
        SQLite rowid so the caller can poll publish status if needed.

        Returns:
            The spool rowid, or 0 on duplicate.

        Raises:
            ValueError: Run is closed.
        """
        if self._closed:
            raise ValueError("run is closed")

        if not self._nodes and not self._edges:
            return 0

        update = GraphUpdate(
            nodes=self._nodes,
            edges=self._edges,
            run_id=self.run_id,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            objective_id=getattr(self, "_objective_id", ""),
        )

        problems = check_invariants(update)
        for problem in problems:
            logger.warning("graph invariant violation: %s", problem)

        rowid = self.spool.enqueue(update)
        self._nodes.clear()
        self._edges.clear()
        return rowid

    def close(self, status: str = "complete", metadata: Optional[dict] = None) -> None:
        """Close the run and optionally record final status.

        Flushes any pending buffer. The RUN node in the spool now carries the
        final status. Safe to call multiple times (second call is no-op).

        Args:
            status: Final status (complete, failed, cancelled, etc).
            metadata: Optional dict of final metadata.
        """
        if self._closed:
            return

        self._status = status
        if metadata:
            self._metadata.update(metadata)

        # Optionally update the RUN node with final status
        if self._nodes and self._nodes[0].node_type == ProvNodeType.RUN:
            self._nodes[0].properties["status"] = status
            if metadata:
                self._nodes[0].properties["metadata"] = metadata

        self.flush()
        self._closed = True

    def _maybe_autoflush(self) -> None:
        """Flush if buffer exceeds autoflush threshold."""
        if len(self._nodes) + len(self._edges) >= self.autoflush:
            self.flush()

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        status = "failed" if exc_type else "complete"
        metadata = None
        if exc_type:
            metadata = {"exception": exc_type.__name__}
        self.close(status=status, metadata=metadata)

    async def __aenter__(self) -> RunRecorder:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.__exit__(exc_type, exc_val, exc_tb)


@contextmanager
def record_run(
    *,
    agent_id: str,
    tenant_id: str = "",
    objective: str = "",
    workspace_id: str = "",
    run_id: str = "",
    spool: Optional[Spool] = None,
) -> Iterator[RunRecorder]:
    """Context manager for recording a run.

    Sets the active run so library code can call record_claim(), etc. without
    passing the recorder around. The run is flushed to the spool on exit; on
    exception, the status is marked failed and the exception is NOT swallowed.

    Example::

        from adk.selfgraph import record_run

        with record_run(agent_id="myagent", objective="find answers") as run:
            run.claim("the answer is 42", sources=["doc.pdf"])
            for item in search_results:
                run.entity(item.name, description=item.desc)

    Args:
        agent_id: Required agent identifier (for attribution).
        tenant_id: Tenant id. Empty for on-prem.
        objective: Optional objective statement.
        workspace_id: Workspace id. Empty for single-tenant.
        run_id: Run id. If empty, generates one automatically.
        spool: Spool instance. If None, creates a default one.

    Yields:
        The RunRecorder instance.
    """
    if not _ENABLED:
        # Disabled: yield a no-op recorder
        yield RunRecorder(
            run_id=run_id or new_run_id(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            objective=objective,
            spool=spool,
        )
        return

    recorder = RunRecorder(
        run_id=run_id or new_run_id(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        objective=objective,
        spool=spool,
    )
    token = _current_run.set(recorder)
    try:
        with recorder:
            yield recorder
    finally:
        _current_run.reset(token)


# Convenience free functions (no-op safe when no active run)


def record_claim(statement: str, **kwargs: Any) -> Optional[ProvNode]:
    """Record a claim on the active run, if any.

    Same arguments as RunRecorder.claim(). No-op if no active run.

    Args:
        statement: The claim text.
        **kwargs: Passed to RunRecorder.claim().

    Returns:
        The CLAIM node, or None if no active run.
    """
    if not _ENABLED:
        return None
    run = current_run()
    if run is None:
        return None
    return run.claim(statement, **kwargs)


def record_artifact(name: str, **kwargs: Any) -> Optional[ProvNode]:
    """Record an artifact on the active run, if any.

    Same arguments as RunRecorder.artifact(). No-op if no active run.

    Args:
        name: Artifact name.
        **kwargs: Passed to RunRecorder.artifact().

    Returns:
        The ARTIFACT node, or None if no active run.
    """
    if not _ENABLED:
        return None
    run = current_run()
    if run is None:
        return None
    return run.artifact(name, **kwargs)


def record_tool_call(tool_name: str, **kwargs: Any) -> Optional[ProvNode]:
    """Record a tool call on the active run, if any.

    Same arguments as RunRecorder.tool_call(). No-op if no active run.

    Args:
        tool_name: Name of the tool.
        **kwargs: Passed to RunRecorder.tool_call().

    Returns:
        The ARTIFACT node (tool call), or None if no active run.
    """
    if not _ENABLED:
        return None
    run = current_run()
    if run is None:
        return None
    return run.tool_call(tool_name, **kwargs)


def record_open_question(text: str) -> Optional[ProvNode]:
    """Record an open question on the active run, if any.

    Same arguments as RunRecorder.open_question(). No-op if no active run.

    Args:
        text: The question.

    Returns:
        The OPEN_QUESTION node, or None if no active run.
    """
    if not _ENABLED:
        return None
    run = current_run()
    if run is None:
        return None
    return run.open_question(text)
