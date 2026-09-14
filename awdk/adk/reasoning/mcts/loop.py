"""MCTS-driven agent loop (experimental adapter).

``MctsPlanLoop`` is a thin :class:`adk.core.agent.AgentLoop` implementation
that plans a *tool ordering* for a prompt by running :class:`UnifiedMCTS` over
a :class:`ToolChainEnv` built from ``agent.tools``, then executes the winning
action path.

Status: EXPERIMENTAL STUB. The :class:`AgentLoop` protocol
(``async run(agent, prompt) -> AgentResult``) is clear, but a *good* tool
environment needs per-tool argument synthesis and a real terminal-value
signal, which are out of scope for the core library port. So:

* :class:`ToolChainEnv` uses **tool names** as actions and a placeholder
  ``evaluate()`` (coverage of distinct tools, capped) — enough to exercise the
  engine, not enough to be a production planner.
* Execution runs each planned tool with **empty arguments**, best-effort,
  wrapped in try/except. Wire a real arg-synthesis step before relying on it.

Imports of ``adk.core`` are guarded so importing this module never breaks the
package if the agent surface changes.
"""

from __future__ import annotations

import copy
from typing import Any, List, Optional

from .core import MCTSConfig, UnifiedMCTS

try:  # pragma: no cover - guarded against agent-surface drift
    from adk.core.agent import Agent, AgentResult
    from adk.core.tool import Tool

    _AGENT_OK = True
except Exception:  # noqa: BLE001
    Agent = Any  # type: ignore[assignment,misc]
    AgentResult = Any  # type: ignore[assignment,misc]
    Tool = Any  # type: ignore[assignment,misc]
    _AGENT_OK = False


class ToolChainEnv:
    """A minimal MCTSEnvironment whose actions are tool names.

    State is the ordered tuple of tool names invoked so far. This is a
    placeholder value surface (see module docstring) — swap ``evaluate`` for a
    task-grounded signal to make it a real planner.
    """

    def __init__(
        self,
        tool_names: List[str],
        *,
        max_len: int = 6,
        chosen: Optional[tuple] = None,
    ) -> None:
        self._tools = list(tool_names)
        self._max_len = max(1, max_len)
        self._chosen: tuple = chosen or ()

    def get_state_hash(self) -> int:
        return hash(self._chosen)

    def get_actions(self) -> List[str]:
        if len(self._chosen) >= self._max_len:
            return []
        # Allow repeats-free ordering; still return all tools so the search
        # can consider each at least once.
        return [t for t in self._tools if t not in self._chosen] or []

    def step(self, action: str) -> tuple:
        # reward: small positive for adding a new distinct tool.
        prev = len(set(self._chosen))
        self._chosen = self._chosen + (action,)
        gained = len(set(self._chosen)) - prev
        reward = 0.2 if gained else 0.0
        done = len(self._chosen) >= self._max_len or not self.get_actions()
        return (self._chosen, reward, done)

    def evaluate(self) -> float:
        if not self._tools:
            return 0.0
        return min(1.0, len(set(self._chosen)) / len(self._tools))

    def clone(self) -> "ToolChainEnv":
        return ToolChainEnv(
            self._tools, max_len=self._max_len, chosen=self._chosen
        )


class MctsPlanLoop:
    """AgentLoop that searches a tool ordering, then runs it (best-effort)."""

    def __init__(
        self,
        *,
        config: Optional[MCTSConfig] = None,
        max_plan_len: int = 6,
        execute: bool = True,
    ) -> None:
        self.config = config or MCTSConfig(iterations=64)
        self.max_plan_len = max_plan_len
        self.execute = execute

    async def run(self, agent: "Agent", prompt: str) -> "AgentResult":
        if not _AGENT_OK:  # pragma: no cover
            raise RuntimeError(
                "MctsPlanLoop requires adk.core.agent; import failed at load time."
            )

        tools_by_name = {t.name: t for t in getattr(agent, "tools", [])}
        env = ToolChainEnv(list(tools_by_name), max_len=self.max_plan_len)

        engine = UnifiedMCTS(self.config)
        result = await engine.search(env)
        plan: List[str] = [str(a) for a in result.best_action_path] or (
            [str(result.best_action)] if result.best_action else []
        )

        tool_calls: List[dict] = []
        if self.execute:
            for step_i, name in enumerate(plan, 1):
                tool = tools_by_name.get(name)
                if tool is None:
                    continue
                try:
                    res = await tool()  # empty-arg best-effort (stub)
                except Exception as exc:  # noqa: BLE001
                    res = f"{type(exc).__name__}: {exc}"
                tool_calls.append({"step": step_i, "name": name, "result": res})

        output = (
            "MCTS plan: " + " -> ".join(plan)
            if plan
            else "MCTS plan: (no tools to order)"
        )
        return AgentResult(
            output=output,
            tool_calls=tool_calls,
            steps=len(plan),
            finish_reason="mcts_plan",
        )


__all__ = ["MctsPlanLoop", "ToolChainEnv"]
