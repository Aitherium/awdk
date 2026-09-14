"""
AitherShell MCP Stdio Bridge
==============================

Implements the MCP (Model Context Protocol) stdio server protocol,
allowing Claude Code and other MCP clients to use AitherOS tools.

Usage:
    aither mcp serve                    # Start MCP stdio server
    aither mcp serve --remote           # Force cloud gateway
    aither mcp serve --local            # Force local Genesis

Claude Code config:
    {
      "mcpServers": {
        "aitheros": {
          "command": "aither",
          "args": ["mcp", "serve"]
        }
      }
    }
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
LOCAL_NODE_URL = os.environ.get("AITHER_NODE_URL", "http://localhost:8080")
LOCAL_GENESIS_URL = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
REMOTE_MCP_URL = os.environ.get("AITHER_MCP_URL", "https://mcp.aitherium.com")


class MCPStdioBridge:
    """Bridges MCP stdio protocol to AitherOS tools (local or remote)."""

    def __init__(self, mode: str = "auto"):
        self.mode = mode  # auto, local, remote
        self._client: Optional[httpx.AsyncClient] = None
        self._tools: List[Dict[str, Any]] = []
        self._base_url: Optional[str] = None
        self._request_id = 0

    async def start(self):
        """Start the MCP stdio server loop."""
        self._client = httpx.AsyncClient(timeout=60.0)
        self._base_url = await self._resolve_backend()
        self._tools = await self._discover_tools()

        logger.info(f"MCP bridge started: backend={self._base_url}, tools={len(self._tools)}")

        # `connect_read_pipe` on stdin raises under Windows' Proactor event loop,
        # which crashed this bridge on Windows. Use the shared thread-backed
        # reader (same one the live-proven ACP server uses) — identical on POSIX.
        from adk.stdio_compat import ThreadStdinReader

        reader = ThreadStdinReader(sys.stdin.buffer)

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8").strip())
                response = await self._handle_message(message)
                if response is not None:
                    self._write_response(response)
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error(f"MCP bridge error: {e}")
                self._write_response({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(e)},
                })

    async def _resolve_backend(self) -> str:
        """Detect whether local Genesis/Node is running or use remote."""
        if self.mode == "remote":
            return REMOTE_MCP_URL
        if self.mode == "local":
            return LOCAL_NODE_URL

        # Auto-detect: try local first
        for url in [LOCAL_NODE_URL, LOCAL_GENESIS_URL]:
            try:
                resp = await self._client.get(f"{url}/health", timeout=3.0)
                if resp.status_code == 200:
                    logger.info(f"Auto-detected local backend: {url}")
                    return url
            except Exception:
                continue

        logger.info("No local backend found, using remote MCP gateway")
        return REMOTE_MCP_URL

    async def _discover_tools(self) -> List[Dict[str, Any]]:
        """Discover available tools from the backend."""
        try:
            resp = await self._client.get(f"{self._base_url}/mcp/tools")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("tools", [])
        except Exception as e:
            logger.warning(f"Tool discovery failed: {e}")

        # Fallback: return core tool set
        return self._default_tools()

    def _default_tools(self) -> List[Dict[str, Any]]:
        """Core tool definitions when discovery fails."""
        return [
            {
                "name": "chat",
                "description": "Send a message to AitherOS and get a response",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "The message to send"},
                        "agent": {"type": "string", "description": "Agent to address (optional)"},
                        "effort": {"type": "integer", "description": "Effort level 1-10"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "search_code",
                "description": "Search the AitherOS codebase",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "file_pattern": {"type": "string", "description": "Glob pattern filter"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "ask_agent",
                "description": "Ask a specific AitherOS agent a question",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "description": "Agent name (demiurge, hydra, athena, etc.)"},
                        "question": {"type": "string", "description": "Question for the agent"},
                    },
                    "required": ["agent", "question"],
                },
            },
            {
                "name": "get_system_status",
                "description": "Get AitherOS system health and service status",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "run_command",
                "description": "Run a shell command via AitherOS",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute"},
                        "cwd": {"type": "string", "description": "Working directory"},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "forge_subagent",
                "description": "Dispatch a sub-agent for a complex task",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "Task description"},
                        "agent": {"type": "string", "description": "Agent identity to use"},
                        "effort": {"type": "integer", "description": "Effort level 1-10"},
                    },
                    "required": ["task"],
                },
            },
            {
                "name": "onboard_full",
                "description": (
                    "Complete end-to-end onboarding for a new user. "
                    "Creates account, gets API key, detects GPU, connects to AitherOS, "
                    "deploys model (optional), and generates MCP config. "
                    "This is THE skill to call when a human wants to get started with AitherOS."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string", "description": "User email for account creation"},
                        "password": {"type": "string", "description": "Account password"},
                        "deploy_model": {"type": "boolean", "description": "Auto-deploy a model (default: false)"},
                    },
                    "required": ["email", "password"],
                },
            },
            {
                "name": "onboard_status",
                "description": "Check what's configured and what's missing for AitherOS onboarding.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "onboard_setup_mcp",
                "description": (
                    "Generate MCP configuration for any agent client "
                    "(Claude Code, Cursor, Windsurf). Returns the JSON config block to add."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "client": {
                            "type": "string",
                            "description": "MCP client type",
                            "enum": ["claude-code", "cursor", "windsurf", "generic"],
                        },
                        "api_key": {"type": "string", "description": "API key for remote mode"},
                    },
                },
            },
        ]

    async def _handle_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle an incoming JSON-RPC message."""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "aitheros",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "notifications/initialized":
            return None  # No response needed

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": self._tools,
                },
            }

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self._call_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            }

        # Unknown method
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    async def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool call by proxying to the backend."""
        # Onboarding tools run locally — no backend needed
        if name.startswith("onboard_"):
            return await self._call_onboarding_tool(name, args)

        try:
            # Try MCP endpoint first
            resp = await self._client.post(
                f"{self._base_url}/mcp/tools/call",
                json={"name": name, "arguments": args},
                timeout=120.0,
            )
            if resp.status_code == 200:
                return resp.json()

            # Fallback: map to Genesis API endpoints
            return await self._fallback_call(name, args)
        except Exception as e:
            return {"error": str(e), "tool": name}

    async def _call_onboarding_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute onboarding tools locally (no backend required)."""
        try:
            # Import lazily to avoid circular deps
            from adk.shell.onboarding import run_onboarding

            if name == "onboard_full":
                result = await run_onboarding(
                    mode="auto",
                    email=args.get("email"),
                    password=args.get("password"),
                    skip_deploy=not args.get("deploy_model", False),
                    skip_mcp=False,
                )
                return result.to_dict()

            if name == "onboard_status":
                # Quick status check
                from adk.shell.auth import AuthStore
                profile = AuthStore.get_active_profile()
                return {
                    "authenticated": profile is not None,
                    "email": profile.get("email") if profile else None,
                    "api_key_set": bool(profile.get("api_key")) if profile else False,
                }

            if name == "onboard_setup_mcp":
                client_type = args.get("client", "generic")
                api_key = args.get("api_key", "")
                local_cfg = {"command": "aither", "args": ["mcp", "serve"]}
                remote_cfg = {"url": "https://mcp.aitherium.com/mcp", "transport": "streamable-http"}
                if api_key:
                    remote_cfg["headers"] = {"Authorization": f"Bearer {api_key}"}

                configs = {
                    "claude-code": {"mcpServers": {"aitheros": local_cfg}},
                    "cursor": {"mcpServers": {"aitheros": local_cfg}},
                    "windsurf": {"mcpServers": {"aitheros": local_cfg}},
                    "generic": {"local": local_cfg, "remote": remote_cfg},
                }
                return {
                    "client": client_type,
                    "config": configs.get(client_type, configs["generic"]),
                    "install_first": "pip install aithershell",
                }

            return {"error": f"Unknown onboarding tool: {name}"}
        except Exception as e:
            return {"error": str(e), "tool": name}

    async def _fallback_call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Map tool names to Genesis REST endpoints as fallback."""
        endpoint_map = {
            "chat": ("/chat/message", "POST"),
            "search_code": ("/codegraph/search", "POST"),
            "ask_agent": ("/forge/dispatch/sync", "POST"),
            "get_system_status": ("/health/deep", "GET"),
            "run_command": ("/node/exec", "POST"),
            "forge_subagent": ("/forge/dispatch/sync", "POST"),
        }

        if name not in endpoint_map:
            return {"error": f"Unknown tool: {name}"}

        path, method = endpoint_map[name]
        url = f"{self._base_url}{path}"

        try:
            if method == "GET":
                resp = await self._client.get(url, timeout=30.0)
            else:
                resp = await self._client.post(url, json=args, timeout=120.0)
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    def _write_response(self, response: Dict[str, Any]):
        """Write a JSON-RPC response to stdout."""
        line = json.dumps(response) + "\n"
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()


async def serve_mcp(mode: str = "auto"):
    """Entry point for `aither mcp serve`."""
    bridge = MCPStdioBridge(mode=mode)
    await bridge.start()
