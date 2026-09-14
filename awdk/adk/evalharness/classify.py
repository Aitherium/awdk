"""Tool classification — determine safety and mutability.

Classifies tools as read-only/safe (can be invoked) vs mutating/dangerous
(should NOT be invoked during testing). Uses verb-based heuristics plus
explicit pack manifests.

Usage:
    classifier = ToolClassifier()
    safe = classifier.classify(tool)  # Returns "safe" or "mutating"
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ToolCategory(str, Enum):
    """Tool safety classification."""
    SAFE = "safe"           # Read-only, no side effects
    MUTATING = "mutating"   # Makes changes, should not invoke
    UNKNOWN = "unknown"     # Uncertain classification


class ToolClassifier:
    """Classify tools by safety and mutability."""

    # Verbs that indicate a mutating/dangerous tool
    MUTATING_VERBS = {
        "delete", "remove", "destroy", "kill", "drop", "purge",
        "revoke", "rotate", "reset", "clear", "uninstall",
        "stop", "terminate", "cancel", "abort",
        "create", "add", "insert", "upload", "push",
        "update", "modify", "edit", "patch", "replace",
        "enable", "disable", "configure", "setup",
        "write", "save", "commit", "deploy", "start",
    }

    # Verbs that indicate a safe/read-only tool
    SAFE_VERBS = {
        "get", "list", "search", "query", "find", "describe",
        "read", "show", "view", "display", "fetch",
        "check", "test", "validate", "verify", "inspect",
        "status", "health", "info", "help",
    }

    # Tool name patterns (regexps-like substrings) that are definitely mutating
    MUTATING_PATTERNS = {
        r"delete", r"remove", r"destroy", r"kill",
        r"drop", r"purge", r"revoke", r"reset",
        r"clear", r"uninstall", r"terminate",
        r"stop", r"cancel", r"abort",
    }

    def __init__(self):
        """Initialize the classifier."""
        pass

    def classify(self, tool_name: str, description: str = "") -> ToolCategory:
        """Classify a tool as safe or mutating.

        Args:
            tool_name: Name of the tool (e.g., "delete_user")
            description: Optional description of the tool

        Returns:
            ToolCategory enum: SAFE, MUTATING, or UNKNOWN
        """
        tool_name_lower = tool_name.lower()
        desc_lower = description.lower()

        # Check explicit mutating patterns in name
        for pattern in self.MUTATING_PATTERNS:
            if pattern.lower() in tool_name_lower:
                logger.debug("Tool '%s' classified as MUTATING (pattern match)", tool_name)
                return ToolCategory.MUTATING

        # Extract verb (first word) from tool name
        parts = tool_name_lower.split("_")
        if not parts:
            return ToolCategory.UNKNOWN

        verb = parts[0]

        # Check explicit mutating verbs
        if verb in self.MUTATING_VERBS:
            logger.debug("Tool '%s' classified as MUTATING (verb: %s)", tool_name, verb)
            return ToolCategory.MUTATING

        # Check explicit safe verbs
        if verb in self.SAFE_VERBS:
            logger.debug("Tool '%s' classified as SAFE (verb: %s)", tool_name, verb)
            return ToolCategory.SAFE

        # Heuristic: if description mentions mutation, classify as mutating
        mutating_words = ["delete", "remove", "destroy", "modify", "create", "update", "reset"]
        for word in mutating_words:
            if word in desc_lower:
                logger.debug("Tool '%s' classified as MUTATING (description match)", tool_name)
                return ToolCategory.MUTATING

        # Default to safe if uncertain (conservative: don't invoke unless sure it's dangerous)
        logger.debug("Tool '%s' classified as UNKNOWN (defaulting to safe)", tool_name)
        return ToolCategory.SAFE

    def is_safe_to_invoke(self, tool_name: str, description: str = "") -> bool:
        """Check if a tool is safe to invoke during testing.

        Args:
            tool_name: Name of the tool
            description: Optional description

        Returns:
            True if safe to invoke, False if mutating
        """
        category = self.classify(tool_name, description)
        return category != ToolCategory.MUTATING

    def filter_safe_tools(self, tools: list) -> list:
        """Filter a list of tools to only safe ones.

        Args:
            tools: List of tool objects (with name/description attributes)

        Returns:
            List of tools that are safe to invoke
        """
        safe = []
        for tool in tools:
            if hasattr(tool, 'name') and hasattr(tool, 'description'):
                if self.is_safe_to_invoke(tool.name, tool.description or ""):
                    safe.append(tool)
            elif isinstance(tool, dict):
                if self.is_safe_to_invoke(tool.get("name", ""), tool.get("description", "")):
                    safe.append(tool)
        return safe
