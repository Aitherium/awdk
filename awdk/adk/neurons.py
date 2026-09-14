"""Neuron architecture — auto-firing context gathering before LLM calls.

Simplified port of AitherOS's 30-neuron pool. Provides pattern-based
detection of what context the query needs, then auto-fires relevant
neurons in parallel before the LLM call.

Core neurons:
  - WebSearchNeuron — DuckDuckGo search for current/factual queries
  - MemoryNeuron   — Agent memory search for recall queries
  - GraphNeuron    — Knowledge graph search for relational queries

Usage:
    from adk.neurons import NeuronPool, AutoNeuronFire

    pool = NeuronPool(agent)
    auto = AutoNeuronFire(pool)

    # Auto-detect and fire relevant neurons
    context = await auto.gather_context("What's the latest news about AI?")
    # Returns: "[WEB SEARCH]\n- AI news result 1\n..."
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adk.agent import AitherAgent

logger = logging.getLogger("adk.neurons")


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NeuronResult:
    """Result from a neuron firing."""
    neuron: str
    content: str
    relevance: float = 0.5
    latency_ms: float = 0.0
    source: str = ""
    metadata: dict = field(default_factory=dict)


class BaseNeuron(ABC):
    """Abstract base for all neurons."""

    name: str = "base"
    description: str = ""

    @abstractmethod
    async def fire(self, query: str, **kwargs) -> NeuronResult:
        """Execute the neuron and return results."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Concrete neurons
# ─────────────────────────────────────────────────────────────────────────────

class WebSearchNeuron(BaseNeuron):
    """Search the web via DuckDuckGo (no API key needed)."""

    name = "web_search"
    description = "Search the web for current information"

    def __init__(self, limit: int = 3):
        self._limit = limit

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        start = time.perf_counter()
        try:
            from adk.builtin_tools import web_search
            raw = await web_search(query, limit=self._limit)
            data = json.loads(raw)
            if "error" in data:
                return NeuronResult(
                    neuron=self.name, content="", relevance=0.0,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            lines = []
            for r in data.get("results", []):
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if title or snippet:
                    lines.append(f"- {title}: {snippet[:200]}")
            content = "\n".join(lines) if lines else ""
            return NeuronResult(
                neuron=self.name, content=content,
                relevance=0.8 if content else 0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
                source="duckduckgo",
            )
        except Exception as e:
            logger.debug("WebSearchNeuron failed: %s", e)
            return NeuronResult(
                neuron=self.name, content="", relevance=0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
            )


class MemoryNeuron(BaseNeuron):
    """Search agent's conversation memory."""

    name = "memory"
    description = "Search conversation history and stored memories"

    def __init__(self, agent: AitherAgent | None = None):
        self._agent = agent

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        start = time.perf_counter()
        agent = kwargs.get("agent", self._agent)
        if not agent:
            return NeuronResult(neuron=self.name, content="", relevance=0.0)

        try:
            results = await agent.memory.search(query, limit=5)
            if not results:
                return NeuronResult(
                    neuron=self.name, content="", relevance=0.0,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            lines = [f"- {r}" for r in results[:5] if r]
            content = "\n".join(lines)
            return NeuronResult(
                neuron=self.name, content=content,
                relevance=0.7 if content else 0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
                source="memory",
            )
        except Exception as e:
            logger.debug("MemoryNeuron failed: %s", e)
            return NeuronResult(
                neuron=self.name, content="", relevance=0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
            )


class GraphNeuron(BaseNeuron):
    """Search the agent's knowledge graph."""

    name = "graph"
    description = "Search knowledge graph for entities and relationships"

    def __init__(self, agent: AitherAgent | None = None):
        self._agent = agent

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        start = time.perf_counter()
        agent = kwargs.get("agent", self._agent)
        if not agent or not getattr(agent, '_graph', None):
            return NeuronResult(neuron=self.name, content="", relevance=0.0)

        try:
            nodes = await agent._graph.search(query, limit=5)
            if not nodes:
                return NeuronResult(
                    neuron=self.name, content="", relevance=0.0,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
            lines = [
                f"- {n.label}: {n.content[:150]}"
                for n in nodes if n.content
            ]
            content = "\n".join(lines)
            return NeuronResult(
                neuron=self.name, content=content,
                relevance=0.8 if content else 0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
                source="graph_memory",
            )
        except Exception as e:
            logger.debug("GraphNeuron failed: %s", e)
            return NeuronResult(
                neuron=self.name, content="", relevance=0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
            )


class WebNeuron(BaseNeuron):
    """Proactive web context gathering for news/current events.

    Unlike WebSearchNeuron (user-triggered), this fires automatically
    when the query involves current events, trending topics, or news.
    """

    name = "web"
    description = "Proactive web context for current events and news"

    def __init__(self, limit: int = 3):
        self._limit = limit

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        start = time.perf_counter()
        try:
            from adk.builtin_tools import web_search
            raw = await web_search(query, limit=self._limit)
            data = json.loads(raw)
            if "error" in data:
                return NeuronResult(neuron=self.name, content="", relevance=0.0,
                                    latency_ms=(time.perf_counter() - start) * 1000)
            lines = []
            for r in data.get("results", []):
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                url = r.get("url", "")
                if title or snippet:
                    lines.append(f"- {title}: {snippet[:200]} [{url}]")
            content = "\n".join(lines) if lines else ""
            return NeuronResult(
                neuron=self.name, content=content,
                relevance=0.7 if content else 0.0,
                latency_ms=(time.perf_counter() - start) * 1000,
                source="web_proactive",
            )
        except Exception as e:
            logger.debug("WebNeuron failed: %s", e)
            return NeuronResult(neuron=self.name, content="", relevance=0.0,
                                latency_ms=(time.perf_counter() - start) * 1000)


class ToolInventoryNeuron(BaseNeuron):
    """Inventory of available tools — injected as context so the LLM knows what it can do."""

    name = "tool_inventory"
    description = "List available tools for the current agent"

    def __init__(self, agent: AitherAgent | None = None):
        self._agent = agent

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        agent = kwargs.get("agent", self._agent)
        if not agent:
            return NeuronResult(neuron=self.name, content="", relevance=0.0)
        try:
            tools = agent._tools.list_tools()
            if not tools:
                return NeuronResult(neuron=self.name, content="", relevance=0.0)
            lines = [f"- {t.name}: {t.description}" for t in tools[:30]]
            return NeuronResult(
                neuron=self.name,
                content=f"Available tools ({len(tools)}):\n" + "\n".join(lines),
                relevance=0.5,
                source="tool_inventory",
            )
        except Exception:
            return NeuronResult(neuron=self.name, content="", relevance=0.0)


class AgentInventoryNeuron(BaseNeuron):
    """Inventory of known agent identities."""

    name = "agent_inventory"
    description = "List known agent identities and their capabilities"

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        try:
            from adk.identity import list_identities
            identities = list_identities()
            if not identities:
                return NeuronResult(neuron=self.name, content="", relevance=0.0)
            lines = [f"- {name}" for name in identities[:20]]
            return NeuronResult(
                neuron=self.name,
                content=f"Known agents ({len(identities)}):\n" + "\n".join(lines),
                relevance=0.4,
                source="agent_inventory",
            )
        except Exception:
            return NeuronResult(neuron=self.name, content="", relevance=0.0)


class ServiceInventoryNeuron(BaseNeuron):
    """Inventory of reachable AitherOS services."""

    name = "service_inventory"
    description = "List reachable AitherOS services"

    async def fire(self, query: str, **kwargs) -> NeuronResult:
        try:
            from adk.services import ServiceBridge
            bridge = ServiceBridge()
            services = await bridge.list_services()
            if not services:
                return NeuronResult(neuron=self.name, content="", relevance=0.0)
            lines = [f"- {s}" for s in services[:20]]
            return NeuronResult(
                neuron=self.name,
                content=f"Reachable services ({len(services)}):\n" + "\n".join(lines),
                relevance=0.3,
                source="service_inventory",
            )
        except Exception:
            return NeuronResult(neuron=self.name, content="", relevance=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Neuron Pool
# ─────────────────────────────────────────────────────────────────────────────

class NeuronPool:
    """Manages and fires neurons in parallel."""

    def __init__(self, agent: AitherAgent | None = None):
        self._agent = agent
        self._neurons: dict[str, BaseNeuron] = {}
        self._fire_count = 0
        self._total_latency_ms = 0.0

        # Register defaults
        self.register(WebSearchNeuron())
        self.register(WebNeuron())
        self.register(MemoryNeuron(agent))
        self.register(GraphNeuron(agent))
        self.register(ToolInventoryNeuron(agent))
        self.register(AgentInventoryNeuron())
        self.register(ServiceInventoryNeuron())

    def register(self, neuron: BaseNeuron):
        """Register a neuron."""
        self._neurons[neuron.name] = neuron

    def unregister(self, name: str):
        """Remove a neuron."""
        self._neurons.pop(name, None)

    @property
    def neurons(self) -> dict[str, BaseNeuron]:
        return dict(self._neurons)

    async def fire(
        self,
        names: list[str],
        query: str,
        timeout: float = 10.0,
        **kwargs,
    ) -> list[NeuronResult]:
        """Fire specified neurons in parallel."""
        if not names:
            return []

        tasks = []
        for name in names:
            neuron = self._neurons.get(name)
            if neuron:
                tasks.append(neuron.fire(query, agent=self._agent, **kwargs))

        if not tasks:
            return []

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Neuron pool timed out after %.1fs", timeout)
            return []

        valid = []
        for r in results:
            if isinstance(r, NeuronResult) and r.content:
                valid.append(r)
                self._fire_count += 1
                self._total_latency_ms += r.latency_ms
        return valid

    async def fire_all(self, query: str, **kwargs) -> list[NeuronResult]:
        """Fire all registered neurons."""
        return await self.fire(list(self._neurons.keys()), query, **kwargs)

    def stats(self) -> dict:
        return {
            "registered": list(self._neurons.keys()),
            "total_fires": self._fire_count,
            "avg_latency_ms": (
                self._total_latency_ms / self._fire_count
                if self._fire_count else 0.0
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Auto-fire detection
# ─────────────────────────────────────────────────────────────────────────────

# Pattern → neurons mapping
_AUTO_PATTERNS: list[tuple[re.Pattern, list[str]]] = [
    # Web search triggers
    (re.compile(r'(?:search|look\s+up|find|google|latest|current|news|today)', re.I), ["web_search"]),
    (re.compile(r'(?:what\s+is|who\s+is|where\s+is|when\s+(?:was|is|did))', re.I), ["web_search", "graph"]),
    # Proactive web triggers (news, current events, trending)
    (re.compile(r'(?:trending|breaking|recent\s+(?:news|events|updates))', re.I), ["web"]),
    # Memory triggers
    (re.compile(r'(?:remember|recall|previous|earlier|last\s+time|history|we\s+discussed)', re.I), ["memory", "graph"]),
    # Graph triggers
    (re.compile(r'(?:related\s+to|connection|relationship|depends|uses|contains)', re.I), ["graph"]),
    (re.compile(r'(?:what\s+do\s+(?:I|you|we)\s+know|tell\s+me\s+about)', re.I), ["graph", "memory"]),
    # Code/technical triggers
    (re.compile(r'(?:how\s+does|architecture|implementation|module|class|function)', re.I), ["graph", "memory"]),
    # System/tool/agent inventory triggers
    (re.compile(r'(?:what\s+tools|available\s+tools|what\s+can\s+you)', re.I), ["tool_inventory"]),
    (re.compile(r'(?:what\s+agents|which\s+agent|list\s+agents)', re.I), ["agent_inventory"]),
    (re.compile(r'(?:what\s+services|which\s+services|status|running)', re.I), ["service_inventory"]),
]

# Intent category → tools that should be available (ported from monorepo UCB)
CATEGORY_TOOLS: dict[str, list[str]] = {
    "system_status": ["get_system_status", "get_service_status"],
    "communication": ["check_inbox", "send_email", "read_email"],
    "code": ["read_file", "write_file", "replace_text", "search_files", "list_directory"],
    "git": ["git_status", "git_log", "git_diff", "git_commit"],
    "memory": ["recall", "remember", "query_memory"],
    "content": ["blog_list_posts", "blog_create_post"],
    "search": ["web_search", "search_knowledge"],
}


class AutoNeuronFire:
    """Detects query patterns and auto-fires appropriate neurons.

    Integrates into the agent chat pipeline to inject context before LLM calls.
    """

    def __init__(self, pool: NeuronPool | None = None, agent: AitherAgent | None = None):
        self._pool = pool or NeuronPool(agent)
        self._agent = agent
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_ttl = 60.0  # seconds

    def detect_neurons(self, query: str, intent_category: str | None = None) -> list[str]:
        """Detect which neurons should fire for this query.

        Args:
            query: The user's message.
            intent_category: Optional intent classification (e.g., "code", "system_status").
                If provided, category-specific neurons are also included.
        """
        needed: set[str] = set()
        for pattern, neurons in _AUTO_PATTERNS:
            if pattern.search(query):
                needed.update(neurons)

        # Always include graph if available (low-cost, high-value)
        if self._agent and getattr(self._agent, '_graph', None):
            needed.add("graph")

        # Category-based neuron injection
        if intent_category and intent_category in CATEGORY_TOOLS:
            # Include tool inventory so LLM knows what's available
            needed.add("tool_inventory")

        return list(needed)

    async def gather_context(self, query: str, **kwargs) -> str:
        """Auto-detect needed neurons, fire them, return formatted context.

        Returns empty string if no neurons fired or no results.
        """
        # Check cache
        cache_key = query[:100].lower().strip()
        now = time.time()
        if cache_key in self._cache:
            ts, cached = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return cached

        neurons = self.detect_neurons(query)
        if not neurons:
            return ""

        results = await self._pool.fire(neurons, query, **kwargs)
        if not results:
            return ""

        # Format results by neuron
        sections = []
        for r in sorted(results, key=lambda x: x.relevance, reverse=True):
            label = {
                "web_search": "WEB SEARCH",
                "web": "WEB CONTEXT",
                "memory": "AGENT MEMORY",
                "graph": "KNOWLEDGE GRAPH",
                "tool_inventory": "AVAILABLE TOOLS",
                "agent_inventory": "KNOWN AGENTS",
                "service_inventory": "SERVICES",
            }.get(r.neuron, r.neuron.upper())
            sections.append(f"[{label}]\n{r.content}")

        context = "\n\n".join(sections)

        # Cache
        self._cache[cache_key] = (now, context)
        return context

    @property
    def pool(self) -> NeuronPool:
        return self._pool
