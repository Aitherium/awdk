"""Aeon — Multi-Agent Group Chat for AitherADK.

Ports the core group chat pattern from AitherOS AitherAeon into a standalone
module that works with any LLM backend (Ollama, vLLM, cloud APIs).

Usage:
    from adk.aeon import AeonSession, group_chat

    # One-shot group chat
    response = await group_chat("Review this architecture", preset="technical")

    # Session-based (maintains history)
    session = AeonSession(preset="balanced")
    r1 = await session.chat("Design a REST API for user auth")
    r2 = await session.chat("Now add rate limiting")
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from adk.agent import AitherAgent, AgentResponse
from adk.config import Config
from adk.identity import load_identity
from adk.llm import LLMRouter

logger = logging.getLogger("adk.aeon")


# ─────────────────────────────────────────────────────────────────────────────
# Presets — curated agent combinations for common workflows
# ─────────────────────────────────────────────────────────────────────────────

AEON_PRESETS: dict[str, list[str]] = {
    "balanced":  ["atlas", "hydra", "aither"],
    "creative":  ["saga", "muse", "aither"],
    "technical": ["demiurge", "hydra", "aither"],
    "security":  ["athena", "atlas", "aither"],
    "minimal":   ["aither"],
    "duo_code":  ["demiurge", "aither"],
    "research":  ["lyra", "atlas", "aither"],
}

# Short descriptions for context injection (loaded from identity YAML on miss)
_AGENT_DESCRIPTIONS: dict[str, str] = {
    "aither": "System overseer — coordination, synthesis, delegation",
    "atlas": "Project management, research delegation, monitoring",
    "hydra": "Code review, quality assurance, testing",
    "demiurge": "Code generation, refactoring, technical implementation",
    "athena": "Security auditing, threat assessment, vulnerability analysis",
    "saga": "Documentation, knowledge base, technical writing",
    "muse": "Narrative storytelling, creative writing, art generation",
    "lyra": "Research, knowledge synthesis, source verification",
    "apollo": "Performance analysis, optimization, benchmarking",
    "morgana": "Secrets management, credential security",
    "prometheus": "Worldbuilding, simulation, procedural generation",
    "viviane": "Memory management, knowledge persistence",
    "vera": "Verification, validation, fact-checking",
    "hera": "Project governance, decision coordination",
    "themis": "Ethics, compliance, policy enforcement",
    "chaos": "Chaos engineering, resilience testing",
    "iris": "Visual generation, image creation, media",
}


def _get_description(agent_name: str) -> str:
    """Get agent description, falling back to identity YAML."""
    if agent_name in _AGENT_DESCRIPTIONS:
        return _AGENT_DESCRIPTIONS[agent_name]
    try:
        ident = load_identity(agent_name)
        desc = ident.description or ident.role
        _AGENT_DESCRIPTIONS[agent_name] = desc
        return desc
    except Exception:
        return agent_name


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AeonMessage:
    """A single message from an agent in the group chat."""
    agent: str
    content: str
    role: str = "assistant"
    timestamp: float = 0.0
    model: str = ""
    tokens_used: int = 0
    latency_ms: float = 0.0
    round_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "content": self.content,
            "role": self.role,
            "timestamp": self.timestamp,
            "model": self.model,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
            "round_number": self.round_number,
        }


@dataclass
class AeonResponse:
    """Response from a group chat round — one message per agent + optional synthesis."""
    messages: list[AeonMessage] = field(default_factory=list)
    synthesis: AeonMessage | None = None
    round_number: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [m.to_dict() for m in self.messages],
            "synthesis": self.synthesis.to_dict() if self.synthesis else None,
            "round_number": self.round_number,
            "total_tokens": self.total_tokens,
            "total_latency_ms": self.total_latency_ms,
            "session_id": self.session_id,
        }


# ─────────────────────────────────────────────────────────────────────────────
# AeonSession — the group chat engine
# ─────────────────────────────────────────────────────────────────────────────

class AeonSession:
    """Multi-agent group chat session.

    Creates one AitherAgent per participant, fires them in parallel (or serial
    for Ollama), and optionally synthesizes via the orchestrator.

    Args:
        participants: Explicit list of agent names. Overrides preset.
        preset: Name of a preset from AEON_PRESETS (default: "balanced").
        orchestrator: Agent that synthesizes responses (default: "aither").
        rounds: Number of discussion rounds per chat() call (default: 1).
        synthesize: Whether orchestrator produces a synthesis (default: True).
        llm: Shared LLMRouter for all agents. Auto-created if None.
        config: Config instance. Auto-created if None.
    """

    def __init__(
        self,
        participants: list[str] | None = None,
        preset: str = "balanced",
        orchestrator: str = "aither",
        rounds: int = 1,
        synthesize: bool = True,
        llm: LLMRouter | None = None,
        config: Config | None = None,
    ):
        self.config = config or Config.from_env()
        self.orchestrator = orchestrator
        self.rounds = max(1, rounds)
        self.synthesize = synthesize
        self.session_id = f"aeon-{uuid.uuid4().hex[:8]}"

        # Resolve participants from preset or explicit list
        if participants:
            self._participant_names = list(participants)
        elif preset in AEON_PRESETS:
            self._participant_names = list(AEON_PRESETS[preset])
        else:
            self._participant_names = list(AEON_PRESETS["balanced"])

        # Ensure orchestrator is present and last
        if self.orchestrator in self._participant_names:
            self._participant_names.remove(self.orchestrator)
        self._participant_names.append(self.orchestrator)

        # Shared LLM router for efficiency (one connection pool)
        self._shared_llm = llm or LLMRouter(config=self.config)

        # Create agents (lazy — only when first chat is called)
        self._agents: dict[str, AitherAgent] = {}

        # History across calls
        self._history: list[AeonMessage] = []
        self._round_counter = 0

    @property
    def participants(self) -> list[str]:
        """Current participant names (orchestrator is always last)."""
        return list(self._participant_names)

    @property
    def history(self) -> list[AeonMessage]:
        """Full message history for this session."""
        return list(self._history)

    def _ensure_agents(self) -> None:
        """Create AitherAgent instances for any participants not yet created."""
        for name in self._participant_names:
            if name not in self._agents:
                self._agents[name] = AitherAgent(
                    name=name,
                    identity=name,
                    llm=self._shared_llm,
                    config=self.config,
                    builtin_tools=False,
                )

    def _build_group_context(self, agent_name: str) -> str:
        """Build the [AEON GROUP CHAT] context block for an agent."""
        others = [
            n for n in self._participant_names
            if n != agent_name
        ]
        lines = [
            "[AEON GROUP CHAT]",
            "You are in a multi-agent discussion. Other participants:",
        ]
        for other in others:
            desc = _get_description(other)
            lines.append(f"- {other}: {desc}")
        lines.append("Respond from YOUR unique perspective. Don't repeat others. Be specific.")
        return "\n".join(lines)

    def _build_history_messages(self, agent_name: str) -> list[dict]:
        """Build the history messages for an agent, including group context."""
        msgs: list[dict] = []
        # Group context as first system message
        msgs.append({"role": "system", "content": self._build_group_context(agent_name)})
        # Prior messages from all participants
        for msg in self._history:
            prefix = f"[{msg.agent}]: " if msg.agent != "user" else ""
            msgs.append({
                "role": msg.role,
                "content": prefix + msg.content,
            })
        return msgs

    def _build_synthesis_prompt(self, responses: list[AeonMessage]) -> str:
        """Build the synthesis prompt for the orchestrator."""
        lines = [
            "[SYNTHESIS]",
            "Multiple agents responded. Synthesize into a coherent answer.",
            "Highlight consensus, note disagreements, add your own assessment.",
            "",
        ]
        for resp in responses:
            lines.append(f"[{resp.agent}]: {resp.content}")
        return "\n".join(lines)

    async def _fire_agent(
        self, agent_name: str, user_message: str, round_num: int,
    ) -> AeonMessage:
        """Fire a single agent and return its AeonMessage."""
        agent = self._agents[agent_name]
        history = self._build_history_messages(agent_name)

        start = time.perf_counter()
        try:
            resp: AgentResponse = await agent.chat(
                user_message,
                history=history,
                session_id=self.session_id,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            return AeonMessage(
                agent=agent_name,
                content=resp.content,
                role="assistant",
                timestamp=time.time(),
                model=resp.model,
                tokens_used=resp.tokens_used,
                latency_ms=elapsed_ms,
                round_number=round_num,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error("Agent %s failed: %s", agent_name, exc)
            return AeonMessage(
                agent=agent_name,
                content=f"[Error: {exc}]",
                role="assistant",
                timestamp=time.time(),
                latency_ms=elapsed_ms,
                round_number=round_num,
            )

    async def chat(self, message: str) -> AeonResponse:
        """Send a message to the group and get responses from all agents.

        Returns an AeonResponse with one message per non-orchestrator agent,
        plus an optional synthesis from the orchestrator.
        """
        self._ensure_agents()

        # Append user message to history
        user_msg = AeonMessage(
            agent="user",
            content=message,
            role="user",
            timestamp=time.time(),
        )
        self._history.append(user_msg)

        all_messages: list[AeonMessage] = []
        synthesis: AeonMessage | None = None

        for round_idx in range(self.rounds):
            self._round_counter += 1
            round_num = self._round_counter

            # Non-orchestrator agents
            non_orch = [
                n for n in self._participant_names
                if n != self.orchestrator
            ]

            if not non_orch:
                # Minimal preset — orchestrator is the only agent
                resp = await self._fire_agent(self.orchestrator, message, round_num)
                resp.round_number = round_num
                all_messages.append(resp)
                self._history.append(resp)
                continue

            # Detect if Ollama (serial) or vLLM/cloud (parallel)
            provider = self._shared_llm._provider_name
            if provider == "ollama":
                # Serial — Ollama serializes requests anyway
                round_responses = []
                for name in non_orch:
                    resp = await self._fire_agent(name, message, round_num)
                    round_responses.append(resp)
                    self._history.append(resp)
            else:
                # Parallel — vLLM, cloud, or unknown
                tasks = [
                    self._fire_agent(name, message, round_num)
                    for name in non_orch
                ]
                round_responses = list(await asyncio.gather(*tasks))
                for resp in round_responses:
                    self._history.append(resp)

            all_messages.extend(round_responses)

            # Orchestrator synthesis
            if self.synthesize:
                synth_prompt = self._build_synthesis_prompt(round_responses)
                synth_history = self._build_history_messages(self.orchestrator)
                synth_history.append({"role": "user", "content": synth_prompt})

                start = time.perf_counter()
                try:
                    synth_resp = await self._agents[self.orchestrator].chat(
                        synth_prompt,
                        history=synth_history,
                        session_id=self.session_id,
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    synthesis = AeonMessage(
                        agent=self.orchestrator,
                        content=synth_resp.content,
                        role="assistant",
                        timestamp=time.time(),
                        model=synth_resp.model,
                        tokens_used=synth_resp.tokens_used,
                        latency_ms=elapsed_ms,
                        round_number=round_num,
                    )
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.error("Orchestrator synthesis failed: %s", exc)
                    synthesis = AeonMessage(
                        agent=self.orchestrator,
                        content=f"[Synthesis error: {exc}]",
                        role="assistant",
                        timestamp=time.time(),
                        latency_ms=elapsed_ms,
                        round_number=round_num,
                    )
                self._history.append(synthesis)

        # Compute totals
        total_tokens = sum(m.tokens_used for m in all_messages)
        total_latency = sum(m.latency_ms for m in all_messages)
        if synthesis:
            total_tokens += synthesis.tokens_used
            total_latency += synthesis.latency_ms

        # Persist to ConversationStore
        try:
            from adk.conversations import get_conversation_store
            store = get_conversation_store()
            conv = await store.get_or_create(self.session_id, agent_name="aeon")
            conv.metadata["type"] = "aeon"
            conv.metadata["participants"] = self._participant_names
            # Append user message
            conv.messages.append({
                "role": "user",
                "content": message,
                "timestamp": user_msg.timestamp,
                "agent": "user",
            })
            # Append agent messages
            for m in all_messages:
                conv.messages.append({
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "agent": m.agent,
                    "round": m.round_number,
                })
            if synthesis:
                conv.messages.append({
                    "role": synthesis.role,
                    "content": synthesis.content,
                    "timestamp": synthesis.timestamp,
                    "agent": synthesis.agent,
                    "round": synthesis.round_number,
                    "is_synthesis": True,
                })
            conv.updated_at = time.time()
            store._save(conv)
        except Exception as exc:
            logger.debug("Failed to persist aeon session: %s", exc)

        return AeonResponse(
            messages=all_messages,
            synthesis=synthesis,
            round_number=self._round_counter,
            total_tokens=total_tokens,
            total_latency_ms=total_latency,
            session_id=self.session_id,
        )

    def reset(self) -> None:
        """Reset the session — clear history and generate a new session ID."""
        self._history.clear()
        self._round_counter = 0
        self.session_id = f"aeon-{uuid.uuid4().hex[:8]}"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function — one-shot group chat
# ─────────────────────────────────────────────────────────────────────────────

async def group_chat(
    message: str,
    participants: list[str] | None = None,
    preset: str = "balanced",
    rounds: int = 1,
    synthesize: bool = True,
    llm: LLMRouter | None = None,
) -> AeonResponse:
    """One-shot group chat: creates a session, fires one round, returns.

    Args:
        message: The user message.
        participants: Explicit agent list (overrides preset).
        preset: Preset name from AEON_PRESETS.
        rounds: Number of discussion rounds.
        synthesize: Whether to produce an orchestrator synthesis.
        llm: Shared LLMRouter (auto-created if None).

    Returns:
        AeonResponse with all agent messages and optional synthesis.
    """
    session = AeonSession(
        participants=participants,
        preset=preset,
        rounds=rounds,
        synthesize=synthesize,
        llm=llm,
    )
    return await session.chat(message)
