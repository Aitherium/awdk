"""Tests for multi-agent dispatch via A2A protocol."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.dispatch import (
    MultiAgentDispatcher,
    DispatchSpec,
    TaskResult,
    DispatchResult,
)


@pytest.fixture
def mock_agent():
    """Create a mock AitherAgent for synthesis."""
    agent = MagicMock()

    class MockResponse:
        def __init__(self, content):
            self.content = content
            self.artifacts = []

    agent.chat = AsyncMock(
        return_value=MockResponse(
            "Synthesis: All subtasks completed successfully."
        )
    )
    return agent


class TestMultiAgentDispatcher:
    def test_dispatcher_creation(self):
        """Test that dispatcher can be created."""
        dispatcher = MultiAgentDispatcher()
        assert dispatcher is not None

    def test_dispatcher_with_agent(self, mock_agent):
        """Test dispatcher with an agent."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)
        assert dispatcher._agent is mock_agent

    @pytest.mark.asyncio
    async def test_dispatch_single_subtask_via_a2a(self, mock_agent):
        """Test dispatching a single subtask via A2A (mocked)."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        spec = DispatchSpec(
            subtasks=[("research-agent", "Summarize AWS pricing")],
            main_task="Create marketing content",
            effort_level=7,
        )

        # Mock A2A send_message to return a successful result
        with patch(
            "adk.a2a_client.send_message"
        ) as mock_send:
            mock_send.return_value = {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [{"type": "text", "text": "AWS pricing"}],
                        }
                    ]
                }
            }

            result = await dispatcher.dispatch(spec)

            assert isinstance(result, DispatchResult)
            assert result.success is True
            assert len(result.task_results) == 1
            assert "task_0" in result.task_results
            assert result.task_results["task_0"].success is True
            assert "AWS pricing" in result.task_results["task_0"].content

    @pytest.mark.asyncio
    async def test_dispatch_multiple_subtasks_via_a2a(self, mock_agent):
        """Test dispatching multiple subtasks in parallel via A2A."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        spec = DispatchSpec(
            subtasks=[
                ("research-agent", "Summarize AWS pricing"),
                ("write-agent", "Write a blog post about cloud costs"),
                ("design-agent", "Create visuals for the content"),
            ],
            main_task="Create marketing content about AWS",
            effort_level=7,
        )

        # Mock A2A send_message
        responses = [
            {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [
                                {
                                    "type": "text",
                                    "text": "AWS has 3 pricing models: on-demand, reserved, spot.",
                                }
                            ],
                        }
                    ]
                }
            },
            {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [
                                {
                                    "type": "text",
                                    "text": "Cloud costs can be optimized by choosing the right "
                                    "pricing model for your workload.",
                                }
                            ],
                        }
                    ]
                }
            },
            {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [
                                {
                                    "type": "text",
                                    "text": "Chart.png shows cost comparison across models.",
                                }
                            ],
                        }
                    ]
                }
            },
        ]

        call_count = 0

        async def mock_send_side_effect(*args, **kwargs):
            nonlocal call_count
            result = responses[call_count]
            call_count += 1
            return result

        with patch("adk.a2a_client.send_message") as mock_send:
            mock_send.side_effect = mock_send_side_effect

            result = await dispatcher.dispatch(spec)

            # Verify result structure
            assert isinstance(result, DispatchResult)
            assert result.success is True
            assert len(result.task_results) == 3
            assert all(tr.success for tr in result.task_results.values())

            # Verify each task result
            assert result.task_results["task_0"].agent_name == "research-agent"
            assert "pricing models" in result.task_results["task_0"].content

            assert result.task_results["task_1"].agent_name == "write-agent"
            assert "optimized" in result.task_results["task_1"].content

            assert result.task_results["task_2"].agent_name == "design-agent"
            assert "Chart" in result.task_results["task_2"].content

            # Verify synthesis ran
            assert result.synthesis != ""
            assert "Synthesis" in result.synthesis or len(result.synthesis) > 0

    @pytest.mark.asyncio
    async def test_dispatch_with_a2a_failure_and_fallback(self, mock_agent):
        """Test that A2A failure triggers fallback to local agent."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        spec = DispatchSpec(
            subtasks=[
                ("unavailable-agent", "Do something"),
            ],
            main_task="Test fallback",
            effort_level=7,
        )

        # Mock A2A send_message to fail
        with patch("adk.a2a_client.send_message") as mock_send:
            mock_send.side_effect = Exception("Agent unreachable")

            result = await dispatcher.dispatch(spec)

            assert isinstance(result, DispatchResult)
            assert len(result.task_results) == 1
            # Fallback should have succeeded (local agent available)
            assert result.task_results["task_0"].success is True
            assert result.task_results["task_0"].via == "fallback"

    @pytest.mark.asyncio
    async def test_dispatch_respects_recursion_depth(self, mock_agent):
        """Test that dispatch respects max recursion depth."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        spec = DispatchSpec(
            subtasks=[("agent", "task")],
            main_task="test",
            recursion_depth=3,  # At max
        )

        result = await dispatcher.dispatch(spec)

        # Should fail due to recursion depth
        assert result.success is False
        assert "recursion depth" in result.error.lower()

    @pytest.mark.asyncio
    async def test_dispatch_respects_fan_out_ceiling(self, mock_agent):
        """Test that dispatch respects fan-out ceiling."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        # Create 25 subtasks (exceeds default ceiling of 20)
        spec = DispatchSpec(
            subtasks=[
                (f"agent-{i}", f"task-{i}") for i in range(25)
            ],
            main_task="test",
        )

        with patch("adk.a2a_client.send_message") as mock_send:
            mock_send.return_value = {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [{"type": "text", "text": "result"}],
                        }
                    ]
                }
            }

            result = await dispatcher.dispatch(spec)

            # Should truncate to ceiling (20)
            assert len(result.task_results) == 20

    @pytest.mark.asyncio
    async def test_task_result_structure(self):
        """Test that TaskResult is structured correctly."""
        tr = TaskResult(
            task_name="task_0",
            agent_name="test-agent",
            success=True,
            content="Test content",
            via="a2a",
        )

        assert tr.task_name == "task_0"
        assert tr.agent_name == "test-agent"
        assert tr.success is True
        assert tr.content == "Test content"
        assert tr.via == "a2a"
        assert tr.error is None

    @pytest.mark.asyncio
    async def test_dispatch_result_synthesis_included(self, mock_agent):
        """Test that dispatch result includes synthesis."""
        dispatcher = MultiAgentDispatcher(agent=mock_agent)

        spec = DispatchSpec(
            subtasks=[
                ("agent-1", "task-1"),
                ("agent-2", "task-2"),
            ],
            main_task="Integration test",
        )

        with patch("adk.a2a_client.send_message") as mock_send:
            mock_send.return_value = {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [{"type": "text", "text": "result"}],
                        }
                    ]
                }
            }

            result = await dispatcher.dispatch(spec)

            # Synthesis should have been called and included
            assert result.synthesis != ""
            mock_agent.chat.assert_called()

    @pytest.mark.asyncio
    async def test_no_agent_fallback_synthesis(self):
        """Test synthesis when no agent is available (fallback mode)."""
        dispatcher = MultiAgentDispatcher(agent=None)

        spec = DispatchSpec(
            subtasks=[
                ("agent-1", "task-1"),
                ("agent-2", "task-2"),
            ],
            main_task="No agent test",
        )

        with patch("adk.a2a_client.send_message") as mock_send:
            mock_send.return_value = {
                "task": {
                    "history": [
                        {
                            "role": "agent",
                            "parts": [{"type": "text", "text": "result"}],
                        }
                    ]
                }
            }

            result = await dispatcher.dispatch(spec)

            # Should still produce a synthesis without an agent — the fallback
            # stitches per-task sections (it no longer echoes the main task).
            assert result.synthesis != ""
            assert "task_0" in result.synthesis
            assert "result" in result.synthesis
