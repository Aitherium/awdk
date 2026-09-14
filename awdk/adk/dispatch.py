"""Multi-agent dispatch via A2A protocol — local runtime.

Enables a local adk runtime to dispatch N subtasks to remote A2A-capable
agents in parallel, collect results, and synthesize. Bounded by recursion
depth and fan-out ceiling to prevent fork-bomb.

This is the **sovereign local alternative** to genesis swarm dispatch.
It uses A2A client primitives (invoke_skill, send_message) to reach
remote agents without touching genesis.

Usage::

    from adk.dispatch import MultiAgentDispatcher, DispatchSpec

    dispatcher = MultiAgentDispatcher()

    spec = DispatchSpec(
        subtasks=[
            ("research-agent", "Summarize the AWS pricing model"),
            ("write-agent", "Write a blog post about cloud costs"),
        ],
        main_task="Create marketing content about AWS",
        effort_level=7,
    )

    result = await dispatcher.dispatch(spec)
    print(f"Synthesis: {result.synthesis}")
    for task_name, task_result in result.task_results.items():
        print(f"  {task_name}: {task_result.content}")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.dispatch")

# Recursion depth safety — prevent a cascade of dispatches from spiraling.
# If an agent calls dispatch, which calls dispatch again, we stop at depth 3.
_MAX_DISPATCH_DEPTH = 3

# Max fan-out ceiling — prevent one dispatch from spawning 100+ tasks.
# Can be per-agent or global; this is the global hard ceiling.
_MAX_FAN_OUT = 20

# TODO(D-XXXX): Tier gate — PROFESSIONAL+ only. Stub for now; lands with
# entitlements system final integration. Once landed, move this to
# adk.licensing check_entitlement("swarm") as fail-closed.
_REQUIRE_PROFESSIONAL_TIER = False


@dataclass
class TaskResult:
    """Result from a single subtask dispatch."""

    task_name: str
    agent_name: str
    success: bool
    content: str = ""
    error: Optional[str] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    via: str = "a2a"  # "a2a" or "fallback"


@dataclass
class DispatchSpec:
    """Specification for a multi-agent dispatch."""

    subtasks: List[tuple[str, str]]  # [(agent_name, task_description), ...]
    main_task: str  # High-level context for the whole dispatch
    effort_level: int = 7
    context: Dict[str, Any] = field(default_factory=dict)
    timeout_per_task: float = 60.0  # Seconds per subtask
    max_fan_out: Optional[int] = None  # Override global ceiling
    recursion_depth: int = 0  # Incremented on each dispatch() call


@dataclass
class DispatchResult:
    """Result from a dispatch operation."""

    success: bool
    task_results: Dict[str, TaskResult] = field(default_factory=dict)
    synthesis: str = ""  # Synthesized summary of all task results
    error: Optional[str] = None
    elapsed_ms: float = 0.0
    via: str = "mixed"  # "a2a", "fallback", or "mixed"


class MultiAgentDispatcher:
    """Dispatches N subtasks to A2A-capable agents in parallel.

    Features:
      - Parallel fan-out with bounded concurrency
      - Graceful degradation when A2A unavailable (fallback to local LLM)
      - Result synthesis via the dispatcher's own agent
      - Recursion depth tracking (max 3 levels)
      - Licensing tier gate (PROFESSIONAL+, stub for now)
    """

    def __init__(self, agent=None):
        """Initialize the dispatcher.

        Args:
            agent: Optional AitherAgent instance. If provided, used for
                   result synthesis. If None, synthesis is skipped.
        """
        self._agent = agent
        self._dispatch_depth = 0

    async def dispatch(self, spec: DispatchSpec) -> DispatchResult:
        """Dispatch N subtasks to agents and collect results.

        Args:
            spec: DispatchSpec with subtasks, main task, effort level, etc.

        Returns:
            DispatchResult with per-task results and synthesis.
        """
        start = time.perf_counter()

        # Recursion depth check
        if spec.recursion_depth >= _MAX_DISPATCH_DEPTH:
            logger.warning(
                "dispatch: max recursion depth (%d) reached",
                _MAX_DISPATCH_DEPTH,
            )
            return DispatchResult(
                success=False,
                error=f"Max recursion depth ({_MAX_DISPATCH_DEPTH})"
                " reached — nested dispatch too deep",
            )

        # Fan-out ceiling
        max_fan = spec.max_fan_out or _MAX_FAN_OUT
        if len(spec.subtasks) > max_fan:
            logger.warning(
                "dispatch: fan-out %d exceeds ceiling %d, truncating",
                len(spec.subtasks), max_fan,
            )
            spec.subtasks = spec.subtasks[:max_fan]

        # Tier gate (stub; TODO: integrate with licensing.check_entitlement)
        if _REQUIRE_PROFESSIONAL_TIER:
            try:
                from adk.licensing import Tier, get_license
                license_obj = get_license()
                if license_obj.tier.value not in ("professional", "enterprise",
                                                    "sovereign", "internal"):
                    logger.warning(
                        "dispatch: tier %s does not permit swarm dispatch"
                        " (PROFESSIONAL+ required)",
                        license_obj.tier.value,
                    )
                    return DispatchResult(
                        success=False,
                        error="Swarm dispatch requires PROFESSIONAL+ tier",
                    )
            except Exception as e:
                logger.debug(
                    "dispatch: licensing check error (proceeding): %s", e
                )

        # Dispatch subtasks in parallel
        task_results: Dict[str, TaskResult] = {}
        try:
            results = await asyncio.gather(
                *[
                    self._dispatch_subtask(
                        agent_name, task_desc, spec, i
                    )
                    for i, (agent_name, task_desc) in enumerate(spec.subtasks)
                ],
                return_exceptions=True,
            )

            for i, (agent_name, _), result in zip(
                range(len(spec.subtasks)), spec.subtasks, results
            ):
                if isinstance(result, Exception):
                    logger.error(
                        "dispatch: subtask %d (%s) raised: %s",
                        i, agent_name, result,
                    )
                    task_results[f"task_{i}"] = TaskResult(
                        task_name=f"task_{i}",
                        agent_name=agent_name,
                        success=False,
                        error=str(result),
                    )
                else:
                    task_results[f"task_{i}"] = result

        except Exception as e:
            logger.error("dispatch: parallel gather failed: %s", e)
            return DispatchResult(
                success=False,
                task_results=task_results,
                error=f"Parallel dispatch failed: {e}",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

        # Synthesize results
        synthesis = await self._synthesize(spec, task_results)

        elapsed_ms = (time.perf_counter() - start) * 1000
        success = all(tr.success for tr in task_results.values())

        return DispatchResult(
            success=success,
            task_results=task_results,
            synthesis=synthesis,
            elapsed_ms=elapsed_ms,
            via="a2a" if all(
                tr.via == "a2a" for tr in task_results.values()
            ) else "mixed",
        )

    async def _dispatch_subtask(
        self,
        agent_name: str,
        task_desc: str,
        spec: DispatchSpec,
        index: int,
    ) -> TaskResult:
        """Dispatch a single subtask to an agent."""
        start = time.perf_counter()
        task_name = f"task_{index}"

        # Build task context
        task_context = {
            **(spec.context or {}),
            "main_task": spec.main_task,
            "subtask_index": index,
            "subtask_total": len(spec.subtasks),
        }

        try:
            # Try A2A first
            from adk.a2a_client import send_message

            result = await asyncio.wait_for(
                send_message(
                    agent_name=agent_name,
                    text=task_desc,
                    this_agent_name="adk-dispatcher",
                    timeout=int(spec.timeout_per_task),
                ),
                timeout=spec.timeout_per_task + 5.0,
            )

            # Parse A2A response
            if result and isinstance(result, dict):
                if "error" in result:
                    logger.warning(
                        "dispatch: A2A to %s failed: %s",
                        agent_name, result.get("message"),
                    )
                    # Fallback to local synthesis
                    return await self._fallback_subtask(
                        agent_name, task_desc, spec, index
                    )

                # Extract content from A2A task result
                if "task" in result:
                    task_obj = result["task"]
                    history = task_obj.get("history", [])
                    content = ""
                    for msg in history:
                        if msg.get("role") == "agent":
                            for part in msg.get("parts", []):
                                if part.get("type") == "text":
                                    content += part.get("text", "") + "\n"
                    content = content.strip()

                    if content:
                        return TaskResult(
                            task_name=task_name,
                            agent_name=agent_name,
                            success=True,
                            content=content,
                            via="a2a",
                            elapsed_ms=(time.perf_counter() - start) * 1000,
                        )

            # A2A succeeded but no content — fallback
            return await self._fallback_subtask(
                agent_name, task_desc, spec, index
            )

        except asyncio.TimeoutError:
            logger.warning(
                "dispatch: A2A to %s timed out after %.1fs",
                agent_name, spec.timeout_per_task,
            )
            # Fallback on timeout
            return await self._fallback_subtask(
                agent_name, task_desc, spec, index
            )
        except Exception as e:
            logger.warning(
                "dispatch: A2A to %s failed: %s (falling back)",
                agent_name, e,
            )
            # Fallback on any A2A error
            return await self._fallback_subtask(
                agent_name, task_desc, spec, index
            )

    async def _fallback_subtask(
        self,
        agent_name: str,
        task_desc: str,
        spec: DispatchSpec,
        index: int,
    ) -> TaskResult:
        """Fallback subtask: use local LLM if agent unavailable."""
        start = time.perf_counter()
        task_name = f"task_{index}"

        # Use local agent if available
        if self._agent:
            try:
                # Build enriched prompt
                prompt = (
                    f"You are substituting for the '{agent_name}' agent. "
                    f"Main task: {spec.main_task}\n\n"
                    f"Your subtask: {task_desc}"
                )

                resp = await self._agent.chat(prompt)
                if resp:
                    return TaskResult(
                        task_name=task_name,
                        agent_name=agent_name,
                        success=True,
                        content=resp.content,
                        via="fallback",
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
            except Exception as e:
                logger.debug(
                    "dispatch: local agent fallback failed: %s", e
                )

        # No fallback available
        return TaskResult(
            task_name=task_name,
            agent_name=agent_name,
            success=False,
            error=f"Agent '{agent_name}' unavailable and no fallback",
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )

    async def _synthesize(
        self,
        spec: DispatchSpec,
        task_results: Dict[str, TaskResult],
    ) -> str:
        """Synthesize all task results into a coherent summary."""
        if not self._agent:
            # No agent, return concatenated results
            parts = []
            for task_name, result in task_results.items():
                if result.success:
                    parts.append(f"## {task_name}\n{result.content}")
                else:
                    parts.append(
                        f"## {task_name}\n**Error:** {result.error}"
                    )
            return "\n\n".join(parts)

        # Use agent to synthesize
        try:
            synthesis_prompt = self._build_synthesis_prompt(
                spec, task_results
            )

            resp = await self._agent.chat(synthesis_prompt)
            if resp:
                return resp.content

            # Fallback if synthesis fails
            return self._fallback_synthesis(spec, task_results)

        except Exception as e:
            logger.warning("dispatch: synthesis via agent failed: %s", e)
            return self._fallback_synthesis(spec, task_results)

    @staticmethod
    def _build_synthesis_prompt(
        spec: DispatchSpec,
        task_results: Dict[str, TaskResult],
    ) -> str:
        """Build the synthesis prompt."""
        parts = [f"## Main Task\n{spec.main_task}\n"]

        for task_name, result in task_results.items():
            if result.success:
                parts.append(
                    f"## {result.agent_name} ({task_name})\n{result.content}"
                )
            else:
                parts.append(
                    f"## {result.agent_name} ({task_name})\n"
                    f"**Failed:** {result.error}"
                )

        synthesis_task = (
            "Synthesize the above subtask results into a coherent, "
            "integrated response to the main task. If any subtask failed, "
            "explain the impact. Preserve critical details from each result."
        )

        return "\n\n".join(parts) + f"\n\n{synthesis_task}"

    @staticmethod
    def _fallback_synthesis(
        spec: DispatchSpec,
        task_results: Dict[str, TaskResult],
    ) -> str:
        """Fallback synthesis when agent unavailable."""
        parts = [f"# {spec.main_task}\n"]

        successful = [tr for tr in task_results.values() if tr.success]
        failed = [tr for tr in task_results.values() if not tr.success]

        if successful:
            parts.append("## Results")
            for result in successful:
                parts.append(f"### {result.agent_name}\n{result.content}")

        if failed:
            parts.append("## Failures")
            for result in failed:
                parts.append(
                    f"### {result.agent_name}\n{result.error}"
                )

        return "\n\n".join(parts)
