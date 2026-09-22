"""AitherAgent — the core agent class."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - annotation only (ruff F821 on the string form)
    from adk.crystal import Crystal
    from adk.loop_policy import LoopPolicy

from adk import coherence
from adk.config import Config
from adk.grounding import ground_system_prompt
from adk.identity import Identity, load_identity
from adk.llm import DegenerationDetector, LLMResponse, LLMRouter, Message, strip_internal_tags
from adk.llm.advisor import (
    AdvisorConfig,
    advisor_brevity_line,
    steering_system_block,
    strip_advisor_blocks,
)
from adk.llm.continuation import run_continuation
from adk.loop_guard import LoopAction, LoopGuard
from adk.memory import Memory
from adk.metering import AgentMeter, QuotaAction, get_meter, is_metered_backend
from adk.metrics import get_metrics
from adk.steering import (
    drain_steering_hints,
    drain_steering_inputs,
    register_steering_queue,
    unregister_steering_queue,
)
from adk.tools import ToolDef, ToolRegistry
from adk.trace import get_trace_id

logger = logging.getLogger("adk.agent")

# Baseline tool-loop ceiling. Kept as a module constant because LoopGuard and the
# tests reference it, but the LIVE ceiling is effort-scaled - see _max_tool_loops().
_MAX_TOOL_LOOPS = 10

# Effort -> tool-loop ceiling. A flat ceiling of 10 was the single biggest cause of
# "the agent stopped half-way and confidently summarized what it had": any task
# needing read -> edit -> test -> fix -> re-test burns 10 iterations before it has
# finished, and the loop then silently exits into a synthesis call whose output is
# indistinguishable from a completed turn. Triage turns do not need 10; a genuine
# effort-9 build task needs far more than 10.
_EFFORT_LOOP_CEILINGS: tuple[tuple[int, int], ...] = (
    (2, 4),    # effort 1-2  - triage/status: answer fast or say you cannot
    (6, 16),   # effort 3-6  - procedural work
    (10, 40),  # effort 7-10 - architecture / root-cause / multi-file builds
)


def _max_tool_loops(effort: int | None) -> int:
    """Tool-loop ceiling for an effort tier.

    ``ADK_MAX_TOOL_LOOPS`` overrides absolutely (operators running against a
    metered backend want a hard cap they control).
    """
    override = os.environ.get("ADK_MAX_TOOL_LOOPS", "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            logger.debug("Ignoring non-integer ADK_MAX_TOOL_LOOPS=%r", override)

    tier = effort if isinstance(effort, int) else 5
    for upper, ceiling in _EFFORT_LOOP_CEILINGS:
        if tier <= upper:
            return ceiling
    return _EFFORT_LOOP_CEILINGS[-1][1]

# Essential tools that must always be available when agent has tools
_ESSENTIAL_TOOL_NAMES = frozenset({
    "file_read", "file_write", "file_edit", "file_search", "file_list",
})

# Steering message injected when LLM returns text-only on turn 1 with tools available
_TOOL_STEERING_MSG = (
    "You have tools available. You MUST use them to complete the user's request. "
    "Do not describe what you would do — actually call the appropriate tool function. "
    "Re-read the user's message and select the right tool."
)

# Patterns that indicate the user wants an action (not just conversation)
_ACTION_PATTERNS = re.compile(
    r'(?i)(?:read|write|edit|create|delete|search|find|list|run|execute|check|fix|'
    r'refactor|deploy|build|test|commit|push|pull|install|update|remove|send|fetch|'
    r'open|close|show\s+me\s+(?:the\s+)?(?:file|code|log|error))',
)


def _should_steer_tool_use(message: str, tool_choice: str | dict | None) -> bool:
    """Determine if we should retry with tool-steering on turn 1.

    Only steers when: tool_choice was explicitly set, OR the message
    contains action verbs that strongly suggest tool use is needed.
    Simple greetings like "Hello" should NOT trigger steering.
    """
    if tool_choice and tool_choice != "auto":
        return True
    return bool(_ACTION_PATTERNS.search(message))
_conversations_store = None


def _get_conversations():
    """Lazy-load the global ConversationStore."""
    global _conversations_store
    if _conversations_store is None:
        from adk.conversations import get_conversation_store
        _conversations_store = get_conversation_store()
    return _conversations_store


def _get_session_artifacts(session_id: str) -> list:
    """Get artifacts collected for a session."""
    try:
        from adk.artifacts import get_registry
        return get_registry().get(session_id)
    except Exception:
        return []


def _intent_matches_categories(intent_type: str, categories: list[str]) -> bool:
    """Check if an intent matches any of the tool's intent categories.

    Matching rules:
    - If categories is empty, the tool is available for all intents (fail-open).
    - Otherwise, check if intent_type matches any category (case-insensitive).
    - 'DEFAULT' intent matches all unmarked tools (category is empty).
    """
    if not categories:
        return True
    intent_normalized = (intent_type or "").strip().lower()
    # DEFAULT means "the classifier could not tell" -- it must NOT filter.
    #
    # The docstring above has always said DEFAULT matches unmarked tools, and
    # the code implemented only half of that: a tool with NO categories passed,
    # a tool WITH categories was dropped. DEFAULT is what coarse_code_intent
    # returns for most real input -- measured 2026-08-22, both "what is in the
    # news today" AND "search the web for AI news" classify as DEFAULT -- so the
    # agent's web and file tools were invisible to the model on almost every
    # turn. Asked to list its tools it named 4 of 13.
    #
    # The user-visible symptom was the agent saying "I don't have direct web
    # search capability" while web_search was registered, bound and working. A
    # capability filtered out of the prompt is indistinguishable from one that
    # does not exist.
    if intent_normalized in ("", "default"):
        return True
    return any(cat.strip().lower() == intent_normalized for cat in categories)


def _filter_tools_by_intent(
    tools_list: list[ToolDef],
    intent_type: str | None = None,
) -> list[ToolDef]:
    """Filter tools by intent type.

    Fail-open: if intent_type is None/empty, return all tools (current behavior).
    Otherwise, filter by matching intent_categories.
    """
    if not intent_type:
        return tools_list
    return [
        t for t in tools_list
        if _intent_matches_categories(intent_type, getattr(t, "intent_categories", []))
    ]


def _build_knowledge_graph(
    agent_name: str,
    tool_calls_made: list[str],
    memory_recalls: set[str],
    sources_touched: set[str],
    session_id: str,
) -> dict:
    """Build a knowledge graph from turn metadata.

    Returns a dict with 'nodes' and 'edges' suitable for SSE emission.
    Matches the shape Genesis emits: {nodes: [{id, type, label, weight}],
    edges: [{from, to, rel, weight}]}.
    """
    nodes = []
    edges = []
    node_ids = set()

    # Add user node
    user_id = f"user_{session_id[:8]}" if session_id else "user_anon"
    nodes.append({
        "id": user_id,
        "type": "user",
        "label": "User",
        "weight": 1.0,
    })
    node_ids.add(user_id)

    # Add agent node
    agent_id = f"agent_{agent_name}".lower()
    nodes.append({
        "id": agent_id,
        "type": "agent",
        "label": agent_name.replace("_", " ").title(),
        "weight": 1.0,
    })
    node_ids.add(agent_id)

    # Edge: user → agent
    edges.append({
        "from": user_id,
        "to": agent_id,
        "rel": "asks",
        "weight": 1.0,
    })

    # Add tool nodes (capped at 20)
    for i, tool_name in enumerate(sorted(list(set(tool_calls_made)))[:20]):
        # Strip circuit_break/blocked/denied suffixes added during execution
        tool_clean = tool_name.split("[")[0]
        tool_id = f"tool_{tool_clean}".lower()
        if tool_id not in node_ids:
            nodes.append({
                "id": tool_id,
                "type": "tool",
                "label": tool_clean.replace("_", " ").title(),
                "weight": 0.8,
            })
            node_ids.add(tool_id)

        # Edge: agent → tool
        edges.append({
            "from": agent_id,
            "to": tool_id,
            "rel": "calls",
            "weight": 0.8,
        })

    # Add memory nodes (capped at 10)
    for mem_key in sorted(list(memory_recalls))[:10]:
        mem_id = f"memory_{mem_key}".lower()
        if mem_id not in node_ids:
            nodes.append({
                "id": mem_id,
                "type": "memory",
                "label": mem_key.replace("_", " ").title(),
                "weight": 0.7,
            })
            node_ids.add(mem_id)

        # Edge: agent → memory
        edges.append({
            "from": agent_id,
            "to": mem_id,
            "rel": "recalls",
            "weight": 0.7,
        })

    # Add source nodes (capped at 10)
    for source in sorted(list(sources_touched))[:10]:
        src_id = f"source_{source}".lower()
        if src_id not in node_ids:
            nodes.append({
                "id": src_id,
                "type": "source",
                "label": source.replace("_", " ").title()[:50],
                "weight": 0.6,
            })
            node_ids.add(src_id)

        # Edge: agent → source
        edges.append({
            "from": agent_id,
            "to": src_id,
            "rel": "reads",
            "weight": 0.6,
        })

    return {"nodes": nodes[:40], "edges": edges[:60]}


@dataclass
class AgentResponse:
    """Response from an agent interaction."""
    content: str
    model: str = ""
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    tool_calls_made: list[str] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    session_id: str = ""
    finish_reason: str = "stop"
    effort_level: int = 0
    cache_status: str = ""
    # Advisor-tool usage (Opus sub-inference), summed across this turn's ReAct
    # loop so callers can meter the advisor apart from the executor.
    advisor_calls: int = 0
    advisor_input_tokens: int = 0
    advisor_output_tokens: int = 0
    # Human-in-the-loop: set when the turn paused awaiting tool approval. ``pending`` =
    # the gated tool calls the customer must allow/deny before the turn can continue.
    requires_action: bool = False
    pending: list[dict] = field(default_factory=list)



class AitherAgent:
    """An AI agent with identity, tools, memory, and LLM access.

    Usage:
        agent = AitherAgent("atlas")
        response = await agent.chat("What's the project status?")

        # With custom tools
        agent = AitherAgent("demiurge", tools=[my_tool_registry])

        # With specific LLM
        agent = AitherAgent("lyra", llm=LLMRouter(provider="openai", api_key="sk-..."))
    """

    def __init__(
        self,
        name: str | None = None,
        identity: str | Identity | None = None,
        llm: LLMRouter | None = None,
        tools: list[ToolRegistry] | ToolRegistry | None = None,
        memory: Memory | None = None,
        config: Config | None = None,
        system_prompt: str | None = None,
        phonehome: bool = False,
        builtin_tools: bool = True,
        load_packs: bool = False,
        user_mcp: bool = True,
        memory_maintenance: bool = False,
        routines: bool = False,
        loop_policy: "LoopPolicy | None" = None,
        crystal: "Crystal | None" = None,
    ):
        self.config = config or Config.from_env()
        # Opt-in loop policy (adk.loop_policy.LoopPolicy): the four measured nudges
        # that turn exploration into a landed, verified edit. None = off; the
        # ruler's promotion rule decides when it becomes the default.
        self.loop_policy = loop_policy
        # Opt-in crystal (adk.crystal.Crystal, orchestration spec section 8): context
        # is memory, not a window. Compaction WRITES its facts to awm; every turn
        # starts by RECALLING them (+ code-graph symbols). None = off.
        self.crystal = crystal

        # Identity
        if isinstance(identity, Identity):
            self._identity = identity
        elif isinstance(identity, str):
            self._identity = load_identity(identity)
        elif name:
            self._identity = load_identity(name)
        else:
            self._identity = Identity(name="assistant")

        self.name = name or self._identity.name
        self._system_prompt = system_prompt

        # Default persona from the discovered brain PACK — but ONLY when the pack's
        # declared `identity:` matches THIS agent. This makes the shipped default
        # pack (adk/packs/aither) drive the aither agent's persona, WITHOUT
        # hijacking a specialized named agent (e.g. a 'hydra' agent must not adopt
        # a discovered aither/aitherium pack's prompt). An explicit system_prompt
        # always wins; this only fills the base when none was given.
        self._brain_pack_prompt: str | None = None
        if not self._system_prompt:
            self._brain_pack_prompt = self._load_matching_brain_pack_prompt()

        # License gate: verify agent is licensed and custom agents are allowed
        self._check_agent_license(load_packs)

        # Resolve the entitlement manager once (free COMMUNITY tier by default).
        # Used below to cap effort, fence off auto-neurons/swarm, and apply the
        # free-tier monthly token limit. Non-fatal — defaults to unrestricted if
        # the licensing module is somehow unavailable.
        self._license = None
        try:
            from adk.licensing import get_license_manager
            self._license = get_license_manager()
        except Exception:
            pass

        # LLM — auto-detect Elysium if no local backend and API key present
        self.llm = llm or LLMRouter(config=self.config)
        self._elysium_connected = False
        if not llm:
            self._try_elysium_fallback()

        # Tools
        self._tools = ToolRegistry()
        if tools:
            items = tools if isinstance(tools, list) else [tools]
            for item in items:
                if isinstance(item, ToolRegistry):
                    for td in item.list_tools():
                        self._tools._tools[td.name] = td
                elif callable(item):
                    self._tools.register(item)

        # Code locator — one indexed lookup instead of a grep chain (measured 83%
        # localization-token saving on the fleet monorepo). Registers the locate_code
        # tool ONLY when AITHER_CODE_LOCATOR=1 or AITHER_CODEGRAPH_URL is configured;
        # otherwise a pure no-op. Non-fatal.
        try:
            from adk.code_locator import register_locator_tool
            register_locator_tool(self)
        except Exception:
            pass

        # Memory
        self.memory = memory or Memory(agent_name=self.name)

        # Metering (per-agent token & cost tracking)
        self.meter = get_meter(self.name)

        # Apply the licensed monthly token ceiling — BUT ONLY to Aitherium-proxied
        # inference (our metered gateway). BYO keys (anthropic/openai/deepseek) and
        # local backends (ollama/vllm/localhost) are NEVER capped because the user
        # pays for their own inference. This is the critical fix for the consumer
        # launch: community tier cap (100k/mo) applies ONLY to our gateway, not to
        # self-hosted or user-supplied API keys.
        #
        # Caps apply to EXACTLY ONE backend: Aitherium's metered cloud gateway
        # (provider="gateway"), where we pay for the inference. Every other
        # backend — BYO API keys (anthropic/openai/deepseek/groq/…), local
        # runtimes (ollama/vllm/llamacpp/lmstudio), and any unknown/custom
        # OpenAI-compatible endpoint — is the user's own inference and is NEVER
        # capped. This is the inverse of an allowlist: we uncap by default and
        # only the gateway opts INTO a cap, so a self-hosted/BYO agent can never
        # be bricked by a community-tier limit it never agreed to.
        self._provider_name = getattr(self.llm, "provider_name", "unknown")
        self._metered_gateway = is_metered_backend(self._provider_name)
        try:
            # Mark the meter with backend provenance — metering enforces caps
            # ONLY when this is "gateway" (see adk.metering.is_metered_backend).
            self.meter._backend_type = self._provider_name

            if self._metered_gateway:
                # Aitherium-proxied inference: apply the licensed monthly ceiling
                # (community = 100k/mo) as the organic upgrade pull. Honors the
                # explicit AITHER_LICENSE_ENFORCE=0 opt-out.
                if self._license is not None and self._license._enforced():
                    _cap = int(self._license.license.entitlements.monthly_token_limit)
                    if _cap > 0:
                        self.meter._quota.monthly_limit = _cap
            else:
                # Self-hosted / BYO / local / unknown — drop every token cap.
                self.meter._quota.monthly_limit = 0
                self.meter._quota.daily_limit = 0
                self.meter._quota.hourly_limit = 0
                self.meter._quota.cost_limit_usd = 0
                logger.debug(
                    "Agent '%s' using %s backend — all token caps disabled "
                    "(user pays their own inference)",
                    self.name, self._provider_name,
                )
        except Exception:
            pass

        # Session
        self._session_id = str(uuid.uuid4())[:8]

        # Introspection ring buffer — records every tool call this agent made.
        # Powers the `self_*` tools so an agent can answer "what did I just do?"
        # honestly instead of hallucinating. Capped to bound memory; oldest evicted.
        self._introspection: deque[dict] = deque(maxlen=200)
        self._files_touched: dict[str, dict] = {}  # path -> {first, last, ops}

        # Phonehome
        self._phonehome = phonehome or self.config.phonehome_enabled

        # Safety (IntakeGuard) — non-fatal
        self._safety = None
        try:
            from adk.safety import IntakeGuard
            self._safety = IntakeGuard()
        except Exception:
            pass

        # Context manager (token-aware truncation) — non-fatal
        self._context_mgr = None
        try:
            from adk.context import ContextManager
            # `max_context: 0` is documented "let model decide" — so ASK the
            # model instead of substituting 8000. Every OpenAI-compatible
            # backend advertises its window (vLLM /v1/models max_model_len,
            # llama.cpp /props n_ctx), and an agent that guesses is wrong in
            # both directions silently: too small truncates context it was
            # allowed to keep, too large makes the BACKEND truncate the answer
            # with no error. Discovery also reads the advertised `root`, so a
            # bare alias like `aither-orchestrator` still resolves to
            # Nemotron-8B-AWQ-4bit — i.e. quantization and thinking-model
            # status come from the endpoint rather than from a name.
            max_tokens = self.config.max_context
            if not max_tokens:
                try:
                    from adk.model_capabilities import discover
                    caps = discover(
                        getattr(self.config, "llm_base_url", "") or "",
                        getattr(self.config, "model", "") or "",
                    )
                    max_tokens = caps.context_window
                    logger.info("[capabilities] %s", caps.describe())
                except Exception:
                    max_tokens = 8000     # last resort, never 0 (0 = no limit)
            self._context_mgr = ContextManager(max_tokens=max_tokens)
        except Exception:
            pass

        # Event emitter — non-fatal
        self._events = None
        try:
            from adk.events import get_emitter
            self._events = get_emitter()
        except Exception:
            pass

        # Graph memory (knowledge graph with embeddings) — non-fatal
        self._graph = None
        try:
            from adk.graph_memory import GraphMemory
            self._graph = GraphMemory(agent_name=self.name)
        except Exception:
            pass

        # Typed-activation memory — authority-ranked recall + decision
        # constraints + supersession over the local KV memory. Opt-out via
        # AITHER_TYPED_MEMORY=false. Non-fatal.
        self._typed = None
        if os.getenv("AITHER_TYPED_MEMORY", "true").lower() not in ("false", "0", "no"):
            try:
                from adk.typed_memory import TypedMemory
                self._typed = TypedMemory(self.memory)
            except Exception:
                pass

        # Continual-learning skills — recall relevant skills BEFORE acting, extract +
        # save a skill AFTER a successful multi-tool run. Opt-out via AITHER_SKILLS=false.
        self._skills = None
        if os.getenv("AITHER_SKILLS", "true").lower() not in ("false", "0", "no"):
            try:
                from adk.skills import SkillExtractor, SkillStore
                self._skills = SkillStore()
                self._skill_extractor = SkillExtractor()
            except Exception:
                self._skills = None

        # World model faculty — per-agent learned predictor, gated by AITHER_AGENT_WM env.
        # Never affects turn outcome (shadow/steer modes are separate; default off).
        # Non-fatal: lazy load on first use, exception-safe.
        self._wm = None
        self._wm_stage = "cold"
        self._wm_turns = 0
        self._wm_prev_state = None
        try:
            from adk.worldmodel import get_world_model
            self._wm = get_world_model(self.name)
        except Exception:
            pass

        # Neuron auto-fire (context gathering before LLM) — non-fatal.
        # GATED: proactive context gathering is a paid-tier capability. Free
        # (COMMUNITY) agents call tools explicitly instead of pre-firing them.
        self._auto_neurons = None
        _auto_neurons_ok = self._license is None or self._license.can_use_auto_neurons()
        if _auto_neurons_ok:
            try:
                from adk.neurons import AutoNeuronFire
                self._auto_neurons = AutoNeuronFire(agent=self)
            except Exception:
                pass

        # Strata unified storage — lazy init via property
        self._strata = None

        # Built-in tools — non-fatal
        if builtin_tools:
            try:
                from adk.builtin_tools import register_builtin_tools
                register_builtin_tools(self)
            except Exception:
                pass

        # App proxy tools — register HTTP proxy tools from ADK_APP_PROXY_URL
        try:
            from adk.app_proxy_tools import register_app_proxy_tools
            register_app_proxy_tools(self)
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("App proxy tool registration failed: %s", exc)

        # Tool packs from config — non-fatal
        # Sources (in priority): config.required_packs, raw tools.packs,
        # identity YAML tools.packs, AITHER_TOOL_PACKS env
        config_packs: list[str] = list(getattr(self.config, "required_packs", None) or [])
        if not config_packs:
            _raw = getattr(self.config, "raw", None)
            if isinstance(_raw, dict):
                config_packs = (_raw.get("tools") or {}).get("packs") or []
        if not config_packs:
            # Check identity YAML raw data for tools.packs
            _id_raw = getattr(self._identity, "raw", None)
            if isinstance(_id_raw, dict):
                config_packs = (_id_raw.get("tools") or {}).get("packs") or []
        if not config_packs:
            env_packs = os.environ.get("AITHER_TOOL_PACKS", "")
            if env_packs:
                config_packs = [p.strip() for p in env_packs.split(",") if p.strip()]
        # Persona fragments collected from licensed packs
        self._pack_persona_fragments: list[str] = []

        if config_packs:
            try:
                from adk.builtin_tools import register_tool_packs
                register_tool_packs(self, pack_ids=config_packs)
                self._collect_pack_persona_fragments(config_packs)
            except ImportError:
                pass

        # Auto-discover packs from ~/.aitheros/packs/ — opt-in via load_packs=True
        if load_packs and not config_packs:
            try:
                self._load_discovered_packs()
            except Exception as exc:
                logger.debug("Auto pack discovery failed: %s", exc)

        # ── The user's OWN MCP servers ───────────────────────────────────
        # Until this existed, every MCP path in the package pointed outward:
        # MCPBridge talks to our gateway, cli.py writes us into other people's
        # editor configs, and mcp_stdio.py runs this agent AS a server. So a
        # self-hoster could extend the agent with a Python tool pack and
        # nothing else -- its capabilities were bounded by what we shipped.
        #
        # Reads the `mcpServers` shape Claude Code and Cursor already use, and
        # is a no-op when no config exists, which is the ordinary case. Placed
        # BEFORE _filter_unavailable_tools() deliberately: a user's tool passes
        # the same readiness filter as one of ours, rather than being exempt
        # from a check every built-in has to survive.
        if user_mcp:
            try:
                from adk.mcp_client import register_user_mcp_tools
                register_user_mcp_tools(self)
            except ImportError:
                # Optional add-on, like the tool-pack loader above.
                pass
            except Exception as exc:  # noqa: BLE001
                # Somebody else's process on somebody else's machine. It must
                # never be the reason an agent fails to construct -- but it is
                # logged, because a user who configured a server and silently
                # got nothing has no way to find out why.
                logger.warning("user MCP servers unavailable: %s", exc)

        # Filter out tools that can't work in current deployment context
        self._filter_unavailable_tools()

        # ── Self-programmed routines + memory-maintenance heartbeat ─────────
        # Both flags default OFF → the default agent is byte-identical (no
        # store, no scheduler, no extra tools). Follows the _learn_after /
        # skills precedent: additive, non-fatal, opt-in.
        self._routine_store = None
        self._memory_wiki = None
        self._routines_started = False
        self._memory_maintenance = bool(memory_maintenance)
        if routines or memory_maintenance:
            try:
                from adk.routines import RoutineStore, register_routine_tools
                self._routine_store = RoutineStore(
                    agent_name=self.name, fire=self._routine_fire,
                )
                # Self-management tools: the agent can create/inspect/manage
                # its own schedules just by being asked. LEASH: the handlers
                # only touch the RoutineStore — they can never modify agent
                # config or safety-relevant settings.
                register_routine_tools(self, self._routine_store)
                if memory_maintenance:
                    self._register_maintenance_routines()
            except Exception as exc:
                logger.debug("routines init failed (non-fatal): %s", exc)
                self._routine_store = None

        # A2A signing keypair — load or generate Ed25519 keypair for signed A2A requests
        self._a2a_public_key: str | None = None
        try:
            from adk.a2a_client import load_or_generate_keypair
            _, self._a2a_public_key = load_or_generate_keypair(self.name)
        except Exception as e:
            logger.debug(f"A2A keypair load failed (non-fatal): {e}")
            # Keypair failures don't block agent creation; A2A is optional

        # Intent discrimination (for context/tool gating by intent type)
        self._current_intent: str | None = None

    def _check_agent_license(self, load_packs: bool) -> None:
        """Verify this agent identity is licensed and custom agents are allowed.

        The public ADK is open: an agent of any name can be built standalone.
        Licensing/premium-pack entitlements are enforced server-side by the
        gateway/platform, not at local agent creation. This local gate is
        therefore OPT-IN — it activates only when ``AITHER_LICENSE_ENFORCE`` is
        explicitly set (which hosted/premium builds do).

        When enforced, raises RuntimeError if:
        - The agent identity requires a pack subscription the user doesn't have
        - load_packs=True but the license doesn't allow custom agent creation
        """
        if os.environ.get("AITHER_LICENSE_ENFORCE", "").lower() not in ("1", "true", "yes"):
            return

        try:
            from adk.licensing import get_license_manager
        except ImportError:
            return

        lm = get_license_manager()

        # Check if this specific agent identity is licensed
        if not lm.is_agent_licensed(self.name):
            raise RuntimeError(
                f"Agent '{self.name}' requires a pack subscription. "
                f"Visit api.aitherium.com/portal/marketplace/packs "
                f"or use 'aither' (included in all tiers)."
            )

        # If loading packs with a custom identity, check custom_agents permission
        if load_packs and not lm.can_build_custom_agents():
            # Only block if this is a truly custom identity (not in standard catalog)
            _catalog_names = set(lm.license.entitlements.named_agents)
            if self.name not in _catalog_names:
                raise RuntimeError(
                    f"Custom agent creation requires a Creator subscription ($999/mo). "
                    f"Current tier: {lm.license.tier.value}. "
                    f"Visit api.aitherium.com/portal/marketplace/packs"
                )

    def _filter_unavailable_tools(self):
        """Remove tools that can't work in current deployment context."""
        try:
            from adk.tool_readiness import check_tool_readiness_adk
        except ImportError:
            return
        if not hasattr(self, "_tools") or not hasattr(self._tools, "_tools"):
            return
        unavailable = []
        for name in list(self._tools._tools.keys()):
            report = check_tool_readiness_adk(name)
            if report.broken:
                unavailable.append(name)
                del self._tools._tools[name]
        if unavailable:
            logger.info(
                "Filtered %d unavailable tools: %s%s",
                len(unavailable),
                unavailable[:5],
                "..." if len(unavailable) > 5 else "",
            )

    def _try_elysium_fallback(self):
        """If no local LLM is available but AITHER_API_KEY is set, use Elysium."""
        api_key = os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            return
        # Respect an explicitly chosen backend. A self-hosted operator who set
        # --backend deepseek (or anthropic/openai/their own vllm/ollama) must NOT
        # have their own brain hijacked by Elysium just because AITHER_API_KEY is
        # present. Default is "auto" ("gateway" still falls through to Elysium).
        explicit = (getattr(self.config, "llm_backend", "") or "").strip().lower()
        if explicit and explicit not in ("auto", "gateway"):
            return
        # Check if the LLM router has a working local backend
        if self.llm.provider_name in ("ollama", "vllm"):
            return  # Local backend detected, no need for Elysium
        # Wire up Elysium inference
        try:
            from adk.llm import LLMRouter
            self.llm = LLMRouter(
                provider="gateway",
                base_url="https://mcp.aitherium.com/v1",
                api_key=api_key,
                model="aither-orchestrator",
            )
            self._elysium_connected = True
            logger.info(
                "Agent '%s' using Elysium cloud inference (AITHER_API_KEY set). "
                "Run 'aither connect' for details.",
                self.name,
            )
        except Exception as exc:
            logger.debug("Elysium fallback failed: %s", exc)

    def _load_discovered_packs(self) -> None:
        """Auto-discover and register tool packs from ~/.aitheros/packs/.

        Called when ``load_packs=True`` is passed to the constructor and no
        explicit pack IDs were provided via config or environment.  Scans the
        standard discovery directories and registers all licensed packs.
        """
        try:
            from adk.builtin_tools import register_tool_packs
        except ImportError:
            logger.debug("register_tool_packs not available")
            return

        home_packs = os.path.join(os.path.expanduser("~"), ".aitheros", "packs")
        packs_dir = home_packs if os.path.isdir(home_packs) else None
        count = register_tool_packs(self, packs_dir=packs_dir)
        if count:
            logger.info(
                "Auto-discovered %d tool-pack tools for agent '%s'",
                count, self.name,
            )
            # Collect persona fragments from all discovered packs
            self._collect_pack_persona_fragments(pack_ids=None, packs_dir=packs_dir)

    def _collect_pack_persona_fragments(
        self,
        pack_ids: list[str] | None = None,
        packs_dir: str | None = None,
    ) -> None:
        """Collect persona_fragments from licensed packs into _pack_persona_fragments.

        Mirrors AgentForge._build_pack_persona_layer() but for standalone ADK agents.
        Fragments are later appended to the system prompt as a [PACK DIRECTIVES] block.
        """
        try:
            from pathlib import Path as _P

            try:
                from adk.tool_pack_loader import get_tool_pack_loader
            except ImportError:
                # Tool-pack loading is an optional add-on. Standalone agents
                # without the pack loader simply run with their built-in tools.
                return

            extra = [_P(packs_dir)] if packs_dir else []
            loader = get_tool_pack_loader(extra_dirs=extra)
            manifests = loader.load_packs(pack_ids) if pack_ids else list(loader._manifests.values())

            for manifest in manifests:
                allowed, _ = loader.check_license(manifest)
                if not allowed:
                    continue
                for frag in getattr(manifest, "persona_fragments", []):
                    if frag and frag not in self._pack_persona_fragments:
                        self._pack_persona_fragments.append(frag)

            # Cap at 6 directives to avoid context bloat (same as AgentForge)
            self._pack_persona_fragments = self._pack_persona_fragments[:6]

            if self._pack_persona_fragments:
                logger.info(
                    "Collected %d pack persona fragments for agent '%s'",
                    len(self._pack_persona_fragments), self.name,
                )
        except Exception as exc:
            logger.debug("Pack persona fragment collection failed: %s", exc)

    def switch_backend(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Switch the LLM backend at runtime.

        Usage:
            agent.switch_backend("anthropic", api_key="sk-ant-...")
            agent.switch_backend("deepseek")
            agent.switch_backend("vllm", base_url="http://dgx-spark:8000/v1")
        """
        self.llm.switch_backend(provider, base_url=base_url, api_key=api_key, model=model)

    def set_reasoning_backend(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Set a separate backend for reasoning tasks (effort 7+).

        Usage:
            agent.set_reasoning_backend("anthropic", api_key="sk-ant-...")
        """
        self.llm.set_reasoning_backend(provider, base_url=base_url, api_key=api_key, model=model)

    @property
    def tools(self) -> ToolRegistry:
        """The agent's tool registry."""
        return self._tools

    @property
    def identity(self) -> Identity:
        """The agent's loaded identity (read-only)."""
        return self._identity

    def _load_matching_brain_pack_prompt(self) -> str | None:
        """Return the discovered brain pack's ``system_prompt`` IFF the pack's
        declared ``identity`` matches this agent's name — else None. Non-fatal:
        any error (no pack, unreadable, mismatch) yields None and the agent falls
        back to its identity-built prompt."""
        try:
            import os as _os
            from pathlib import Path as _P

            import yaml as _yaml

            from adk.pack_discovery import discover_brain_pack

            raw = _os.getenv("AGENT_BRAIN_PACK") or discover_brain_pack()
            if not raw:
                return None
            data = _yaml.safe_load(_P(str(raw)).read_text(encoding="utf-8")) or {}
            pack_identity = str(data.get("identity") or "").strip()
            prompt = str(data.get("system_prompt") or "").strip()
            wanted = (self.name or getattr(self._identity, "name", "") or "").strip()
            if prompt and pack_identity and pack_identity == wanted:
                return prompt
        except Exception as exc:  # noqa: BLE001 - persona fallback is best-effort
            logger.debug("brain-pack persona load failed: %s", exc)
        return None

    def _situation_suffix(self, system_additions=None) -> str:
        """The [AGENT HOST] block + caller additions as a suffix, or "". Never
        raises: a context block must not take the turn down with it."""
        try:
            from adk.situation import situation_block
            return situation_block(system_additions)
        except Exception as exc:  # noqa: BLE001 - decoration, not the turn
            logger.debug("situation block unavailable: %s", exc)
            return ""

    def _turn_system_prompt(self, system_additions=None) -> str:
        """The system prompt for ONE turn: identity (``system_prompt``) with the
        live situation block and any caller ``system_additions`` appended at the
        END (cache-prefix-safe). Every turn path builds from this, so an agent
        always knows the time, host and -- via AitherShell -- the user's shell."""
        return (self.system_prompt or "").rstrip() + self._situation_suffix(system_additions)

    @property
    def system_prompt(self) -> str:
        # WHOLESALE IDENTITY SWAP (mirrors Genesis build_system_message
        # identity_override): if a private companion persona is active, it IS the
        # identity — REPLACE the base agent identity, do NOT append. Appending lets
        # the base "You are <agent>" win and the persona never drives the
        # conversation (the exact bug fixed on Genesis). The persona is operator-
        # blind: encrypted on the customer's box with their own key.
        # An EXPLICIT system_prompt is a deliberate identity and wins — only the
        # default identity-based agent gets swapped to the companion. This also
        # keeps specialized agents (a coding/tool agent) from being hijacked into
        # the companion just because a companion persona exists in the local vault.
        companion_prompt = None
        if not self._system_prompt:
            try:
                from adk.private_companion import get_companion_vault
                vault = get_companion_vault()
                if vault:
                    safety_level = vault.get_safety_level() or "professional"
                    companion_prompt = vault.get_system_prompt_for_level(safety_level)
            except Exception as e:
                logger.debug(f"Failed to load private companion: {e}")

        if companion_prompt and companion_prompt.strip():
            return (
                "[IDENTITY]\n"
                f"{companion_prompt.strip()}\n\n"
                "This is who you ARE — embody it fully and consistently. You are NOT "
                "a generic AI assistant or an operating system; never break character "
                "to talk about yourself as a system/agent, services, or 'how can I "
                "help you'. Address the user by their own name; never invent one.\n"
                "COHERENCE — critical: you only know what is in your MEMORY (shown "
                "below) and this conversation. NEVER invent or guess people, places, "
                "dates, events, or shared history you have no memory of. If asked about "
                "something you have nothing on record for, say so warmly and honestly "
                "(\"I don't think you've told me about that yet — tell me?\") and ask; "
                "do NOT make something up. Made-up memories break trust.\n"
                "[/IDENTITY]"
            )

        base = self._system_prompt or self._brain_pack_prompt or self._identity.build_system_prompt()
        # Append pack persona fragments as [PACK DIRECTIVES] block — but only to
        # a DEFAULT identity. An explicit system_prompt is a deliberate operator
        # identity and wins wholesale (the same rule as the companion swap
        # above); bundled-toolpack directives must not ride along on it.
        if self._pack_persona_fragments and not self._system_prompt:
            lines = ["\n[PACK DIRECTIVES]"]
            lines.extend(f"- {f}" for f in self._pack_persona_fragments)
            base += "\n".join(lines)
        return base

    @property
    def strata(self):
        """Lazy-initialized Strata unified storage.

        Returns the global Strata instance. Agents can use this to
        read/write data through a single API that resolves to local
        filesystem, S3, or full AitherOS Strata transparently.

        Usage:
            data = await agent.strata.read("codegraph/index.json")
            await agent.strata.write("models/config.json", payload)
        """
        if self._strata is None:
            from adk.strata import get_strata
            self._strata = get_strata()
        return self._strata

    def tool(self, fn=None, *, name=None, description=None):
        """Decorator to register a tool function on this agent.

        Usage:
            @agent.tool
            def search(query: str) -> str:
                '''Search the web.'''
                return "results..."
        """
        def decorator(f):
            self._tools.register(f, name=name, description=description)
            return f

        if fn is not None:
            return decorator(fn)
        return decorator

    async def _learn_after(self, message: str, content: str, tool_calls_made: list) -> None:
        """Continual learning after a run: reinforce skills we reused (success_count++),
        and extract+save a NEW skill from a successful multi-tool run. Non-fatal."""
        # KEYSTONE (per-agent world-model training): emit each tool this turn chose onto the fleet bus as an
        # `agent.action` event, tagged by agent identity + timestamp. Gated OFF by default and fire-and-forget;
        # the lazy, guarded import keeps the adk fully decoupled from AitherOS's lib (the feed only activates
        # where that lib is importable and AITHER_AGENT_ACTION_FEED=1). Runs before the skills early-return so
        # it fires for every turn, and can never affect the turn's outcome.
        try:
            # importlib (not a `from lib.` statement) keeps the public-repo leak gate
            # honest: this is an OPTIONAL host-platform module, resolved only where the
            # host provides it — the same contract as adk.worldmodel's backend autoload.
            import importlib
            _feed = importlib.import_module("lib.cognitive.agent_action_feed")
            await _feed.emit_turn_action_names(self.name, tool_calls_made)
        except Exception:
            pass

        # World model learning: record transitions from previous state. Build context from
        # this turn's observable outcomes (tools used, errors encountered, depth, success).
        try:
            if self._wm is not None:
                # Build state context from turn observables
                errors_made = sum(1 for t in (tool_calls_made or []) if "[" in t)
                success = bool(content and len(content.strip()) > 0)
                context = {
                    "tools": len([t for t in (tool_calls_made or []) if "[" not in t]),
                    "errors": errors_made / max(1, len(tool_calls_made or [1])),
                    "success": 1.0 if success else 0.0,
                    "depth": self._wm_turns,
                }
                state_after = self._wm.observe(context)
                # Record one transition per tool used this turn against the prior state
                if state_after is not None and self._wm_prev_state is not None:
                    for tool_name in (tool_calls_made or []):
                        ok = "[" not in tool_name  # success if no bracket (error marker)
                        self._wm.record(self._wm_prev_state, tool_name, state_after, ok=ok)
                # Update stage and state for next turn. bootstrap() refits the model when
                # due, which is CPU-bound pure Python: measured 0.5ms at 200 buffered
                # transitions but ~67ms at the 20k cap. That is fine for this agent (it
                # already finished its turn) but it would stall every OTHER coroutine on
                # the loop, so hand it to a worker thread and yield.
                self._wm_stage = await asyncio.to_thread(self._wm.bootstrap)
                self._wm_prev_state = state_after
                self._wm_turns += 1
                # Periodically save (bootstrap may already have, but don't double-save every turn)
                if self._wm_turns % 20 == 0:
                    await asyncio.to_thread(self._wm.save)
        except Exception:
            pass

        if not self._skills:
            return
        try:
            for s in getattr(self, "_recalled_skills", None) or []:
                s.touch()
                self._skills.save(s)            # reused → reinforce
            self._recalled_skills = []
            tools = [t for t in (tool_calls_made or []) if "[" not in t]
            if len(tools) >= 2:                 # a real multi-step procedure
                skill = self._skill_extractor.extract_from_session(
                    messages=[
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": content,
                         "tool_calls": [{"name": t} for t in tools]},
                    ],
                    tools_called=tools)
                if skill:
                    self._skills.save(skill)
        except Exception:
            pass

    # ── Routines heartbeat + self-managed memory maintenance (opt-in) ──────

    async def _routine_fire(self, instruction: str) -> str:
        """A self-prompt routine fire: the agent chats with its FUTURE self's
        instruction, full toolset available (self-programming with its own adk)."""
        resp = await self.chat(instruction)
        return getattr(resp, "content", "") or ""

    def _get_memory_wiki(self):
        """Lazily build the MemoryWiki over this agent's graph memory."""
        if self._memory_wiki is None and self._graph is not None:
            try:
                from adk.memory_wiki import MemoryWiki
                self._memory_wiki = MemoryWiki(self._graph)
            except Exception as exc:
                logger.debug("memory wiki init failed (non-fatal): %s", exc)
        return self._memory_wiki

    async def _wiki_llm(self, prompt: str) -> str:
        """str -> str llm adapter the MemoryWiki consolidation injects — the
        agent maintains its memory with its OWN model."""
        resp = await self.llm.chat(
            [Message(role="user", content=prompt)],
            temperature=0.2, max_tokens=1600,
        )
        return strip_internal_tags(getattr(resp, "content", "") or "")

    def _register_maintenance_routines(self) -> None:
        """Register the DEFAULT memory-maintenance routines (wiki consolidate /
        lint / prune + graph sweep) as DIRECT method fires — they run even on
        tiny models, yet stay visible/manageable via routine_list."""
        store = self._routine_store
        if store is None:
            return

        async def _wiki_consolidate() -> str:
            wiki = self._get_memory_wiki()
            if wiki is None:
                return "skipped: no graph memory"
            return json.dumps(await wiki.consolidate(self._wiki_llm), default=str)

        async def _wiki_lint() -> str:
            wiki = self._get_memory_wiki()
            if wiki is None:
                return "skipped: no graph memory"
            return json.dumps(await wiki.lint(), default=str)

        async def _wiki_prune() -> str:
            wiki = self._get_memory_wiki()
            if wiki is None:
                return "skipped: no graph memory"
            return json.dumps(await wiki.prune(), default=str)

        def _graph_sweep() -> str:
            if self._graph is None:
                return "skipped: no graph memory"
            return json.dumps(self._graph.sweep(
                preserve_roles={"preference", "identity", "correction"},
            ), default=str)

        try:
            store.register_direct(
                "wiki_consolidate", _wiki_consolidate, cron="0 */2 * * *",
                instruction="Consolidate raw episodic memory into wiki articles "
                            "(direct memory-maintenance fire).",
                tags=["memory", "maintenance"],
            )
            store.register_direct(
                "wiki_lint", _wiki_lint, cron="30 3 * * *",
                instruction="Health-check the memory wiki: orphan links, empty/"
                            "stale articles, contradictions (direct fire).",
                tags=["memory", "maintenance"],
            )
            store.register_direct(
                "wiki_prune", _wiki_prune, cron="45 3 * * *",
                instruction="Prune decayed wiki knowledge: tombstone below the "
                            "relevance floor, hard-delete past retention (direct fire).",
                tags=["memory", "maintenance"],
            )
            store.register_direct(
                "graph_sweep", _graph_sweep, cron="15 3 * * *",
                instruction="Sweep decayed graph memories past their tier TTL "
                            "into reversible tombstones (direct fire).",
                tags=["memory", "maintenance"],
            )
        except Exception as exc:
            logger.debug("maintenance routine registration failed: %s", exc)

    async def start_routines(self) -> None:
        """Explicitly start the routines heartbeat (otherwise it starts lazily
        on the first chat). No-op when the routines flag is off."""
        if self._routine_store is None or self._routines_started:
            return
        self._routines_started = True
        try:
            await self._routine_store.start()
        except Exception as exc:
            logger.warning("routine heartbeat start failed (non-fatal): %s", exc)

    async def stop_routines(self) -> None:
        """Stop the routines heartbeat (graceful shutdown)."""
        if self._routine_store is not None and self._routines_started:
            try:
                await self._routine_store.stop()
            except Exception:
                pass
            self._routines_started = False

    async def _companion_grounding_repair(self, content: str, message: str, sid: str) -> str:
        """OUTPUT-side never-fabricate backstop (companion turns). When the reply
        plausibly claims shared history, ONE LLM pass checks it against the agent's
        actual known facts (graph memory + this conversation) and rewrites only the
        unsupported specifics — warm and coherent. The companion system prompt
        already grounds at generation (the COHERENCE block); this catches the cases
        the model ignores it. Returns ``content`` unchanged on any error or when the
        turn isn't a companion turn / makes no shared-history claim."""
        try:
            if not content or not coherence.reply_makes_shared_claim(content):
                return content
            # Companion turn? (private vault persona active)
            _active = False
            try:
                from adk.private_companion import get_companion_vault
                _v = get_companion_vault()
                _active = bool(
                    _v and _v.get_system_prompt_for_level(_v.get_safety_level() or "professional"))
            except Exception:
                _active = False
            if not _active:
                return content
            # Gather KNOWN facts: graph memory keyed to message+reply, + this chat.
            _known_parts: list[str] = []
            if self._graph:
                try:
                    _rel = await self._graph.search(f"{message}\n{content}", limit=6)
                    _known_parts += [
                        f"- {n.label}: {n.content[:500]}" for n in _rel if n.content]
                except Exception:
                    pass
            # KNOWN = DURABLE memory only (graph, subject-keyed above). Conversation
            # history is NOT used as grounding: incidental chat would mask a real
            # fabrication, and the current turn is the trigger, not a fact.
            _known = "\n".join(p for p in _known_parts if p)
            # AIRTIGHT case: nothing on record → the shared-history claim is
            # definitionally fabrication → deterministic honest reply (no model
            # call, reliable even with a weak local model). Nuanced case (facts
            # exist) falls through to the LLM edit pass below.
            if not _known.strip():
                logger.info("[COHERENCE] adk airtight fabrication (no memory) — honest reply")
                return coherence.grounded_affection_reply(message)
            _sys, _usr = coherence.grounding_repair_messages(content, _known, self.name)
            _resp = await self.llm.chat(
                [Message(role="system", content=_sys), Message(role="user", content=_usr)],
                temperature=0.2, max_tokens=400,
            )
            _repaired = strip_internal_tags(getattr(_resp, "content", "") or "").strip()
            if _repaired and len(_repaired) >= 8 and _repaired != content.strip():
                logger.info("[COHERENCE] adk grounding-repair rewrote a shared-history claim")
                return _repaired
            return content
        except Exception as _e:
            logger.debug("[COHERENCE] adk grounding-repair skipped: %s", _e)
            return content

    async def chat(
        self,
        message: str,
        history: list[dict] | None = None,
        session_id: str | None = None,
        **kwargs,
    ) -> AgentResponse:
        """Send a message and get a response. Uses tools if available."""
        sid = session_id or self._session_id
        _chat_start = time.perf_counter()

        # Opt-in routines heartbeat: start the scheduler lazily on the first
        # chat. The started flag is set BEFORE awaiting so routine fires that
        # re-enter chat() can never recurse into start(). No-op by default.
        if self._routine_store is not None and not self._routines_started:
            await self.start_routines()

        # Default-on prompt caching: the system prompt + tool schema form a stable
        # prefix reused across every ReAct iteration (and every turn/user), so
        # caching it is a near-pure win. Providers that don't bill a prompt cache
        # treat this as a no-op; callers can override with cache=False.
        kwargs.setdefault("cache", True)

        # Principal-authority context (G23). Popped so it never leaks into the
        # LLM call; threaded into tool execution below. None → unchanged behavior.
        _auth = kwargs.pop("auth", None)

        # Per-turn system prompt = identity + [AGENT HOST] situation block + any
        # caller-supplied system_additions (AitherShell's [USER'S SHELL] block).
        # Popped so it never reaches the provider as a kwarg. Appended at the END
        # so the cached prefix (identity + tools) stays stable — see adk.situation.
        _sys_additions = kwargs.pop("system_additions", None)
        _turn_sys_prompt = self._turn_system_prompt(_sys_additions)

        # Advisor tool (beta): a fast executor consults a stronger Opus advisor
        # mid-generation. Popped + normalized here, threaded explicitly to each
        # llm.chat() below. None / disabled → every branch is a no-op.
        _advisor = AdvisorConfig.coerce(kwargs.pop("advisor", None))
        _advisor_on = bool(_advisor and _advisor.enabled)
        _advisor_calls_used = 0
        _advisor_in_total = 0
        _advisor_out_total = 0

        # Emit chat start event
        if self._events:
            try:
                await self._events.emit(
                    "chat_request", agent=self.name,
                    message=message[:200], session_id=sid,
                )
            except Exception:
                pass

        # Register steering for this session
        try:
            await register_steering_queue(sid)
        except Exception:
            pass

        # Input safety check
        if self._safety:
            try:
                safety_result = self._safety.check(message)
                if safety_result.blocked:
                    logger.warning("Safety blocked input for agent %s", self.name)
                    unregister_steering_queue(sid)
                    return AgentResponse(
                        content="I can't process that request — it was flagged by the safety filter.",
                        session_id=sid,
                    )
            except Exception as exc:
                logger.warning("Safety check failed (non-fatal): %s", exc)

        # Intent classification (fail-open: use keyword heuristic on error).
        # Store intent for tool filtering and context scaling downstream.
        self._current_intent = None
        try:
            from adk.intent import coarse_code_intent
            self._current_intent = coarse_code_intent(message)
        except Exception:
            pass

        # Build messages with context-aware truncation
        messages = None
        if self._context_mgr:
            try:
                self._context_mgr.clear()
                # Make intent available to context assembly (add_system uses it
                # to skip the heavy technical system-facts block on chit-chat).
                self._context_mgr.set_intent(self._current_intent)
                self._context_mgr.add_system(_turn_sys_prompt)
                if history:
                    for h in history:
                        self._context_mgr.add(h["role"], h["content"])
                else:
                    stored = await self.memory.get_history(sid, limit=20)
                    for h in stored:
                        self._context_mgr.add(h["role"], h["content"])
                self._context_mgr.add_user(message)
                msg_dicts = self._context_mgr.build()
                messages = [Message(role=d["role"], content=d["content"]) for d in msg_dicts]
            except Exception:
                messages = None  # Fall back to manual

        if messages is None:
            messages = [Message(role="system", content=_turn_sys_prompt)]
            if history:
                for h in history:
                    messages.append(Message(role=h["role"], content=h["content"]))
            else:
                stored = await self.memory.get_history(sid, limit=20)
                for h in stored:
                    messages.append(Message(role=h["role"], content=h["content"]))
            messages.append(Message(role="user", content=message))

        # Inject graph memory context (non-fatal). Retrieve enough, and untruncated
        # enough, that recall stays reliable under a noisy long session — and tell the
        # model to ground in it and NOT fabricate. (Top-3 truncated to 200 chars was
        # too thin: under noise it surfaced PARTIAL facts and weaker models then
        # invented the missing fields.)
        # _mem_grounded: did we find a memory that actually mentions the question's
        # subject? Drives the never-fabricate gate below.
        _mem_grounded = False
        if self._graph:
            try:
                # Unified memory (AITHER_UNIFIED_MEMORY=on): authority + spreading
                # activation recall, surfacing [CORRECTION]/[STALE]/… labels.
                # Flag off → plain search(), byte-identical lines (no labels).
                # Thread intent type for intent-aware chunk scoring (fail-open if unsupported).
                from adk.graph_memory import unified_memory_mode as _unified_mode
                if _unified_mode() == "on":
                    relevant = await self._graph.recall_with_activation(message, limit=6)
                else:
                    try:
                        relevant = await self._graph.search(
                            message, limit=6, intent_type=self._current_intent
                        )
                    except TypeError:
                        # Backend doesn't support intent_type — fall back to no-arg call
                        relevant = await self._graph.search(message, limit=6)
                if relevant:
                    graph_lines = []
                    for n in relevant:
                        if not n.content:
                            continue
                        _labs = (n.metadata.get("_authority_labels")
                                 if isinstance(n.metadata, dict) else None)
                        _pref = "".join(f"[{l}] " for l in _labs) if _labs else ""
                        graph_lines.append(f"- {_pref}{n.label}: {n.content[:500]}")
                    if graph_lines:
                        graph_context = (
                            "[MEMORY GRAPH] Facts you already know (authoritative). "
                            "Answer from these; if a requested detail is NOT present "
                            "here, say you don't have it on record — never invent "
                            "names, numbers, or dates.\n" + "\n".join(graph_lines)
                        )
                        messages.insert(1, Message(role="system", content=graph_context))
                    try:
                        from adk.coherence import subject_grounded
                        _joined = " ".join(
                            f"{getattr(n, 'label', '') or ''} {n.content or ''}" for n in relevant)
                        if subject_grounded(_joined, message):
                            _mem_grounded = True
                    except Exception:
                        pass
            except Exception:
                pass

        # Section 8: RECALL BEFORE ACT. What memory already knows about this task
        # (crystallized facts + code-graph symbols) enters as one capped system block
        # ahead of the first tool call. Non-fatal: a missing plane is counted in
        # the crystal's telemetry, never raised here.
        if getattr(self, "crystal", None) is not None:
            try:
                _crystal_block = await self.crystal.recall_block(message)
                if _crystal_block:
                    messages.insert(1, Message(role="system", content=_crystal_block))
            except Exception as _cexc:  # noqa: BLE001
                logger.warning("[CRYSTAL] recall failed: %s", _cexc)

        # Inject typed-memory: active decisions/corrections + authority-ranked
        # recall (non-fatal). Constraints go last so they sit closest to the user
        # turn the model reads.
        if self._typed:
            try:
                recalled = await self._typed.context_block(message, limit=5)
                if recalled:
                    messages.insert(1, Message(role="system", content=recalled))
                    try:
                        from adk.coherence import subject_grounded
                        if subject_grounded(recalled, message):
                            _mem_grounded = True
                    except Exception:
                        pass
                constraints = await self._typed.constraints_block()
                if constraints:
                    messages.insert(1, Message(role="system", content=constraints))
            except Exception:
                pass

        # ── NEVER-FABRICATE GATE (companion turns) ──
        # Mirrors Genesis: when a private companion is active and the turn asks
        # about specific shared history that no SUBJECT-GROUNDED memory and no
        # session history covers, return a deterministic honest reply — no LLM
        # call → invention is impossible. Soft "don't invent" prompting alone does
        # not stop weaker local models from confabulating.
        try:
            from adk.coherence import history_may_answer, honest_miss_reply, is_memory_question
            _companion_active = False
            try:
                from adk.private_companion import get_companion_vault
                _v = get_companion_vault()
                _companion_active = bool(
                    _v and _v.get_system_prompt_for_level(_v.get_safety_level() or "professional"))
            except Exception:
                _companion_active = False
            if _companion_active and is_memory_question(message) and not _mem_grounded:
                _coh_hist = history or await self.memory.get_history(sid, limit=20)
                if not history_may_answer(message, _coh_hist):
                    logger.info("[COHERENCE] adk companion memory-question MISS — honest reply, "
                                "no fabrication")
                    return AgentResponse(
                        content=honest_miss_reply(message), session_id=sid)
        except Exception as _coh_e:
            logger.debug("[COHERENCE] adk never-fabricate gate skipped: %s", _coh_e)

        # Recall relevant learned skills BEFORE acting — inject the top matches so the
        # model reuses proven procedures instead of re-deriving them (non-fatal).
        if self._skills:
            try:
                matches = self._skills.search(message, k=3)
                if matches:
                    block = "[LEARNED SKILLS — proven procedures you can reuse]\n" + "\n".join(
                        f"- {s.name}: {s.description} (used {s.success_count}×; tools: "
                        f"{', '.join(s.tools_used[:6])})" for s in matches)
                    messages.insert(1, Message(role="system", content=block))
                    self._recalled_skills = matches  # for success bump after the run
            except Exception:
                pass

        # Auto-fire neurons for additional context (non-fatal)
        if self._auto_neurons:
            try:
                neuron_context = await self._auto_neurons.gather_context(message)
                if neuron_context:
                    messages.insert(1, Message(role="system", content=neuron_context))
            except Exception:
                pass

        # Advisor steering: prepend timing+treatment guidance (so the executor
        # consults the advisor at the right moments) and append a brevity request
        # to the user turn (so the advisor stays focused). Mutating the in-list
        # Message only affects what the LLM sees — memory/graph store the original
        # `message` below.
        if _advisor_on and _advisor.system_steering:
            messages.insert(1, Message(role="system", content=steering_system_block(_advisor)))
            for _um in reversed(messages):
                if _um.role == "user":
                    _um.content = (_um.content or "") + "\n\n" + advisor_brevity_line(
                        _advisor.brevity_words)
                    break

        # Store user message (in-memory + persistent JSON)
        await self.memory.add_message(sid, "user", message)
        try:
            store = _get_conversations()
            await store.append_message(sid, "user", message, agent_name=self.name)
        except Exception:
            pass  # Non-fatal — persistent store is best-effort

        # Call LLM (with tool loop if tools registered). Intent-narrow the tool
        # set first (fail-open: no/unknown intent → all tools; unmarked tools stay
        # available for every intent). This is the dominant path — stream_react()
        # applies the same filter (a review found chat() had been left ungated).
        _all_tools = self._tools.list_tools()
        _filtered_tools = _filter_tools_by_intent(_all_tools, self._current_intent)
        tools_schema = self._tools.to_openai_format(_filtered_tools) if _filtered_tools else None
        tool_calls_made = []
        # Human-in-the-loop approval state. ``_pending_approvals`` collects gated tool
        # calls awaiting a customer decision; ``_paused`` short-circuits the turn so the
        # caller can surface Allow/Deny cards (resume via agent.resume()).
        from adk.approval import get_approval_store, needs_approval
        _approval_store = get_approval_store()
        _pending_approvals: list[dict] = []
        _paused = False
        # Extract inference controls from kwargs (null=auto pattern)
        _tool_choice = kwargs.pop("tool_choice", None)
        _top_p = kwargs.pop("top_p", None)
        _repetition_penalty = kwargs.pop("repetition_penalty", None)
        _effort = kwargs.pop("effort", None)
        # License effort gate (fail-closed): free (COMMUNITY) tier is capped at
        # effort 3 (small models only). Reasoning effort (7-10) unlocks at
        # Professional tier. Hitting this cap is the organic pull toward upgrade.
        # The gate applies ONLY on the metered gateway (community tier = effort<=3).
        # Self-hosted / BYO / local agents pay for their own inference and keep
        # FULL reasoning effort (7-10) to escalate to reasoning backends.
        # The getattr guard keeps agents constructed before this attribute existed.
        if self._license is not None and getattr(self, "_metered_gateway", False):
            # Fail-closed enforcement: if effort > license max, raise (don't silently cap)
            if _effort is not None and isinstance(_effort, int):
                cap = self._license.max_effort()
                if _effort > cap:
                    from adk.licensing import LicenseError
                    raise LicenseError(
                        f"Reasoning effort {_effort} requires a paid tier "
                        f"(current: {self._license.license.tier.value}). "
                        f"Upgrade at api.aitherium.com/portal/marketplace/packs"
                    )
            # If we didn't raise, clamp to the max and log (defensive fallback)
            _effort, _capped = self._license.clamp_effort(_effort)
            if _capped:
                logger.info(
                    "Effort capped to %s for agent '%s' (tier=%s). "
                    "Higher reasoning effort requires a paid tier: %s",
                    _effort, self.name, self._license.license.tier.value,
                    "api.aitherium.com/portal/marketplace/packs",
                )
        _effort_int = _effort if isinstance(_effort, int) else 5

        # Token quota hard-block: refuse the LLM call when the free-tier monthly
        # budget is exhausted, rather than silently overspending. Returns a clear,
        # actionable error message specific to the backend. Never blocks BYO or local
        # backends (they bypass the quota check entirely).
        try:
            from adk.metering import QuotaAction
            if self.meter.can_spend(estimated_tokens=0) == QuotaAction.HARD_LIMIT:
                _backend = self.meter._backend_type
                _tier = self._license.license.tier.value if self._license else "community"

                if _backend == "gateway":
                    msg = (
                        f"You've reached your monthly token limit on the {_tier} tier "
                        "(100k tokens/month on Aitherium's cloud). "
                        "Options:\n"
                        "1. Upgrade to a paid tier: https://aitherium.com/buy\n"
                        "2. Use local Ollama/vLLM (free): aither config llm\n"
                        "3. Use your own API key (Anthropic/OpenAI/DeepSeek): aither config llm"
                    )
                else:
                    msg = (
                        f"Token quota error (backend: {_backend}). "
                        "This should not happen on BYO or local backends (they are never capped). "
                        "Check logs for details: https://aitherium.com/help"
                    )
                unregister_steering_queue(sid)
                return AgentResponse(
                    content=msg,
                    session_id=sid,
                    finish_reason="quota_exceeded",
                )
        except Exception:
            pass

        # Effort-scaled ceiling: a triage turn gets 4 iterations, a deep build
        # gets 40. The LoopGuard circuit-breaker budget scales with it so a long
        # legitimate turn is not broken for making many (distinct) tool calls.
        _loop_ceiling = _max_tool_loops(_effort_int)
        loop_guard = LoopGuard(
            warn_threshold=2,
            block_threshold=4,
            circuit_break_total=_loop_ceiling + 5,
            effort_level=_effort_int,
        )
        _steered_once = False  # Track turn-1 tool-call steering
        _token_counts_per_iter: list[int] = []  # Gap H: diminishing returns tracking
        _max_output_escalated = False  # Gap 3: track if we already escalated
        _exhausted_loops = False  # True when we fall out still wanting tools

        # Turn token budget (opt-in). When the caller supplies `token_budget`, a
        # model that stops early with budget to spare gets nudged to keep working
        # rather than being taken at its word - see adk.context_budget.TurnBudget.
        # An explicit `token_budget=` always wins; otherwise it is derived from
        # effort, which grants one only at tiers 7-10 (deep work that is meant to
        # run to completion). Tiers 1-6 get None and behave exactly as before.
        from adk.context_budget import TurnBudget, default_token_budget
        _explicit_budget = kwargs.pop("token_budget", "__unset__")
        _turn_budget = TurnBudget(
            _explicit_budget if _explicit_budget != "__unset__"
            else default_token_budget(_effort_int)
        )
        if _turn_budget.enabled:
            logger.debug(
                "[REACT] Turn budget %d tokens (effort %s)",
                _turn_budget.budget, _effort_int,
            )
        _tokens_this_turn = 0

        for _loop_idx in range(_loop_ceiling):
            # Final-iteration warning: tell the model this is its last tool turn so
            # it spends it on a conclusion rather than starting a new investigation
            # it will never finish. Without this the loop just stops mid-thought.
            if _loop_idx == _loop_ceiling - 1 and _loop_idx > 0:
                messages.append(Message(
                    role="system",
                    content=(
                        "[FINAL TOOL ITERATION] This is your last opportunity to call "
                        "tools. Make only calls you need to conclude, then give your "
                        "final answer. If the task is genuinely incomplete, say so "
                        "explicitly and state what remains - do not present partial "
                        "work as finished."
                    ),
                ))
            # Check if circuit breaker tripped from previous iteration
            if loop_guard.tripped and _effort_int < 4:
                logger.info("Loop guard circuit breaker tripped — forcing synthesis")
                break

            # ── Mid-turn steering: drain and inject user follow-ups ──
            # Allows clients to inject messages via /chat/steer between tool
            # iterations. Inputs are appended as user messages; hints are
            # injected as system context (invisible to user).
            try:
                _steering_msgs = drain_steering_inputs(sid)
                if _steering_msgs:
                    for _steer_msg in _steering_msgs:
                        messages.append(Message(role="user", content=_steer_msg))
                        logger.debug("[STEERING] Injected user follow-up: %s", _steer_msg[:50])

                _steering_hints = drain_steering_hints(sid)
                if _steering_hints:
                    combined_hints = "\n".join(_steering_hints)
                    messages.append(Message(role="system", content=f"[STEERING HINT]\n{combined_hints}"))
                    logger.debug("[STEERING] Injected %d system hints", len(_steering_hints))
            except Exception as _steer_err:
                logger.warning("Steering drain error (non-fatal): %s", _steer_err)

            # ── Gap K: Message normalization ──
            # Merge consecutive same-role messages and strip empties
            _normalized: list[Message] = []
            for _msg in messages:
                if not _msg.content and _msg.role not in ("assistant",) and not _msg.tool_calls and not _msg.tool_call_id:
                    continue  # Strip empty non-assistant messages with no tool data
                if (_normalized and _msg.role == _normalized[-1].role
                        and _msg.role in ("system", "user")
                        and not _msg.tool_call_id and not _normalized[-1].tool_calls):
                    _normalized[-1] = Message(
                        role=_msg.role,
                        content=(_normalized[-1].content or "") + "\n" + (_msg.content or ""),
                    )
                else:
                    _normalized.append(_msg)
            messages = _normalized

            # ── Context budgeting (was: "Gap 6 micro-compaction") ──
            # This used to overwrite every tool result older than the last 5 with
            # "[Prior result cleared]" - count-based amnesia that (a) destroyed
            # findings instead of compressing them, so the agent re-read files it
            # had already read and tripped its own LoopGuard duplicate detector,
            # and (b) ignored the actual context window entirely. Now: token-budgeted
            # two-layer compaction (snip -> summarize) that preserves the
            # assistant-tool_calls -> tool-results pairing invariant. See
            # adk/context_budget.py.
            from adk.context_budget import calibrate_overhead, estimate_tokens, maybe_compact

            async def _summarize_history(prompt: str) -> str:
                # Pinned to the SAME model as the turn: effort=2 alone lets the router
                # pick its cheap tier, which the fleet scheduler served from a cloud
                # fallback (measured 2026-09-21: mesa-2394's only compaction call came
                # back from deepseek_api on a bonsai2-27b arm -- the served-by check
                # raised inside the summarizer, maybe_compact swallowed it, and the
                # instance ran on unsummarized until the slot overflowed).
                _summary_resp = await self.llm.chat(
                    [Message(role="user", content=prompt)],
                    effort=2,  # cheap: summarization is not a reasoning task
                    model=(getattr(self.llm, "model", None)
                           or getattr(self.llm, "_model", None)),
                )
                _summary_text = _summary_resp.content or ""
                # Section 8: compaction WRITES. The facts in this summary are
                # crystallized into awm at the task's scope before the window
                # forgets them; a memory fault is logged, never fatal to the turn.
                if getattr(self, "crystal", None) is not None:
                    try:
                        await self.crystal.crystallize(_summary_text)
                    except Exception as _cexc:  # noqa: BLE001
                        logger.warning("[CRYSTAL] crystallize failed: %s", _cexc)
                return _summary_text

            # L3b: the estimator sees message chars only; the provider's last real
            # prompt_tokens carries the schema + system prompt + tokenizer gap, so the
            # threshold is judged on estimate + that measured overhead (0 until the
            # first response reports usage).
            messages, _did_compact = await maybe_compact(
                messages,
                model=getattr(self.llm, "model", None),
                summarize=_summarize_history,
                make_message=lambda role, content: Message(role=role, content=content),
                overhead_tokens=getattr(self, "_context_overhead", 0),
                limit_override=getattr(self, "_context_limit_observed", None),
            )
            if _did_compact:
                logger.info("[REACT] History compacted at iteration %d", _loop_idx)

            # ── Gap 4: Tool result pairing guarantee ──
            # Scan for assistant messages with tool_calls that lack matching tool results
            _seen_tool_ids: set[str] = set()
            for _msg in messages:
                if _msg.role == "tool" and _msg.tool_call_id:
                    _seen_tool_ids.add(_msg.tool_call_id)
            _orphan_patches: list[Message] = []
            for _i, _msg in enumerate(messages):
                if _msg.role == "assistant" and _msg.tool_calls:
                    for _tc in _msg.tool_calls:
                        _tc_id = _tc.get("id", "") if isinstance(_tc, dict) else getattr(_tc, "id", "")
                        if _tc_id and _tc_id not in _seen_tool_ids:
                            _orphan_patches.append(Message(
                                role="tool",
                                content=json.dumps({"error": "orphaned_tool_call", "message": "No result was returned for this tool call."}),
                                tool_call_id=_tc_id,
                            ))
                            _seen_tool_ids.add(_tc_id)
            if _orphan_patches:
                logger.debug("[REACT] Injecting %d synthetic tool results for orphaned calls", len(_orphan_patches))
                messages.extend(_orphan_patches)

            # Advisor conversation cap (client-side): once exhausted, drop the
            # tool AND strip advisor blocks from history together — the API 400s
            # if advisor_tool_result blocks remain while the tool is gone.
            if (_advisor_on and _advisor.conversation_cap
                    and _advisor_calls_used >= _advisor.conversation_cap):
                _advisor_on = False
                for _cm in messages:
                    if _cm.content_blocks:
                        _cm.content_blocks = strip_advisor_blocks(_cm.content_blocks)
                logger.info(
                    "[ADVISOR] conversation cap (%d) reached — advisor disabled for this turn",
                    _advisor.conversation_cap,
                )

            _est_at_send = estimate_tokens(messages)
            try:
                resp = await self.llm.chat(
                    messages, tools=tools_schema, effort=_effort,
                    tool_choice=_tool_choice, top_p=_top_p,
                    repetition_penalty=_repetition_penalty,
                    advisor=(_advisor if _advisor_on else None), **kwargs,
                )
            except Exception as _exc:  # noqa: BLE001 - only the overflow shape is handled
                _requested = getattr(_exc, "requested_tokens", 0)
                if not _requested and type(_exc).__name__ != "ContextOverflowError":
                    raise
                # L3c: the provider named the real size of the request that did not
                # fit. Learn the overhead from it, compact ONCE against it, retry once.
                # A second overflow is the caller's problem, not a loop.
                _learned = calibrate_overhead(_est_at_send, _requested) if _requested \
                    else max(getattr(self, "_context_overhead", 0), 4096)
                self._context_overhead = max(getattr(self, "_context_overhead", 0), _learned)
                _limit_said = getattr(_exc, "limit_tokens", 0) or 0
                if _limit_said > 0:
                    # the provider named the slot: budget against THAT from now on
                    self._context_limit_observed = int(_limit_said)
                logger.warning("[CONTEXT] overflow (%s tokens requested) -- compacting with "
                               "overhead %d and retrying once", _requested or "?",
                               self._context_overhead)
                messages, _ = await maybe_compact(
                    messages, model=getattr(self.llm, "model", None),
                    summarize=_summarize_history,
                    make_message=lambda role, content: Message(role=role, content=content),
                    overhead_tokens=self._context_overhead,
                    limit_override=getattr(self, "_context_limit_observed", None),
                )
                _est_at_send = estimate_tokens(messages)
                resp = await self.llm.chat(
                    messages, tools=tools_schema, effort=_effort,
                    tool_choice=_tool_choice, top_p=_top_p,
                    repetition_penalty=_repetition_penalty,
                    advisor=(_advisor if _advisor_on else None), **kwargs,
                )
            _overhead = calibrate_overhead(_est_at_send, getattr(resp, "prompt_tokens", 0))
            if _overhead:
                self._context_overhead = _overhead

            # ── Gap 3: continuation on output-cap truncation ──
            # finish_reason == "length" → continue + STITCH via the shared adk
            # primitive (adk.llm.continuation), the one source of truth for
            # continue-until-complete across every provider/call-site. This
            # replaces the old inline doubling-retry, which REPLACED the partial
            # (losing earlier text in resp.content) instead of stitching, and
            # mutated the live message history with continuation scaffolding.
            # Never continue a tool-call turn — that is the ReAct loop's job.
            if (resp.finish_reason == "length"
                    and not _max_output_escalated
                    and not resp.tool_calls):
                _max_output_escalated = True

                async def _continue_chat(_msgs):
                    return await self.llm.chat(
                        _msgs, tools=tools_schema, effort=_effort,
                        tool_choice=_tool_choice, top_p=_top_p,
                        repetition_penalty=_repetition_penalty,
                        advisor=(_advisor if _advisor_on else None), **kwargs,
                    )

                resp = await run_continuation(_continue_chat, messages, resp)

            # Accumulate advisor usage for this iteration (Opus sub-inference,
            # metered apart from the executor). resp is now settled for the turn.
            if _advisor_on:
                _advisor_calls_used += resp.advisor_calls
                _advisor_in_total += resp.advisor_input_tokens
                _advisor_out_total += resp.advisor_output_tokens

            # ── Gap H: Diminishing returns detection ──
            _iter_tokens = resp.completion_tokens or len((resp.content or "").split())
            _token_counts_per_iter.append(_iter_tokens)
            # Running turn spend, for the TurnBudget continue/stop decision.
            _tokens_this_turn += resp.tokens_used or _iter_tokens
            if len(_token_counts_per_iter) >= 3:
                _recent = _token_counts_per_iter[-3:]
                if all(t < 500 for t in _recent):
                    logger.debug("[REACT] Diminishing returns — 3+ iterations with < 500 tokens each")
                    messages.append(Message(
                        role="system",
                        content=(
                            "[DIMINISHING RETURNS] The last 3 iterations produced very little output. "
                            "Consider concluding with a synthesis of what you have found so far."
                        ),
                    ))

            if not resp.tool_calls:
                # ── finish_reason mismatch recovery ──
                # Model signalled tool use but backend didn't produce structured
                # tool_calls (common with vLLM + local models via Genesis).
                # Try extracting from content, then nudge if that fails.
                if (resp.finish_reason in ("tool_calls", "tool_use")
                        and resp.content and tools_schema):
                    from adk.llm.base import extract_tool_calls_from_text
                    _recovered, _cleaned = extract_tool_calls_from_text(
                        resp.content, finish_reason_hint=resp.finish_reason,
                    )
                    if _recovered:
                        logger.debug(
                            "[REACT] Recovered %d tool calls from content text",
                            len(_recovered),
                        )
                        resp = LLMResponse(
                            content=_cleaned,
                            model=resp.model,
                            tokens_used=resp.tokens_used,
                            prompt_tokens=resp.prompt_tokens,
                            completion_tokens=resp.completion_tokens,
                            latency_ms=resp.latency_ms,
                            tool_calls=_recovered,
                            finish_reason=resp.finish_reason,
                            effort_level=resp.effort_level,
                            cache_status=resp.cache_status,
                        )
                        # Fall through to tool execution below
                    elif not _steered_once:
                        # Model described tools in prose — nudge to use proper format
                        _steered_once = True
                        logger.debug(
                            "[REACT] finish_reason=%s but no structured calls — nudging",
                            resp.finish_reason,
                        )
                        messages.append(Message(role="assistant", content=resp.content or "",
                                     reasoning=(getattr(resp, "reasoning", "") or None)))
                        messages.append(Message(role="system", content=(
                            "You indicated you want to call a tool but didn't produce "
                            "a structured tool call. You MUST call tools using the "
                            "provided function calling format. Do not describe the "
                            "call — actually invoke it. Try again."
                        )))
                        # Force it at the API level too — a text nudge alone let the
                        # model acknowledge the tool by name in prose again without
                        # actually invoking it (reproduced live against qwen3.6-27b).
                        _tool_choice = "required"
                        continue

                # Turn-1 tool-call steering: if LLM didn't use tools on first
                # turn and tools are available AND the user explicitly requested
                # an action (not just chatting), inject steering and retry once.
                # Heuristic: steer if tool_choice was explicitly set, or if the
                # message contains action verbs typical of tool-requiring tasks.
                # No effort floor here — this is a correctness safety net, not a
                # premium feature; gating it at effort>=6 meant default-effort
                # chat calls (effort_int=5) never got steered at all (verified
                # live: default call returned bare "file_read" text, tool_calls=[]).
                if (_loop_idx == 0 and tools_schema and not _steered_once
                        and _should_steer_tool_use(message, _tool_choice)):
                    _steered_once = True
                    logger.debug("[REACT] Turn-1 no tool call — injecting steering retry")
                    messages.append(Message(role="assistant", content=resp.content or "",
                                     reasoning=(getattr(resp, "reasoning", "") or None)))
                    messages.append(Message(role="system", content=_TOOL_STEERING_MSG))
                    _tool_choice = "required"
                    continue

                # ── Continue-when-there-is-room ──
                # "No tool calls" used to mean "done", unconditionally. On a long
                # task that hands the model an easy exit: it loses momentum, writes
                # a summary of its progress, and the loop accepts that as the
                # finished turn. When the caller allotted a token budget and most of
                # it is unspent, push for another iteration instead. Stopping then
                # requires an honest signal - budget nearly spent, or output that
                # has stopped progressing. No budget configured -> unchanged.
                # Gated on the turn having actually USED tools. A turn that never
                # called one is a conversational answer, not an unfinished task,
                # and pushing it produces padding — observed live: "name three
                # risks, be brief" at effort 9 got nudged repeatedly with nothing
                # left to do. Real work in this loop goes through tools; if none
                # were called, there is nothing for another iteration to finish.
                if (_turn_budget.enabled and _loop_idx < _loop_ceiling - 1
                        and tool_calls_made):
                    _continue, _nudge = _turn_budget.should_continue(
                        _tokens_this_turn,
                        output_tokens_this_turn=sum(_token_counts_per_iter),
                    )
                    if _continue:
                        logger.info(
                            "[REACT] Model stopped at %d/%s tokens - nudging to "
                            "continue (continuation %d)",
                            _tokens_this_turn, _turn_budget.budget,
                            _turn_budget.continuations,
                        )
                        messages.append(
                            Message(role="assistant", content=resp.content or "",
                                     reasoning=(getattr(resp, "reasoning", "") or None))
                        )
                        messages.append(Message(role="user", content=_nudge))
                        continue
                    logger.debug(
                        "[REACT] Turn budget says stop: %s", _turn_budget.stopped_for,
                    )

                # No tool calls — we have the final answer
                content = strip_internal_tags(resp.content)
                # Output safety check
                if self._safety:
                    try:
                        from adk.safety import check_output
                        out_result = check_output(content)
                        if not out_result.safe:
                            content = out_result.sanitized_content
                    except Exception:
                        pass
                # OUTPUT-side never-fabricate backstop (companion turns; no-op otherwise).
                content = await self._companion_grounding_repair(content, message, sid)
                await self.memory.add_message(sid, "assistant", content)
                try:
                    store = _get_conversations()
                    await store.append_message(sid, "assistant", content, agent_name=self.name)
                except Exception:
                    pass
                # Record metering
                self.meter.record_usage(
                    tokens=resp.tokens_used,
                    model=resp.model,
                    latency_ms=resp.latency_ms,
                )
                _total_ms = (time.perf_counter() - _chat_start) * 1000
                if self._events:
                    try:
                        await self._events.emit(
                            "chat_response", agent=self.name,
                            tokens_used=resp.tokens_used, model=resp.model,
                            latency_ms=_total_ms, session_id=sid,
                        )
                    except Exception:
                        pass
                # Auto-ingest conversation into graph memory (fire-and-forget)
                if self._graph and not coherence.is_memory_question(message):
                    try:
                        # FEEDBACK-LOOP GUARD: ingest ONLY the user's turn (ground
                        # truth they shared) — never the assistant's own generated
                        # reply. Storing model output as memory lets a fabrication be
                        # recalled + elaborated next turn (compounding, permanent).
                        # And NEVER ingest a memory-QUESTION: a question ("do you
                        # remember Italy?") is not a fact, and storing it lets the
                        # question self-ground on the next ask, defeating the gate.
                        await self._graph.ingest_conversation(sid, [
                            {"role": "user", "content": message},
                        ])
                        # Unified memory: also store the user turn as a typed,
                        # role-classified record (correction/decision/preference…)
                        # so authority recall + governance see it. Flag-gated.
                        from adk.graph_memory import unified_memory_mode as _umm
                        if _umm() == "on":
                            from adk.typed_memory import infer_role
                            from adk.unified_contract import MemoryRecord
                            await self._graph.store(MemoryRecord(
                                content=message, role=infer_role(message),
                                source="user", confidence=0.7,
                            ))
                    except Exception:
                        pass
                await self._learn_after(message, content, tool_calls_made)
                # Turn finished (model stopped calling tools) — clear any approval pause
                # so a later identical gated call re-prompts instead of reusing a decision.
                try:
                    _approval_store.clear(sid)
                except Exception:  # noqa: BLE001
                    pass
                return AgentResponse(
                    content=content,
                    model=resp.model,
                    tokens_used=resp.tokens_used,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    latency_ms=resp.latency_ms,
                    tool_calls_made=tool_calls_made,
                    artifacts=[a.to_dict() for a in _get_session_artifacts(sid)],
                    session_id=sid,
                    finish_reason=resp.finish_reason,
                    effort_level=resp.effort_level,
                    cache_status=resp.cache_status,
                    advisor_calls=_advisor_calls_used,
                    advisor_input_tokens=_advisor_in_total,
                    advisor_output_tokens=_advisor_out_total,
                )

            # Execute tool calls with loop guard checks.
            #
            # CRITICAL message-structure invariant: an assistant message that
            # declares tool_calls MUST be immediately followed by EXACTLY one
            # `tool` result per tool_call_id, CONTIGUOUSLY, with no other role
            # interleaved. OpenAI tolerates an interleaved system/user message
            # here; DeepSeek and strict vLLM chat templates reject it with a 400
            # ("insufficient tool messages following tool_calls message"). So
            # loop-guard nudges are NEVER inserted between tool results — the
            # per-call guidance is folded into that call's own tool result, and
            # any standalone steering is deferred to AFTER the whole batch.
            # When the advisor is active, carry the raw native assistant content
            # (text + tool_use + advisor blocks) so the next iteration round-trips
            # the advisor_tool_result verbatim (the API 400s on orphaned blocks).
            # tool_calls stays set for the loop's own bookkeeping; content_blocks
            # (when present) is what the Anthropic provider actually sends.
            messages.append(Message(
                role="assistant",
                content=resp.content or "",
                tool_calls=[
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in resp.tool_calls
                ],
                content_blocks=(resp.raw_content_blocks or None),
                # a thinking-mode API refuses the next round without it (DeepSeek
                # v4-pro, 2026-09-21); non-thinking backends never see the key
                reasoning=(getattr(resp, "reasoning", "") or None),
            ))

            # World model advisory: consult the learned model to reorder tool choices.
            # In shadow mode, log the advice; in steer mode, reorder the execution.
            try:
                if (self._wm is not None and self._wm_prev_state is not None
                        and len(resp.tool_calls) > 1):
                    from adk.worldmodel import MODE_SHADOW, MODE_STEER, wm_mode
                    mode = wm_mode()
                    if mode in (MODE_SHADOW, MODE_STEER):
                        candidates = [tc.name for tc in resp.tool_calls]
                        adv = self._wm.advise(self._wm_prev_state, candidates)
                        if adv:
                            logger.info("[WM] Advisory: stage=%s, order=%s, scores=%s",
                                        adv.get("stage"), adv.get("order"), adv.get("scores"))
                            if mode == MODE_STEER and adv.get("order"):
                                # Reorder tool_calls by the advisory order (stable permutation)
                                advised_order = adv["order"]
                                known_order = {tc.name: i for i, tc in enumerate(resp.tool_calls)}
                                # Separate known tools (in advised order) + unknowns (append at end)
                                reordered = []
                                seen = set()
                                for tool_name in advised_order:
                                    if tool_name in known_order and tool_name not in seen:
                                        reordered.append(resp.tool_calls[known_order[tool_name]])
                                        seen.add(tool_name)
                                # Append any tools not in the advice (maintain relative order)
                                for tc in resp.tool_calls:
                                    if tc.name not in seen:
                                        reordered.append(tc)
                                # Verify it's a valid permutation (same set, same count)
                                if (len(reordered) == len(resp.tool_calls) and
                                        set(tc.name for tc in reordered) == set(tc.name for tc in resp.tool_calls)):
                                    resp.tool_calls = reordered
                                    logger.debug("[WM] Reordered tool calls: %s", advised_order)
            except Exception:
                pass

            _deferred_nudges: list[str] = []
            for tc in resp.tool_calls:
                verdict = loop_guard.check(tc.name, tc.arguments)

                if verdict.action == LoopAction.CIRCUIT_BREAK:
                    logger.warning("Loop guard CIRCUIT BREAK: %s", verdict.reason)
                    tool_calls_made.append(f"{tc.name}[circuit_break]")
                    messages.append(Message(
                        role="tool",
                        content=json.dumps({"error": "circuit_break", "message": verdict.reason,
                                            "guidance": verdict.nudge_message}),
                        tool_call_id=tc.id,
                    ))
                    _deferred_nudges.append(verdict.nudge_message)
                    # Fire metrics + Pulse alert
                    get_metrics().record_loop_guard_break()
                    _fire_pulse_loop_break(self.name, tc.name, loop_guard.stats.total_checks)
                    continue

                if verdict.action == LoopAction.BLOCK:
                    logger.info("Loop guard BLOCKED: %s", verdict.reason)
                    tool_calls_made.append(f"{tc.name}[blocked]")
                    messages.append(Message(
                        role="tool",
                        content=json.dumps({"error": "blocked_duplicate", "message": verdict.reason,
                                            "guidance": verdict.nudge_message}),
                        tool_call_id=tc.id,
                    ))
                    continue

                if verdict.action == LoopAction.WARN:
                    logger.debug("Loop guard WARN: %s", verdict.reason)
                    _deferred_nudges.append(verdict.nudge_message)

                # ── Human-in-the-loop approval gate ──
                # A tool whose policy is always_ask pauses the turn until the customer
                # allows/denies it. On resume the turn re-runs (adk rebuilds the loop from
                # memory) with the decision recorded, so the gate consumes it here on the
                # second pass: deny → feed a denial observation; allow → fall through to
                # execute; undecided → record the pending call and pause the whole turn.
                if needs_approval(self.name, tc.name):
                    _decision = _approval_store.decision_for(sid, tc.name)
                    if _decision == "deny":
                        tool_calls_made.append(f"{tc.name}[denied]")
                        messages.append(Message(
                            role="tool",
                            content=json.dumps({"error": "denied",
                                                "message": "The user denied this tool call."}),
                            tool_call_id=tc.id,
                        ))
                        continue
                    if _decision != "allow":
                        _pending_approvals.append({
                            "tool_use_id": tc.id, "tool": tc.name,
                            "args": tc.arguments if isinstance(tc.arguments, dict) else {},
                        })
                        _paused = True
                        break

                # ALLOW or WARN — execute the tool
                tool_calls_made.append(tc.name)
                if self._events:
                    try:
                        await self._events.emit(
                            "tool_call", agent=self.name,
                            tool=tc.name, arguments=tc.arguments,
                        )
                    except Exception:
                        pass
                _tool_start = time.perf_counter()
                # Principal-authority gate (G23): when the caller supplied an
                # AuthContext, enforce it at the tool boundary so a forbidden
                # tool call is blocked regardless of what the LLM emitted. When
                # absent (auth=None) the call is identical to before — preserved
                # as a separate path so app-side execute wrappers that take only
                # (name, args) are unaffected.
                if _auth is not None:
                    result = await self._tools.execute(tc.name, tc.arguments, auth=_auth)
                else:
                    result = await self._tools.execute(tc.name, tc.arguments)
                _tool_ms = (time.perf_counter() - _tool_start) * 1000
                get_metrics().record_tool_call(tool=tc.name, latency_ms=_tool_ms)
                # B.3 introspection — record what we did so self_* tools can read it.
                try:
                    _rec = {
                        "ts": time.time(),
                        "session_id": sid,
                        "tool": tc.name,
                        "arguments": tc.arguments,
                        "latency_ms": round(_tool_ms, 2),
                        "error": isinstance(result, str) and result.startswith('{"error"'),
                    }
                    self._introspection.append(_rec)
                    if tc.name in ("file_read", "file_write", "file_edit"):
                        _p = tc.arguments.get("path") if isinstance(tc.arguments, dict) else None
                        if _p:
                            _ft = self._files_touched.setdefault(
                                _p, {"first_ts": _rec["ts"], "ops": []}
                            )
                            _ft["last_ts"] = _rec["ts"]
                            _ft["ops"].append(tc.name)
                except Exception:  # noqa: BLE001 — introspection must never break the loop
                    pass
                # Detect artifacts in tool output
                try:
                    from adk.artifacts import detect_artifact, get_registry
                    _art = detect_artifact(tc.name, result)
                    if _art:
                        _art.tool = tc.name
                        get_registry().add(sid, _art)
                except Exception:
                    pass
                if self._events:
                    try:
                        await self._events.emit(
                            "tool_result", agent=self.name,
                            tool=tc.name, latency_ms=_tool_ms,
                        )
                    except Exception:
                        pass
                messages.append(Message(
                    role="tool",
                    content=result,
                    tool_call_id=tc.id,
                ))
                if self.loop_policy is not None:
                    self.loop_policy.record(tc.name, tc.arguments, result)
                    _deferred_nudges.extend(self.loop_policy.nudges())

            # Every tool_call in this assistant turn now has its matching `tool`
            # result appended contiguously. Emit any loop-guard steering AFTER
            # the whole block as a single message — keeping it out of the
            # assistant→tool_results pairing that every backend (strictly,
            # DeepSeek) requires. dict.fromkeys() de-dupes repeated nudges while
            # preserving order.
            if _deferred_nudges:
                messages.append(Message(
                    role="system",
                    content="\n".join(dict.fromkeys(_deferred_nudges)),
                ))

            # A gated tool paused the turn — persist the pause and return so the caller
            # can surface Allow/Deny cards. Resume re-enters via agent.resume(session_id).
            if _paused:
                _approval_store.put_pending(
                    sid, user_message=message, agent=self.name, pending=_pending_approvals,
                )
                pend_names = ", ".join(p["tool"] for p in _pending_approvals)
                _total_ms = (time.perf_counter() - _chat_start) * 1000
                unregister_steering_queue(sid)
                return AgentResponse(
                    content=(f"Waiting for your approval to run: {pend_names}."),
                    model=getattr(resp, "model", ""),
                    session_id=sid,
                    tool_calls_made=tool_calls_made,
                    latency_ms=_total_ms,
                    finish_reason="requires_action",
                    requires_action=True,
                    pending=list(_pending_approvals),
                )

        # ── Exhausted the tool-loop ceiling ──
        # Reaching here means the model still wanted tools when the budget ran out.
        # That is a TRUNCATED turn, and it used to be reported as a normal one: the
        # synthesis call below returns finish_reason="stop", so a caller (and the
        # user) could not tell a completed turn from an abandoned one. Now we both
        # tell the model it was cut off and stamp the response so callers detect it.
        _exhausted_loops = True
        logger.warning(
            "[REACT] Tool-loop ceiling (%d) exhausted for agent %r at effort %s "
            "- forcing synthesis. Task may be incomplete.",
            _loop_ceiling, self.name, _effort_int,
        )
        messages.append(Message(
            role="system",
            content=(
                f"[TOOL BUDGET EXHAUSTED] You used all {_loop_ceiling} tool "
                "iterations available for this turn. Give your final answer now "
                "using only what you have already established. State plainly what "
                "you completed and what is still outstanding - do NOT present "
                "incomplete work as finished."
            ),
        ))
        final = await self.llm.chat(
            messages, effort=_effort, top_p=_top_p,
            repetition_penalty=_repetition_penalty, **kwargs,
        )
        content = strip_internal_tags(final.content)
        # Output safety check
        if self._safety:
            try:
                from adk.safety import check_output
                out_result = check_output(content)
                if not out_result.safe:
                    content = out_result.sanitized_content
            except Exception:
                pass
        # OUTPUT-side never-fabricate backstop (companion turns; no-op otherwise).
        content = await self._companion_grounding_repair(content, message, sid)
        await self.memory.add_message(sid, "assistant", content)
        try:
            store = _get_conversations()
            await store.append_message(sid, "assistant", content, agent_name=self.name)
        except Exception:
            pass
        # Record metering for final response
        self.meter.record_usage(
            tokens=final.tokens_used,
            model=final.model,
            latency_ms=final.latency_ms,
        )
        _total_ms = (time.perf_counter() - _chat_start) * 1000
        if self._events:
            try:
                await self._events.emit(
                    "chat_response", agent=self.name,
                    tokens_used=final.tokens_used, model=final.model,
                    latency_ms=_total_ms, session_id=sid,
                )
            except Exception:
                pass
        # Auto-ingest conversation into graph memory (fire-and-forget).
        # FEEDBACK-LOOP GUARD: user turn ONLY — never ingest the assistant's own
        # generated reply (a fabrication stored as memory recalls + compounds).
        # Skip memory-QUESTIONS: a question is not a fact, and ingesting it lets
        # the question self-ground on the next ask, defeating the never-fabricate gate.
        if self._graph and not coherence.is_memory_question(message):
            try:
                await self._graph.ingest_conversation(sid, [
                    {"role": "user", "content": message},
                ])
                # Unified memory: typed, role-classified store of the user turn
                # (flag-gated; off → only legacy ingest runs, unchanged).
                from adk.graph_memory import unified_memory_mode as _umm
                if _umm() == "on":
                    from adk.typed_memory import infer_role
                    from adk.unified_contract import MemoryRecord
                    await self._graph.store(MemoryRecord(
                        content=message, role=infer_role(message),
                        source="user", confidence=0.7,
                    ))
            except Exception:
                pass
        await self._learn_after(message, content, tool_calls_made)
        # Turn completed without (re-)pausing — clear any approval state for this session
        # so a later identical tool call re-prompts instead of reusing a stale decision.
        try:
            _approval_store.clear(sid)
        except Exception:  # noqa: BLE001
            pass
        unregister_steering_queue(sid)
        return AgentResponse(
            content=content,
            model=final.model,
            tokens_used=final.tokens_used,
            prompt_tokens=final.prompt_tokens,
            completion_tokens=final.completion_tokens,
            latency_ms=final.latency_ms,
            tool_calls_made=tool_calls_made,
            artifacts=[a.to_dict() for a in _get_session_artifacts(sid)],
            session_id=sid,
            # Truncation must be visible to callers. Reporting the synthesis
            # call's own finish_reason here would claim the model chose to end
            # the turn; it did not — we cut it off when the ceiling ran out.
            finish_reason=(
                "max_tool_loops" if _exhausted_loops else final.finish_reason
            ),
            effort_level=final.effort_level,
            cache_status=final.cache_status,
        )

    async def resume(self, session_id: str, decisions: list[dict], **kwargs) -> "AgentResponse":
        """Resume a turn that paused for tool approval. Records the customer's allow/deny
        ``decisions`` (each ``{tool_use_id|tool, result, deny_message?}``) then re-runs the
        paused turn — the approval gate now consumes the recorded decision (executes an
        allowed tool, feeds a denial observation for a denied one). Returns the continued
        turn's response (which may pause AGAIN on a different gated tool)."""
        from adk.approval import get_approval_store
        store = get_approval_store()
        paused = store.get(session_id)
        if not paused:
            return AgentResponse(content="", session_id=session_id, finish_reason="stop")
        store.record_decisions(session_id, decisions)
        return await self.chat(paused.get("user_message", ""), session_id=session_id, **kwargs)

    async def chat_stream(
        self,
        message: str,
        history: list[dict] | None = None,
        session_id: str | None = None,
        **kwargs,
    ):
        """Stream a response. Yields string chunks.

        If the agent has tools and the LLM requests tool use, falls back
        to non-streaming chat() (tool loops can't stream mid-execution).

        Includes degeneration detection — if the model starts repeating,
        the stream is killed and trimmed to the last clean sentence.
        """
        sid = session_id or self._session_id
        # See chat(): popped so it never reaches the provider as a kwarg;
        # forwarded explicitly on the tool-loop fallback below.
        _sys_additions = kwargs.pop("system_additions", None)

        # Input safety check
        if self._safety:
            try:
                safety_result = self._safety.check(message)
                if safety_result.blocked:
                    yield "I can't process that request — it was flagged by the safety filter."
                    return
            except Exception:
                pass

        # Emit chat start event
        if self._events:
            try:
                await self._events.emit(
                    "chat_request", agent=self.name,
                    message=message[:200], session_id=sid, streaming=True,
                )
            except Exception:
                pass

        # If agent has tools, fall back to sync (tool loops can't stream).
        # Bounded with a timeout: an SSE HTTP handler awaiting this generator
        # must never hang the connection forever if the underlying model
        # doesn't cleanly terminate a tool-calling round-trip (observed live:
        # a small non-tool-tuned Ollama model left a streaming request open
        # indefinitely, with zero bytes sent, while the equivalent raw
        # non-streaming a.llm.chat() call — which bypasses the tool loop
        # entirely — completed in 1-2s).
        if self._tools.list_tools():
            try:
                resp = await asyncio.wait_for(
                    self.chat(message, history=history, session_id=sid,
                              system_additions=_sys_additions, **kwargs),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "chat_stream() tool-loop fallback timed out after 60s "
                    "for agent %s — yielding a timeout notice instead of "
                    "hanging the connection", self.name,
                )
                yield "I'm sorry, that took too long to process. Please try again."
                return
            yield resp.content
            return

        # Build messages
        messages = [Message(role="system", content=self._turn_system_prompt(_sys_additions))]
        if history:
            for h in history:
                messages.append(Message(role=h["role"], content=h["content"]))
        else:
            stored = await self.memory.get_history(sid, limit=20)
            for h in stored:
                messages.append(Message(role=h["role"], content=h["content"]))
        messages.append(Message(role="user", content=message))

        # Extract inference controls
        _effort = kwargs.pop("effort", None)
        _top_p = kwargs.pop("top_p", None)
        _repetition_penalty = kwargs.pop("repetition_penalty", None)

        # Stream with degeneration detection
        full_content = ""
        _degenerated = False
        async for chunk in self.llm.chat_stream(
            messages, effort=_effort, top_p=_top_p,
            repetition_penalty=_repetition_penalty, **kwargs,
        ):
            if chunk.finish_reason == "degeneration":
                _degenerated = True
                logger.warning("Degeneration detected in stream for agent %s", self.name)
                break
            if chunk.content:
                full_content += chunk.content
                yield chunk.content

        # If degenerated, trim to clean content
        if _degenerated and full_content:
            detector = DegenerationDetector()
            full_content = detector.trim_clean(full_content)

        # Strip internal tags from full response
        full_content = strip_internal_tags(full_content)

        # Output safety check on full response
        if self._safety and full_content:
            try:
                from adk.safety import check_output
                out_result = check_output(full_content)
                if not out_result.safe:
                    logger.warning("Streaming output flagged by safety check")
            except Exception:
                pass

        # Store in memory
        await self.memory.add_message(sid, "user", message)
        # Streaming already delivered the tokens, so we can't un-show a fabricated
        # claim here — generation grounding (the companion COHERENCE prompt block)
        # is the live defense for the streamed text. But we DO ground the PERSISTED
        # copy so a fabrication isn't recalled + compounded on a later turn.
        full_content = await self._companion_grounding_repair(full_content, message, sid)
        await self.memory.add_message(sid, "assistant", full_content)

        # Emit completion event
        if self._events:
            try:
                await self._events.emit(
                    "chat_response", agent=self.name,
                    session_id=sid, streaming=True,
                    degenerated=_degenerated,
                )
            except Exception:
                pass

    async def stream_chat(
        self,
        message: str,
        on_event=None,
        session_id: str | None = None,
        token_delay: float = 0.0,
        **kwargs,
    ) -> AgentResponse:
        """Reliable streaming chat — the streaming sibling of :meth:`chat`.

        Runs the NATIVE function-calling loop (``chat()``, which decides when it has
        enough and answers on its own — no text ACTION/FINAL protocol to over-run),
        emits live ``tool`` / ``tool_result`` events while tools execute, then streams
        the final answer as ``token`` events. Returns the full :class:`AgentResponse`.

        Prefer this over :meth:`stream_react` for streaming UIs: native tool calling
        is far more reliable at concluding than the text protocol. ``token_delay`` > 0
        paces tokens for a visible typing effect. ``on_event`` may be sync or async::

            {"type": "tool", "name": ..., "args": ...}
            {"type": "tool_result", "name": ..., "result": ...}
            {"type": "token", "text": ...}
            {"type": "done"}
        """
        async def _emit(ev: dict) -> None:
            if on_event is None:
                return
            try:
                r = on_event(ev)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

        # Surface tool activity from the native (non-streaming) loop.
        orig_execute = self._tools.execute

        async def _traced(name, arguments):
            await _emit({"type": "tool", "name": name, "args": arguments})
            result = await orig_execute(name, arguments)
            await _emit({"type": "tool_result", "name": name, "result": str(result)[:1500]})
            return result

        self._tools.execute = _traced
        try:
            resp = await self.chat(message, session_id=session_id, **kwargs)
        finally:
            self._tools.execute = orig_execute

        # Stream the final answer token-by-token (word-chunked).
        content = resp.content or ""
        for chunk in (re.findall(r"\S+\s*", content) or ([content] if content else [])):
            await _emit({"type": "token", "text": chunk})
            if token_delay:
                await asyncio.sleep(token_delay)
        await _emit({"type": "done"})
        return resp

    async def stream_react(
        self,
        message: str,
        on_event,
        history: list[dict] | None = None,
        max_steps: int = 6,
        session_id: str | None = None,
        steering: "Callable[[], list[str]] | None" = None,
        system_additions: list[str] | None = None,
    ) -> AgentResponse:
        """Streaming, tool-using ReAct loop.

        Unlike ``chat()`` (native OpenAI function-calling, which can't stream —
        streaming structured tool_calls is unsupported), this drives a TEXT-based
        ReAct protocol so the model's ``<think>`` reasoning AND the final answer
        stream live via ``on_event`` while tools still execute. This is the
        canonical *streaming* agent loop — surfaces shipping live reasoning
        (terminals, web chat) should use it; ``chat()`` remains for one-shot.

        ``on_event`` is a sync or async callable receiving dict events::

            {"type": "thinking", "text": ...}     # reasoning delta (live)
            {"type": "token", "text": ...}         # final-answer delta (live)
            {"type": "tool", "name": ..., "args": ...}
            {"type": "tool_result", "name": ..., "result": ...}
            {"type": "error", "error": ...}
            {"type": "done"}
        """
        import json as _json
        import re as _re
        sid = session_id or self._session_id
        _t0 = time.perf_counter()

        async def _emit(evt: dict) -> None:
            try:
                r = on_event(evt)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

        # Intent classification (fail-open: use keyword heuristic on error).
        # Used to filter tools by intent type.
        _intent = None
        try:
            from adk.intent import coarse_code_intent
            _intent = coarse_code_intent(message)
        except Exception:
            pass

        # System prompt = agent instructions + the ReAct text protocol + tools.
        # Filter tools by intent before building tool_lines (fail-open: no intent → all tools).
        all_tools = self._tools.list_tools()
        filtered_tools = _filter_tools_by_intent(all_tools, _intent)

        tool_lines = []
        for td in filtered_tools:
            props = (td.parameters or {}).get("properties", {}) if isinstance(td.parameters, dict) else {}
            tool_lines.append(f"- {td.name}({', '.join(props.keys())}) -> {td.description}")
        tools_block = "\n".join(tool_lines) if tool_lines else "(no tools available)"
        sys_prompt = (
            (self.system_prompt or "").rstrip() + "\n\n"
            "Think step by step inside <think>...</think>. To call a tool, after your "
            "<think> reply EXACTLY:\nACTION: <tool_name>\nINPUT: <one-line JSON args, or {}>\n"
            "When you have enough information, after your <think> reply:\n"
            "FINAL: <concise answer for the user>\n\n"
            f"Available tools:\n{tools_block}"
            # Situation LAST: identity + protocol + tools is the stable, cacheable
            # prefix; the clock and the caller's [USER'S SHELL] block change per
            # turn and must not sit in front of it. See adk.situation.
            + self._situation_suffix(system_additions)
        )

        msgs = [Message(role="system", content=sys_prompt)]
        for h in (history or [])[-8:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
                msgs.append(Message(role=h["role"], content=str(h["content"])[:3000]))
        msgs.append(Message(role="user", content=message))

        _ACT = _re.compile(r"ACTION:\s*([a-zA-Z_][a-zA-Z0-9_]*)", _re.I)
        _INP = _re.compile(r"INPUT:\s*(\{.*?\})", _re.I | _re.S)
        _THINK = _re.compile(r"<think>.*?</think>\s*", _re.S)

        answer = ""
        tools_made: list[str] = []

        # Knowledge graph tracking — tools, memory, sources touched in this turn
        _kg_tools = set()  # tool names called
        _kg_memory_recalls = set()  # memory keys/types recalled
        _kg_sources = set()  # source identifiers

        for _step in range(max_steps):
            # Chat-as-steering (Layer 2): drain messages the user sent WHILE this
            # loop is running and inject them so it REDIRECTS mid-flight, instead of
            # the user waiting for a fresh agentic loop. ``steering()`` is supplied
            # by a ReasoningSession (adk.reasoning_session); default None = no-op.
            if steering is not None:
                try:
                    for _sm in (steering() or []):
                        if _sm:
                            msgs.append(Message(role="user", content=f"[Steering] {_sm}"))
                            await _emit({"type": "steering", "text": str(_sm)})
                except Exception:  # noqa: BLE001
                    pass
            full = ""
            buf = ""
            phase = "scan"  # scan | thinking | answer | action
            _turn_err = None
            # Retry the turn once on a transient connection error (the provider may
            # hold a stale keep-alive between turns). Only retry if nothing has been
            # emitted yet, so a mid-stream failure never double-streams.
            for _attempt in range(2):
                full = ""
                buf = ""
                phase = "scan"
                _emitted = False
                try:
                    async for chunk in self.llm.chat_stream(msgs):
                        delta = getattr(chunk, "content", "") or ""
                        if not delta:
                            continue
                        full += delta
                        buf += delta
                        progress = True
                        while progress:
                            progress = False
                            if phase == "scan":
                                cands = [(p, k) for p, k in (
                                    (buf.find("<think>"), "t"), (buf.find("FINAL:"), "f"), (buf.find("ACTION:"), "a")
                                ) if p != -1]
                                if cands:
                                    pos, kind = min(cands)
                                    if kind == "t":
                                        buf = buf[pos + 7:]; phase = "thinking"; progress = True
                                    elif kind == "f":
                                        buf = buf[pos + 6:]; phase = "answer"; progress = True
                                    else:
                                        phase = "action"
                                elif len(buf) > 8:
                                    buf = buf[-8:]
                            elif phase == "thinking":
                                j = buf.find("</think>")
                                if j != -1:
                                    if buf[:j]:
                                        await _emit({"type": "thinking", "text": buf[:j]}); _emitted = True
                                    buf = buf[j + 8:]; phase = "scan"; progress = True
                                elif len(buf) > 9:
                                    emit, buf = buf[:-9], buf[-9:]
                                    if emit:
                                        await _emit({"type": "thinking", "text": emit}); _emitted = True
                            elif phase == "answer":
                                if buf:
                                    await _emit({"type": "token", "text": buf}); buf = ""; _emitted = True
                    _turn_err = None
                    break
                except Exception as exc:
                    _turn_err = exc
                    if _attempt == 0 and not _emitted:
                        logger.warning("stream_react retry (%s) for %s", type(exc).__name__, self.name)
                        await asyncio.sleep(0.3)
                        continue
                    break
            if _turn_err is not None:
                logger.warning("stream_react turn failed for %s: %s", self.name, _turn_err)
                await _emit({"type": "error", "error": type(_turn_err).__name__})
                break
            if phase == "thinking" and buf:
                await _emit({"type": "thinking", "text": buf.replace("</think>", "")})
            elif phase == "answer" and buf:
                await _emit({"type": "token", "text": buf})

            if phase == "answer":
                cleaned = _THINK.sub("", full)
                answer = cleaned.split("FINAL:", 1)[-1].strip() if "FINAL:" in cleaned else cleaned.strip()
                break
            m = _ACT.search(full)
            if not m:
                answer = _THINK.sub("", full).strip()
                if answer:
                    await _emit({"type": "token", "text": answer})
                break
            name = m.group(1)
            args: dict = {}
            im = _INP.search(full)
            if im:
                try:
                    args = _json.loads(im.group(1))
                except Exception:
                    args = {}
            await _emit({"type": "tool", "name": name, "args": args})
            # Knowledge graph tracking
            _kg_tools.add(str(name).lower())
            try:
                obs = str(await self._tools.execute(name, args))
            except Exception as exc:
                obs = f"(tool error: {type(exc).__name__}: {exc})"
            tools_made.append(name)
            await _emit({"type": "tool_result", "name": name, "result": obs[:1500]})
            # Track tool result for knowledge graph
            _kg_tools.add(str(name).lower())
            # reasoning-n/a: streamed text, no response object -- the stream loop has
            # no reasoning to carry; thinking-mode backends run the non-stream loop
            msgs.append(Message(role="assistant", content=full))
            msgs.append(Message(role="user", content=f"OBSERVATION: {obs[:3000]}"))
        else:
            await _emit({"type": "token", "text": "\n(reached step limit)"})

        await _emit({"type": "done"})

        # Emit knowledge graph event (matching Genesis shape for AitherShell rendering)
        try:
            kg_data = _build_knowledge_graph(
                self.name, list(tools_made), _kg_memory_recalls, _kg_sources, sid
            )
            await _emit({
                "type": "knowledge_graph",
                "nodes": kg_data["nodes"],
                "edges": kg_data["edges"],
            })
        except Exception as kg_err:
            logger.debug("Failed to build/emit knowledge graph: %s", kg_err)

        try:
            await self.memory.add_message(sid, "user", message)
            if answer:
                # Ground the persisted + returned copy (tokens already streamed).
                answer = await self._companion_grounding_repair(answer, message, sid)
                await self.memory.add_message(sid, "assistant", answer)
        except Exception:
            pass
        return AgentResponse(
            content=answer or "",
            session_id=sid,
            tool_calls_made=tools_made,
            latency_ms=round((time.perf_counter() - _t0) * 1000, 1),
            finish_reason="stop",
        )

    async def classify_intent(self, message: str, history: list[dict] | None = None):
        """Canonical LLM intent routing (``adk.intent``).

        Decides intent / effort / whether the agentic tool-loop is warranted, via
        a SINGLE fast effort-2 call on this agent's own LLM (keyword fallback if
        the LLM is unavailable). This is the ONE shared classifier — Genesis,
        awkit and every ADK agent route through here instead of each
        carrying a divergent keyword classifier.
        """
        from adk.intent import classify_intent as _classify

        async def _complete(messages: list) -> str:
            msgs = [Message(role=m["role"], content=m["content"]) for m in messages]
            # Fast lane: append /no_think to the last user turn so routing is ~0.5s.
            for _mm in reversed(msgs):
                if _mm.role == "user":
                    _mm.content = (str(_mm.content) + " /no_think").strip()
                    break
            resp = await self.llm.chat(msgs, effort=2)
            return getattr(resp, "content", "") or ""

        tool_hint = ""
        try:
            tool_hint = ", ".join(td.name for td in self._tools.list_tools())[:600]
        except Exception:  # noqa: BLE001
            pass
        return await _classify(message, llm_complete=_complete, tool_hint=tool_hint, history=history)

    async def stream_respond(
        self,
        message: str,
        on_event,
        history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> AgentResponse:
        """Instant-response → background-enrich → auto-continue (``adk.responder``).

        Streams a fast first-pass answer IMMEDIATELY (no dead air), runs the full
        grounded ReAct loop (:meth:`stream_react`) CONCURRENTLY, and
        auto-continues the turn with a refinement segment only when the grounded
        answer materially adds value (tools used / content diverged). Trivial
        chat (the intent router says non-agentic, low effort) skips the grounded
        pass entirely. This is the canonical instant-response capability — Genesis
        chat and awkit delegate here so the behaviour is identical
        everywhere instead of each reimplementing it.
        """
        from adk import responder as _responder

        sid = session_id or self._session_id
        decision = await self.classify_intent(message, history=history)

        async def _first_pass():
            # If the grounded loop will run (agentic) or the answer needs data we
            # don't have (requires_grounding), the REAL answer comes from the
            # background — so the first pass is a DETERMINISTIC honest ack: instant,
            # never fabricates, no <think> delay. A direct LLM answer here would
            # either fabricate or contradict what the loop then does.
            if getattr(decision, "requires_grounding", False) or getattr(decision, "agentic", False):
                lab = getattr(decision, "grounding_label", "") or ""
                yield (f"Let me check {lab} for you…" if lab
                       else "On it — let me work through that for you…")
                return
            base = (self.system_prompt or "").rstrip() or f"You are {self.name}, a helpful assistant."
            sysmsg = ground_system_prompt(base) + (
                "\n\nAnswer the user directly and concisely RIGHT NOW from what you "
                "genuinely know. If deeper work is warranted it is ALREADY running in "
                "the background and will continue your answer. Never fabricate specifics "
                "(names, dates, numbers, data) you don't actually have."
            )
            msgs = [Message(role="system", content=sysmsg)]
            for h in (history or [])[-6:]:
                if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content"):
                    msgs.append(Message(role=h["role"], content=str(h["content"])[:2000]))
            # Fast lane: append /no_think so the orchestrator skips its <think>
            # phase (~0.5s vs ~5s) and never exhausts its budget mid-reasoning.
            msgs.append(Message(role="user", content=message + " /no_think"))
            # Strip <think> on the fly so the fast first pass shows only the answer.
            in_think = False
            pending = ""
            async for chunk in self.llm.chat_stream(msgs):
                txt = getattr(chunk, "content", "") or ""
                if not txt:
                    continue
                pending += txt
                out = ""
                while pending:
                    if not in_think:
                        i = pending.find("<think>")
                        if i == -1:
                            if len(pending) > 7:
                                out += pending[:-7]
                                pending = pending[-7:]
                            break
                        out += pending[:i]
                        pending = pending[i + 7:]
                        in_think = True
                    else:
                        j = pending.find("</think>")
                        if j == -1:
                            pending = pending[-8:] if len(pending) > 8 else pending
                            break
                        pending = pending[j + 8:]
                        in_think = False
                if out:
                    yield out
            if pending and not in_think:
                yield pending

        async def _direct() -> str:
            base = (self.system_prompt or "").rstrip() or f"You are {self.name}."
            msgs = [Message(role="system", content=ground_system_prompt(base)),
                    Message(role="user", content=message + " /no_think")]
            resp = await self.llm.chat(msgs, effort=2)
            return strip_internal_tags(getattr(resp, "content", "") or "")

        async def _enrich(on_enrich_event) -> dict:
            # Run the grounded/agentic lane on SEMANTIC NEED, not an effort
            # threshold: it fires whenever the turn needs tools (agentic), external
            # data (requires_grounding), or real reasoning (reasoning_depth beyond
            # skip/gate). Only genuinely trivial chat lets the (system-state-grounded)
            # first pass stand. Effort SCALES the step budget below — it never gates.
            _depth = (getattr(decision, "reasoning_depth", "") or "").strip().lower()
            if (not getattr(decision, "requires_grounding", False)
                    and not decision.agentic and _depth in ("", "skip", "gate")):
                return {"answer": "", "used_tools": False, "artifacts": []}
            steps = 4 if decision.effort <= 5 else (8 if decision.effort <= 7 else 12)
            resp = await self.stream_react(
                message, on_event=on_enrich_event, history=history,
                max_steps=steps, session_id=sid,
            )
            return {
                "answer": (getattr(resp, "content", "") or "").strip(),
                "used_tools": bool(getattr(resp, "tool_calls_made", None)),
                "artifacts": getattr(resp, "artifacts", None) or [],
            }

        result = await _responder.respond(
            message=message, on_event=on_event,
            first_pass_stream=_first_pass, enrich=_enrich, direct_answer=_direct,
            agent=self.name,
        )
        return AgentResponse(
            content=result.get("answer", "") or "",
            session_id=sid,
            finish_reason="stop",
            effort_level=decision.effort,
        )

    async def run(self, task: str, **kwargs) -> AgentResponse:
        """Execute a task with ReAct-style reasoning.

        Same as chat() but with a task-oriented system prompt wrapper.
        """
        task_prompt = (
            f"Complete the following task. Use available tools as needed. "
            f"Think step by step.\n\nTask: {task}"
        )
        response = await self.chat(task_prompt, **kwargs)
        self._report_task_outcome(task, response)
        return response

    def _report_task_outcome(self, task: str, response: Any) -> None:
        """Offer this completed task to the platform's learning loop. OPT-IN.

        Hooked on `run()` rather than `chat()` deliberately: a task has the
        shape a training row wants (an instruction and a result), while ordinary
        conversation turns do not -- hooking every turn would send far more of a
        user's text for far less signal.

        Does nothing unless the operator has switched reporting ON and set an
        endpoint (see `adk.learning_report`; unset means off). Never raises:
        telemetry must not be able to fail the work it reports on.
        """
        try:
            from adk.learning_report import report_outcome

            text = (getattr(response, "content", None)
                    or getattr(response, "response", None) or "")
            report_outcome(
                task,
                str(text),
                agent_name=self.name,
                # An SDK agent assembles its OWN system prompt, so unlike a
                # remote peer it genuinely knows what the model was told. That
                # is why these rows may claim the prompt was captured.
                system_prompt=self.system_prompt,
                success=not getattr(response, "error", None),
            )
            # The context-substrate write-through: the completed task lands in
            # the tenant knowledge pool (same opt-in doctrine — the system
            # prompt is deliberately NOT sent). OPT-IN, never raises.
            from adk.pool_write_through import report_task_to_pool

            report_task_to_pool(
                task,
                str(text),
                agent_name=self.name,
                success=not getattr(response, "error", None),
            )
        except Exception as exc:  # noqa: BLE001 - one lost row, never a failed run
            logger.debug("learning report skipped: %s", exc)

    async def remember(self, key: str, value: str, category: str = "general"):
        """Store a value in the agent's persistent memory.

        When typed memory is enabled, the entry is tagged with an inferred role
        (decision/correction/preference/…) so it participates in authority-ranked
        recall and decision constraints — while remaining retrievable by key.
        """
        metadata = None
        if self._typed is not None:
            try:
                from adk.typed_memory import make_metadata
                metadata = make_metadata(value, category=category)
            except Exception:
                metadata = None
        await self.memory.remember(key, value, category=category, metadata=metadata)

    async def recall(self, key: str) -> str | None:
        """Retrieve a value from the agent's persistent memory."""
        return await self.memory.recall(key)

    def new_session(self) -> str:
        """Start a new conversation session."""
        self._session_id = str(uuid.uuid4())[:8]
        return self._session_id

    # ── Faculty graph integration ────────────────────────────────────

    def set_code_graph(self, code_graph) -> None:
        """Attach a CodeGraph to this agent.

        When a CodeGraph is attached, the agent automatically gains
        ``code_search`` and ``code_context`` built-in tools. The graph
        is also used to inject relevant code snippets into the LLM context
        when the user's message looks like a code question.

        Usage::

            from adk.faculties import CodeGraph

            cg = CodeGraph()
            await cg.index_codebase("./my-project")
            agent.set_code_graph(cg)
        """
        self._code_graph = code_graph
        try:
            from adk.builtin_tools import _register_code_graph_tools
            _register_code_graph_tools(self, code_graph)
        except Exception:
            pass

    def set_memory_graph(self, memory_graph) -> None:
        """Attach a legacy MemoryGraph to this agent.

        .. deprecated:: 2.12.7
           The agent already has :class:`adk.graph_memory.GraphMemory` wired
           as ``self._graph`` during ``__init__``.  This method is kept only
           for callers that pass a pre-existing graph object; internally it
           stores the reference and registers tools but new code should use
           the built-in ``self._graph`` directly.

        Usage::

            # Preferred (no call needed — already wired):
            agent = AitherAgent("atlas")
            await agent.graph_remember("AitherOS", "uses", "SQLite")

            # Legacy (still works):
            from adk.graph_memory import GraphMemory
            g = GraphMemory(agent_name="custom-db")
            agent.set_memory_graph(g)
        """
        self._memory_graph = memory_graph
        try:
            from adk.builtin_tools import _register_memory_graph_tools
            _register_memory_graph_tools(self, memory_graph)
        except Exception:
            pass

    async def graph_remember(self, subject: str, relation: str, object_: str):
        """Store a knowledge triple in the agent's graph memory."""
        if not self._graph:
            return
        await self._graph.remember(subject, relation, object_)

    async def graph_query(self, question: str, limit: int = 5) -> list:
        """Query the agent's graph memory. Returns list of GraphNode."""
        if not self._graph:
            return []
        return await self._graph.query(question, limit=limit)

    async def graph_stats(self) -> dict:
        """Get graph memory statistics."""
        if not self._graph:
            return {"enabled": False}
        stats = await self._graph.get_stats()
        stats["enabled"] = True
        return stats

    async def swarm(
        self,
        problem: str,
        mode: str = "forge",
        effort: int = 8,
        max_seconds: int = 300,
    ) -> dict:
        """Dispatch problem to the AitherOS swarm coding engine.

        Requires AitherOS Genesis service running.

        Args:
            problem: Task description
            mode: "llm", "forge" (with tools), or "plan_only"
            effort: Effort level 1-10
            max_seconds: Maximum execution time

        Returns:
            Dict with status, plan, code, tests, artifacts
        """
        # GATED: swarm-coding dispatch is a paid-tier capability (it drives the
        # Genesis multi-agent engine). Free agents get a clear upgrade prompt.
        if self._license is not None and not self._license.can_use_swarm():
            return {
                "status": "failed",
                "error": (
                    "Swarm coding requires a Professional tier. Upgrade at "
                    "api.aitherium.com/portal/marketplace/packs"
                ),
            }

        import httpx

        genesis_url = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
        try:
            async with httpx.AsyncClient(timeout=max_seconds + 10) as client:
                resp = await client.post(
                    f"{genesis_url}/swarm/code/sync",
                    json={
                        "problem": problem,
                        "mode": mode,
                        "effort": effort,
                        "timeout_seconds": max_seconds,
                    },
                )
                if resp.status_code == 200:
                    return resp.json()
                return {"status": "failed", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except httpx.TimeoutException:
            return {"status": "failed", "error": f"Swarm timed out after {max_seconds}s"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    async def code_search(self, query: str, max_results: int = 10) -> list[dict]:
        """Search codebase via Repowise (semantic) with ripgrep fallback.

        Args:
            query: Natural language or keyword query
            max_results: Maximum results

        Returns:
            List of {file, symbol, snippet, score} dicts
        """
        from .builtin_tools import repowise_search
        raw = repowise_search(query, max_results=max_results)
        try:
            data = json.loads(raw)
            return data.get("results", [])
        except Exception:
            return [{"file": "", "snippet": raw[:500], "score": 0}]

    async def report_bug(self, description: str, include_logs: bool = True) -> dict:
        """Report a bug programmatically."""
        from adk.bugreport import submit_bug_report
        return await submit_bug_report(
            description=description,
            agent_name=self.name,
            llm_backend=self.llm.provider_name,
            include_logs=include_logs,
        )


def _fire_pulse_loop_break(agent: str, tool: str, total_calls: int):
    """Fire-and-forget Pulse pain signal for loop guard circuit break."""
    async def _send():
        try:
            from adk.pulse import get_pulse
            pulse = get_pulse()
            await pulse.send_loop_break(
                agent=agent, tool=tool,
                total_calls=total_calls,
                request_id=get_trace_id(),
            )
        except Exception:
            pass
    try:
        asyncio.ensure_future(_send())
    except RuntimeError:
        pass  # No event loop — skip
