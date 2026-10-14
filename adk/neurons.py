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
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
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
    # Seconds this neuron may run before NeuronPool.fire gives up on it alone. One
    # whole-batch timeout used to drop every result when the web search was slow,
    # so memory and graph hits that were already back were thrown away with it.
    budget_s: float = 5.0

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
    budget_s = 6.0

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


def _memory_line(entry: Any) -> str:
    """One memory hit as ``- key: value``.

    ``Memory.search`` returns ``MemoryEntry`` dataclasses; formatting them with
    ``f"- {r}"`` put ``MemoryEntry(key=..., timestamp=..., metadata=...)`` reprs into
    the prompt, which cost tokens and carried no more than the key and value.
    """
    key = getattr(entry, "key", None)
    value = getattr(entry, "value", None)
    if key is not None and value is not None:
        return f"- {key}: {str(value)[:200]}"
    if isinstance(entry, dict) and ("value" in entry or "content" in entry):
        k = entry.get("key") or entry.get("title") or ""
        v = str(entry.get("value", entry.get("content", "")))[:200]
        return f"- {k}: {v}" if k else f"- {v}"
    return f"- {str(entry)[:200]}"


class MemoryNeuron(BaseNeuron):
    """Search agent's conversation memory."""

    name = "memory"
    description = "Search conversation history and stored memories"
    budget_s = 4.0

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
            lines = [_memory_line(r) for r in results[:5] if r]
            content = "\n".join(line for line in lines if line)
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
    budget_s = 3.0

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
    budget_s = 6.0

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
    budget_s = 2.0

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
    budget_s = 2.0

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
    budget_s = 3.0

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
        """Fire specified neurons in parallel; keep every result that finished.

        Each neuron runs under its own ``budget_s`` (capped by ``timeout``), and the
        batch waits at most ``timeout``. A neuron that overruns or raises is logged and
        dropped on its own; the others' results are still returned.
        """
        if not names:
            return []

        tasks: dict[asyncio.Future, str] = {}
        for name in names:
            neuron = self._neurons.get(name)
            if not neuron:
                continue
            budget = min(float(getattr(neuron, "budget_s", timeout) or timeout), timeout)
            coro = asyncio.wait_for(
                neuron.fire(query, agent=self._agent, **kwargs), timeout=budget,
            )
            tasks[asyncio.ensure_future(coro)] = name

        if not tasks:
            return []

        try:
            done, pending = await asyncio.wait(set(tasks), timeout=timeout)
        finally:
            # Also on the caller's cancellation (its own wait_for): asyncio.wait does
            # not cancel what it waits on, so the neurons would run on, orphaned.
            for t in tasks:
                if not t.done():
                    t.cancel()
        for t in pending:
            logger.warning("Neuron %s still running after %.1fs; dropped", tasks[t], timeout)

        valid = []
        for t in done:
            try:
                r = t.result()
            except asyncio.TimeoutError:
                logger.warning("Neuron %s exceeded its budget; dropped", tasks[t])
                continue
            except Exception as e:
                logger.warning("Neuron %s failed: %s", tasks[t], e)
                continue
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


# Coarse intent (from ``adk.intent``: the LLM router's ``IntentDecision.intent`` or
# ``coarse_code_intent``'s CODE/CONVERSATION/DEFAULT) -> CATEGORY_TOOLS key. The two
# vocabularies never matched, so ``intent_category in CATEGORY_TOOLS`` was always False
# and the category branch never ran. None means "no category": the regexes decide alone.
INTENT_TO_CATEGORY: dict[str, str | None] = {
    "code": "code",
    "git": "git",
    "memory": "memory",
    "recall": "memory",
    "research": "search",
    "web_research": "search",
    "search": "search",
    "system_status": "system_status",
    "status": "system_status",
    "communication": "communication",
    "email": "communication",
    "content": "content",
    "creation": "content",
    "document": "content",
    "conversation": None,
    "question": None,
    "analysis": None,
    "command": None,
    "default": None,
}

# CATEGORY_TOOLS key -> the neurons that category wants. A category adds context
# sources; it never adds tool_inventory, whose list the system prompt already carries.
CATEGORY_NEURONS: dict[str, list[str]] = {
    "code": ["graph", "memory"],
    "git": ["graph"],
    "memory": ["memory", "graph"],
    "search": ["web_search", "web"],
    "system_status": ["service_inventory"],
    "communication": ["memory"],
    "content": ["memory"],
}

# Neurons that reach the public web. They fire only for a research intent (or, when
# the caller gave no intent, for explicit research phrasing). Genesis measured generic
# words like "find", "current" and "status" pushing search results into ordinary turns
# and gated web out of its context pipeline for every non-research intent.
_WEB_NEURONS = frozenset({"web_search", "web"})
_RESEARCH_RE = re.compile(
    r"\b(?:search\s+(?:the\s+web|online|for)|look\s+up|google|latest|news|trending|"
    r"breaking|on\s+the\s+(?:web|internet)|online)\b",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|yo|sup|hiya|howdy|thanks|thank\s+you|thx|"
    r"good\s+(?:morning|afternoon|evening|night)|how\s+are\s+you|what'?s\s+up|bye)"
    r"(?:\s+(?:there|again|all|everyone|[a-z]+))?\s*[!.?,]*\s*$",
    re.I,
)

# Which neurons survive when the scaler's count is smaller than what the regexes
# asked for: the agent's own memory and graph first, the web next, inventories last.
_NEURON_PRIORITY = (
    "memory", "graph", "web_search", "web",
    "service_inventory", "agent_inventory", "tool_inventory",
)

_SECTION_LABELS = {
    "web_search": "WEB SEARCH",
    "web": "WEB CONTEXT",
    "memory": "AGENT MEMORY",
    "graph": "KNOWLEDGE GRAPH",
    "tool_inventory": "AVAILABLE TOOLS",
    "agent_inventory": "KNOWN AGENTS",
    "service_inventory": "SERVICES",
}


# Intents that carry no signal. coarse_code_intent returns "DEFAULT" for most input
# and documents it as neutral; gating on it like a real intent meant web search could
# never fire once a caller passed it. These are treated exactly like "no intent".
_NEUTRAL_INTENTS = frozenset({"", "default"})


def _no_intent(intent_category: str | None) -> bool:
    return intent_category is None or str(intent_category).strip().lower() in _NEUTRAL_INTENTS


def _category_for(intent_category: str | None) -> str | None:
    """Map a coarse intent or a CATEGORY_TOOLS key to a CATEGORY_TOOLS key."""
    if not intent_category:
        return None
    key = str(intent_category).strip().lower()
    if key in CATEGORY_TOOLS:
        return key
    return INTENT_TO_CATEGORY.get(key)


class NeuronScaler:
    """How many neurons a turn deserves, from the message, its intent and its effort.

    The table mirrors Genesis's context pipeline (greeting 0, simple 2, code 8,
    multi 16, research 24, complex 32). The pool registers seven neurons today, so the
    larger counts are no-ops; the table is kept whole so a larger pool scales unchanged.
    """

    SCALING_TABLE: dict[str, int] = {
        "greeting": 0,
        "simple": 2,
        "code": 8,
        "multi": 16,
        "research": 24,
        "complex": 32,
    }

    @staticmethod
    def classify_complexity(
        message: str, intent_category: str | None = None, effort: int | None = None,
    ) -> str:
        msg = (message or "").strip()
        if not msg or _GREETING_RE.match(msg):
            return "greeting"
        # effort <= 2 is a triage turn: firing neurons costs more than the answer.
        if effort is not None and effort <= 2:
            return "greeting"
        category = _category_for(intent_category)
        if category == "search" or (_no_intent(intent_category) and _RESEARCH_RE.search(msg)):
            return "research"
        if effort is not None and effort >= 8:
            return "complex"
        if category in ("code", "git"):
            return "code"
        if len(msg.split()) <= 6:
            return "simple"
        return "multi"

    @classmethod
    def max_neurons(
        cls, message: str, intent_category: str | None = None, effort: int | None = None,
    ) -> int:
        return cls.SCALING_TABLE[cls.classify_complexity(message, intent_category, effort)]


# record_consumption rotates its file to ``.1`` past this size.
_CONSUMPTION_MAX_BYTES = 5 * 1024 * 1024
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{4,}")


def _consumption_path() -> Path:
    home = os.environ.get("AITHER_HOME") or str(Path.home() / ".aither")
    return Path(home) / "neuron_fires.jsonl"


class AutoNeuronFire:
    """Detects query patterns and auto-fires appropriate neurons.

    Integrates into the agent chat pipeline to inject context before LLM calls.
    """

    def __init__(self, pool: NeuronPool | None = None, agent: AitherAgent | None = None):
        self._pool = pool or NeuronPool(agent)
        self._agent = agent
        self._cache: dict[str, tuple[float, str, list, list, dict]] = {}
        self._cache_ttl = 60.0  # seconds
        # Entries are evicted when expired or past this count; a long-running daemon
        # sees a new key on nearly every turn and the dict used to grow without bound.
        self._cache_max = 256
        # Whole-batch ceiling for NeuronPool.fire; each neuron also has its own budget_s.
        self.timeout_s = 10.0
        # Names of the neurons the LAST gather_context fired, for the turn trace. A cache
        # hit leaves it as the turn that filled the cache saw it.
        self.last_fired: list[str] = []
        # Budget accounting of the last gather_context, for the turn trace:
        # {"complexity", "chars_kept", "chars_dropped", "neurons_kept"}.
        self.last_budget: dict[str, Any] = {}
        # Results behind the last block, kept for record_consumption.
        self._last_results: list[NeuronResult] = []

    def detect_neurons(
        self,
        query: str,
        intent_category: str | None = None,
        effort: int | None = None,
        skip: "tuple | list | set" = (),
    ) -> list[str]:
        """Detect which neurons should fire for this query.

        Args:
            query: The user's message.
            intent_category: Optional coarse intent ("code", "research", "CODE", ...)
                or a CATEGORY_TOOLS key; mapped through INTENT_TO_CATEGORY.
            effort: Optional turn effort 1-10. Effort <= 2 fires nothing.
            skip: Names removed BEFORE the cap, so a skipped name never takes a slot.

        Greetings fire nothing; web neurons need a research intent; the count is capped
        by NeuronScaler and filled in _NEURON_PRIORITY order.
        """
        cap = NeuronScaler.max_neurons(query, intent_category, effort)
        if cap <= 0:
            return []

        needed: set[str] = set()
        for pattern, neurons in _AUTO_PATTERNS:
            if pattern.search(query):
                needed.update(neurons)

        # Always include graph if available (low-cost, high-value)
        if self._agent and getattr(self._agent, '_graph', None):
            needed.add("graph")

        category = _category_for(intent_category)
        if category:
            needed.update(CATEGORY_NEURONS.get(category, ()))

        research = category == "search" or (
            _no_intent(intent_category) and bool(_RESEARCH_RE.search(query or ""))
        )
        if not research:
            needed -= _WEB_NEURONS
        needed -= set(skip or ())

        rank = {n: i for i, n in enumerate(_NEURON_PRIORITY)}
        ordered = sorted(needed, key=lambda n: (rank.get(n, len(rank)), n))
        return ordered[:cap]

    async def gather_context(
        self,
        message: str,
        *,
        intent_category: str | None = None,
        effort: int | None = None,
        max_chars: int = 1200,
        skip: "tuple | list | set" = (),
        **kwargs,
    ) -> str:
        """Auto-detect needed neurons, fire them, return formatted context.

        Args:
            message: the user's message (positional; the old name ``query`` is the
                same argument).
            intent_category: the turn's coarse intent; gates web neurons and adds the
                category's neurons (INTENT_TO_CATEGORY, CATEGORY_NEURONS).
            effort: the turn's effort 1-10, or None. Effort <= 2 fires nothing.
            max_chars: the block's budget, filled in relevance order; the returned text
                is never longer.
            skip: neuron names NOT to fire, because another context source already
                covers them this turn (e.g. "graph" when the code graph block exists).
                ``tool_inventory`` is always skipped: the tools menu already lists them.
            **kwargs: passed through to every neuron's ``fire``.

        Returns the formatted context, or "" when no neuron fired or none returned
        anything. Fails open: any fault here is logged and yields "".
        """
        try:
            return await self._gather(message, intent_category, effort, max_chars, skip,
                                      kwargs)
        except Exception as e:
            logger.warning("Neuron gather failed; turn continues without it: %s", e)
            self.last_budget = {"error": str(e)[:200]}
            return ""

    async def _gather(self, message, intent_category, effort, max_chars, skip,
                      kwargs) -> str:
        query = message or ""
        skip_set = set(skip or ()) | {"tool_inventory"}
        # intent, effort and skip change which neurons fire, so they are part of the key:
        # a skip=("graph",) turn must not be served the graph-bearing block cached before.
        cache_key = (query[:100].lower().strip()
                     + f"|{intent_category or ''}|{'' if effort is None else effort}"
                     + f"|{max_chars}|{','.join(sorted(skip_set))}")
        now = time.time()
        if cache_key in self._cache:
            ts, cached, fired, results, budget = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                # Restore the trace of the turn that filled the cache, so last_fired
                # and record_consumption describe THIS block, not the latest miss.
                self.last_fired = list(fired)
                self._last_results = list(results)
                self.last_budget = dict(budget)
                return cached

        neurons = self.detect_neurons(query, intent_category, effort, skip=skip_set)
        self.last_fired = list(neurons)
        self._last_results = []
        self.last_budget = {
            "complexity": NeuronScaler.classify_complexity(query, intent_category, effort),
            "chars_kept": 0, "chars_dropped": 0, "neurons_kept": [],
        }
        if not neurons:
            return ""

        results = await self._pool.fire(neurons, query, timeout=self.timeout_s, **kwargs)
        if not results:
            return ""

        context, kept, dropped, kept_names = _fill_budget(results, max_chars)
        self._last_results = [r for r in results if r.neuron in kept_names]
        self.last_budget.update(chars_kept=kept, chars_dropped=dropped,
                                neurons_kept=kept_names)
        if not context:
            return ""

        self._evict(now)
        self._cache[cache_key] = (now, context, list(self.last_fired),
                                  list(self._last_results), dict(self.last_budget))
        return context

    def _evict(self, now: float) -> None:
        """Drop expired entries, then the oldest ones until one more fits."""
        for k in [k for k, v in self._cache.items() if now - v[0] >= self._cache_ttl]:
            del self._cache[k]
        while self._cache and len(self._cache) >= max(1, self._cache_max):
            del self._cache[next(iter(self._cache))]

    def record_consumption(self, query: str, answer: str, *,
                           path: str | Path | None = None) -> list[dict]:
        """Append one row per neuron behind the last block: did the answer use it?

        ``consumed`` is a token-overlap heuristic (>= 2 shared 4+ char tokens between
        the neuron's content and the answer). The rows are training data for a later
        fire predictor; nothing reads them on the turn path. Never raises.
        """
        rows: list[dict] = []
        try:
            answer_tokens = {t.lower() for t in _TOKEN_RE.findall(answer or "")}
            ts = time.time()
            for r in self._last_results:
                content_tokens = {t.lower() for t in _TOKEN_RE.findall(r.content)}
                overlap = len(content_tokens & answer_tokens)
                rows.append({
                    "ts": ts, "query": (query or "")[:200], "neuron": r.neuron,
                    "consumed": overlap >= 2, "overlap": overlap,
                    "chars": len(r.content), "latency_ms": round(r.latency_ms, 1),
                })
            if not rows:
                return rows
            target = Path(path) if path else _consumption_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size > _CONSUMPTION_MAX_BYTES:
                target.replace(target.with_name(target.name + ".1"))
            with target.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
        except Exception as e:
            logger.warning("Neuron consumption record failed: %s", e)
        return rows

    @property
    def pool(self) -> NeuronPool:
        return self._pool


def _fill_budget(
    results: list[NeuronResult], max_chars: int,
) -> tuple[str, int, int, list[str]]:
    """Join result sections in relevance order without exceeding ``max_chars``.

    A section that does not fit whole is cut at a line boundary when at least one of
    its lines fits; otherwise it is dropped. Returns (text, kept, dropped, neurons).
    """
    budget = max(0, int(max_chars))
    parts: list[str] = []
    used = dropped = 0
    names: list[str] = []
    for r in sorted(results, key=lambda x: x.relevance, reverse=True):
        label = _SECTION_LABELS.get(r.neuron, r.neuron.upper())
        section = f"[{label}]\n{r.content}"
        sep = 2 if parts else 0
        room = budget - used - sep
        if len(section) <= room:
            parts.append(section)
            used += sep + len(section)
            names.append(r.neuron)
            continue
        lines = section.split("\n")
        cut = lines[0]
        for line in lines[1:]:
            if len(cut) + 1 + len(line) > room:
                break
            cut += "\n" + line
        if cut != lines[0] and len(cut) <= room:
            parts.append(cut)
            used += sep + len(cut)
            names.append(r.neuron)
            dropped += len(section) - len(cut)
        else:
            dropped += len(section)
    return "\n\n".join(parts), used, dropped, names
