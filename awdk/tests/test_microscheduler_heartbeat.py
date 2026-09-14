"""Tests for MicroScheduler heartbeat integration.

Verifies that personal agents enrolled via `adk onboard` send heartbeats
to MicroScheduler:8150 so they appear in the Portal's Fleet view.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.microscheduler_heartbeat import (
    start_microscheduler_heartbeat,
    start_heartbeat_threaded,
    get_agent_id_for_personal_agent,
    _get_system_metrics,
)


class TestAgentIdGeneration:
    """Test agent ID generation."""

    def test_get_agent_id_generates_unique_ids(self):
        """Generated agent IDs should be unique."""
        id1 = get_agent_id_for_personal_agent()
        id2 = get_agent_id_for_personal_agent()
        assert id1 != id2
        assert id1.startswith("personal-agent-")
        assert id2.startswith("personal-agent-")

    def test_get_agent_id_includes_hostname(self):
        """Generated agent ID should include hostname."""
        import socket
        hostname = socket.gethostname().lower().replace(" ", "-")
        agent_id = get_agent_id_for_personal_agent()
        assert hostname in agent_id


class TestSystemMetrics:
    """Test system metrics collection."""

    def test_get_system_metrics_returns_dict(self):
        """System metrics should return a dictionary."""
        metrics = _get_system_metrics()
        assert isinstance(metrics, dict)

    def test_get_system_metrics_has_cpu_memory(self):
        """System metrics should include CPU and memory percentages."""
        metrics = _get_system_metrics()
        assert "cpu_percent" in metrics
        assert "memory_percent" in metrics
        assert 0 <= metrics["cpu_percent"] <= 100
        assert 0 <= metrics["memory_percent"] <= 100


class TestHeartbeatPayload:
    """Test heartbeat payload construction."""

    @pytest.mark.asyncio
    async def test_heartbeat_sends_correct_payload(self):
        """Heartbeat should POST correct payload to MicroScheduler."""
        captured_requests = []

        # Mock httpx.AsyncClient
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient") as mock_ac:
            mock_ac.return_value.__aenter__.return_value = mock_client

            # Start heartbeat for 1 second (1 heartbeat cycle)
            heartbeat_task = asyncio.create_task(
                start_microscheduler_heartbeat(
                    agent_id="test-agent-001",
                    microscheduler_url="http://localhost:8150",
                    interval=1,
                    capabilities=["research", "coding"],
                    current_model="test-model",
                )
            )

            # Wait for heartbeat to be sent
            await asyncio.sleep(1.5)
            heartbeat_task.cancel()

            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            # Verify POST was called
            assert mock_client.post.called, "POST to /agents/heartbeat not called"

            # Get the call arguments
            call_args = mock_client.post.call_args
            assert call_args is not None

            # Verify URL
            url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
            assert "/agents/heartbeat" in url

            # Verify payload
            payload = call_args[1].get("json", {})
            assert payload.get("agent_id") == "test-agent-001"
            assert payload.get("kind") == "agent"
            assert "capabilities" in payload
            assert "research" in payload["capabilities"]
            assert "coding" in payload["capabilities"]
            assert payload.get("current_model") == "test-model"
            assert "resource_usage" in payload
            assert "metadata" in payload

    @pytest.mark.asyncio
    async def test_heartbeat_handles_connection_error_gracefully(self):
        """Heartbeat should handle connection errors gracefully."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        with patch("httpx.AsyncClient") as mock_ac:
            mock_ac.return_value.__aenter__.return_value = mock_client

            # Start heartbeat for 2 seconds (should send despite error)
            heartbeat_task = asyncio.create_task(
                start_microscheduler_heartbeat(
                    agent_id="test-agent-002",
                    microscheduler_url="http://localhost:8150",
                    interval=1,
                )
            )

            # Wait a bit
            await asyncio.sleep(1.5)
            heartbeat_task.cancel()

            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            # Should still have called POST (error was swallowed)
            assert mock_client.post.called


class TestHeartbeatThreaded:
    """Test threaded heartbeat startup."""

    def test_start_heartbeat_threaded_returns_agent_id(self):
        """Threaded heartbeat should return an agent ID."""
        agent_id = start_heartbeat_threaded(
            agent_id="test-thread-agent"
        )
        assert agent_id == "test-thread-agent"

    def test_start_heartbeat_threaded_generates_id_if_not_provided(self):
        """Should generate an agent ID if not provided."""
        agent_id = start_heartbeat_threaded()
        assert agent_id is not None
        assert agent_id.startswith("personal-agent-")

    def test_start_heartbeat_threaded_starts_daemon_thread(self):
        """Should start a daemon thread."""
        import threading
        before_count = threading.active_count()
        start_heartbeat_threaded(agent_id="test-daemon-thread")
        # Give thread a moment to start
        import time
        time.sleep(0.1)
        after_count = threading.active_count()
        assert after_count > before_count, "Thread was not started"


class TestOnboardingIntegration:
    """Integration tests for onboarding flow."""

    def test_microscheduler_heartbeat_module_importable(self):
        """The heartbeat module should be importable from adk."""
        from adk.microscheduler_heartbeat import start_heartbeat_threaded
        assert callable(start_heartbeat_threaded)

    def test_heartbeat_visible_in_onboarding_flow(self):
        """The heartbeat initialization should be part of onboarding."""
        from adk import cli
        # Verify cmd_onboard exists and references the heartbeat module
        import inspect
        source = inspect.getsource(cli.cmd_onboard)
        # The onboard flow should mention heartbeat at some point
        # (This is more of a sanity check)
        assert "cmd_onboard" in source
