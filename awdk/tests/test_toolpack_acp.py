"""ACP tool pack tests: registration, manifests, and a live external-agent turn.

The external agent is a REAL subprocess speaking ACP JSON-RPC on stdio (same
mock as test_acp_cli.py), so acp_prompt exercises the actual transport through
the toolpack's client cache.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from adk import toolpacks
from adk.toolpacks.acp import (
    acp_close,
    acp_connect,
    acp_list_agents,
    acp_prompt,
    register,
)

_MOCK_AGENT = textwrap.dedent(
    """
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

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
                "info": {"name": "mock-pack", "version": "1"}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s-1"}})
        elif method == "session/prompt":
            blocks = (req.get("params") or {}).get("prompt") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "s-1", "update": {
                      "sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": "answer:" + text}}}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        elif method == "session/close":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    """
).strip()


@pytest.fixture
def mock_agent(tmp_path) -> dict:
    script = tmp_path / "mock_agent.py"
    script.write_text(_MOCK_AGENT, encoding="utf-8")
    return {"command": sys.executable, "args": f"-u {script}"}


def _run(coro):
    return asyncio.run(coro)


def test_register_exposes_acp_tools():
    """register() adds the acp_* tools to a ToolRegistry."""
    from adk.tools import ToolRegistry

    registry = ToolRegistry()
    n = register(registry)
    names = {t.name for t in registry.list_tools()}
    assert n == 4
    assert {"acp_list_agents", "acp_connect", "acp_prompt", "acp_close"} <= names


def test_list_agents_returns_bundled_manifests():
    out = acp_list_agents()
    ids = {a["id"] for a in out["agents"]}
    assert "claude-agent-acp" in ids
    assert "codex-acp" in ids
    assert "gemini-cli" in ids
    assert "awdk" in ids


def test_bundled_manifests_are_valid_agent_packs():
    """Every agents/*.yaml parses as a Supervisor-drivable AgentPackManifest."""
    from adk.agent_pack import load_agent_pack
    from adk.toolpacks.acp import _AGENTS_DIR

    for path in sorted(_AGENTS_DIR.glob("*.yaml")):
        m = load_agent_pack(path)
        assert m.protocol == "acp"
        assert m.runtime.cmd, f"{path.name}: runtime.cmd is required"


def test_ui_panel_is_mounted_by_the_pack_loader():
    """The toolpack's ui/ panel resolves through the loader's ui_assets_dir —
    the same generic path the adk console uses to mount every toolpack UI."""
    from adk.tool_pack_loader import get_tool_pack_loader

    loader = get_tool_pack_loader()
    manifest = loader.discover().get("acp")
    assert manifest is not None
    assets = manifest.ui_assets_dir
    assert assets is not None
    assert assets.is_dir()
    assert (assets / "index.html").is_file()


def test_connect_then_prompt_then_close(mock_agent):
    """A full lifecycle against a real subprocess through the tool cache.

    Runs on ONE event loop — the same way the agent runtime executes tools
    (``await registry.execute(...)`` inside the agent's single loop), which is
    what makes the cached session reusable across tool calls.
    """
    cmd, args = mock_agent["command"], mock_agent["args"]

    async def lifecycle():
        connect = await acp_connect(command=cmd, args=args)
        prompt = await acp_prompt(command=cmd, args=args, message="hi", session_id="s-1")
        closed = await acp_close(command=cmd, args=args, session_id="s-1")
        return connect, prompt, closed

    connect, prompt, closed = asyncio.run(lifecycle())
    assert connect["ok"] is True
    assert connect["session_id"] == "s-1"
    assert connect["agent"] == "mock-pack"
    assert prompt["reply"] == "answer:hi"
    assert prompt["session_id"] == "s-1"
    assert closed["ok"] is True


def test_cross_loop_reuse_reconnects_without_hanging(mock_agent):
    """A client cached on one loop must not be reused on another (it would
    hang — the old loop's read task is dead). It reconnects fresh instead."""
    cmd, args = mock_agent["command"], mock_agent["args"]

    async def first_loop():
        connect = await acp_connect(command=cmd, args=args)
        return connect

    connect = asyncio.run(first_loop())
    assert connect["session_id"] == "s-1"

    # A SECOND asyncio.run is a different loop: the cached client is dropped
    # and a fresh agent is spawned. It must NOT hang.
    async def second_loop():
        prompt = await acp_prompt(command=cmd, args=args, message="hi", session_id="s-1")
        await acp_close(command=cmd, args=args)
        return prompt

    prompt = asyncio.run(second_loop())
    assert prompt["reply"] == "answer:hi"


def test_prompt_without_message_fails_loud(mock_agent):
    out = _run(acp_prompt(command=mock_agent["command"], message=""))
    assert "error" in out and "message" in out["error"]


def test_connect_without_command_fails_loud():
    out = _run(acp_connect(command=""))
    assert "error" in out and "command" in out["error"]


def test_close_with_no_running_agent_is_safe(mock_agent):
    out = _run(acp_close(command=mock_agent["command"], args=mock_agent["args"]))
    assert out["ok"] is True
    assert "note" in out
