"""End-to-end managed-agent rail: manifest -> Supervisor.spawn -> ACP driver -> prompt.

This closes the residual where Supervisor only RECORDED the declared protocol.
It also guards two structural bugs found while wiring it:
  * spawn() did not pipe stdin, so a stdio (ACP/MCP) agent was unreachable;
  * a non-drivable protocol must fail LOUD, not hand back an inert handle.

The "agent" here is a real subprocess speaking ACP JSON-RPC on stdio, so this
exercises the actual transport (not an in-process mock).
"""
import asyncio
import sys
import textwrap

import pytest

from adk.agent_pack import AgentPackManifest, Supervisor

# A minimal real ACP server: reads JSON-RPC lines on stdin, answers on stdout.
MOCK_ACP_SERVER = textwrap.dedent(
    """
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    while True:
        # readline(), NOT "for line in sys.stdin": iterating stdin block-buffers
        # on a pipe and would never yield a line until EOF.
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid, method = req.get("id"), req.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocol_version": 1,
                "agent_info": {"name": "mock", "version": "0.1"},
                "agent_capabilities": {"load_session": True}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"session_id": "s-1"}})
        elif method == "session/prompt":
            for note in (
                {"session_update": "agent_message_chunk",
                 "content": {"type": "text", "text": "driven!"}},
                {"session_update": "tool_call_start", "tool_call_id": "t1",
                 "tool_name": "read_file", "function_args": {"path": "/x"}},
                {"session_update": "tool_call_complete", "tool_call_id": "t1",
                 "result": "contents"},
                {"session_update": "usage_update", "input_tokens": 5, "output_tokens": 2},
            ):
                send({"jsonrpc": "2.0", "method": "session/update", "params": note})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stop_reason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    """
).strip()


def _manifest(protocol: str = "acp", cmd: str | None = None) -> AgentPackManifest:
    return AgentPackManifest(
        id=f"mock-{protocol}",
        name="Mock Agent",
        framework="hermes",
        protocol=protocol,
        runtime={"type": "python", "cmd": cmd or f'python -c "{MOCK_ACP_SERVER}"'},
        entrypoint="mock",
    )


def test_spawn_pipes_stdin_for_stdio_protocols():
    """Without a piped stdin an ACP agent can never receive a request."""
    async def scenario():
        sup = Supervisor()
        h = await sup.spawn(_manifest(cmd=f'{sys.executable} -c "pass"'))
        try:
            return h.process.stdin is not None
        finally:
            await sup.terminate_all()

    assert asyncio.run(scenario()) is True


def test_manifest_to_prompt_end_to_end():
    """The whole managed rail: manifest -> spawn -> acp driver -> prompt."""
    async def scenario():
        sup = Supervisor()
        # Pass the server source as a real argv element (no shell quoting games).
        m = _manifest()
        m.runtime.cmd = sys.executable
        m.runtime.args = ["-u", "-c", MOCK_ACP_SERVER]
        handle = await sup.spawn(m)
        try:
            assert handle.is_running()
            client = await handle.driver()
            caps = await client.initialize()
            session_id = await client.create_session(cwd=".", model="mock-model")
            result = await client.prompt(session_id, "go", drain_timeout=2.0)
            return caps, session_id, result
        finally:
            await sup.terminate_all()

    caps, session_id, result = asyncio.run(scenario())
    assert caps.agent_name == "mock"
    assert session_id == "s-1"
    assert result.text == "driven!"
    assert [c.tool_call_id for c in result.tool_calls] == ["t1"]
    assert result.tool_calls[0].result == "contents"
    assert result.usage.input_tokens == 5 and result.usage.output_tokens == 2


def test_driver_is_cached():
    async def scenario():
        sup = Supervisor()
        m = _manifest()
        m.runtime.cmd = sys.executable
        m.runtime.args = ["-u", "-c", MOCK_ACP_SERVER]
        h = await sup.spawn(m)
        try:
            return (await h.driver()) is (await h.driver())
        finally:
            await sup.terminate_all()

    assert asyncio.run(scenario()) is True


def test_non_drivable_protocol_fails_loud():
    """A protocol with no driver must raise, never return an inert handle."""
    async def scenario():
        sup = Supervisor()
        m = _manifest(protocol="http")
        m.runtime.cmd = sys.executable
        m.runtime.args = ["-c", "import time; time.sleep(5)"]
        h = await sup.spawn(m)
        try:
            with pytest.raises(NotImplementedError):
                await h.driver()
        finally:
            await sup.terminate_all()

    asyncio.run(scenario())


def test_driver_on_dead_process_raises():
    async def scenario():
        sup = Supervisor()
        m = _manifest()
        m.runtime.cmd = sys.executable
        m.runtime.args = ["-c", "pass"]  # exits immediately
        h = await sup.spawn(m)
        await h.process.wait()
        try:
            with pytest.raises(RuntimeError):
                await h.driver()
        finally:
            await sup.terminate_all()

    asyncio.run(scenario())
