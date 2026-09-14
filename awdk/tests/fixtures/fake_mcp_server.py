#!/usr/bin/env python3
"""A real, minimal MCP server over stdio — the test subject for mcp_client.

DELIBERATELY A SUBPROCESS, NOT A MOCK. The client's whole job is to spawn a
process and speak newline-delimited JSON-RPC to it, so a mocked transport would
assert that our mock matches our client and prove nothing about either. This
repo has already paid for that lesson once, in the AsyncMock-as-an-httpx-
response class: the mock satisfied every assertion while the real call fell
through to something else entirely.

It also reproduces three things real servers do that a tidy fixture would not,
each of which broke a client somewhere:

  * prints a BANNER to stdout before any protocol traffic (servers do this
    despite the spec; a client that treats the first line as JSON dies);
  * writes chatter to STDERR (which must not be folded into stdout);
  * emits a NOTIFICATION with no id in the middle of the stream (which a client
    correlating by id must skip rather than mistake for its reply).

Behaviour is switched by argv so one file covers every case:

    fake_mcp_server.py              well-behaved, 2 tools
    fake_mcp_server.py --no-tools   connects, exposes nothing
    fake_mcp_server.py --die        exits during initialize
    fake_mcp_server.py --tool-error returns isError on tools/call
    fake_mcp_server.py --hang       accepts the connection, never replies
"""
from __future__ import annotations

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else ""

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "what to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    # Real servers print things. A client that assumes line 1 is protocol dies here.
    print("fake-mcp-server starting up", flush=True)
    sys.stderr.write("fake-mcp-server: stderr chatter\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        # A wedged server is wedged for EVERY method, not just the handshake.
        # Scoping this to initialize made the client's timeout untestable through
        # any public call -- the test had to reach into a private method, which is
        # the tell that the fixture was modelling something narrower than reality.
        if MODE == "--hang":
            time.sleep(3600)

        if method == "initialize":
            if MODE == "--die":
                return 3
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }})
            continue

        if method == "notifications/initialized":
            continue  # a notification has no reply

        if method == "tools/list":
            # An unsolicited notification mid-stream. A client correlating by id
            # must skip it; one that returns the first line it reads gets this
            # instead of its answer.
            send({"jsonrpc": "2.0", "method": "notifications/message",
                  "params": {"level": "info", "data": "listing"}})
            tools = [] if MODE == "--no-tools" else TOOLS
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
            continue

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if MODE == "--tool-error":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "the well is dry"}],
                    "isError": True,
                }})
                continue
            if name == "echo":
                text = str(args.get("text", ""))
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"echo: {text}"}]}})
                continue
            if name == "add":
                try:
                    total = float(args.get("a", 0)) + float(args.get("b", 0))
                except (TypeError, ValueError):
                    total = float("nan")
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(total)}]}})
                continue
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32602, "message": f"no such tool {name!r}"}})
            continue

        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"no such method {method!r}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
