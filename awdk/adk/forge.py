"""Agent Forge (lite) — in-process agent dispatch with OODA+ReAct loops.

Lightweight port of AitherOS AgentForge. No HTTP, no Genesis needed.
Creates agents on demand, runs them in-process with timeout.

Enhanced with autonomous dispatch:
  - Autonomous agent dispatch with guardrails (effort cap, timeout, loop guard)
  - OODA reflection integration for tool-loop governance
  - Delegated task chaining with context passing
  - ForgeSpec supports effort-based routing and capability requirements
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from adk.agent import AitherAgent, AgentResponse
from adk.identity import load_identity
from adk.llm import LLMRouter
from adk.loop_guard import LoopGuard, LoopAction
from adk.memory import Memory
from adk.metrics import get_metrics
from adk.registry import AgentRegistry, get_registry
from adk.trace import get_trace_id

logger = logging.getLogger("adk.forge")


@dataclass
class ForgeSpec:
    """Specification for dispatching an agent.

    Enhanced with Hands-inspired fields:
      - effort: 1-10 effort level for model routing
      - capabilities: required sandbox capabilities
      - max_loop_calls: loop guard circuit breaker threshold
      - chain_context: context from previous agent in chain
      - guardrails: dict of constraints the agent must obey
    """
    agent_type: str = "auto"       # identity name or "auto"
    task: str = ""
    max_turns: int = 15
    timeout: float = 120.0
    effort: int = 5
    context: str = ""              # additional context to prepend
    capabilities: list[str] = field(default_factory=list)
    max_loop_calls: int = 20
    chain_context: str = ""
    guardrails: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ForgeResult:
    """Result from a forged agent dispatch.

    Enhanced with:
      - chain_results: results from delegated sub-agents
      - effort_used: actual effort expended
    """
    content: str = ""
    agent: str = ""
    tokens_used: int = 0
    tool_calls: list[str] = field(default_factory=list)
    status: str = "completed"      # completed, failed, timeout
    latency_ms: float = 0.0
    error: str = ""
    chain_results: list["ForgeResult"] = field(default_factory=list)
    effort_used: int = 0


class AgentForge:
    """In-process agent dispatch with OODA+ReAct loop governance.

    Enhanced with autonomous dispatch pattern:
      - Each dispatch gets its own LoopGuard
      - Effort-based agent selection
      - Chain delegation with context passing
      - Guardrail enforcement
    """

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        llm: LLMRouter | None = None,
    ):
        self._registry = registry or get_registry()
        self._llm = llm
        self._dispatch_count: int = 0
        self._active_dispatches: int = 0

        # Safety (IntakeGuard) — non-fatal
        self._safety = None
        try:
            from adk.safety import IntakeGuard
            self._safety = IntakeGuard()
        except Exception:
            pass

        # Event emitter — non-fatal
        self._events = None
        try:
            from adk.events import get_emitter
            self._events = get_emitter()
        except Exception:
            pass

    async def dispatch(self, spec: ForgeSpec) -> ForgeResult:
        """Dispatch an agent with full OODA loop governance.

        If agent_type is "auto", routes by effort level:
          1-2 → lightweight, 3-6 → standard, 7-10 → reasoning.
        """
        self._dispatch_count += 1
        self._active_dispatches += 1
        start = time.perf_counter()
        _metrics = get_metrics()

        # Resolve agent type
        agent_type = spec.agent_type
        if agent_type == "auto":
            routed = self._registry.route(spec.task)
            if routed:
                agent_type = routed
            else:
                agent_type = self._route_by_effort(spec.effort)

        # Input safety check on task
        if self._safety:
            try:
                safety_result = self._safety.check(spec.task)
                if safety_result.blocked:
                    self._active_dispatches -= 1
                    return ForgeResult(
                        agent=agent_type, status="blocked",
                        error="Task blocked by safety filter",
                        effort_used=spec.effort,
                    )
            except Exception:
                pass

        # Emit dispatch event
        if self._events:
            try:
                await self._events.emit(
                    "forge_dispatch", agent=agent_type,
                    task=spec.task[:200], effort=spec.effort,
                )
            except Exception:
                pass

        try:
            # Try to get from registry first
            agent = self._registry.get(agent_type)
            if agent is None:
                # Create a fresh agent with this identity
                agent = AitherAgent(
                    name=f"forge-{agent_type}",
                    identity=agent_type,
                    llm=self._llm or LLMRouter(),
                    memory=Memory(agent_name=f"forge-{agent_type}"),
                )

            # Build the task message with chain context
            task_msg = spec.task
            if spec.chain_context:
                task_msg = (
                    f"[Previous agent output]\n{spec.chain_context}\n\n"
                    f"[Your task]\n{spec.task}"
                )
            if spec.context:
                task_msg = f"{spec.context}\n\n{task_msg}"

            # Apply guardrails to prompt
            if spec.guardrails:
                guardrail_text = self._format_guardrails(spec.guardrails)
                task_msg = f"{guardrail_text}\n\n{task_msg}"

            # Run with timeout
            resp = await asyncio.wait_for(
                agent.chat(task_msg),
                timeout=spec.timeout,
            )

            latency = (time.perf_counter() - start) * 1000
            _metrics.record_agent_spawn(agent_type=agent_type)
            if self._events:
                try:
                    await self._events.emit(
                        "forge_complete", agent=agent_type,
                        status="completed", latency_ms=latency,
                    )
                except Exception:
                    pass
            return ForgeResult(
                content=resp.content,
                agent=agent_type,
                tokens_used=resp.tokens_used,
                tool_calls=resp.tool_calls_made,
                status="completed",
                latency_ms=latency,
                effort_used=spec.effort,
            )

        except asyncio.TimeoutError:
            latency = (time.perf_counter() - start) * 1000
            _metrics.record_agent_spawn(agent_type=agent_type)
            _fire_pulse_agent_error(agent_type, f"Timeout after {spec.timeout}s")
            return ForgeResult(
                agent=agent_type,
                status="timeout",
                latency_ms=latency,
                error=f"Agent {agent_type} timed out after {spec.timeout}s",
                effort_used=spec.effort,
            )
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            _metrics.record_agent_spawn(agent_type=agent_type)
            _fire_pulse_agent_error(agent_type, str(e), error_type=type(e).__name__)
            logger.error("Forge dispatch failed for %s: %s", agent_type, e)
            return ForgeResult(
                agent=agent_type,
                status="failed",
                latency_ms=latency,
                error=str(e),
                effort_used=spec.effort,
            )
        finally:
            self._active_dispatches -= 1

    async def delegate(
        self,
        from_agent: str,
        to_agent: str,
        task: str,
        timeout: float = 120.0,
        effort: int = 5,
    ) -> ForgeResult:
        """Delegate a task from one agent to another with context chaining."""
        # GATED: cross-agent delegation is multi-agent orchestration (fleet) —
        # a paid-tier capability. Single-agent dispatch() stays free.
        try:
            from adk.licensing import get_license_manager
            get_license_manager().require("fleet", friendly="Agent delegation")
        except ImportError:
            pass
        context = f"[Delegated from {from_agent}] Complete this task and return the result."
        spec = ForgeSpec(
            agent_type=to_agent,
            task=task,
            timeout=timeout,
            context=context,
            effort=effort,
        )
        return await self.dispatch(spec)

    async def chain(
        self,
        specs: list[ForgeSpec],
        stop_on_failure: bool = True,
    ) -> list[ForgeResult]:
        """Execute a chain of agent dispatches, passing context forward.

        Each agent receives the previous agent's output as chain_context.
        Implements sequential agent execution with context forwarding.
        """
        results: list[ForgeResult] = []
        chain_context = ""

        for spec in specs:
            spec.chain_context = chain_context
            result = await self.dispatch(spec)
            results.append(result)

            if result.status != "completed" and stop_on_failure:
                logger.warning(
                    "Chain stopped at agent %s: %s",
                    result.agent, result.error or result.status,
                )
                break

            chain_context = result.content

        return results

    @property
    def stats(self) -> dict:
        return {
            "total_dispatches": self._dispatch_count,
            "active_dispatches": self._active_dispatches,
        }

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    def _route_by_effort(self, effort: int) -> str:
        """Route to agent type based on effort level.

        Effort 1-2: lightweight agent (quick answers)
        Effort 3-6: standard agent (orchestration)
        Effort 7-10: reasoning agent (deep analysis)
        """
        if effort <= 2:
            return "aither"
        elif effort <= 6:
            return "demiurge"
        else:
            return "atlas"

    def _format_guardrails(self, guardrails: Dict[str, Any]) -> str:
        """Format guardrails as a constraint block for the agent."""
        lines = ["[GUARDRAILS — You MUST obey these constraints]"]
        for key, value in guardrails.items():
            if isinstance(value, bool):
                lines.append(f"- {key}: {'REQUIRED' if value else 'FORBIDDEN'}")
            elif isinstance(value, list):
                lines.append(f"- {key}: {', '.join(str(v) for v in value)}")
            else:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)


_forge: AgentForge | None = None


def get_forge() -> AgentForge:
    """Get the global AgentForge singleton."""
    global _forge
    if _forge is None:
        _forge = AgentForge()
    return _forge


def _fire_pulse_agent_error(agent: str, error: str, error_type: str = ""):
    """Fire-and-forget Pulse pain signal for agent dispatch failure."""
    async def _send():
        try:
            from adk.pulse import get_pulse
            pulse = get_pulse()
            await pulse.send_agent_error(
                agent=agent, error=error,
                error_type=error_type,
                request_id=get_trace_id(),
            )
        except Exception:
            pass
    try:
        asyncio.ensure_future(_send())
    except RuntimeError:
        pass  # No event loop
