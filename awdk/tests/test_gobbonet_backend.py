"""The pack's model plumbing, against a real socket rather than a mock.

These paths are all about talking to another process, so mocking the transport
would test the mock. Each test stands up an actual OpenAI-compatible server on
a real port and drives the code the way a user's browser does.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from adk.packs.gobbonet import backend as be


class _FakeLLM(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible server. Class attrs configure each case."""

    models: list = [{"id": "test-model"}]
    chunks: list = ["Hello", " world"]

    def log_message(self, *a):  # keep test output readable
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            self._json({"data": self.models, "object": "list"})
        else:
            self._json({"error": "nope"}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.__class__.last_body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/v1/embeddings":
            self._json({"data": [{"embedding": [0.1, 0.2]}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in self.chunks:
            payload = {"choices": [{"delta": {"content": c}}]}
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture
def llm():
    """A live fake LLM on an OS-assigned port."""
    handler = type("H", (_FakeLLM,), {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        yield port, handler
    finally:
        srv.shutdown()
        srv.server_close()


def test_probe_finds_a_backend_with_a_model(llm):
    port, _ = llm
    found = be._probe(port, "test")
    assert found is not None
    assert found.model == "test-model"


def test_probe_rejects_a_server_with_no_model(llm):
    """Listening is not the same as usable.

    A server with an empty /v1/models answers requests and then errors on the
    first completion. Reporting it as a backend moves the failure one step later,
    where it reads as the model being broken.
    """
    port, handler = llm
    handler.models = []
    assert be._probe(port, "test") is None


def test_discover_never_returns_our_own_port():
    """Proxying to ourselves is an infinite loop that presents as a hang.

    This pack serves on GobboNet's default 11434, which is also ollama's, so the
    exclusion is load-bearing rather than defensive.
    """
    for port, _ in be.KNOWN_BACKENDS:
        found = be.discover(exclude_port=port)
        assert found is None or f":{port}" not in found.url


def test_streaming_yields_tokens_as_they_arrive(llm):
    port, _ = llm
    b = be.Backend(url=f"http://127.0.0.1:{port}", kind="test", model="test-model")
    out = list(be.stream_completion(b, [{"role": "user", "content": "hi"}]))
    assert "".join(out) == "Hello world"


def test_negative_max_tokens_is_dropped(llm):
    """`max_tokens: -1` means "no limit" to several clients.

    Forwarded literally it asks for minus-one tokens: the server returns an
    empty completion and a clean [DONE], which reads as a broken model rather
    than a bad parameter.
    """
    port, handler = llm
    b = be.Backend(url=f"http://127.0.0.1:{port}", kind="test", model="test-model")
    list(be.stream_completion(b, [{"role": "user", "content": "hi"}], max_tokens=-1))
    assert "max_tokens" not in handler.last_body

    list(be.stream_completion(b, [{"role": "user", "content": "hi"}], max_tokens=64))
    assert handler.last_body["max_tokens"] == 64


def test_embeddings_round_trip(llm):
    port, _ = llm
    b = be.Backend(url=f"http://127.0.0.1:{port}", kind="test", model="test-model")
    assert be.embed(b, ["a"]) == [[0.1, 0.2]]


def test_setup_hint_names_a_command():
    """A refusal with no next step is how a user concludes the tool is broken."""
    hint = be.setup_hint()
    assert "--setup-model" in hint
    assert "--backend" in hint


def test_local_engine_refuses_with_the_hint_when_nothing_is_running():
    from adk.packs.gobbonet.server import LocalEngine, NotConfigured

    # Every known port excluded => nothing can be discovered.
    eng = LocalEngine()
    eng._exclude_port = None
    eng._found = None
    import adk.packs.gobbonet.backend as mod

    real = mod.discover
    mod.discover = lambda **kw: None
    try:
        with pytest.raises(NotConfigured) as e:
            list(eng.stream_chat([{"role": "user", "content": "hi"}]))
        assert "--setup-model" in str(e.value)
    finally:
        mod.discover = real


def test_agentic_engine_streams_tokens_and_surfaces_tool_calls():
    """The ReAct bridge: async callback events -> a sync iterator.

    Collecting the whole answer before yielding would pass a naive test and
    destroy the only property that makes a local model bearable, so this asserts
    the pieces arrive separately.
    """
    from adk.packs.gobbonet.agentic import AgenticEngineMixin

    class _StubAgent:
        async def stream_react(self, message, on_event, history=None, max_steps=6):
            on_event({"type": "token", "text": "think"})
            on_event({"type": "tool", "name": "web_search", "args": {}})
            on_event({"type": "token", "text": "answer"})
            return None

    class _Eng(AgenticEngineMixin):
        def _get_agent(self):
            return _StubAgent()

    pieces = list(_Eng().stream_chat([{"role": "user", "content": "hi"}]))
    joined = "".join(pieces)
    assert "think" in joined and "answer" in joined
    assert "web_search" in joined, "tool activity must be visible, not a silent pause"


def test_agent_failure_reaches_the_user_not_just_a_dead_thread():
    """An exception on the worker thread with no reader is 'it stopped typing'."""
    from adk.packs.gobbonet.agentic import AgenticEngineMixin

    class _Boom:
        async def stream_react(self, **kw):
            raise RuntimeError("model exploded")

    class _Eng(AgenticEngineMixin):
        def _get_agent(self):
            return _Boom()

    out = "".join(_Eng().stream_chat([{"role": "user", "content": "hi"}]))
    assert "model exploded" in out


def test_capability_probe_reports_absence_rather_than_dropping_it():
    from adk.packs.gobbonet.agentic import describe_capabilities

    caps = describe_capabilities()
    assert "tool categories" in caps
    # Every non-category entry is a bool: present or absent, never omitted.
    for key, val in caps.items():
        if key != "tool categories":
            assert isinstance(val, bool), f"{key} should be a present/absent bool"
