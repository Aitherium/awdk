"""
awnode -- Lightweight local MCP server.

Modes:
- proxy: Proxies tool calls to mcp.aitherium.com (default, needs account)
- standalone: Runs basic tools locally (filesystem, git, shell -- no account needed)

Start via CLI:
    aither mcp node                      # proxy mode (default)
    aither mcp node --mode standalone    # local-only tools
    aither mcp node --port 9000          # custom port
"""

__all__ = ["run_node"]

from adk.node.server import run_node
