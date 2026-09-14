"""ACP LLM provider tests: drive an external ACP agent subprocess as a backend.

The "external agent" here is a REAL subprocess speaking ACP JSON-RPC on stdio
(the same pattern as test_agent_pack_acp_drive.py), so this exercises the
actual transport: spawn -> initialize -> session/new -> session/prompt with
multi-block prompts -> session/delete. The mock records every prompt block it
receives to a JSONL file, so the delta/skip logic of ACPProvider is asserted on
the exact wire.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from adk.llm.acp_provider import ACPProvider
from adk.llm.base import Message

# A real minimal ACP server that echoes each received prompt text block and
# records every prompt it receives to the file named in argv[1].
MOCK_ACP_AGENT = textwrap.dedent(
    """
    import json, sys

    OUT = sys.argv[1]

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    with open(OUT, "a") as f:
        while True:
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
                    "protocolVersion": 2,
                    "capabilities": {"session": {"list": {}}},
                    "info": {"name": "mock-acp", "version": "1"}}})
            elif method == "session/new":
                send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s-1"}})
            elif method == "session/prompt":
                blocks = (req.get("params") or {}).get("prompt") or []
                f.write(json.dumps({"blocks": blocks}) + "\\n")
                f.flush()
                for b in blocks:
                    if b.get("type") == "text":
                        send({"jsonrpc": "2.0", "method": "session/update",
                              "params": {"sessionId": "s-1", "update": {
                                  "sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": "echo:" + b.get("text", "")}}}})
                # A v2 turn is only complete at a terminal state_update: idle.
                # The client's drain waits for it (text arrives as chunks BEFORE
                # idle); omitting it makes every prompt hang the full drain_timeout.
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": "s-1", "update": {
                          "sessionUpdate": "state_update", "state": "idle",
                          "stopReason": "end_turn"}}})
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 3, "outputTokens": 5}}})
            elif method == "session/delete":
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
    """
).strip()


def _spawn_provider(tmp_path, **kwargs) -> ACPProvider:
    """Build an ACPProvider pointed at the mock agent subprocess."""
    record = tmp_path / "prompts.jsonl"
    provider = ACPProvider(
        command=sys.executable,
        args=["-u", "-c", MOCK_ACP_AGENT.replace("@@OUT@@", "argv[1]"), str(record)],
        **kwargs,
    )
    return provider


def _msgs(*turns) -> list[Message]:
    """Build a message list from (role, content) or (role, content, name) tuples."""
    out = []
    for t in turns:
        role, content = t[0], t[1]
        name = t[2] if len(t) > 2 else None
        out.append(Message(role=role, content=content, name=name))
    return out


def _records(tmp_path) -> list[dict]:
    path = tmp_path / "prompts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_missing_command_fails_loud():
    """A provider without a command must raise at construction, never be inert."""
    with pytest.raises(ValueError, match="requires a command"):
        ACPProvider(command="")


def test_chat_first_turn_sends_system_and_user(tmp_path):
    async def scenario():
        provider = _spawn_provider(tmp_path)
        try:
            resp = await provider.chat(_msgs(("system", "You are helpful."), ("user", "hi")))
            return resp, _records(tmp_path)
        finally:
            await provider.disconnect()

    resp, records = asyncio.run(scenario())
    # The aggregated reply concatenates the agent's echo of each block.
    assert "You are helpful." in resp.content
    assert "hi" in resp.content
    assert resp.finish_reason == "end_turn"
    assert resp.prompt_tokens == 3 and resp.completion_tokens == 5
    # The mock saw exactly the system block then the user block.
    assert len(records) == 1
    blocks = records[0]["blocks"]
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[0].get("metadata", {}).get("role") == "system"
    assert blocks[1]["text"] == "hi"


def test_chat_second_turn_skips_assistant_sends_tool_and_user(tmp_path):
    """The delta rule: the external agent's own assistant turn is NOT re-sent."""
    async def scenario():
        provider = _spawn_provider(tmp_path)
        try:
            await provider.chat(_msgs(("user", "first")))
            resp = await provider.chat(_msgs(
                ("user", "first"),
                ("assistant", "I will compute."),
                ("tool", "42", "calc"),
                ("user", "and now?"),
            ))
            return resp, _records(tmp_path)
        finally:
            await provider.disconnect()

    resp, records = asyncio.run(scenario())
    assert "and now?" in resp.content
    assert "Tool result: calc" in resp.content
    assert "I will compute." not in resp.content  # assistant turn skipped
    assert len(records) == 2
    blocks = records[1]["blocks"]
    texts = [b["text"] for b in blocks]
    assert texts == ["[Tool result: calc]: 42", "and now?"]


def test_conversation_reset_starts_fresh_session(tmp_path):
    """A message count going backwards deletes the old session and reopens."""
    async def scenario():
        provider = _spawn_provider(tmp_path)
        try:
            await provider.chat(_msgs(("user", "one"), ("user", "two")))
            resp = await provider.chat(_msgs(("user", "fresh")))
            return resp, _records(tmp_path)
        finally:
            await provider.disconnect()

    resp, records = asyncio.run(scenario())
    # Two prompts, and the second (smaller list) re-delivers the fresh message.
    assert len(records) == 2
    assert records[1]["blocks"] == [{"type": "text", "text": "fresh"}]


def test_chat_stream_yields_live_chunks(tmp_path):
    async def scenario():
        provider = _spawn_provider(tmp_path)
        try:
            chunks = []
            async for chunk in provider.chat_stream(_msgs(("user", "a"), ("user", "b"))):
                chunks.append(chunk)
            return chunks
        finally:
            await provider.disconnect()

    chunks = asyncio.run(scenario())
    assert [c.content for c in chunks if not c.done] == ["echo:a", "echo:b"]
    assert chunks[-1].done is True


def test_health_check_and_list_models(tmp_path):
    async def scenario():
        provider = _spawn_provider(tmp_path)
        try:
            ok = await provider.health_check()
            models = await provider.list_models()
            return ok, models
        finally:
            await provider.disconnect()

    ok, models = asyncio.run(scenario())
    assert ok is True
    assert models == ["acp"]
