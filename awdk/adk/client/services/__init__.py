"""AitherOS service sub-clients."""

from adk.client.services.context import ContextClient
from adk.client.services.a2a import A2AClient
from adk.client.services.strata import StrataClient
from adk.client.services.data_plane import DataPlaneClient
from adk.client.services.expeditions import ExpeditionClient
from adk.client.services.voice import VoiceClient
from adk.client.services.conversations import ConversationClient
from adk.client.services.capabilities import CapabilitiesClient

__all__ = [
    "ContextClient",
    "A2AClient",
    "StrataClient",
    "DataPlaneClient",
    "ExpeditionClient",
    "VoiceClient",
    "ConversationClient",
    "CapabilitiesClient",
]
