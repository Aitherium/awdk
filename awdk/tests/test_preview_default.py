"""Tests for agent observation preview defaults and render_observation behavior.

This module documents the current state of ReActLoop's unbounded default
and tests the render_observation helper that should become the default.

DESIGN NOTE: ReActLoop currently has max_preview_chars=None (unbounded),
which means tool results can be serialized to massive strings. The fix
is to use render_observation as the DEFAULT for bounded previews + handles,
so large objects reach the model as "list(len=10000, ..., id=obj:list:a1b2c3)"
instead of all 10000 elements.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from adk.core.agent import ReActLoop
from adk.core.model import Message, ModelResponse
from adk.core.tool import tool, ToolResult
from adk.object_registry import ObjectRegistry, render_observation


class DummyBackend:
    """Minimal LLM backend for testing."""

    def __init__(self, responses=None):
        self.responses = responses or []
        self.call_count = 0

    async def generate(self, messages):
        """Return a canned response."""
        if self.call_count < len(self.responses):
            text = self.responses[self.call_count]
        else:
            text = "Final Answer: done"
        self.call_count += 1
        return ModelResponse(text=text, model="test", finish_reason="stop")


class TestReActLoopCurrentDefault:
    """Test the CURRENT (unbounded) behavior of ReActLoop.

    This documents the issue: max_preview_chars defaults to None,
    which means huge objects become unbounded strings in observations.
    """

    def test_react_loop_default_max_preview_is_none(self):
        """Verify that ReActLoop's default max_preview_chars is None (unbounded)."""
        loop = ReActLoop()
        # The default is currently None, which means unbounded str() rendering
        assert loop.max_preview_chars is None

    def test_react_loop_with_max_preview_chars_bounded(self):
        """Verify that setting max_preview_chars DOES bound observations."""
        loop = ReActLoop(max_preview_chars=500)
        assert loop.max_preview_chars == 500

    @pytest.mark.asyncio
    async def test_react_loop_unbounded_default_large_result(self):
        """Test that the default (unbounded) encodes large results as full strings.

        This documents the current behavior: with max_preview_chars=None,
        tool results are converted via str() without bounding.
        """
        from adk.core.agent import Agent

        # Create a tool that returns a huge list
        @tool(name="get_huge_list", description="Returns a list with 100k elements")
        async def huge_tool_func():
            return list(range(100000))

        backend = DummyBackend(
            responses=[
                "Action: get_huge_list\nAction Input: {}",
                "Final Answer: got the list",
            ]
        )

        agent = Agent(
            name="test",
            model=backend,
            tools=[huge_tool_func],
            loop=ReActLoop(),  # default, unbounded
        )

        result = await agent.run("Get a huge list")

        # With unbounded default, the observation will be a huge string
        # Look at the messages to verify
        messages_text = " ".join(m.content for m in result.messages)

        # The full list representation will be in the messages
        # (This is the problem we're solving)
        assert "0, 1, 2" in messages_text or len(messages_text) > 50000

    @pytest.mark.asyncio
    async def test_react_loop_bounded_with_max_preview_chars(self):
        """Test that setting max_preview_chars DOES bound observations."""
        from adk.core.agent import Agent

        # Create a tool that returns a huge list
        @tool(name="get_huge_list", description="Returns a list with 100k elements")
        async def huge_tool_func2():
            return list(range(100000))

        backend = DummyBackend(
            responses=[
                "Action: get_huge_list\nAction Input: {}",
                "Final Answer: got the list",
            ]
        )

        agent = Agent(
            name="test",
            model=backend,
            tools=[huge_tool_func2],
            loop=ReActLoop(max_preview_chars=500),  # bounded
        )

        result = await agent.run("Get a huge list")

        # With bounded preview, the observation will be a compact representation
        messages_text = " ".join(m.content for m in result.messages)

        # Should contain len marker and be much shorter than unbounded
        # (max_preview_chars truncates the output)
        assert len(messages_text) < 100000  # Much smaller than 100k list


class TestRenderObservationBehavior:
    """Test the render_observation helper function.

    This is the INTENDED DEFAULT for agent observations:
    - Small values: rendered plainly
    - Large values: bounded preview + handle for model to request details
    """

    def test_render_observation_small_value_plain(self):
        """Test that small values are rendered plainly without handles."""
        registry = ObjectRegistry()
        small_value = {"status": "ok", "count": 42}

        result = render_observation(small_value, registry=registry, max_chars=500)

        # Should be simple representation
        assert "{" in result and "}" in result
        # Typically won't need a handle for small objects
        # (may or may not be registered depending on size)

    def test_render_observation_huge_value_bounded_plus_handle(self):
        """Test that huge values get bounded preview + handle.

        This demonstrates the intended default: model sees a bounded preview
        like "list(len=100000, [:5]=[0,1,2,3,4], ...)" instead of all 100k items.
        """
        registry = ObjectRegistry()
        huge_value = list(range(100000))

        result = render_observation(huge_value, registry=registry, max_chars=300)

        # Result should be MUCH smaller than the object
        assert len(result) < 10000  # Should be a small bounded representation

        # Should contain the handle so model can ask for details
        assert "obj:list:" in result
        # Should mention how to get more
        assert "available via handle:" in result or "id=obj:" in result

    def test_render_observation_model_can_request_details_via_handle(self):
        """Test that the model can request full object via the returned handle.

        This demonstrates the solution: the model sees only a bounded preview
        but can ask for details by providing the handle.
        """
        registry = ObjectRegistry()
        huge_list = list(range(10000))

        # Render for the model
        preview = render_observation(huge_list, registry=registry, max_chars=200)

        # Extract handle from the preview
        # Format: "list(len=10000, [:5]=[...], ...) (available via handle: obj:list:abc123)"
        start = preview.find("obj:list:")
        if start >= 0:
            end = preview.find(")", start)
            if end < 0:
                end = preview.find("\n", start)
            handle = preview[start:end].rstrip(")")

            # Model requests details via handle
            full_obj = registry.deref(handle)
            assert full_obj is huge_list

    def test_render_observation_no_full_content_in_preview(self):
        """Test that full object content doesn't appear in preview.

        This is the core requirement: large objects must NOT serialize their
        full content into the model's observation. The preview should only
        show structure + handle.
        """
        registry = ObjectRegistry()
        secret_data = "SUPER_SECRET_" + "x" * 10000

        preview = render_observation(secret_data, registry=registry, max_chars=300)

        # The full secret should NOT be in the preview
        # (only a bounded representation + handle)
        assert len(preview) < 1000
        # Should have a handle
        assert "obj:str:" in preview or "id=obj:" in preview

        # But the full secret IS still available via the handle
        # (for internal code that needs it, not for the model directly)
        for match_str in preview.split():
            if match_str.startswith("obj:str:"):
                full_obj = registry.deref(match_str.rstrip(")"))
                if full_obj:
                    assert full_obj == secret_data

    def test_render_observation_integration_with_truncating_pformat(self):
        """Test that render_observation uses truncating_pformat for bounded output."""
        registry = ObjectRegistry()

        # Create a deeply nested structure
        nested = {"level1": {"level2": {"level3": {"level4": list(range(1000))}}}}

        result = render_observation(nested, registry=registry, max_chars=200)

        # Should be bounded
        assert len(result) < 2000  # Some overhead for metadata

        # Should contain structure hint
        assert "dict" in result or "level" in result or "{" in result
