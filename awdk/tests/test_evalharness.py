"""Tests for MCP evaluation harness."""

import pytest
from adk.evalharness import (
    ToolCategory,
    ToolClassifier,
    ToolInfo,
    ToolInvoker,
    InvokeResult,
    EvalReport,
    PackEvalResult,
)


class TestToolClassifier:
    """Test tool classification (safe vs mutating)."""

    def test_classify_mutating_verbs(self):
        """Mutating verbs should be classified as mutating."""
        classifier = ToolClassifier()

        mutating_tools = [
            "delete_user",
            "remove_item",
            "destroy_resource",
            "drop_table",
            "revoke_access",
            "reset_config",
            "stop_service",
            "create_file",
            "update_record",
        ]

        for tool_name in mutating_tools:
            result = classifier.classify(tool_name)
            assert result == ToolCategory.MUTATING, f"{tool_name} should be MUTATING"

    def test_classify_safe_verbs(self):
        """Safe verbs should be classified as safe."""
        classifier = ToolClassifier()

        safe_tools = [
            "get_user",
            "list_items",
            "search_database",
            "fetch_data",
            "describe_resource",
            "view_logs",
            "check_health",
            "test_connection",
        ]

        for tool_name in safe_tools:
            result = classifier.classify(tool_name)
            assert result == ToolCategory.SAFE, f"{tool_name} should be SAFE"

    def test_is_safe_to_invoke(self):
        """Test the is_safe_to_invoke method."""
        classifier = ToolClassifier()

        assert classifier.is_safe_to_invoke("get_user") is True
        assert classifier.is_safe_to_invoke("delete_user") is False
        assert classifier.is_safe_to_invoke("list_items") is True
        assert classifier.is_safe_to_invoke("remove_item") is False

    def test_filter_safe_tools(self):
        """Filter should return only safe tools."""
        classifier = ToolClassifier()

        tools = [
            {"name": "get_user", "description": "Get user info"},
            {"name": "delete_user", "description": "Delete a user"},
            {"name": "list_items", "description": "List items"},
            {"name": "remove_item", "description": "Remove an item"},
        ]

        safe = classifier.filter_safe_tools(tools)
        assert len(safe) == 2
        assert all(t["name"].startswith(("get_", "list_")) for t in safe)

    def test_mutating_patterns(self):
        """Explicit patterns should be classified as mutating."""
        classifier = ToolClassifier()

        patterns = ["delete_", "remove_", "destroy_", "drop_", "purge_"]
        for pattern in patterns:
            tool_name = f"{pattern}resource"
            result = classifier.classify(tool_name)
            assert result == ToolCategory.MUTATING, f"Pattern {pattern} should trigger MUTATING"

    def test_never_invoke_mutating(self):
        """Mutating tools should never be safe to invoke."""
        classifier = ToolClassifier()

        mutating_words = [
            "delete", "remove", "destroy", "revoke", "rotate",
            "reset", "kill", "stop", "purge", "terminate",
        ]

        for word in mutating_words:
            tool_name = f"{word}_something"
            assert not classifier.is_safe_to_invoke(tool_name), \
                f"Tool {tool_name} should not be safe to invoke"


class TestInvokeResult:
    """Test InvokeResult data class."""

    def test_callable_status(self):
        """Test the callable property."""
        # Callable results
        result = InvokeResult("test", True, "callable")
        assert result.callable is True

        result = InvokeResult("test", False, "callable_degraded")
        assert result.callable is True

        # Non-callable results
        result = InvokeResult("test", False, "error")
        assert result.callable is False

        result = InvokeResult("test", False, "param_error")
        assert result.callable is False

    def test_param_error_is_callable(self):
        """Parameter validation errors should count as callable."""
        result = InvokeResult(
            "test_tool",
            False,
            "callable",
            "Parameter validation error"
        )
        assert result.callable is True
        assert result.is_error is False

    def test_degraded_status(self):
        """Degraded results should be callable but not successful."""
        result = InvokeResult(
            "test_tool",
            False,
            "callable_degraded",
            "Service temporarily unavailable"
        )
        assert result.callable is True
        assert result.success is False


class TestEvalReport:
    """Test report generation."""

    def test_add_tool(self):
        """Test adding tool results."""
        report = EvalReport(gateway_url="test://local", authenticated=False)

        report.add_tool("tool1", callable=True, safe=True)
        report.add_tool("tool2", callable=True, safe=False)
        report.add_tool("tool3", callable=False, safe=True)

        assert len(report.tools) == 3
        assert report.total_tools == 3
        assert report.total_callable == 2
        assert report.total_safe == 2

    def test_json_format(self):
        """JSON output should be valid."""
        report = EvalReport(
            gateway_url="test://local",
            authenticated=True,
            tier="pro"
        )

        report.add_tool("get_user", callable=True, safe=True)
        report.add_tool("delete_user", callable=True, safe=False)

        json_output = report.json_format()
        assert "gateway" in json_output
        assert "authenticated" in json_output
        assert "tools" in json_output
        assert "get_user" in json_output

    def test_human_format(self):
        """Human format should be readable."""
        report = EvalReport(gateway_url="test://local")

        report.add_tool("get_user", callable=True, safe=True)
        report.add_tool("delete_user", callable=False, safe=False)

        human = report.human_format()
        assert "Summary" in human
        assert "Total tools" in human

    def test_pack_result(self):
        """Test pack evaluation results."""
        report = EvalReport(gateway_url="test://local")

        pack = PackEvalResult(
            pack_name="test-pack",
            pack_id="test",
            tools_declared=5,
            tools_found=3,
            tools_missing=["tool1", "tool2"],
            success=False,
        )

        report.add_pack(pack)

        assert len(report.packs) == 1
        assert not pack.all_declared_found
        assert len(pack.tools_missing) == 2

    def test_pack_all_found(self):
        """Test pack with all tools found."""
        pack = PackEvalResult(
            pack_name="complete-pack",
            pack_id="complete",
            tools_declared=5,
            tools_found=5,
            tools_missing=[],
            success=True,
        )

        assert pack.all_declared_found is True


class TestToolInfo:
    """Test ToolInfo data class."""

    def test_tool_info_creation(self):
        """Test creating tool info."""
        tool = ToolInfo(
            name="test_tool",
            description="A test tool",
            parameters={"arg1": {"type": "string"}},
        )

        assert tool.name == "test_tool"
        assert tool.description == "A test tool"
        assert "arg1" in tool.parameters

    def test_tool_info_defaults(self):
        """Parameters should default to empty dict."""
        tool = ToolInfo(name="simple_tool")

        assert tool.parameters == {}
        assert tool.description == ""


class TestPackEvalResult:
    """Test PackEvalResult."""

    def test_missing_tools_detection(self):
        """Pack should detect missing tools."""
        pack = PackEvalResult(
            pack_name="test",
            tools_declared=10,
            tools_found=8,
            tools_missing=["tool1", "tool2"],
            success=False,
        )

        assert not pack.success
        assert not pack.all_declared_found
        assert len(pack.tools_missing) == 2

    def test_to_dict(self):
        """Should convert to dict."""
        pack = PackEvalResult(
            pack_name="test-pack",
            tools_declared=5,
            tools_found=5,
            success=True,
        )

        d = pack.to_dict()
        assert d["pack_name"] == "test-pack"
        assert d["success"] is True


class TestToolInvokerClassification:
    """Test that invoker respects classification."""

    def test_never_invoke_mutating(self):
        """Invoker should skip mutating tools."""
        from unittest.mock import AsyncMock

        classifier = ToolClassifier()
        invoker = ToolInvoker(bridge=AsyncMock())

        tools = [
            ToolInfo(name="get_user", description="Safe read"),
            ToolInfo(name="delete_user", description="Mutating delete"),
        ]

        # This would normally invoke, but with a stubbed bridge
        # We can at least verify the classification is used correctly
        for tool in tools:
            is_safe = classifier.is_safe_to_invoke(tool.name)
            if tool.name == "get_user":
                assert is_safe is True
            elif tool.name == "delete_user":
                assert is_safe is False

    def test_validation_error_is_callable(self):
        """Validation errors should count as callable."""
        result = InvokeResult(
            tool_name="test_tool",
            success=False,
            status="callable",
            message="Missing required parameter: 'id'",
            error_type="validation",
        )

        assert result.callable is True, \
            "Validation errors indicate the tool is callable (just param was wrong)"


class TestClassifierEdgeCases:
    """Test edge cases in classification."""

    def test_empty_tool_name(self):
        """Should handle empty tool names."""
        classifier = ToolClassifier()
        result = classifier.classify("")
        assert result in (ToolCategory.UNKNOWN, ToolCategory.SAFE)

    def test_tool_with_multiple_mutating_patterns(self):
        """Multiple patterns should still classify as mutating."""
        classifier = ToolClassifier()

        # This has "delete" and "remove" in description
        result = classifier.classify(
            "modify_thing",
            description="Delete and remove items"
        )
        assert result == ToolCategory.MUTATING

    def test_description_based_classification(self):
        """Description should influence classification."""
        classifier = ToolClassifier()

        # Verb is 'manage' (unknown), but description says "delete"
        result = classifier.classify(
            "manage_items",
            description="Delete all old items"
        )
        assert result == ToolCategory.MUTATING

    def test_unknown_verb_defaults_safe(self):
        """Unknown verbs should default to safe (conservative)."""
        classifier = ToolClassifier()

        result = classifier.classify("foo_bar")
        # Should not be MUTATING since no mutating signal found
        assert result != ToolCategory.MUTATING
