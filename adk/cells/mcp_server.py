"""Cells as an MCP server: every tool is generated from a contract, none hand-written.

The tool list is the caller's slice of the contracts (``surfaces.mcp_tools``), so an
agent is never shown a tool it could not call. Each call goes through ``Cells``, so the
same server fronts cells hosted in this process or on another node, and the node
re-checks scope on every call. Requires the ``node`` extra (``mcp<2``).
"""

from __future__ import annotations

import json
from typing import Any

from .caller import Caller
from .contract import OpSpec
from .registry import Cells
from .surfaces import mcp_tools


def tool_index(cells: Cells) -> dict[str, OpSpec]:
    """``{"memory_recall": OpSpec}`` for every op on every placed cell."""
    return {f"{op.cell}_{op.name}": op
            for spec in cells.specs.values() for op in spec.ops.values()}


async def call_tool(cells: Cells, caller: Caller, name: str,
                    arguments: dict[str, Any] | None) -> str:
    """Run one tool call and return its JSON result. Raises on an unknown tool, a
    denied scope or invalid arguments, which the MCP layer reports as a tool error."""
    op = tool_index(cells).get(name)
    if op is None or not op.mcp:
        raise LookupError(f"unknown tool {name!r}")
    bound = getattr(cells.as_caller(caller), op.cell)
    result = await getattr(bound, op.name)(**(arguments or {}))
    return json.dumps(op.result_adapter().dump_python(result, mode="json"))


def build_mcp_server(cells: Cells, caller: Caller, name: str = "aither-cells") -> Any:
    from mcp.server.lowlevel import Server
    from mcp.types import TextContent, Tool

    server = Server(name)
    specs = list(cells.specs.values())

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=t["name"], description=t["description"],
                     inputSchema=t["inputSchema"]) for t in mcp_tools(specs, caller)]

    @server.call_tool()
    async def handle_call(tool: str, arguments: dict[str, Any]) -> list[TextContent]:
        return [TextContent(type="text", text=await call_tool(cells, caller, tool, arguments))]

    return server


def caller_from_whoami(doc: dict[str, Any]) -> Caller:
    from .contract import Scope

    return Caller(subject=str(doc.get("subject") or "unknown"),
                  scopes=frozenset(Scope(s) for s in doc.get("scopes") or []),
                  workspace=doc.get("workspace"))


async def serve_stdio(cells: Cells, caller: Caller) -> None:
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(cells, caller)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
