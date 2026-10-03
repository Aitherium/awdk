"""Cells over MCP: generated tools only, scoped to the caller, through a real MCP client
session in memory and a real stdio subprocess."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version

import pytest

from adk.cells import Caller, Cells, Scope, contract, op
from adk.cells.mcp_server import build_mcp_server, call_tool, caller_from_whoami
from tests.test_cells_node import DANA_TOKEN, running_node

mcp = pytest.importorskip("mcp")
pytestmark = pytest.mark.skipif(
    int(version("mcp").split(".")[0]) >= 2, reason="awdk pins the node extra to mcp<2"
)

ME = Caller(subject="agent", scopes=frozenset({Scope.workspace}), workspace="acme:me:p")


@contract("notes", version=1)
class Notes:
    @op(scope=Scope.workspace)
    async def add(self, text: str) -> int:
        """Add a note; returns how many notes exist."""

    @op(scope=Scope.workspace)
    async def search(self, query: str, limit: int = 5) -> list[str]:
        """Find notes containing the query."""

    @op(scope=Scope.operator)
    async def purge(self) -> int:
        """Delete every note."""


class MemNotes(Notes):
    def __init__(self):
        self.items: list[str] = []

    async def add(self, text: str) -> int:
        self.items.append(text)
        return len(self.items)

    async def search(self, query: str, limit: int = 5) -> list[str]:
        return [t for t in self.items if query in t][:limit]

    async def purge(self) -> int:
        n = len(self.items)
        self.items.clear()
        return n


async def test_mcp_session_lists_only_callable_tools_and_runs_them():
    from mcp.shared.memory import create_connected_server_and_client_session

    notes = MemNotes()
    server = build_mcp_server(Cells().host(notes), ME)
    async with create_connected_server_and_client_session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"notes_add", "notes_search"}  # purge is operator-only
        assert tools["notes_search"].inputSchema["required"] == ["query"]

        added = await client.call_tool("notes_add", {"text": "tls everywhere"})
        assert not added.isError and json.loads(added.content[0].text) == 1
        found = await client.call_tool("notes_search", {"query": "tls"})
        assert json.loads(found.content[0].text) == ["tls everywhere"]

        denied = await client.call_tool("notes_purge", {})
        assert denied.isError and notes.items == ["tls everywhere"]
        bad = await client.call_tool("notes_search", {"query": "x", "workspace": "rival"})
        assert bad.isError


async def test_call_tool_rejects_unknown_names():
    with pytest.raises(LookupError):
        await call_tool(Cells().host(MemNotes()), ME, "notes_drop", {})


def test_whoami_maps_to_a_caller():
    caller = caller_from_whoami({"subject": "dana", "scopes": ["workspace"],
                                 "workspace": "acme:dana:p"})
    assert caller.scopes == frozenset({Scope.workspace}) and caller.workspace == "acme:dana:p"


async def test_stdio_server_end_to_end_with_the_memory_cell(tmp_path):
    pytest.importorskip("awm")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "adk.cells", "mcp", "--local", f"memory={tmp_path / 'm.db'}",
              "--workspace", "acme:me:proj"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == {"memory_remember", "memory_recall", "memory_forget",
                             "memory_history"}
            await session.call_tool("memory_remember", {"key": "db", "value": "pg17"})
            hits = json.loads((await session.call_tool(
                "memory_recall", {"query": "db"})).content[0].text)
            assert [(h["scope"], h["value"]) for h in hits] == [("acme:me:proj", "pg17")]


async def test_stdio_remote_mode_against_a_live_tls_node(tmp_path):
    with running_node(tmp_path) as (url, ca):
        await _remote_round_trip(url, ca)


async def _remote_round_trip(url: str, ca: str) -> None:
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "adk.cells", "mcp", "--remote", f"memory={url}", "--ca", ca],
        env={**os.environ, "AITHER_CELLS_TOKEN": DANA_TOKEN},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert "memory_recall" in names
            await session.call_tool("memory_remember", {"key": "k", "value": "remote"})
            hits = json.loads((await session.call_tool(
                "memory_recall", {"query": "k"})).content[0].text)
            # The node decides the workspace from the token: dana's.
            assert [(h["scope"], h["value"]) for h in hits] == [
                ("acme:dana:proj", "remote")]
