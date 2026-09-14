"""Test that ReActLoop emits observations with role='user' (not role='tool').

This test pins the fix where adk ReActLoop must emit observations
in user-role for OpenAI API compatibility (DeepSeek, GitHub Models).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.core.agent import Agent, ReActLoop
from adk.core.model import Message, ModelBackend, ModelResponse
from adk.core.tool import Tool, ToolResult


class MockBackend(ModelBackend):
    """A mock backend that returns ReAct-formatted responses."""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_count = 0
        self.call_messages = []

    async def generate(self, messages: list[Message]) -> ModelResponse:
        """Return a pre-defined response and track messages."""
        self.call_messages.append(messages)
        if self.call_count >= len(self.responses):
            resp = "Final Answer: done"
        else:
            resp = self.responses[self.call_count]
        self.call_count += 1
        return ModelResponse(text=resp, model="mock", finish_reason="stop")


class AsyncCallableTool:
    """A tool that can be called as an async function."""

    def __init__(self, name: str, description: str, result: ToolResult):
        self.name = name
        self.description = description
        self.result = result

    async def __call__(self, **kwargs):
        return self.result


@pytest.mark.asyncio
async def test_react_loop_observations_are_user_role():
    """Verify that observations are emitted as role='user', not role='tool'."""
    # A tool that always succeeds
    test_tool = AsyncCallableTool(
        name="test_tool",
        description="A test tool",
        result=ToolResult(ok=True, value="test output successfully"),
    )

    # Mock backend that returns: tool call, then final answer
    backend = MockBackend([
        "Action: test_tool\nAction Input: {}",
        "Final Answer: success",
    ])

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[test_tool],
        loop=ReActLoop(max_steps=8),
    )

    result = await agent.run("Do something")

    # Check the messages history
    # Expected flow:
    # 1. system prompt
    # 2. user prompt
    # 3. assistant response (Action call)
    # 4. user response (Observation) <- THIS MUST BE role="user"
    # 5. assistant response (Final Answer)
    messages = result.messages

    # Find the observation message (should come after the action)
    observation_msgs = [m for m in messages if "test output successfully" in m.content]
    assert len(observation_msgs) > 0, (
        f"No observation message found. Messages: "
        f"{[(i, m.role, m.content[:80]) for i, m in enumerate(messages)]}"
    )

    for obs_msg in observation_msgs:
        # THE FIX: Observations must be role="user", not role="tool"
        assert obs_msg.role == "user", (
            f"Observation must have role='user' for OpenAI compatibility, "
            f"got role='{obs_msg.role}'. This breaks DeepSeek/GitHub Models "
            f"which enforce message-role alternation (tool→user required)."
        )


@pytest.mark.asyncio
async def test_react_loop_error_observations_are_user_role():
    """Verify that error observations are also emitted as role='user'."""
    # A tool that fails
    test_tool = AsyncCallableTool(
        name="test_tool",
        description="A test tool",
        result=ToolResult(ok=False, error="tool failed"),
    )

    # Mock backend that returns: tool call, then final answer
    backend = MockBackend([
        "Action: test_tool\nAction Input: {}",
        "Final Answer: failed gracefully",
    ])

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[test_tool],
        loop=ReActLoop(max_steps=8),
    )

    result = await agent.run("Do something")

    # Find error observation message (tool errors appear as "ERROR: tool failed")
    error_msgs = [m for m in result.messages if "tool failed" in m.content]
    assert len(error_msgs) > 0, "No error observation message found"

    for err_msg in error_msgs:
        assert err_msg.role == "user", (
            f"Error observation must have role='user', got role='{err_msg.role}'"
        )


@pytest.mark.asyncio
async def test_react_loop_unknown_tool_error_is_user_role():
    """Verify that unknown tool errors are emitted as role='user'."""
    test_tool = AsyncCallableTool(
        name="real_tool",
        description="A real tool",
        result=ToolResult(ok=True, value="should not be called"),
    )

    # Mock backend that calls a non-existent tool, then provides final answer
    backend = MockBackend([
        "Action: nonexistent_tool\nAction Input: {}",
        "Final Answer: handled gracefully",
    ])

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[test_tool],
        loop=ReActLoop(max_steps=8),
    )

    result = await agent.run("Do something")

    # Find unknown tool error
    unknown_tool_msgs = [m for m in result.messages if "unknown tool" in m.content]
    assert len(unknown_tool_msgs) > 0, "No unknown tool error found"

    for msg in unknown_tool_msgs:
        assert msg.role == "user", (
            f"Unknown tool error must be role='user', got role='{msg.role}'"
        )


@pytest.mark.asyncio
async def test_react_loop_invalid_json_error_is_user_role():
    """Verify that invalid JSON errors are emitted as role='user'."""
    mock_tool = MagicMock(spec=Tool)
    mock_tool.name = "test_tool"
    mock_tool.description = "A test tool"

    # Mock backend that sends invalid JSON, then provides final answer
    backend = MockBackend([
        "Action: test_tool\nAction Input: not valid json",
        "Final Answer: handled gracefully",
    ])

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[mock_tool],
        loop=ReActLoop(max_steps=8),
    )

    result = await agent.run("Do something")

    # Find JSON error
    json_error_msgs = [m for m in result.messages if "invalid Action Input JSON" in m.content]
    assert len(json_error_msgs) > 0, "No JSON error found"

    for msg in json_error_msgs:
        assert msg.role == "user", (
            f"JSON error must be role='user', got role='{msg.role}'"
        )
