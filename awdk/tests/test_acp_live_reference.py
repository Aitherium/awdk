"""LIVE handshake against a REAL ACP agent built on Zed's reference library.

This is the check that a hand-written mock could never make: the server is
`agent-client-protocol` (the same package hermes pins), so the bytes on the wire
are the authoritative ACP format — camelCase (`protocolVersion`, `agentInfo`,
`sessionId`, `toolCallId`, `stopReason`), `session/new`, and `session/update`
notifications whose payload is nested under `update` with a `sessionUpdate`
discriminator (`agent_message_chunk`, `tool_call`, `tool_call_update`).

Running it caught real interop bugs that the mock-based suite passed:
  * client parsed snake_case -> `create_session()` returned "" against real agents;
  * client sent `session/create` (spec is `session/new`) and `content=` on prompt
    (spec is `prompt=`);
  * client expected `tool_call_start`/`tool_call_complete` (spec: `tool_call` /
    `tool_call_update` with a `status`), and `usage` rides the PromptResponse.

Skips (not fails) when the reference venv is absent, so CI without it stays green.
"""
import asyncio
import os
from pathlib import Path

import pytest

from adk.acp import ACPClient

# No default: the previous fallback was one developer's personal scratchpad path,
# shipped in the public package — it leaked a username and pointed somewhere no
# other machine can have. Set ADK_ACP_REF_DIR to a local reference checkout to run
# these; unset, they skip exactly as they did before.
_SCRATCH = Path(os.environ.get("ADK_ACP_REF_DIR", ""))
_VENV_PY = _SCRATCH / "acpvenv" / "Scripts" / "python.exe"
_AGENT = _SCRATCH / "real_acp_agent.py"

pytestmark = pytest.mark.skipif(
    not (_VENV_PY.exists() and _AGENT.exists()),
    reason="reference agent-client-protocol venv not present",
)


def _drive():
    async def scenario():
        client = ACPClient(command=str(_VENV_PY), args=["-u", str(_AGENT)])
        await client.connect()
        try:
            caps = await client.initialize()
            session_id = await client.create_session(cwd=".", model="any")
            result = await client.prompt(session_id, "hello", drain_timeout=3.0)
            return caps, session_id, result
        finally:
            await client.disconnect()

    return asyncio.run(scenario())


def test_live_reference_acp_handshake():
    caps, session_id, result = _drive()

    # initialize -> camelCase agentInfo/protocolVersion/agentCapabilities
    assert caps.agent_name == "proof-agent", "agentInfo.name not parsed from real wire"
    assert caps.agent_version == "9.9.9"
    assert caps.protocol_version == 1
    assert caps.load_session is True, "agentCapabilities.loadSession not parsed"

    # session/new -> camelCase sessionId (this returned "" before the fix)
    assert session_id == "real-session-42", "sessionId not parsed from real wire"

    # streamed agent_message_chunk (nested under params.update)
    assert result.text == "live-ACP-ok"

    # real tool_call + tool_call_update(status=completed), content-block result
    assert [c.tool_call_id for c in result.tool_calls] == ["tc-1"]
    call = result.tool_calls[0]
    assert call.tool_name == "read_file", "ACP `title` should map to tool_name"
    assert call.result == "FILE-BODY", "content-block tool result not extracted"

    # usage rides the PromptResponse in real ACP, not a usage_update notification
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 10
    assert result.stop_reason == "end_turn"


def test_live_reference_no_outstanding_tool_calls():
    """A tool_call_update with status=completed must settle the drain (no warning)."""
    _, _, result = _drive()
    assert all(c.result is not None for c in result.tool_calls), (
        "every tool call should have resolved via tool_call_update(status=completed)"
    )
