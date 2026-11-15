"""`adk chat <agent>` over the "aitheros" protocol: POST /chat on a platform service.

The service here is a local fake that answers in Atlas's own reply shape; nothing
in this file reaches a real service.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adk import aitheros_chat
from adk.aitheros_chat import AitherOSChatError, build_request, chat, parse_reply
from adk.mesh_discovery import CHAT_PROTOCOLS, AgentRef, _load_local_agents

#: What Atlas returned to a status question on 2026-11-10 (session id shortened).
ATLAS_REPLY = {
    "session_id": "session_f2daafcd",
    "response": "Online and standing by.",
    "reasoning_available": True,
    "actions_proposed": None,
    "auto_executing": False,
    "message_count": 2,
    "timestamp": "2026-11-11T04:52:17.168276",
}


class _Service:
    """A platform service that only serves POST /chat, like Atlas."""

    def __init__(self, reply=ATLAS_REPLY, status: int = 200, raw: bytes | None = None):
        self.requests: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append((self.path, body))
                if self.path != "/chat":
                    out, code = b'{"detail":"Not Found"}', 404
                else:
                    out = raw if raw is not None else json.dumps(reply).encode()
                    code = status
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *_a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def service():
    made: list = []

    def _make(**kw):
        made.append(_Service(**kw))
        return made[-1]

    yield _make
    for s in made:
        s.close()


# ── which protocol an agent speaks ─────────────────────────────────────────

def test_aitheros_is_a_protocol_only_when_named():
    assert "aitheros" in CHAT_PROTOCOLS
    ref = AgentRef(name="atlas", provider_hint="aitheros", chat_protocol_override="aitheros")
    assert ref.chat_protocol == "aitheros"
    # Not inferred from the hint: a registry row that says nothing keeps "adk".
    assert AgentRef(name="atlas", provider_hint="aitheros").chat_protocol == "adk"
    # An unknown override is ignored rather than passed through.
    assert AgentRef(name="x", chat_protocol_override="telnet").chat_protocol == "adk"


def test_agents_json_carries_the_protocol(tmp_path, monkeypatch):
    f = tmp_path / "agents.json"
    f.write_text(json.dumps({"agents": {"atlas": {
        "name": "atlas", "invoke_url": "https://127.0.0.1:8778",
        "provider_hint": "aitheros", "chat_protocol": "aitheros"}}}), encoding="utf-8")
    monkeypatch.setenv("AITHER_AGENTS_FILE", str(f))
    (ref,) = _load_local_agents()
    assert ref.chat_protocol == "aitheros"
    assert ref.to_dict()["chat_protocol"] == "aitheros"


# ── the request and the reply ──────────────────────────────────────────────

def test_request_never_asks_the_service_to_act_on_its_own():
    assert build_request("status?") == {"message": "status?", "auto_execute": False}


def test_reply_text_is_read_from_the_services_own_shape():
    assert parse_reply(ATLAS_REPLY) == "Online and standing by."


@pytest.mark.parametrize("payload", [
    {**ATLAS_REPLY, "response": ""},
    {**ATLAS_REPLY, "response": "   \n"},
    {**ATLAS_REPLY, "response": None},
    {"session_id": "s"},
    {"choices": [{"message": {"content": "an openai-shaped reply"}}]},
    ["not", "an", "object"],
    "text",
])
def test_a_reply_with_no_text_is_an_error_not_an_empty_success(payload):
    with pytest.raises(AitherOSChatError):
        parse_reply(payload)


async def test_chat_posts_to_chat_and_returns_the_text(service):
    svc = service()
    assert await chat(svc.url + "/", "status?") == "Online and standing by."
    assert svc.requests == [("/chat", {"message": "status?", "auto_execute": False})]


async def test_chat_raises_on_a_non_200(service):
    svc = service(status=503, reply={"detail": "warming up"})
    with pytest.raises(AitherOSChatError, match="503"):
        await chat(svc.url, "status?")


async def test_chat_raises_on_a_non_json_body(service):
    svc = service(raw=b"<html>gateway</html>")
    with pytest.raises(AitherOSChatError, match="non-JSON"):
        await chat(svc.url, "status?")


async def test_chat_raises_when_nothing_listens():
    with pytest.raises(AitherOSChatError, match="could not reach"):
        await chat("http://127.0.0.1:9", "status?")


async def test_chat_verifies_tls_through_the_shared_policy(monkeypatch):
    """The verify value is whatever adk._tls.tls_verify() says: the internal CA
    when its bundle is installed. It is not decided, or switched off, here."""
    seen: dict = {}
    sentinel = "/path/to/aithernet-ca-bundle.pem"

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return ATLAS_REPLY

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, json):  # noqa: A002 - httpx's keyword
            seen["url"] = url
            return _Resp()

    monkeypatch.setattr(aitheros_chat, "tls_verify", lambda: sentinel)
    monkeypatch.setattr(aitheros_chat.httpx, "AsyncClient", _Client)
    await chat("https://127.0.0.1:8778", "status?")
    assert seen["verify"] == sentinel
    assert seen["url"] == "https://127.0.0.1:8778/chat"


# ── the CLI ────────────────────────────────────────────────────────────────

def _ref(url: str, protocol: str = "aitheros") -> AgentRef:
    return AgentRef(name="atlas", invoke_url=url, provider_hint="aitheros",
                    chat_protocol_override=protocol)


async def test_stream_to_agent_uses_chat_and_no_other_route(service, capsys):
    from adk.cli import _stream_to_agent

    svc = service()
    await _stream_to_agent(_ref(svc.url), "status?")
    assert capsys.readouterr().out == "Online and standing by.\n"
    assert [path for path, _ in svc.requests] == ["/chat"]


def _run_cmd_chat(monkeypatch, ref) -> int:
    from adk import cli, mesh_discovery

    async def _resolve(_name):
        return ref

    monkeypatch.setattr(mesh_discovery, "resolve_agent", _resolve)
    return cli.cmd_chat(argparse.Namespace(agent="atlas", message="status?"))


def test_cmd_chat_one_shot_prints_the_reply_and_exits_zero(service, monkeypatch, capsys):
    svc = service()
    assert _run_cmd_chat(monkeypatch, _ref(svc.url)) == 0
    assert "Online and standing by." in capsys.readouterr().out


def test_cmd_chat_exits_one_when_the_service_says_nothing(service, monkeypatch, capsys):
    svc = service(reply={**ATLAS_REPLY, "response": ""})
    assert _run_cmd_chat(monkeypatch, _ref(svc.url)) == 1
    assert "ERROR:" in capsys.readouterr().err


def test_cmd_chat_exits_one_on_a_refused_request(service, monkeypatch, capsys):
    svc = service(status=500, reply={"detail": "boom"})
    assert _run_cmd_chat(monkeypatch, _ref(svc.url)) == 1
    assert "500" in capsys.readouterr().err


# ── the mesh chat proxy (adk server) ───────────────────────────────────────

def test_server_proxy_has_an_aitheros_branch():
    """The proxy generator is a closure inside create_app, so it is pinned in
    source: without this branch a proxied "aitheros" agent falls through to
    /chat/stream, which a platform service answers with 404."""
    from pathlib import Path

    import adk

    src = (Path(adk.__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    start = src.index("async def _proxy_agent_stream")
    body = src[start:src.index("@app.post", start)]
    assert 'ref.chat_protocol == "aitheros"' in body
    assert "adk.aitheros_chat" in body

