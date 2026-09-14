"""Fleet mode — multi-agent orchestration from YAML config.

Loads a fleet configuration, creates all agents, registers them
in the AgentRegistry, and wires up delegation tools.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from adk.agent import AitherAgent
from adk.config import Config
from adk.forge import AgentForge, ForgeSpec, get_forge
from adk.identity import Identity, load_identity
from adk.llm import LLMRouter
from adk.registry import AgentRegistry, get_registry
from adk.tools import ToolRegistry

logger = logging.getLogger("adk.fleet")


def load_fleet(
    path: str | Path | None = None,
    agent_names: list[str] | None = None,
    config: Config | None = None,
    llm: LLMRouter | None = None,
) -> FleetConfig:
    """Load a fleet from YAML file or a list of identity names.

    Usage:
        # From YAML
        fleet = load_fleet("fleet.yaml")

        # From agent names (CLI: --agents aither,lyra,demiurge)
        fleet = load_fleet(agent_names=["aither", "lyra", "demiurge"])
    """
    config = config or Config.from_env()

    if path:
        return FleetConfig.from_yaml(Path(path), config=config, llm=llm)

    if agent_names:
        return FleetConfig.from_names(agent_names, config=config, llm=llm)

    raise ValueError("Either path or agent_names must be provided")


class FleetConfig:
    """Fleet configuration — creates and manages multiple agents."""

    def __init__(
        self,
        name: str = "default-fleet",
        orchestrator: str = "",
        agents: list[AitherAgent] | None = None,
        registry: AgentRegistry | None = None,
        forge: AgentForge | None = None,
    ):
        # GATED: multi-agent fleet orchestration is a paid-tier capability.
        # Free (COMMUNITY) runtimes are single-agent. Raises LicenseError unless
        # entitled (or enforcement is disabled / tier is INTERNAL).
        try:
            from adk.licensing import get_license_manager
            get_license_manager().require("fleet", friendly="Fleet mode")
        except ImportError:
            pass

        self.name = name
        self.orchestrator_name = orchestrator
        self.agents = agents or []
        self.registry = registry or get_registry()
        self.forge = forge or get_forge()
        self.forge._registry = self.registry

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        config: Config | None = None,
        llm: LLMRouter | None = None,
    ) -> FleetConfig:
        """Load fleet from a YAML configuration file.

        Format:
            name: my-fleet
            orchestrator: aither
            agents:
              - identity: aither
              - identity: lyra
              - identity: demiurge
                tools: [search_web]
              - name: custom-agent
                system_prompt: "You are a custom agent..."
        """
        config = config or Config.from_env()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        fleet_name = data.get("name", path.stem)
        orchestrator = data.get("orchestrator", "")
        agent_defs = data.get("agents", [])

        registry = get_registry()
        agents = []

        for agent_def in agent_defs:
            if isinstance(agent_def, str):
                agent_def = {"identity": agent_def}

            identity_name = agent_def.get("identity", agent_def.get("name", "assistant"))
            agent_name = agent_def.get("name", identity_name)
            system_prompt = agent_def.get("system_prompt")

            agent = AitherAgent(
                name=agent_name,
                identity=identity_name if not system_prompt else None,
                llm=llm or LLMRouter(config=config),
                config=config,
                system_prompt=system_prompt,
            )
            agents.append(agent)
            registry.register(agent_name, agent)

        if not orchestrator and agents:
            orchestrator = agents[0].name

        fleet = cls(
            name=fleet_name,
            orchestrator=orchestrator,
            agents=agents,
            registry=registry,
        )
        fleet._wire_delegation_tools()
        return fleet

    @classmethod
    def from_names(
        cls,
        names: list[str],
        config: Config | None = None,
        llm: LLMRouter | None = None,
    ) -> FleetConfig:
        """Create a fleet from a list of identity names."""
        config = config or Config.from_env()
        registry = get_registry()
        agents = []

        for name in names:
            agent = AitherAgent(
                name=name,
                identity=name,
                llm=llm or LLMRouter(config=config),
                config=config,
            )
            agents.append(agent)
            registry.register(name, agent)

        orchestrator = names[0] if names else ""

        fleet = cls(
            name="cli-fleet",
            orchestrator=orchestrator,
            agents=agents,
            registry=registry,
        )
        fleet._wire_delegation_tools()
        return fleet

    def _wire_delegation_tools(self) -> None:
        """Add ask_agent and list_agents tools to all agents in the fleet."""
        registry = self.registry
        forge = self.forge

        for agent in self.agents:
            agent_name = agent.name

            # Create closures with correct binding
            def make_ask_agent(caller_name: str):
                async def ask_agent(agent_name: str, message: str) -> str:
                    """Delegate a task to another agent in the fleet. Use this to ask specialized agents for help."""
                    result = await forge.delegate(caller_name, agent_name, message)
                    if result.status == "completed":
                        return result.content
                    return f"[{result.status}] {result.error or 'Agent did not respond'}"
                return ask_agent

            def make_list_agents():
                def list_agents() -> str:
                    """List all available agents in the fleet and their specialties."""
                    agents_info = registry.list()
                    lines = []
                    for info in agents_info:
                        skills = ", ".join(info["skills"][:5]) if info["skills"] else "general"
                        lines.append(f"- {info['name']}: {info['description'] or info['identity']} (skills: {skills})")
                    return "\n".join(lines) if lines else "No agents registered."
                return list_agents

            agent._tools.register(make_ask_agent(agent_name), name="ask_agent")
            agent._tools.register(make_list_agents(), name="list_agents")

        logger.info("Fleet '%s' ready: %d agents, orchestrator=%s",
                     self.name, len(self.agents), self.orchestrator_name)

    def get_orchestrator(self) -> AitherAgent | None:
        """Get the orchestrator agent."""
        return self.registry.get(self.orchestrator_name)

    def get_agent(self, name: str) -> AitherAgent | None:
        """Get an agent by name."""
        return self.registry.get(name)
