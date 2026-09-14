"""adk.node_link — the device's outbound reverse link.

THE PROPERTY THAT MATTERS MOST IS THE REFUSAL. This process is one end of a socket
whose other end is on the public internet, and it forwards to the customer's own
loopback — where the harness daemon, awnode, and on a dev box a dozen other
services are listening. So the allowlist here is not a duplicate of the tunnel's:
it is the gate that holds if the tunnel is ever wrong. Every "refuses" case below
is testing that, and `path_target` defaults to REFUSE so that adding a route on
the server does not implicitly open one here.

The rest: the ws(s) coercion (a plain-http base must NOT silently become wss and
appear to work), reconnect/backoff bounds, and `reach_kind()` not being sticky —
a phone that lost its link must stop claiming reach within one heartbeat, or the
owner's page offers a device that cannot answer.
"""

import asyncio

import pytest

from adk import node_link as NL


# ══════════════════════════════════════════════════════════════════════════
# URL construction
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("base,want", [
    ("https://tunnel.aitherium.com", "wss://tunnel.aitherium.com/tunnel/nodes/n1/attach"),
    ("https://tunnel.aitherium.com/", "wss://tunnel.aitherium.com/tunnel/nodes/n1/attach"),
    ("http://127.0.0.1:8310", "ws://127.0.0.1:8310/tunnel/nodes/n1/attach"),
    ("tunnel.aitherium.com", "wss://tunnel.aitherium.com/tunnel/nodes/n1/attach"),
    ("wss://x.example", "wss://x.example/tunnel/nodes/n1/attach"),
])
def test_tunnel_ws_url(base, want):
    assert NL.tunnel_ws_url(base, "n1") == want


def test_http_is_not_silently_upgraded():
    """An http base yields ws://, not wss://.

    A silent upgrade would make a production misconfiguration look like it works
    until the first handshake fails somewhere the log does not reach.
    """
    assert NL.tunnel_ws_url("http://tunnel.example", "n").startswith("ws://")


# ══════════════════════════════════════════════════════════════════════════
# the allowlist — refuse by default
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path,target,local", [
    ("v1/chat/completions", "inference", "v1/chat/completions"),
    ("/v1/models", "inference", "v1/models"),
    ("health", "inference", "health"),
    ("status", "inference", "status"),
    ("chat", "inference", "chat"),
    ("v1/models?x=1", "inference", "v1/models"),
])
def test_inference_paths_allowed(path, target, local):
    assert NL.path_target(path) == (target, local)


@pytest.mark.parametrize("path", [
    # `v1` is NOT a permitted segment. Opening it would expose every other
    # /v1/... route the local server happens to serve.
    "v1", "v1/completions", "v1/embeddings", "v1/chat",
    "v1/chat/completions/extra",
    # The local API surface the tunnel must never reach.
    "mcp/execute", "config", "deploy", "connect", "license", "admin",
    # Nothing at all.
    "", "/", "   ",
    # Traversal dressed as an allowed prefix.
    "../config", "harness/../config", "harness/sessions/../../config",
    # Harness prefix with a path outside the scoped set.
    "harness/", "harness/awrun/submit", "harness/desk/fleet", "harness/desk",
    "harness/config",
])
def test_refused_paths(path):
    assert NL.path_target(path) == ("", "")


@pytest.mark.parametrize("path,local", [
    ("harness/sessions/unified", "sessions/unified"),
    ("harness/sessions", "sessions"),
    ("harness/sessions/abc/stream", "sessions/abc/stream"),
    ("harness/decisions/1/wait", "decisions/1/wait"),
    ("harness/desk/fleet/status", "desk/fleet/status"),
])
def test_harness_paths_allowed_and_stripped(path, local):
    assert NL.path_target(path) == ("harness", local)


def test_allowlist_is_case_insensitive():
    assert NL.path_target("V1/Chat/Completions")[0] == "inference"
    assert NL.path_target("HARNESS/Sessions")[0] == "harness"


# ══════════════════════════════════════════════════════════════════════════
# header handling — no credential crosses the link
# ══════════════════════════════════════════════════════════════════════════

def test_local_headers_drop_everything_but_the_allowlist():
    out = NL.NodeLink._local_headers({
        "Authorization": "Bearer stolen",
        "Cookie": "session=x",
        "X-Internal-Token": "k",
        "Host": "evil.example",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }, "")
    assert set(k.lower() for k in out) == {"content-type", "accept"}


def test_harness_bearer_is_added_locally_never_relayed():
    """The harness token is minted on this device and must be added HERE.

    If it crossed the link it would be a device credential travelling over a
    public socket on every request.
    """
    out = NL.NodeLink._local_headers({"Authorization": "Bearer from-the-wire"},
                                     "local-harness-token")
    assert out["authorization"] == "Bearer local-harness-token"


# ══════════════════════════════════════════════════════════════════════════
# body decoding — a tunnel frame is untrusted input
# ══════════════════════════════════════════════════════════════════════════

def test_body_decode():
    import base64
    assert NL._decode_body(base64.b64encode(b"hi").decode()) == b"hi"
    assert NL._decode_body("") == b""
    assert NL._decode_body(None) == b""
    assert NL._decode_body("not!base64!") == b""
    assert NL._decode_body("A" * (NL.MAX_FRAME_BYTES * 2 + 4)) == b""


# ══════════════════════════════════════════════════════════════════════════
# reach_kind is not sticky
# ══════════════════════════════════════════════════════════════════════════

def test_reach_kind_tracks_the_socket():
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="t")
    assert link.reach_kind() == "none"
    link.connected = True
    assert link.reach_kind() == "ws"
    link.connected = False
    assert link.reach_kind() == "none"


# ══════════════════════════════════════════════════════════════════════════
# reconnect / backoff
# ══════════════════════════════════════════════════════════════════════════

def test_run_retries_and_records_the_error(monkeypatch):
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="tok")
    calls = []

    async def _boom():
        calls.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(link, "_serve_once", _boom)
    slept = []

    async def _sleep(s):
        slept.append(s)

    monkeypatch.setattr(NL.asyncio, "sleep", _sleep)
    asyncio.run(link.run(max_attempts=4))
    assert len(calls) == 4
    assert "connection refused" in link.last_error
    # Backoff ramps and is jittered, but never past the cap.
    assert slept and all(0 < s <= NL._BACKOFF_MAX * 1.5 for s in slept)
    assert slept[-1] > slept[0]


def test_run_without_websockets_is_loud_not_silent(monkeypatch):
    """An absent transport must SAY so. A silent no-op is how "the phone is
    enrolled but unreachable" becomes a mystery nobody can debug."""
    import builtins
    real_import = builtins.__import__

    def _no_ws(name, *a, **kw):
        if name == "websockets":
            raise ImportError("no websockets")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_ws)
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="tok")
    asyncio.run(link.run(max_attempts=1))
    assert "websockets" in link.last_error


# ══════════════════════════════════════════════════════════════════════════
# request handling — the refusal is answered, not dropped
# ══════════════════════════════════════════════════════════════════════════

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        import json
        self.sent.append(json.loads(text))


def test_a_refused_path_answers_403_and_forwards_nothing():
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="tok",
                       inference_url="http://127.0.0.1:8080")
    ws = _FakeWS()
    asyncio.run(link._handle_request(ws, "r1", {"path": "config", "method": "GET"}))
    assert ws.sent == [{"t": "err", "id": "r1", "status": 403,
                        "detail": "path not permitted on this link"}]


def test_no_local_inference_server_answers_503():
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="tok",
                       inference_url="")
    ws = _FakeWS()
    asyncio.run(link._handle_request(
        ws, "r2", {"path": "v1/models", "method": "GET"}))
    assert ws.sent[0]["status"] == 503


def test_harness_path_without_a_harness_answers_503():
    link = NL.NodeLink(tunnel_url="https://t", node_id="n", token="tok",
                       inference_url="http://127.0.0.1:8080")
    ws = _FakeWS()
    asyncio.run(link._handle_request(
        ws, "r3", {"path": "harness/sessions/unified", "method": "GET"}))
    assert ws.sent[0]["status"] == 503
