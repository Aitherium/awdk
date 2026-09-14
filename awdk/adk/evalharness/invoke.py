"""Tool invocation — smoke-test safe tools.

Attempts to invoke safe tools with empty/minimal arguments to verify
they are callable and schema-valid. Parameter validation errors count
as "callable" (handler resolved). Downstream outages count as
"callable but degraded."

Usage:
    invoker = ToolInvoker(bridge)
    results = await invoker.invoke_safe_tools(tools)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class InvokeResult:
    """Result of a tool invocation attempt."""
    tool_name: str
    success: bool
    status: str  # "callable", "callable_degraded", "error", "param_error"
    message: str = ""
    response: str = ""
    error_type: str = ""  # "auth", "notfound", "validation", "timeout", "unknown"

    @property
    def callable(self) -> bool:
        """Tool is callable (resolved to handler)."""
        return self.status in ("callable", "callable_degraded")

    @property
    def is_error(self) -> bool:
        """Invocation failed."""
        return self.status == "error"


class ToolInvoker:
    """Invoke safe tools with empty arguments for smoke testing."""

    def __init__(self, bridge=None):
        """Initialize with an MCP bridge.

        Args:
            bridge: MCPBridge instance for tool invocation
        """
        self._bridge = bridge

    def set_bridge(self, bridge):
        """Set or replace the MCP bridge."""
        self._bridge = bridge

    async def invoke_safe_tools(self, tools: list, classifier=None) -> list[InvokeResult]:
        """Invoke safe tools and collect results.

        Args:
            tools: List of ToolInfo objects to test
            classifier: ToolClassifier to filter safe tools (if None, invokes all)

        Returns:
            List of InvokeResult objects
        """
        results = []

        if classifier is None:
            # Import here to avoid hard dependency
            from evalharness.classify import ToolClassifier
            classifier = ToolClassifier()

        for tool in tools:
            tool_name = tool.name if hasattr(tool, 'name') else tool.get("name", "")
            if not tool_name:
                continue

            # Skip mutating tools
            description = tool.description if hasattr(tool, 'description') else tool.get("description", "")
            if not classifier.is_safe_to_invoke(tool_name, description or ""):
                logger.debug("Skipping mutating tool: %s", tool_name)
                continue

            result = await self.invoke_tool(tool_name)
            results.append(result)

        return results

    async def invoke_tool(self, tool_name: str, arguments: dict = None) -> InvokeResult:
        """Invoke a single tool with empty/minimal arguments.

        Args:
            tool_name: Name of the tool to invoke
            arguments: Optional arguments (default: empty dict)

        Returns:
            InvokeResult with outcome
        """
        if not self._bridge:
            return InvokeResult(
                tool_name=tool_name,
                success=False,
                status="error",
                message="No bridge connected",
                error_type="unknown",
            )

        arguments = arguments or {}
        logger.debug("Invoking tool: %s with args %s", tool_name, arguments)

        try:
            response = await self._bridge.call_tool(tool_name, arguments)
            logger.debug("Tool %s succeeded", tool_name)
            return InvokeResult(
                tool_name=tool_name,
                success=True,
                status="callable",
                message="Tool invoked successfully",
                response=response[:200] if response else "",  # Truncate response for log
            )
        except Exception as exc:
            error_msg = str(exc)
            error_type = self._classify_error(error_msg, type(exc).__name__)

            # Parameter validation errors are "callable" (handler resolved)
            if error_type == "validation":
                logger.debug("Tool %s: parameter validation error (callable)", tool_name)
                return InvokeResult(
                    tool_name=tool_name,
                    success=False,
                    status="callable",  # Handler resolved, params were wrong
                    message=f"Parameter validation error: {error_msg[:100]}",
                    error_type=error_type,
                )

            # Downstream outages are "callable but degraded"
            if "503" in error_msg or "502" in error_msg or "timeout" in error_msg.lower():
                logger.debug("Tool %s: downstream outage", tool_name)
                return InvokeResult(
                    tool_name=tool_name,
                    success=False,
                    status="callable_degraded",
                    message=f"Downstream service unavailable: {error_msg[:100]}",
                    error_type="timeout",
                )

            # Other errors
            logger.debug("Tool %s failed: %s", tool_name, error_msg)
            return InvokeResult(
                tool_name=tool_name,
                success=False,
                status="error",
                message=error_msg[:200],
                error_type=error_type,
            )

    @staticmethod
    def _classify_error(error_msg: str, exc_type: str) -> str:
        """Classify an error type."""
        error_lower = error_msg.lower()

        if "401" in error_msg or "authentication" in error_lower or "unauthorized" in error_lower:
            return "auth"
        if "404" in error_msg or "not found" in error_lower or "notfound" in error_lower:
            return "notfound"
        if "422" in error_msg or "validation" in error_lower or "invalid" in error_lower:
            return "validation"
        if "timeout" in error_lower or "timed out" in error_lower:
            return "timeout"
        if "503" in error_msg or "502" in error_msg or "unavailable" in error_lower:
            return "timeout"

        return "unknown"
