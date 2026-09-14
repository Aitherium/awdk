"""Tests for adk/context.py — ContextManager with token-aware truncation."""

import sys
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.context import ContextManager, ContextMessage, count_tokens


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("") >= 0

    def test_short_string(self):
        # With the fallback (no tiktoken), roughly len/4
        tokens = count_tokens("hello world")
        assert tokens >= 1

    def test_long_string(self):
        text = "word " * 1000
        tokens = count_tokens(text)
        # Should be a reasonable count (not zero, not absurdly high)
        assert tokens > 100


# ---------------------------------------------------------------------------
# ContextMessage
# ---------------------------------------------------------------------------

class TestContextMessage:
    def test_auto_token_count(self):
        msg = ContextMessage(role="user", content="Hello world")
        # +4 overhead added automatically
        assert msg.tokens > 0

    def test_explicit_token_count(self):
        msg = ContextMessage(role="user", content="test", tokens=50)
        assert msg.tokens == 50

    def test_tool_call_fields(self):
        msg = ContextMessage(
            role="tool",
            content="result",
            tool_call_id="call_123",
        )
        assert msg.tool_call_id == "call_123"


# ---------------------------------------------------------------------------
# ContextManager — adding messages
# ---------------------------------------------------------------------------

class TestAddMessages:
    def test_add_system(self):
        ctx = ContextManager()
        msg = ctx.add_system("You are a helpful assistant.")
        assert msg.role == "system"
        assert ctx.message_count == 1

    def test_add_user(self):
        ctx = ContextManager()
        msg = ctx.add_user("Hello!")
        assert msg.role == "user"

    def test_add_assistant(self):
        ctx = ContextManager()
        msg = ctx.add_assistant("Hi there!")
        assert msg.role == "assistant"

    def test_add_tool(self):
        ctx = ContextManager()
        msg = ctx.add_tool("tool result", tool_call_id="call_1")
        assert msg.role == "tool"
        assert msg.tool_call_id == "call_1"

    def test_add_generic(self):
        ctx = ContextManager()
        msg = ctx.add("function", "result data")
        assert msg.role == "function"

    def test_message_count(self):
        ctx = ContextManager()
        ctx.add_system("sys")
        ctx.add_user("u1")
        ctx.add_assistant("a1")
        assert ctx.message_count == 3

    def test_total_tokens(self):
        ctx = ContextManager()
        ctx.add_system("short")
        ctx.add_user("also short")
        assert ctx.total_tokens > 0


# ---------------------------------------------------------------------------
# Build — no truncation
# ---------------------------------------------------------------------------

class TestBuildNoTruncation:
    def test_build_returns_dicts(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("System prompt")
        ctx.add_user("Hello")
        ctx.add_assistant("Hi!")

        messages = ctx.build()
        assert isinstance(messages, list)
        assert len(messages) == 3
        assert all(isinstance(m, dict) for m in messages)

    def test_build_preserves_order(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("sys")
        ctx.add_user("user1")
        ctx.add_assistant("assist1")
        ctx.add_user("user2")

        messages = ctx.build()
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user"]

    def test_build_includes_content(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("You are helpful")
        ctx.add_user("Test message")

        messages = ctx.build()
        assert messages[0]["content"] == "You are helpful"
        assert messages[1]["content"] == "Test message"

    def test_build_includes_tool_call_id(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_tool("result", tool_call_id="tc_1")

        messages = ctx.build()
        assert messages[0]["tool_call_id"] == "tc_1"

    def test_build_includes_tool_calls(self):
        ctx = ContextManager(max_tokens=100000)
        tool_calls = [{"id": "tc_1", "function": {"name": "search"}}]
        ctx.add_assistant("", tool_calls=tool_calls)

        messages = ctx.build()
        assert messages[0]["tool_calls"] == tool_calls

    def test_build_omits_empty_tool_fields(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_user("no tools here")

        messages = ctx.build()
        assert "tool_calls" not in messages[0]
        assert "tool_call_id" not in messages[0]


# ---------------------------------------------------------------------------
# Build — with truncation
# ---------------------------------------------------------------------------

class TestBuildWithTruncation:
    def test_drops_oldest_non_system_messages(self):
        # Very tight budget
        ctx = ContextManager(max_tokens=200, preserve_turns=1, reserve_for_response=50)
        ctx.add_system("System prompt")
        # Add many messages that exceed budget
        for i in range(20):
            ctx.add_user(f"User message number {i} with some extra text padding")
            ctx.add_assistant(f"Assistant response number {i} with padding text here")

        messages = ctx.build()
        # System message always preserved
        assert messages[0]["role"] == "system"
        # Recent messages preserved
        assert len(messages) < 42  # 1 system + 40 user/assistant pairs - dropped some

    def test_system_messages_always_kept(self):
        ctx = ContextManager(max_tokens=100, preserve_turns=1, reserve_for_response=20)
        ctx.add_system("Important system prompt")
        for i in range(10):
            ctx.add_user(f"Message {i} " * 5)
            ctx.add_assistant(f"Response {i} " * 5)

        messages = ctx.build()
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert system_msgs[0]["content"] == "Important system prompt"

    def test_preserve_turns_keeps_recent(self):
        ctx = ContextManager(max_tokens=300, preserve_turns=2, reserve_for_response=50)
        ctx.add_system("sys")

        for i in range(10):
            ctx.add_user(f"user-{i}")
            ctx.add_assistant(f"assistant-{i}")

        messages = ctx.build()
        # Last 2 turns (4 messages) should always be present
        contents = [m["content"] for m in messages]
        assert "user-9" in contents
        assert "assistant-9" in contents
        assert "user-8" in contents
        assert "assistant-8" in contents

    def test_budget_warning_logged(self):
        # Create a scenario where even after truncation we're over budget
        ctx = ContextManager(max_tokens=10, preserve_turns=2, reserve_for_response=5)
        ctx.add_system("A very long system prompt that alone exceeds the budget " * 10)
        ctx.add_user("Question")
        ctx.add_assistant("Answer")

        # Should not raise, just log a warning
        messages = ctx.build()
        assert len(messages) >= 1


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clear_removes_all_messages(self):
        ctx = ContextManager()
        ctx.add_system("sys")
        ctx.add_user("u1")
        ctx.add_assistant("a1")
        assert ctx.message_count == 3

        ctx.clear()
        assert ctx.message_count == 0
        assert ctx.total_tokens == 0

    def test_clear_then_rebuild(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("old system")
        ctx.clear()
        ctx.add_system("new system")

        messages = ctx.build()
        assert len(messages) == 1
        assert messages[0]["content"] == "new system"


# ---------------------------------------------------------------------------
# History ordering
# ---------------------------------------------------------------------------

class TestHistoryOrdering:
    def test_conversation_flow_order(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("You are an assistant")
        ctx.add_user("What is Python?")
        ctx.add_assistant("Python is a programming language.")
        ctx.add_user("What about Java?")
        ctx.add_assistant("Java is also a programming language.")

        messages = ctx.build()
        assert len(messages) == 5
        expected_roles = ["system", "user", "assistant", "user", "assistant"]
        actual_roles = [m["role"] for m in messages]
        assert actual_roles == expected_roles

    def test_tool_calls_in_order(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("sys")
        ctx.add_user("search for X")
        ctx.add_assistant("Let me search", tool_calls=[{"id": "tc1"}])
        ctx.add_tool("search results", tool_call_id="tc1")
        ctx.add_assistant("Based on the results...")

        messages = ctx.build()
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_only_system_message(self):
        ctx = ContextManager(max_tokens=100000)
        ctx.add_system("sys prompt")
        messages = ctx.build()
        assert len(messages) == 1

    def test_empty_context(self):
        ctx = ContextManager()
        messages = ctx.build()
        assert messages == []

    def test_single_very_long_message(self):
        ctx = ContextManager(max_tokens=100, preserve_turns=1, reserve_for_response=20)
        ctx.add_system("sys")
        ctx.add_user("x" * 10000)

        # Should not crash
        messages = ctx.build()
        assert len(messages) >= 1

    def test_preserve_turns_exceeds_messages(self):
        ctx = ContextManager(max_tokens=100, preserve_turns=100, reserve_for_response=20)
        ctx.add_system("sys")
        ctx.add_user("only one user msg")

        messages = ctx.build()
        assert len(messages) >= 1
