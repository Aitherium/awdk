"""Pack protocol drivers driven over a REAL socket, not httpx.MockTransport.

`tests/test_pack_drivers.py` proves the parsing logic against MockTransport,
which never opens a connection: it cannot catch a wrong URL join, a header the
server rejects, a real non-2xx path, real JSON serialization, or a genuine
connection failure. These tests stand up an actual HTTP server on a loopback
port and drive each of the four drivers against it end-to-end.

The server replies in each protocol's real wire shape (LangGraph SSE frames,
A2A/MCP JSON-RPC 2.0 envelopes), and RECORDS what the driver actually sent so
the request side is asserted too — not just the response parsing.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adk.pack_drivers import DriverResult, get_driver

RECEIVED: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence the test log
        return

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError:
            return {"_unparsed": body.decode("utf-8", "replace")}

    def _send(self, status: int, body: bytes, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        payload = self._read_json()
        RECEIVED.append({"path": self.path, "payload": payload})

        # ── plain HTTP ──
        if self.path == "/prompt":
            return self._send(200, json.dumps({"text": f"http-said:{payload.get('prompt')}"}).encode())
        if self.path == "/plaintext":
            return self._send(200, b"just-plain-text", ctype="text/plain")
        if self.path == "/boom":
            return self._send(500, json.dumps({"detail": "server exploded"}).encode())

        # ── LangGraph REST: SSE frames ──
        if self.path == "/runs/stream":
            text = payload.get("input", {}).get("messages", [{}])[0].get("content", "")
            frames = (
                b": comment line that must be ignored\n"
                b'data: {"messages": [["user", "ignored"]]}\n'
                b"\n"
                + f'data: {{"messages": [["assistant", "graph-said:{text}"]]}}\n'.encode()
                + b"\n"
            )
            return self._send(200, frames, ctype="text/event-stream")

        # ── A2A: JSON-RPC message/send ──
        if self.path == "/a2a":
            said = payload["params"]["message"]["parts"][0]["text"]
            return self._send(200, json.dumps({
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"message": {"role": "agent",
                                       "parts": [{"type": "text", "text": f"a2a-said:{said}"}]}},
            }).encode())
        if self.path == "/a2a-error":
            return self._send(200, json.dumps({
                "jsonrpc": "2.0", "id": payload.get("id"),
                "error": {"code": -32000, "message": "agent refused"},
            }).encode())

        # ── MCP: JSON-RPC tools/call ──
        if self.path == "/mcp":
            said = payload["params"]["arguments"]["prompt"]
            return self._send(200, json.dumps({
                "jsonrpc": "2.0", "id": payload.get("id"),
                "result": {"text": f"mcp-said:{said}"},
            }).encode())

        return self._send(404, json.dumps({"detail": "no route"}).encode())


@pytest.fixture(scope="module")
def live_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address[0], srv.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _clear():
    RECEIVED.clear()
    yield


def _run(coro):
    return asyncio.run(coro)


async def _drive(protocol: str, url: str, **kw) -> DriverResult:
    d = get_driver(protocol, url, **kw)
    try:
        return await d.prompt("ping")
    finally:
        await d.close()


# ── each protocol, over a real TCP connection ──────────────────────────────


def test_http_driver_live(live_url):
    res = _run(_drive("http", live_url))
    assert res.text == "http-said:ping"
    assert RECEIVED[0]["path"] == "/prompt"
    assert RECEIVED[0]["payload"] == {"prompt": "ping"}, "request body must reach the server intact"


def test_http_driver_live_plaintext_body(live_url):
    """A non-JSON 200 must fall back to the raw body, proven over the wire."""
    res = _run(_drive("http", live_url, endpoint="/plaintext"))
    assert res.text == "just-plain-text"


def test_langgraph_driver_live_sse(live_url):
    res = _run(_drive("langgraph_rest", live_url))
    assert res.text == "graph-said:ping"
    sent = RECEIVED[0]["payload"]
    assert sent["stream_mode"] == "messages"
    assert sent["input"]["messages"] == [{"role": "user", "content": "ping"}]


def test_a2a_driver_live_jsonrpc(live_url):
    res = _run(_drive("a2a", live_url))
    assert res.text == "a2a-said:ping"
    sent = RECEIVED[0]["payload"]
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "message/send"
    assert sent["params"]["message"]["parts"][0]["text"] == "ping"


def test_mcp_driver_live_jsonrpc(live_url):
    res = _run(_drive("mcp", live_url))
    assert res.text == "mcp-said:ping"
    sent = RECEIVED[0]["payload"]
    assert sent["method"] == "tools/call"
    assert sent["params"]["arguments"] == {"prompt": "ping"}


# ── real failure paths ─────────────────────────────────────────────────────


def test_live_non_2xx_raises_with_status_and_body(live_url):
    with pytest.raises(RuntimeError) as ei:
        _run(_drive("http", live_url, endpoint="/boom"))
    msg = str(ei.value)
    assert "500" in msg and "server exploded" in msg, msg


def test_live_jsonrpc_error_envelope_raises(live_url):
    """A 200 carrying a JSON-RPC error must still fail, not return empty text."""
    with pytest.raises(RuntimeError) as ei:
        _run(_drive("a2a", live_url, endpoint="/a2a-error"))
    assert "agent refused" in str(ei.value)


def test_live_unroutable_path_raises(live_url):
    with pytest.raises(RuntimeError) as ei:
        _run(_drive("http", live_url, endpoint="/nope"))
    assert "404" in str(ei.value)


def test_connection_refused_is_a_clean_error():
    """A dead endpoint must raise RuntimeError, not hang or leak httpx internals."""
    # Port 1 on loopback: nothing listens, connect fails fast.
    with pytest.raises(RuntimeError):
        _run(_drive("http", "http://127.0.0.1:1"))


def test_driver_closes_its_own_client(live_url):
    """A driver that created the client must release it (no leaked sockets)."""

    async def go():
        d = get_driver("http", live_url)
        await d.prompt("ping")
        await d.close()
        return d._client

    assert _run(go()) is None
