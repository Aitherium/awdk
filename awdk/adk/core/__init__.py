"""AitherADK core primitives.

First-principles agent framework. Zero hard dependencies on AitherOS.
See ``AitherOS/packages/aither_adk/SPEC.md`` for the full contract.
"""

from adk.core.agent import (
    Agent,
    AgentLoop,
    AgentResult,
    PredictLoop,
    ReActLoop,
    Strategy,
)
from adk.core.capability import (
    Capability,
    CapabilityContext,
    CapabilityDenied,
    requires,
)
from adk.core.loader import AgentSpecError, load_agent
from adk.core.logging import get_logger
from adk.core.mcp import MCPClient, MCPError, MCPTool, mcp_tools
from adk.core.memory import InMemoryStore, Memory, MemoryRecord
from adk.core.model import Message, ModelBackend, auto_backend
from adk.core.oo import AgenticReturnError, OOAgent
from adk.core.otel import OTelNotInstalled, OTelTracer, try_build_otel_tracer
from adk.core.persistence import FileStore
from adk.core.sandbox import (
    RunResult,
    Sandbox,
    SandboxEscape,
    get_sandbox,
    safe_read_text,
    safe_run,
    safe_write_text,
    set_default_sandbox,
    use_sandbox,
)
from adk.core.scaffold import scaffold_agent
from adk.core.secrets import (
    ChainStore,
    EnvStore,
    KeyringStore,
    SecretHandle,
    SecretNotFound,
    SecretStore,
    get_store,
    set_default_store,
    use_store,
)
from adk.core.secrets import (
    FileStore as SecretFileStore,
)
from adk.core.secrets import (
    resolve as resolve_secret,
)
from adk.core.spawn import SpawnAgentTool, spawn_agent_tool
from adk.core.tool import Tool, ToolError, ToolResult, tool
from adk.core.trace import (
    LoggingTracer,
    NoOpTracer,
    Span,
    Tracer,
    get_tracer,
    set_tracer,
)
from adk.core.typed_memory import (
    RecalledItem,
    Role,
    Tier,
    TypedMemory,
    as_typed,
    infer_role,
    parse_constraint,
)
from adk.core.typed_memory import (
    labels as memory_labels,
)
from adk.core.typed_memory import (
    score as memory_score,
)

__all__ = [
    "Agent",
    "AgentLoop",
    "AgentResult",
    "AgentSpecError",
    "Capability",
    "CapabilityContext",
    "CapabilityDenied",
    "ChainStore",
    "EnvStore",
    "FileStore",
    "InMemoryStore",
    "KeyringStore",
    "RunResult",
    "Sandbox",
    "SandboxEscape",
    "SecretFileStore",
    "SecretHandle",
    "SecretNotFound",
    "SecretStore",
    "get_sandbox",
    "get_store",
    "resolve_secret",
    "safe_read_text",
    "safe_run",
    "safe_write_text",
    "set_default_sandbox",
    "set_default_store",
    "use_sandbox",
    "use_store",
    "MCPClient",
    "MCPError",
    "MCPTool",
    "Memory",
    "MemoryRecord",
    "Message",
    "ModelBackend",
    "RecalledItem",
    "Role",
    "Tier",
    "TypedMemory",
    "as_typed",
    "infer_role",
    "memory_labels",
    "memory_score",
    "parse_constraint",
    "LoggingTracer",
    "NoOpTracer",
    "OTelNotInstalled",
    "OTelTracer",
    "AgenticReturnError",
    "OOAgent",
    "PredictLoop",
    "ReActLoop",
    "Strategy",
    "Span",
    "set_tracer",
    "try_build_otel_tracer",
    "SpawnAgentTool",
    "Tool",
    "ToolError",
    "ToolResult",
    "Tracer",
    "auto_backend",
    "get_logger",
    "get_tracer",
    "load_agent",
    "mcp_tools",
    "requires",
    "scaffold_agent",
    "spawn_agent_tool",
    "tool",
]
