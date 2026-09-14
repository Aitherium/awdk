"""MCP Evaluation Harness — test your tool/pack integration with an MCP gateway.

This module provides a customer-facing capability to bootstrap any awdk install
against an MCP gateway and self-test every tool/pack it has. Use this after installing
a paid pack to verify:

1. The pack's declared tools exist on the gateway
2. Tools are callable (resolve to handlers)
3. Tools are safe to invoke (read-only, no mutation)
4. Your authentication and tier are correct

Example usage:

    from adk.evalharness import run_eval_tools

    # Evaluate all tools on the connected gateway
    exit_code = await run_eval_tools(
        gateway_url="https://mcp.aitherium.com",
        api_key="aither_sk_live_...",
    )

    # Exit codes:
    #   0 = all verdicts pass
    #   1 = failures (named in output)
    #   2 = cannot judge (no gateway/auth/transport down)

For the CLI: `adk eval --help`
"""

from __future__ import annotations

from .classify import ToolCategory, ToolClassifier
from .enumerate import ToolEnumerator, ToolInfo
from .invoke import ToolInvoker, InvokeResult
from .report import EvalReport, PackEvalResult, ToolEvalResult

__all__ = [
    "ToolEnumerator",
    "ToolInfo",
    "ToolClassifier",
    "ToolCategory",
    "ToolInvoker",
    "InvokeResult",
    "EvalReport",
    "PackEvalResult",
    "ToolEvalResult",
]
