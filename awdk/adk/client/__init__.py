"""AitherOS client library — absorbed from aithersdk into adk.client.

Usage:
    from adk.client import AitherClient

    client = AitherClient()  # connects to localhost:8001
    response = await client.chat("Hello!")
    print(response.text)

    # Service sub-clients
    await client.context.status("session-1")
    await client.strata.write("/path", "content")
    await client.a2a.services()
    await client.expeditions.submit("objective")
    await client.voice.transcribe(audio_bytes)
    await client.conversations.list()

    # Gateway (cloud)
    from adk.client import GatewayClient
    gw = GatewayClient(api_key="aither_pat_...")
"""

from adk.client._client import AitherClient, AitherResponse
from adk.client._models import (
    ChatRequest,
    ChatResponse,
    ChatMetadata,
    ToolCall,
    WillInfo,
    AgentInfo,
    ServiceHealth,
)
from adk.client._gateway import GatewayClient
from adk.client._gateway_mcp import GatewayMCPClient, create_gateway_mcp_client
from adk.client._base import ServiceClient
from adk.client.services import (
    ContextClient,
    A2AClient,
    StrataClient,
    ExpeditionClient,
    VoiceClient,
    ConversationClient,
)

__all__ = [
    "AitherClient",
    "AitherResponse",
    "ChatRequest",
    "ChatResponse",
    "ChatMetadata",
    "ToolCall",
    "WillInfo",
    "AgentInfo",
    "ServiceHealth",
    "GatewayClient",
    "GatewayMCPClient",
    "create_gateway_mcp_client",
    "ServiceClient",
    "ContextClient",
    "A2AClient",
    "StrataClient",
    "ExpeditionClient",
    "VoiceClient",
    "ConversationClient",
]
