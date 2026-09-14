"""Agent Registry — in-process registry of running agents.

Lightweight port of AitherOS CapabilityRegistry for standalone use.
No network, no heartbeat — just a dict of agent instances.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.registry")

_registry: AgentRegistry | None = None


class AgentRegistry:
    """In-process registry of running agents."""

    def __init__(self):
        self._agents: dict[str, AitherAgent] = {}

    def register(self, name: str, agent: AitherAgent) -> None:
        """Register an agent by name."""
        self._agents[name] = agent
        logger.info("Registered agent: %s", name)

    def unregister(self, name: str) -> None:
        """Remove an agent from the registry."""
        self._agents.pop(name, None)
        logger.info("Unregistered agent: %s", name)

    def get(self, name: str) -> AitherAgent | None:
        """Get an agent by name."""
        return self._agents.get(name)

    def list(self) -> list[dict]:
        """List all registered agents with metadata."""
        result = []
        for name, agent in self._agents.items():
            identity = agent._identity
            result.append({
                "name": name,
                "identity": identity.name,
                "description": identity.description,
                "skills": identity.skills,
                "tools": [t.name for t in agent._tools.list_tools()],
                "status": "running",
            })
        return result

    def route(self, task: str) -> str | None:
        """Route a task to the best agent using keyword matching.

        Simplified port of AtlasIntelligence.which_agent_for() — matches
        task keywords against agent descriptions and skills.
        """
        if not self._agents:
            return None

        task_lower = task.lower()
        best_name = None
        best_score = 0

        for name, agent in self._agents.items():
            identity = agent._identity
            score = 0

            # Match against skills
            for skill in identity.skills:
                if skill.lower() in task_lower:
                    score += 3

            # Match against description words
            if identity.description:
                for word in identity.description.lower().split():
                    if len(word) > 3 and word in task_lower:
                        score += 1

            # Match against agent name
            if name.lower() in task_lower:
                score += 5

            if score > best_score:
                best_score = score
                best_name = name

        # If no good match, return the first agent (orchestrator)
        if best_score == 0:
            return next(iter(self._agents))

        return best_name

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, name: str) -> bool:
        return name in self._agents


def get_registry() -> AgentRegistry:
    """Get the global agent registry singleton."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
