"""ACP cross-implementation conformance: our wire vs the reference SDK.

The reference SDK is the ``agent-client-protocol`` PyPI package (import name
``acp``) — the same SDK Zed ships and kimi-cli uses. It is pinned in the
``dev`` extra purely as a CONFORMANCE ORACLE: these tests prove the adk
hand-rolled client/server speak the same wire as the reference implementation,
so a drift in our framing cannot hide. They skip cleanly when the SDK is not
installed (``pytest.importorskip``), and the SDK is deliberately NOT a runtime
dependency.

Two directions, each over a REAL subprocess (like ``test_agent_pack_acp_drive``
does — no in-process mocks, so the transport itself is exercised):

  * reference client  -> our ``adk.acp_server.serve_stdio``
  * our ``adk.acp.ACPClient`` -> reference ``AgentSideConnection`` server

Note: SDK 0.12.0 speaks an older wire revision (snake_case fields, and
``tool_call_start``/``tool_call_progress`` discriminators where v2 docs use
``tool_call_update``/``tool_call_content_chunk``). These tests assert the
interop subset the SDK understands (initialize / session/new / prompt /
agent message); the v2 state machine is asserted by ``test_acp_server_v2.py``
with our own in-process client.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

pytest.importorskip("acp")

from adk.acp import ACPClient  # noqa: E402

# ── A minimal agent our server can drive: anything exposing async run(prompt)
# ── with .output / .tool_calls / .finish_reason (the adk agent contract).
OUR_SERVER = textwrap.dedent(
    """
    import asyncio, sys
    from types import SimpleNamespace
    from adk.acp_server import serve_stdio

    class Stub:
        async def run(self, prompt):
            return SimpleNamespace(output=f"hello:{prompt}", tool_calls=[])

    asyncio.run(serve_stdio(Stub(), name="conformance", version="1.0.0"))
    """
).strip()

def _spawn(code: str):
    return asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _collect_agent_text(updates) -> str:
    """Pull text out of AgentMessageChunk/agent_message_chunk updates.

    The reference SDK hands session_update a PARSED model (v2 shapes
    accepted); our own client hands raw dicts. Accept both.
    """
    parts = []
    for upd in updates:
        if hasattr(upd, "content") and not isinstance(upd.content, str):
            c = upd.content
            if getattr(c, "type", None) == "text":
                parts.append(getattr(c, "content", getattr(c, "text", "")) or "")
            elif isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("content", c.get("text", "")))
            continue
        if isinstance(upd, dict):
            if upd.get("sessionUpdate") == "agent_message_chunk":
                c = upd.get("content") or {}
                if isinstance(c, dict):
                    parts.append(c.get("text", c.get("content", "")))
    return "".join(parts)


def test_reference_client_drives_our_server():
    """Direction A: the reference SDK client drives adk's serve_stdio."""
    from acp.client import ClientSideConnection
    from acp.interfaces import Client

    class RefClient(Client):
        def __init__(self):
            self.updates: list = []

        def on_connect(self, conn):
            # The SDK calls on_connect synchronously (see connection.py) — an
            # async def here would be a discarded coroutine (RuntimeWarning).
            pass

        async def session_update(self, session_id, update, **kwargs):
            self.updates.append(update)

        async def request_permission(self, session_id, tool_call, options, **kwargs):
            # Auto-approve so a gated tool call can never stall the test.
            first = options[0] if options else None
            option_id = getattr(first, "option_id", None) or getattr(first, "optionId", None)
            return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def scenario():
        proc = await _spawn(OUR_SERVER)
        holder: dict = {}
        try:
            conn = ClientSideConnection(
                lambda agent: holder.setdefault("client", RefClient()),
                # For a CLIENT connection: input_stream is what we WRITE to the
                # agent (its stdin), output_stream is what we READ from it (stdout).
                input_stream=proc.stdin,
                output_stream=proc.stdout,
            )
            async with conn:
                init = await conn.initialize(2)
                assert init.protocol_version == 2
                assert init.agent_info.name == "conformance"
                sid = (await conn.new_session(cwd=".")).session_id
                assert sid
                await conn.prompt(sid, [{"type": "text", "text": "hi"}])
                # agent_message_chunk is emitted before the prompt response, but
                # delivery through the read loop is async — drain until it lands.
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    if "hello:" in _collect_agent_text(holder["client"].updates):
                        break
                    await asyncio.sleep(0.05)
                got = _collect_agent_text(holder["client"].updates)
            return init, sid, got
        finally:
            proc.terminate()
            try:
                await proc.wait()
            except (ProcessLookupError, asyncio.CancelledError):
                pass

    init, sid, got = asyncio.run(scenario())
    assert sid
    assert "hello:hi" in got


def test_our_client_drives_reference_server():
    """Direction B: our ACPClient drives the reference SDK's AgentSideConnection.

    The reference server runs IN-PROCESS over a socket pair: the SDK's
    AgentSideConnection (like ClientSideConnection) requires real asyncio
    StreamReader/StreamWriter, and on Windows Proactor the subprocess pipes are
    not those types (the same trap adk.stdio_compat exists for). A socket pair
    yields real streams on every platform, so this runs on Windows AND CI.
    """
    import socket

    from acp.agent import AgentSideConnection
    from acp.interfaces import Agent
    from acp.schema import (
        AgentCapabilities,
        Implementation,
        InitializeResponse,
        NewSessionResponse,
        PromptResponse,
    )

    class RefAgent(Agent):
        def __init__(self):
            self.conn = None

        def on_connect(self, conn):
            self.conn = conn

        async def initialize(self, protocol_version, client_capabilities=None,
                             client_info=None, **kwargs):
            return InitializeResponse(
                protocol_version=2,
                agent_capabilities=AgentCapabilities(),
                agent_info=Implementation(name="ref-agent", version="1.0.0"),
                auth_methods=[],
            )

        async def new_session(self, cwd, additional_directories=None,
                              mcp_servers=None, **kwargs):
            return NewSessionResponse(session_id="ref-1")

        async def prompt(self, session_id, prompt, **kwargs):
            # The SDK parses our prompt blocks into pydantic models before
            # invoking the handler — handle both dicts and TextContentBlock.
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
                for b in prompt
            )
            await self.conn.session_update(
                session_id,
                {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text", "text": f"echo:{text}"}},
            )
            return PromptResponse(stop_reason="end_turn")

    async def scenario():
        a, b = socket.socketpair()
        # open_connection returns (reader, writer) — do not swap the pair.
        server_reader, server_writer = await asyncio.open_connection(sock=a)
        client_reader, client_writer = await asyncio.open_connection(sock=b)
        # Both SDK connections take input_stream=StreamWriter / output_stream=
        # StreamReader: the writer that leads to the peer and the reader that
        # comes from it.
        srv = AgentSideConnection(
            RefAgent(), input_stream=server_writer, output_stream=server_reader
        )
        async with srv:
            client = ACPClient(reader=client_reader, writer=client_writer)
            await client.connect()
            try:
                caps = await client.initialize()
                sid = await client.create_session(cwd=".")
                result = await client.prompt(sid, "hi", drain_timeout=1.0)
                return caps, sid, result
            finally:
                await client.disconnect()

    caps, sid, result = asyncio.run(scenario())
    assert sid == "ref-1"
    assert result.text == "echo:hi"
    assert result.stop_reason == "end_turn"
