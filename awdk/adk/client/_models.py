"""Typed models for AitherOS API requests and responses."""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class ChatRequest(BaseModel):
    """Request to the /chat endpoint."""
    message: str
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    persona: Optional[str] = None
    system_prompt: Optional[str] = None
    max_tokens: Optional[int] = None
    effort: Optional[int] = Field(None, ge=1, le=10)
    temperature: Optional[float] = Field(None, ge=0, le=2)
    stream: bool = False
    include_neurons: bool = True
    include_memory: bool = True
    include_affect: bool = True


class ToolCall(BaseModel):
    """A tool call made during processing."""
    category: Optional[str] = None
    tool: Optional[str] = None
    success: bool = True


class ChatMetadata(BaseModel):
    """Metadata about the chat response."""
    elapsed_ms: Optional[int] = None
    effort_level: Optional[int] = None
    reasoning_depth: Optional[str] = None
    intent_complexity: Optional[str] = None
    axiom_grounding: bool = False
    tools_used: List[str] = Field(default_factory=list)
    fast_mode: bool = False
    via_ucb_think: bool = False


class ChatResponse(BaseModel):
    """Response from the /chat endpoint."""
    response: str = ""
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    model_used: Optional[str] = None
    complexity: Optional[str] = None
    reasoning_used: bool = False
    neurons_fired: int = 0
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tokens: Optional[Dict[str, int]] = None
    metadata: Optional[ChatMetadata] = None
    agentic: bool = False
    turns_completed: int = 0
    error: Optional[str] = None

    @property
    def text(self) -> str:
        """Convenience: get response text."""
        return self.response

    @property
    def success(self) -> bool:
        """True if no error."""
        return not self.error

    @property
    def elapsed_seconds(self) -> float:
        """Response time in seconds."""
        if self.metadata and self.metadata.elapsed_ms:
            return self.metadata.elapsed_ms / 1000.0
        return 0.0


class WillInfo(BaseModel):
    """Will (persona) information."""
    id: str
    name: str
    description: str = ""
    display_name: Optional[str] = None
    active: bool = False
    tags: List[str] = Field(default_factory=list)


class AgentInfo(BaseModel):
    """Agent information."""
    id: str
    name: str
    role: str = ""
    capabilities: List[str] = Field(default_factory=list)
    status: str = "unknown"


class ServiceHealth(BaseModel):
    """Service health status."""
    status: str = "unknown"
    service: str = ""
    uptime_sec: float = 0.0
