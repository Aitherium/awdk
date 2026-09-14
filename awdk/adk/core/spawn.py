"""Spawn-agent tool — lets an agent build and run sub-agents on demand.

This is the meta-agent primitive. Drop :func:`spawn_agent_tool` (or an
instance of :class:`SpawnAgentTool`) into an agent's toolset and it can
create child agents from a YAML/JSON spec, hand them a task, and get
the result back as an observation.

By default, child agents inherit the parent's tracer and start with the
*intersection* of (parent caps, requested caps). The parent cannot grant
caps it doesn't hold itself — same default-deny rule as everywhere else.

The :data:`Capability.AGENT_SPAWN` capability is required.
"""

from __future__ import annotations

import json
from typing import Any

from adk.core.agent import Agent
from adk.core.capability import (
    Capability,
    CapabilityContext,
    current_context,
)
from adk.core.loader import load_agent
from adk.core.tool import Tool, ToolResult


class SpawnAgentTool(Tool):
    """Spawn a sub-agent from a spec and run it once.

    Args (via call):
        spec: An agent spec dict OR a path to a YAML/JSON file.
        task: The prompt to hand to the child.
        inherit_tracer: If True (default), child shares the parent's tracer.
    """

    name = "spawn_agent"
    description = (
        "Spawn a sub-agent from a spec (dict or path) and run it on a task. "
        "Returns the child's final output."
    )
    requires = (Capability.AGENT_SPAWN,)

    def __init__(
        self,
        *,
        parent: Agent | None = None,
        registry: dict[str, dict[str, Any]] | None = None,
        max_depth: int = 3,
        _depth: int = 0,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._registry = registry or {}
        self._max_depth = max_depth
        self._depth = _depth

    async def call(
        self,
        spec: Any,
        task: str,
        inherit_tracer: bool = True,
    ) -> Any:
        if self._depth >= self._max_depth:
            return ToolResult.failure(
                f"max spawn depth {self._max_depth} reached"
            )

        resolved = self._resolve_spec(spec)
        if isinstance(resolved, ToolResult):
            return resolved
        if not isinstance(resolved, (dict, str)):
            return ToolResult.failure(
                f"spec must resolve to a dict or path, got {type(resolved).__name__}"
            )

        try:
            child = load_agent(resolved)
        except Exception as e:  # noqa: BLE001 — surface to model
            return ToolResult.failure(f"spec error: {type(e).__name__}: {e}")

        # Intersect requested caps with parent's caps (default-deny).
        parent_ctx = current_context()
        granted = CapabilityContext()
        for cap in child.capabilities:
            if cap in parent_ctx:
                granted.grant(cap)
        # Always grant LLM_INFERENCE if parent has it (agent already auto-grants).
        if Capability.LLM_INFERENCE in parent_ctx:
            granted.grant(Capability.LLM_INFERENCE)
        child._caps = granted  # noqa: SLF001 — intentional override

        # Give the child its own spawn tool one level deeper so it can recurse.
        if any(t.name == "spawn_agent" for t in child.tools):
            child.tools = [
                SpawnAgentTool(
                    parent=child,
                    registry=self._registry,
                    max_depth=self._max_depth,
                    _depth=self._depth + 1,
                )
                if t.name == "spawn_agent"
                else t
                for t in child.tools
            ]

        if inherit_tracer and self._parent is not None:
            child.tracer = self._parent.tracer

        result = await child.run(task)
        return ToolResult.success(
            {
                "output": result.output,
                "steps": result.steps,
                "finish_reason": result.finish_reason,
                "tool_calls": [
                    {"name": tc["name"], "ok": tc["result"].ok}
                    for tc in result.tool_calls
                ],
            }
        )

    def _resolve_spec(self, spec: Any) -> Any:
        if isinstance(spec, str):
            # Try registry lookup first; fall back to JSON parse.
            if spec in self._registry:
                return self._registry[spec]
            try:
                return json.loads(spec)
            except (ValueError, json.JSONDecodeError):
                # Treat as file path via loader on call site.
                from pathlib import Path

                if Path(spec).exists():
                    return spec  # loader handles path
                return ToolResult.failure(f"unknown spec or path: {spec!r}")
        return spec


def spawn_agent_tool(
    *,
    parent: Agent | None = None,
    registry: dict[str, dict[str, Any]] | None = None,
    max_depth: int = 3,
) -> SpawnAgentTool:
    """Convenience factory."""
    return SpawnAgentTool(parent=parent, registry=registry, max_depth=max_depth)
