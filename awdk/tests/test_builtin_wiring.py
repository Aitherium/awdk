"""Tests for builtin tools and ServiceBridge wiring."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestBuiltinToolsOnAgent:
    """Builtin tools are auto-registered on agents."""

    def test_agent_has_builtin_tools(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="demiurge", llm=llm, builtin_tools=True,
            system_prompt="System",
        )
        tool_names = [t.name for t in agent._tools.list_tools()]
        # Demiurge gets: file_io(5) + shell(1) + python(1) + web(2) = 9
        assert len(tool_names) >= 5
        assert "file_read" in tool_names
        assert "file_write" in tool_names

    def test_builtin_tools_disabled(self):
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, builtin_tools=False,
            system_prompt="System",
        )
        tool_names = [t.name for t in agent._tools.list_tools()]
        assert "file_read" not in tool_names

    def test_explicit_tools_preserved(self):
        """User-provided tools are kept alongside builtins."""
        from adk.agent import AitherAgent
        from adk.tools import ToolRegistry
        custom_reg = ToolRegistry()
        custom_reg.register(lambda x="": "custom", name="my_tool", description="Custom")

        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(
            name="test", llm=llm, tools=[custom_reg],
            builtin_tools=True, system_prompt="System",
        )
        tool_names = [t.name for t in agent._tools.list_tools()]
        assert "my_tool" in tool_names
        assert "file_read" in tool_names  # Builtins also present

    def test_identity_based_tool_selection(self):
        """Different identities get different builtin tool categories."""
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"

        # Scribe gets file_io + web (no shell, no python)
        agent = AitherAgent(
            name="scribe", llm=llm, builtin_tools=True,
            system_prompt="System",
        )
        tool_names = [t.name for t in agent._tools.list_tools()]
        assert "file_read" in tool_names
        assert "shell_exec" not in tool_names

    def test_builtin_failure_nonfatal(self):
        """Agent still initializes if builtin registration fails."""
        from adk.agent import AitherAgent
        llm = AsyncMock()
        llm.provider_name = "test"
        with patch("adk.builtin_tools.register_builtin_tools", side_effect=ImportError("boom")):
            agent = AitherAgent(
                name="test", llm=llm, builtin_tools=True,
                system_prompt="System",
            )
        # Agent should exist, just without builtin tools
        assert agent.name == "test"


class TestForgeBuiltinTools:
    """Forge-spawned agents get builtin tools."""

    @pytest.mark.asyncio
    async def test_forged_agent_has_tools(self):
        """Agents created by forge have builtin tools by default."""
        from adk.forge import AgentForge, ForgeSpec
        from adk.agent import AitherAgent

        # The forge creates agents with AitherAgent() which has builtin_tools=True
        # We just verify the agent class default
        llm = AsyncMock()
        llm.provider_name = "test"
        agent = AitherAgent(name="forge-test", llm=llm, system_prompt="System")
        tool_names = [t.name for t in agent._tools.list_tools()]
        assert len(tool_names) > 0


class TestServiceBridgeWiring:
    """ServiceBridge connects on server startup."""

    @pytest.mark.asyncio
    async def test_bridge_standalone_mode(self):
        from adk.services import ServiceBridge
        bridge = ServiceBridge(node_url="http://localhost:99999")
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=Exception("refused"))
            mock_cls.return_value = mock_client
            status = await bridge.connect()
        assert status.mode == "standalone"
        assert bridge.connected

    @pytest.mark.asyncio
    async def test_bridge_register_on_agent(self):
        """Standalone mode returns 0 tools registered."""
        from adk.services import ServiceBridge
        bridge = ServiceBridge()
        bridge._connected = True
        bridge._status.mode = "standalone"
        mock_agent = MagicMock()
        count = await bridge.register_on_agent(mock_agent)
        assert count == 0

    @pytest.mark.asyncio
    async def test_bridge_failure_nonfatal(self):
        """Server still works if ServiceBridge fails."""
        from adk.services import ServiceBridge
        bridge = ServiceBridge(node_url="http://localhost:99999")
        bridge._connected = True
        bridge._status.mode = "standalone"
        status = await bridge.status()
        assert status["mode"] == "standalone"

    def test_bridge_singleton(self):
        from adk.services import get_service_bridge
        import adk.services
        adk.services._instance = None
        bridge1 = get_service_bridge()
        bridge2 = get_service_bridge()
        assert bridge1 is bridge2
        adk.services._instance = None  # Cleanup

    @pytest.mark.asyncio
    async def test_bridge_local_mode(self):
        """Bridge detects local awnode."""
        from adk.services import ServiceBridge
        bridge = ServiceBridge(node_url="http://localhost:8080")

        mock_resp_node = MagicMock()
        mock_resp_node.status_code = 200
        mock_resp_node.json.return_value = {"services": ["genesis"]}
        mock_resp_node.headers = {"content-type": "application/json"}

        mock_resp_genesis = MagicMock()
        mock_resp_genesis.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def mock_get(url, **kwargs):
            if "8080" in url:
                return mock_resp_node
            if "8001" in url:
                return mock_resp_genesis
            raise Exception("unknown")

        mock_client.get = mock_get

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await bridge.connect()
        assert status.mode == "local"
        assert status.node_available is True

    @pytest.mark.asyncio
    async def test_get_available_services_standalone(self):
        from adk.services import ServiceBridge
        bridge = ServiceBridge()
        bridge._connected = True
        bridge._status.mode = "standalone"
        services = await bridge.get_available_services()
        assert "file_io" in services
        assert "shell" in services
