"""Aither ADK — Build AI agent fleets with any LLM backend."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:  # single source of truth = the installed package metadata (pyproject version)
    __version__ = _pkg_version("awdk")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "3.8.20"  # kept in sync by packaging/sync_versions.py

from adk.agent import AitherAgent
from adk.dispatch import MultiAgentDispatcher, DispatchSpec, DispatchResult
from adk.gate import CompletionGate, GateVerdict, gated_run, hard_checks
from adk.tools import tool, ToolRegistry
from adk.llm import LLMRouter
from adk.config import Config

__all__ = [
    "AitherAgent",
    # Multi-agent dispatch
    "MultiAgentDispatcher",
    "DispatchSpec",
    "DispatchResult",
    "CompletionGate",
    "GateVerdict",
    "gated_run",
    "hard_checks",
    "tool",
    "ToolRegistry",
    "LLMRouter",
    "Config",
    "AgentRegistry",
    "AgentForge",
    "FleetConfig",
    "ConversationStore",
    # Extended capabilities
    "LoopGuard",
    "AitherSandbox",
    "AgentMeter",
    "SkillManifest",
    # Observability
    "ChronicleClient",
    "WatchReporter",
    "MetricsCollector",
    "PulseClient",
    # Core infrastructure
    "ServiceBridge",
    "EventEmitter",
    "IntakeGuard",
    "ContextManager",
    "register_builtin_tools",
    "GraphMemory",
    # Mesh placement plane (node pairing is a CLI command: `adk pair <CODE>`)
    "A2AAdapter",
    "InferencePlacementSolver",
    "PlacementMemory",
    "LocalInferenceDiscovery",
    "measure_node_latencies",
    "NanoGPT",
    "NeuronPool",
    "AutoNeuronFire",
    "DegenerationDetector",
    "strip_internal_tags",
    "extract_tool_calls_from_text",
    "CATEGORY_TOOLS",
    # Elysium cloud
    "Elysium",
    # Faculty graphs (local knowledge)
    "CodeGraph",
    "MemoryGraph",
    "EmbeddingProvider",
    # Standalone tools
    "repowise_search",
    "swarm_code",
    # Mesh relay
    "AitherNetRelay",
    # Chat + Mail
    "ChatRelay",
    "MailRelay",
    # MCP client + server
    "MCPAuth",
    "MCPBridge",
    "MCPServer",
    "MCPError",
    "MCPAuthError",
    "MCPBalanceError",
    # Multi-agent group chat
    "AeonSession",
    "AeonResponse",
    "AeonMessage",
    "group_chat",
    "AEON_PRESETS",
    # A2A protocol
    "A2AServer",
    # Unified storage
    "Strata",
    "StrataBackend",
    "LocalBackend",
    # Cross-platform pairing
    "PairingManager",
    "PairingResult",
    "PlatformIdentity",
    # Voice
    "VoiceClient",
    "VoiceAgent",
    "get_voice_backend",
    "TranscriptionResult",
    "SynthesisResult",
    "EmotionResult",
    # Channels
    "ChannelAdapter",
    "TelegramAdapter",
    "DiscordAdapter",
    "SlackAdapter",
    "WebhookAdapter",
    # Cron
    "CronScheduler",
    "CronJob",
    # Skills
    "Skill",
    "SkillStore",
    "SkillExtractor",
    # Client (absorbed from aithersdk)
    "AitherClient",
    "AitherResponse",
    "GatewayClient",
    # Agent Onboarding Fabric — onboard an external agent (any framework) or run
    # a managed one: ACP drive-rail, universal Agent Pack, scoped managed identity,
    # zero-code connect templates, best-effort code validation.
    "ACPClient",
    "ACPServer",
    "serve_stdio",
    "ACPPromptResult",
    "ACPToolCall",
    "AgentPackManifest",
    "AgentHandle",
    "Supervisor",
    "load_agent_pack",
    "ManagedAgentIdentityProvider",
    "ManagedAgentIdentity",
    "ManagedAgentState",
    "render_connect",
    "SUPPORTED_FRAMEWORKS",
    "CodeValidator",
    "ValidationIssue",
    "ValidationContext",
    # Code-as-action: the model writes and runs Python over live objects.
    "CodeActLoop",
    # Real containment for untrusted cells (no network / read-only / killable).
    "DockerCellExecutor",
    "CellOutcome",
    "docker_available",
    # Pass-by-reference: bounded observations + handles to live objects.
    "ObjectRegistry",
    "get_registry",
    "render_observation",
    # Managed packs: drive a pack over its own protocol, and distribute it.
    "ToolCall",
    "DriverResult",
    "ProtocolDriver",
    "get_driver",
    "PackRegistry",
    "PublishReceipt",
    "PackSummary",
    "validate_pack",
    # Harness APIs the model itself can call, + NOOA-style `...` ergonomics.
    "EventLog",
    "ContextBlocks",
    "has_ellipsis_body",
    "strategy",
    "get_strategy_meta",
    # OO agents: subclass API where `...` methods dispatch to the LLM.
    "OOAgent",
    "AgenticReturnError",
]


def connect_mcp(api_key: str = "", mcp_url: str = "https://mcp.aitherium.com"):
    """Quick access to the MCP bridge. Returns a coroutine."""
    from adk.mcp import connect_mcp as _connect
    return _connect(api_key=api_key, mcp_url=mcp_url)


def connect_federation(host: str = "http://localhost", tenant: str = "public"):
    """Quick access to the federation client for connecting to Elysium."""
    from adk.federation import FederationClient
    return FederationClient(host=host, tenant=tenant)


def auto_setup(**kwargs):
    """Quick access to agent self-setup. Returns a coroutine."""
    from adk.setup import auto_setup as _auto_setup
    return _auto_setup(**kwargs)


# Lazy imports for heavier modules
def __getattr__(name):
    if name == "AgentRegistry":
        from adk.registry import AgentRegistry
        return AgentRegistry
    if name == "AgentForge":
        from adk.forge import AgentForge
        return AgentForge
    if name == "FleetConfig":
        from adk.fleet import FleetConfig
        return FleetConfig
    if name == "ConversationStore":
        from adk.conversations import ConversationStore
        return ConversationStore
    if name == "LoopGuard":
        from adk.loop_guard import LoopGuard
        return LoopGuard
    if name == "AitherSandbox":
        from adk.sandbox import AitherSandbox
        return AitherSandbox
    if name == "AgentMeter":
        from adk.metering import AgentMeter
        return AgentMeter
    if name == "SkillManifest":
        from adk.identity import SkillManifest
        return SkillManifest
    if name == "ChronicleClient":
        from adk.chronicle import ChronicleClient
        return ChronicleClient
    if name == "WatchReporter":
        from adk.watch import WatchReporter
        return WatchReporter
    if name == "MetricsCollector":
        from adk.metrics import MetricsCollector
        return MetricsCollector
    if name == "PulseClient":
        from adk.pulse import PulseClient
        return PulseClient
    if name == "ServiceBridge":
        from adk.services import ServiceBridge
        return ServiceBridge
    if name == "EventEmitter":
        from adk.events import EventEmitter
        return EventEmitter
    if name == "IntakeGuard":
        from adk.safety import IntakeGuard
        return IntakeGuard
    if name == "ContextManager":
        from adk.context import ContextManager
        return ContextManager
    if name == "register_builtin_tools":
        from adk.builtin_tools import register_builtin_tools
        return register_builtin_tools
    if name == "GraphMemory":
        from adk.graph_memory import GraphMemory
        return GraphMemory
    if name == "A2AAdapter":
        from adk.a2a_adapter import A2AAdapter
        return A2AAdapter
    if name == "InferencePlacementSolver":
        from adk.inference_placement_solver import InferencePlacementSolver
        return InferencePlacementSolver
    if name == "PlacementMemory":
        from adk.placement_memory import PlacementMemory
        return PlacementMemory
    if name == "LocalInferenceDiscovery":
        from adk.local_inference import LocalInferenceDiscovery
        return LocalInferenceDiscovery
    if name == "measure_node_latencies":
        from adk.latency_probe import measure_node_latencies
        return measure_node_latencies
    if name == "NanoGPT":
        # On-device training (NanoGPT) is proprietary IP and is NOT shipped in
        # the published open SDK. It lives in the internal AitherOS runtime.
        try:
            from adk.nanogpt import NanoGPT
            return NanoGPT
        except ImportError as exc:
            raise ImportError(
                "NanoGPT (on-device training) is not available in the open "
                "awdk SDK. It is part of the internal AitherOS runtime."
            ) from exc
    if name == "NeuronPool":
        from adk.neurons import NeuronPool
        return NeuronPool
    if name == "AutoNeuronFire":
        from adk.neurons import AutoNeuronFire
        return AutoNeuronFire
    if name == "DegenerationDetector":
        from adk.llm.base import DegenerationDetector
        return DegenerationDetector
    if name == "strip_internal_tags":
        from adk.llm.base import strip_internal_tags
        return strip_internal_tags
    if name == "extract_tool_calls_from_text":
        from adk.llm.base import extract_tool_calls_from_text
        return extract_tool_calls_from_text
    if name == "CATEGORY_TOOLS":
        from adk.neurons import CATEGORY_TOOLS
        return CATEGORY_TOOLS
    if name == "CodeGraph":
        from adk.faculties.code_graph import CodeGraph
        return CodeGraph
    if name == "MemoryGraph":
        # Back-compat: MemoryGraph now resolves to the canonical GraphMemory
        from adk.graph_memory import GraphMemory as MemoryGraph
        return MemoryGraph
    if name == "EmbeddingProvider":
        from adk.faculties.embeddings import EmbeddingProvider
        return EmbeddingProvider
    if name == "Elysium":
        from adk.elysium import Elysium
        return Elysium
    if name == "repowise_search":
        from adk.builtin_tools import repowise_search
        return repowise_search
    if name == "swarm_code":
        from adk.builtin_tools import swarm_code
        return swarm_code
    if name == "AitherNetRelay":
        from adk.relay import AitherNetRelay
        return AitherNetRelay
    if name == "ChatRelay":
        from adk.chat import ChatRelay
        return ChatRelay
    if name == "MailRelay":
        from adk.smtp import MailRelay
        return MailRelay
    if name == "MCPServer":
        from adk.mcp_server import MCPServer
        return MCPServer
    if name == "MCPAuth":
        from adk.mcp import MCPAuth
        return MCPAuth
    if name == "MCPBridge":
        from adk.mcp import MCPBridge
        return MCPBridge
    if name == "MCPError":
        from adk.mcp import MCPError
        return MCPError
    if name == "MCPAuthError":
        from adk.mcp import MCPAuthError
        return MCPAuthError
    if name == "MCPBalanceError":
        from adk.mcp import MCPBalanceError
        return MCPBalanceError
    if name == "AeonSession":
        from adk.aeon import AeonSession
        return AeonSession
    if name == "AeonResponse":
        from adk.aeon import AeonResponse
        return AeonResponse
    if name == "AeonMessage":
        from adk.aeon import AeonMessage
        return AeonMessage
    if name == "group_chat":
        from adk.aeon import group_chat
        return group_chat
    if name == "AEON_PRESETS":
        from adk.aeon import AEON_PRESETS
        return AEON_PRESETS
    if name == "A2AServer":
        from adk.a2a import A2AServer
        return A2AServer
    if name == "Strata":
        from adk.strata import Strata
        return Strata
    if name == "StrataBackend":
        from adk.strata import StrataBackend
        return StrataBackend
    if name == "LocalBackend":
        from adk.strata import LocalBackend
        return LocalBackend
    if name == "PairingManager":
        from adk.pairing import PairingManager
        return PairingManager
    if name == "PairingResult":
        from adk.pairing import PairingResult
        return PairingResult
    if name == "PlatformIdentity":
        from adk.pairing import PlatformIdentity
        return PlatformIdentity
    if name == "VoiceClient":
        from adk.voice import VoiceClient
        return VoiceClient
    if name == "VoiceAgent":
        from adk.voice_agent import VoiceAgent
        return VoiceAgent
    if name == "get_voice_backend":
        from adk.voice import get_voice_backend
        return get_voice_backend
    if name == "TranscriptionResult":
        from adk.voice import TranscriptionResult
        return TranscriptionResult
    if name == "SynthesisResult":
        from adk.voice import SynthesisResult
        return SynthesisResult
    if name == "EmotionResult":
        from adk.voice import EmotionResult
        return EmotionResult
    # ── Channels ───────────────────────────────────────────────────────────
    if name == "ChannelAdapter":
        from adk.channels import ChannelAdapter
        return ChannelAdapter
    if name == "TelegramAdapter":
        from adk.channels import TelegramAdapter
        return TelegramAdapter
    if name == "DiscordAdapter":
        from adk.channels import DiscordAdapter
        return DiscordAdapter
    if name == "SlackAdapter":
        from adk.channels import SlackAdapter
        return SlackAdapter
    if name == "WebhookAdapter":
        from adk.channels import WebhookAdapter
        return WebhookAdapter
    # ── Cron ──────────────────────────────────────────────────────────────
    if name == "CronScheduler":
        from adk.cron import CronScheduler
        return CronScheduler
    if name == "CronJob":
        from adk.cron import CronJob
        return CronJob
    # ── Skills ────────────────────────────────────────────────────────────
    if name == "Skill":
        from adk.skills import Skill
        return Skill
    if name == "SkillStore":
        from adk.skills import SkillStore
        return SkillStore
    if name == "SkillExtractor":
        from adk.skills import SkillExtractor
        return SkillExtractor
    # ── Client (absorbed from aithersdk) ──────────────────────────────────
    if name == "AitherClient":
        from adk.client import AitherClient
        return AitherClient
    if name == "AitherResponse":
        from adk.client import AitherResponse
        return AitherResponse
    if name == "GatewayClient":
        from adk.client import GatewayClient
        return GatewayClient
    # ── Agent Onboarding Fabric ───────────────────────────────────────────
    if name in ("ACPClient", "ACPPromptResult", "ACPToolCall"):
        import adk.acp as _acp
        return getattr(_acp, name)
    if name in ("ACPServer", "serve_stdio"):
        import adk.acp_server as _acps
        return getattr(_acps, name)
    if name in ("AgentPackManifest", "AgentHandle", "Supervisor", "load_agent_pack"):
        import adk.agent_pack as _ap
        return getattr(_ap, name)
    if name in (
        "ManagedAgentIdentityProvider",
        "ManagedAgentIdentity",
        "ManagedAgentState",
    ):
        import adk.managed_identity as _mi
        return getattr(_mi, name)
    if name in ("render_connect", "SUPPORTED_FRAMEWORKS"):
        import adk.connect as _c
        return getattr(_c, name)
    if name in ("CodeValidator", "ValidationIssue", "ValidationContext"):
        import adk.core.validator as _v
        return getattr(_v, name)
    if name == "CodeActLoop":
        from adk.core.codeact import CodeActLoop
        return CodeActLoop
    if name in ("DockerCellExecutor", "CellOutcome", "docker_available"):
        import adk.core.cell_executors as _ce
        return getattr(_ce, name)
    if name in ("ObjectRegistry", "get_registry", "render_observation"):
        import adk.object_registry as _or
        return getattr(_or, name)
    if name in ("ToolCall", "DriverResult", "ProtocolDriver", "get_driver"):
        import adk.pack_drivers as _pd
        return getattr(_pd, name)
    if name in ("PackRegistry", "PublishReceipt", "PackSummary", "validate_pack"):
        import adk.pack_registry as _pr
        return getattr(_pr, name)
    if name in ("EventLog", "ContextBlocks"):
        import adk.harness as _h
        return getattr(_h, name)
    if name in ("has_ellipsis_body", "strategy", "get_strategy_meta"):
        import adk.ellipsis as _e
        return getattr(_e, name)
    if name in ("OOAgent", "AgenticReturnError"):
        import adk.core.oo as _oo
        return getattr(_oo, name)
    raise AttributeError(f"module 'adk' has no attribute {name!r}")
