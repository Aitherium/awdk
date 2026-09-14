"""MCP stdio transport — run an ADK agent as a stdio MCP server.

Claude Code and other MCP clients expect:
  - Read newline-delimited JSON-RPC 2.0 from stdin
  - Write newline-delimited JSON-RPC 2.0 to stdout

Usage:
    adk mcp serve              # loads agent from ./agent.py or ./config.yaml
    adk mcp serve --port 9000  # proxy to a running ADK server instead
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("adk.mcp_stdio")


def _load_agent_from_project(project_dir: str = "."):
    """Load an AitherAgent from a project directory (agent.py or config.yaml)."""
    from adk.agent import AitherAgent
    from adk.config import Config

    project = Path(project_dir).resolve()

    # Try agent.py first
    agent_file = project / "agent.py"
    if agent_file.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("_user_agent", str(agent_file))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Find the AitherAgent instance
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, AitherAgent):
                return obj

    # Fall back to config.yaml
    config_file = project / "config.yaml"
    if config_file.exists():
        cfg = Config.from_yaml(str(config_file))
        agent = AitherAgent(cfg.identity or "adk-agent")
        return agent

    # Minimal agent with no tools
    return AitherAgent("adk-agent")


async def run_stdio(agent=None, project_dir: str = "."):
    """Run the MCP server on stdin/stdout."""
    from adk.mcp_server import MCPServer

    if agent is None:
        agent = _load_agent_from_project(project_dir)

    server = MCPServer(
        tool_registry=agent._tools,
        server_name=agent.name,
        require_auth=False,  # stdio is local — no auth needed
    )

    logger.info(
        "MCP stdio server started (%d tools from %s)",
        len(server.registry.list_tools()),
        agent.name,
    )

    # `loop.connect_read_pipe` on stdin raises under Windows' Proactor event loop,
    # which crashed this server outright on Windows. Use the shared thread-backed
    # reader (same one the live-proven ACP server uses) — identical on POSIX.
    from adk.stdio_compat import ThreadStdinReader

    reader = ThreadStdinReader(sys.stdin.buffer)

    while True:
        line = await reader.readline()
        if not line:
            break  # EOF

        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = await server.handle(request)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def cmd_mcp_serve(args) -> int:
    """Entry point for `adk mcp serve`."""
    project_dir = getattr(args, "directory", ".") or "."

    # If --port given, print config for connecting to a running server instead
    port = getattr(args, "port", None)
    if port:
        print(json.dumps({
            "mcpServers": {
                "adk": {
                    "url": f"http://localhost:{port}/mcp",
                    "transport": "http",
                }
            }
        }, indent=2))
        print()
        print(f"Add the above to your Claude Code MCP config to connect to")
        print(f"your running ADK server at localhost:{port}.")
        return 0

    # stdio mode
    try:
        asyncio.run(run_stdio(project_dir=project_dir))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mcp_config(args) -> int:
    """Print Claude Code / OpenClaw MCP configuration."""
    port = getattr(args, "port", 8080) or 8080
    mode = getattr(args, "mode", "stdio") or "stdio"

    if mode == "http":
        config = {
            "mcpServers": {
                "adk": {
                    "url": f"http://localhost:{port}/mcp",
                    "transport": "http",
                }
            }
        }
    else:
        config = {
            "mcpServers": {
                "adk": {
                    "command": "adk",
                    "args": ["mcp", "serve"],
                    "transport": "stdio",
                }
            }
        }

    print(json.dumps(config, indent=2))
    print()
    print("Add the above to your MCP client configuration:")
    print("  Claude Code:  ~/.claude/settings.local.json")
    print("  OpenClaw:     ~/.openclaw/openclaw.json")
    return 0
