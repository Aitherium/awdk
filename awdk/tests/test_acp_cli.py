"""adk acp CLI tests: parser wiring, editor config emission, and a live prompt.

The parser regression this guards: the top-level subparsers use ``dest="command"``
and the acp subcommands' ``--command`` option overwrote it (argparse sets the
option's dest, so ``acp prompt --command c`` turned ``command`` into ``c`` and
the dispatch never saw ``acp``). The option now uses ``dest="agent_command"``.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from unittest.mock import AsyncMock, patch

from adk.cli import (
    _cmd_acp_config,
    _cmd_acp_prompt,
    _cmd_acp_serve,
    _register_commands,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="adk")
    sub = parser.add_subparsers(dest="command")
    _register_commands(sub)
    return parser.parse_args(argv)


def test_acp_dispatch_survives_dash_command_option():
    """--command must not clobber the top-level subparsers' `command` dest."""
    for argv in (
        ["acp", "config", "zed"],
        ["acp", "prompt", "--command", "c", "hi"],
        ["acp", "serve"],
    ):
        args = _parse(argv)
        assert args.command == "acp", f"{argv}: command was clobbered -> {args.command!r}"


def test_acp_config_emits_agent_json():
    """config zed emits a registry-style agent.json with a stdio runtime."""
    args = argparse.Namespace(agent_command=None, ide="zed")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cmd_acp_config(args)
    assert rc == 0
    # The JSON doc is everything before the "# Install:" guidance line.
    data = json.loads(buf.getvalue().split("# Install:", 1)[0])
    assert data["name"] == "awdk"
    assert data["runtime"]["type"] == "stdio"
    assert data["runtime"]["command"][-2:] == ["acp", "serve"]
    assert "prompt" in data["capabilities"]


# A real minimal ACP agent that echoes the prompt text back (no external deps).
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
                "info": {"name": "mock-cli", "version": "1"}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s-1"}})
        elif method == "session/prompt":
            blocks = (req.get("params") or {}).get("prompt") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "s-1", "update": {
                      "sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": "reply:" + text}}}})
            send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    """
).strip()


def test_acp_prompt_live(tmp_path):
    """adk acp prompt drives a real ACP agent subprocess and prints its reply."""
    script = tmp_path / "mock_agent.py"
    script.write_text(_MOCK_AGENT, encoding="utf-8")
    args = argparse.Namespace(
        agent_command=sys.executable,
        arg=["-u", str(script)],
        message="hello",
        timeout=2.0,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cmd_acp_prompt(args)
    assert rc == 0
    assert "reply:hello" in buf.getvalue()


def test_acp_serve_starts_without_a_backend():
    """serve must reach stdio even with NO LLM backend configured.

    This test asserted the OPPOSITE until 2026-08-07 — serve awaited
    `get_provider()` at startup and raised there. That is the wrong place: an
    ACP client handshakes and may `authenticate` before any model is needed, and
    the ACP registry's CI verifier runs exactly that handshake on a machine with
    no fleet, no Ollama and no API key. Exiting at startup makes it time out
    waiting for `initialize`, which reads as a protocol bug rather than a
    missing backend, and the entry is rejected.

    Loudness is not lost, it MOVED: the first `session/prompt` resolves the
    provider, so a missing backend becomes a real turn error the user sees in
    their editor — with a session to see it in.
    """
    args = argparse.Namespace(name="x", version="1", model=None)
    serve_calls = []
    with patch(
        "adk.llm.LLMRouter.get_provider",
        new=AsyncMock(side_effect=RuntimeError("no LLM backend available")),
    ):
        with patch(
            "adk.acp_server.serve_stdio",
            new=AsyncMock(side_effect=lambda agent, **kw: serve_calls.append(agent)),
        ):
            rc = _cmd_acp_serve(args)
    assert rc == 0
    assert len(serve_calls) == 1, (
        "serve must hand control to stdio with no backend — otherwise the ACP "
        "registry's handshake times out and the agent is unlistable"
    )


def test_acp_serve_reaches_stdio_with_backend():
    """With a configured backend, serve hands control to the ACP stdio server."""
    args = argparse.Namespace(name="atlas", version="2.0.0", model=None)
    serve_calls = []
    with patch("adk.llm.LLMRouter.get_provider", new=AsyncMock(return_value=None)):
        with patch(
            "adk.acp_server.serve_stdio",
            new=AsyncMock(side_effect=lambda agent, **kw: serve_calls.append(agent)),
        ):
            rc = _cmd_acp_serve(args)
    assert rc == 0
    assert len(serve_calls) == 1, "serve_stdio should be invoked once"
    assert serve_calls[0].name == "atlas"
