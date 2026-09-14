"""Tool enumeration — discover all tools available on a gateway.

Lists tools available via the MCP gateway, filtering by tier as needed.
This module handles the gateway connection and tool discovery without
assuming any specific backend.

Usage:
    enumerator = ToolEnumerator()
    await enumerator.connect(gateway_url, api_key)
    tools = await enumerator.list_all()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolInfo:
    """Information about a single tool available on the gateway."""
    name: str
    description: str = ""
    parameters: dict = None
    category: str = ""  # Tool category if available from gateway metadata
    tier: str = ""      # Minimum tier required for this tool

    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}


class ToolEnumerator:
    """Enumerate tools available on an MCP gateway."""

    def __init__(self):
        self._bridge = None
        self._tools: list[ToolInfo] = []
        self._connected = False
        self._gateway_url = ""

    @property
    def connected(self) -> bool:
        """Whether we're connected to a gateway."""
        return self._connected

    @property
    def gateway_url(self) -> str:
        """The gateway URL we're connected to."""
        return self._gateway_url

    async def connect(
        self,
        gateway_url: str = "",
        api_key: str = "",
        auth=None,
    ) -> bool:
        """Connect to an MCP gateway.

        Args:
            gateway_url: MCP gateway URL (e.g., mcp.aitherium.com)
            api_key: API key for authentication
            auth: MCPAuth context (takes precedence over api_key)

        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Import here to avoid hard dependency at module level
            from adk.mcp import MCPBridge, MCPAuth

            if auth:
                self._bridge = MCPBridge(auth=auth)
                self._gateway_url = auth.gateway_url
            elif api_key:
                self._bridge = MCPBridge(mcp_url=gateway_url, api_key=api_key)
                self._gateway_url = gateway_url or "https://mcp.aitherium.com"
            else:
                self._bridge = MCPBridge(mcp_url=gateway_url or "https://mcp.aitherium.com")
                self._gateway_url = gateway_url or "https://mcp.aitherium.com"

            # Test connectivity
            health = await self._bridge.health()
            self._connected = health
            if health:
                logger.info("Connected to MCP gateway: %s", self._gateway_url)
            else:
                logger.warning("Gateway health check failed: %s", self._gateway_url)
        except Exception as exc:
            logger.error("Failed to connect to MCP gateway: %s", exc)
            self._connected = False
            return False

        return True

    async def list_all(self) -> list[ToolInfo]:
        """List all available tools from the connected gateway.

        Returns:
            List of ToolInfo objects
        """
        if not self._connected or not self._bridge:
            raise RuntimeError("Not connected to a gateway. Call connect() first.")

        try:
            mcp_tools = await self._bridge.list_tools(refresh=True)
            self._tools = []

            for tool in mcp_tools:
                info = ToolInfo(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters or {},
                )
                self._tools.append(info)

            logger.info("Enumerated %d tools", len(self._tools))
            return self._tools
        except Exception as exc:
            logger.error("Failed to enumerate tools: %s", exc)
            raise

    def get_tool(self, name: str) -> Optional[ToolInfo]:
        """Get a specific tool by name."""
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def find_tools(self, pattern: str) -> list[ToolInfo]:
        """Find tools matching a name pattern (substring match)."""
        pattern_lower = pattern.lower()
        return [
            t for t in self._tools
            if pattern_lower in t.name.lower()
            or pattern_lower in t.description.lower()
        ]
