"""MCP client bridge — load remote MCP server tools as ADK Tools.

LAYER: Minimal generic JSON-RPC adapter for any MCP server.
ROLE: Bridges ANY MCP server's tools into ADK's Tool/Capability system.
      NOT platform-specific — works with awnode, external MCP servers, etc.
      No authentication/billing/caching — pure protocol translation.

See also: adk.mcp — enterprise client for AitherOS MCP gateway with auth/billing.

Lets any agent call tools hosted by an MCP server (awnode, an external
MCP server, or anything else that speaks the Model Context Protocol over
HTTP JSON-RPC).

Usage::

    tools = await mcp_tools("http://localhost:8080")
    agent = Agent(name="x", model=auto_backend(), tools=tools)

The bridge issues two JSON-RPC calls at construction:

  - ``tools/list`` — discover available tools
  - (later, per invocation) ``tools/call`` — execute

Each remote tool becomes a :class:`Tool` instance whose ``schema()`` mirrors
the MCP advertisement and whose ``call()`` proxies via HTTP.

Requires the optional ``httpx`` extra (``pip install 'awdk[full]'``).
"""

from __future__ import annotations

import json
from typing import Any

from adk.core.capability import Capability, current_context
from adk.core.tool import Tool, ToolResult


class MCPError(RuntimeError):
    """Raised when an MCP call fails."""


class MCPClient:
    """Tiny JSON-RPC client for MCP over HTTP."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dep
            raise MCPError(
                "MCP bridge requires httpx. Install with: pip install 'awdk[full]'"
            ) from e
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/rpc",
                json=payload,
                headers={"content-type": "application/json", **self.headers},
            )
            r.raise_for_status()
            data = r.json()
        if "error" in data and data["error"]:
            raise MCPError(
                f"MCP error from {self.base_url} on {method}: {data['error']}"
            )
        return data.get("result")

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.call("tools/list")
        if isinstance(result, dict):
            return result.get("tools") or []
        if isinstance(result, list):
            return result
        return []

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return await self.call(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )


class MCPTool(Tool):
    """An ADK :class:`Tool` backed by a remote MCP tool."""

    requires = (Capability.AGENT_CALL,)

    def __init__(
        self,
        *,
        client: MCPClient,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name=name, description=description)
        self._client = client
        self._input_schema = input_schema or {"type": "object", "properties": {}}

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._input_schema,
        }

    async def __call__(self, **kwargs: Any) -> ToolResult:
        # Capability check happens here (base class would call self.call which
        # we override below, so do the check manually).
        ctx = current_context()
        for cap in self.requires:
            ctx.check(cap)
        try:
            result = await self._client.invoke(self.name, kwargs)
        except MCPError as e:
            return ToolResult.failure(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.failure(f"{type(e).__name__}: {e}")
        return ToolResult.success(_normalize_mcp_result(result))

    async def call(self, **kwargs: Any) -> Any:  # pragma: no cover - unused
        # Required by Tool base; __call__ above shortcuts it.
        raise NotImplementedError


def _normalize_mcp_result(value: Any) -> Any:
    """Coerce MCP responses into a JSON-serializable payload."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return str(value)


async def mcp_tools(
    server_url: str,
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> list[Tool]:
    """Discover and wrap every tool advertised by an MCP server.

    Returns an empty list if the server reports no tools. Raises
    :class:`MCPError` on transport/protocol failure.
    """
    client = MCPClient(server_url, timeout=timeout, headers=headers)
    advertised = await client.list_tools()
    out: list[Tool] = []
    for spec in advertised:
        name = spec.get("name")
        if not name:
            continue
        out.append(
            MCPTool(
                client=client,
                name=name,
                description=spec.get("description", ""),
                input_schema=spec.get("inputSchema") or spec.get("input_schema"),
            )
        )
    return out
