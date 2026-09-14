"""`--backend` must reach the server you named, and recognise it when it answers.

Two independent bugs made `adk gobbonet --backend URL` refuse a healthy model
server. Both were measured against a REAL llama.cpp (bonsai-27b) that answered
/v1/models with 200 and completed chat requests the whole time:

    NotConfigured: nothing usable at http://aither-llamacpp-bonsai:8090

1. THE HOST WAS DISCARDED. The call site did
   `_probe(int(pinned.rsplit(":", 1)[-1]))` -- keep the last colon-segment as a
   port -- and `_probe` built `http://127.0.0.1:{port}` unconditionally. Every
   pinned backend was silently rewritten to localhost, and a URL with no port
   raised ValueError on `//host/v1`.

2. ONLY ONE RESPONSE SHAPE WAS READ. `data["data"]` is OpenAI's. llama.cpp
   answers `{"models": [{"name": ...}]}`, so a working server was classified
   "listening but empty" and refused -- on localhost too, not only when pinned.

Either bug alone produces the same user experience: the UI serves perfectly and
the chat box cannot reach a model. After the fix, verified end to end against
that same server: tokens streamed through GobboNet's own proxy.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from adk.packs.gobbonet.backend import _model_names, _probe

OPENAI_SHAPE = {"object": "list", "data": [{"id": "gpt-oss-20b"}, {"id": "second"}]}
LLAMACPP_SHAPE = {"models": [{"name": "bonsai-27b", "model": "bonsai-27b"}]}


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(payload: dict) -> tuple[ThreadingHTTPServer, int]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A003
            pass

        def do_GET(self):  # noqa: N802
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ── shape parsing ────────────────────────────────────────────────────────

def test_reads_the_openai_shape():
    assert _model_names(OPENAI_SHAPE) == ["gpt-oss-20b", "second"]


def test_reads_the_llamacpp_shape():
    """The bug: this returned [] and the server was called 'listening but empty'."""
    assert _model_names(LLAMACPP_SHAPE) == ["bonsai-27b"]


def test_an_empty_listing_is_still_empty():
    # The original guard was RIGHT — a server with no models is not usable, and
    # accepting it moves the failure to the first completion. Only the reading
    # was wrong, so this must keep saying no.
    assert _model_names({"object": "list", "data": []}) == []
    assert _model_names({"models": []}) == []
    assert _model_names({}) == []
    assert _model_names("not a dict") == []


# ── the probe reaches the host you named ─────────────────────────────────

def test_a_pinned_url_is_probed_at_that_host_not_localhost():
    httpd, port = _serve(LLAMACPP_SHAPE)
    try:
        found = _probe(f"http://127.0.0.1:{port}", "pinned")
        assert found is not None, "a healthy llama.cpp-shaped server was refused"
        assert found.model == "bonsai-27b"
        # The URL must survive intact. When the host was scraped off, this came
        # back pointing somewhere the user never named.
        assert found.url == f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def test_an_int_port_still_works_because_discover_passes_ints():
    # discover() walks KNOWN_BACKENDS as ints; changing the signature must not
    # break the path that was never broken.
    httpd, port = _serve(OPENAI_SHAPE)
    try:
        found = _probe(port, "ollama")
        assert found is not None and found.url == f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def test_a_url_with_no_port_does_not_crash():
    """The old code did int('//host/v1') and raised ValueError.

    A hostname with no port is an ordinary thing to type. It should probe and
    return None when nothing answers — never blow up inside the launcher.
    """
    assert _probe("http://no-such-host.invalid", "pinned") is None


def test_a_trailing_v1_is_not_doubled():
    """People paste the URL they use with an OpenAI client, which ends in /v1.

    /v1 + /v1/models = /v1/v1/models, a 404 from a perfectly healthy server —
    refused for a reason the user cannot see.
    """
    httpd, port = _serve(OPENAI_SHAPE)
    try:
        found = _probe(f"http://127.0.0.1:{port}/v1", "pinned")
        assert found is not None, "a URL ending in /v1 was refused"
        assert found.url == f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def test_a_dead_backend_is_refused_not_guessed():
    # The refusal itself was right: pointing at a dead port and finding out
    # mid-conversation is worse than being told now.
    assert _probe(f"http://127.0.0.1:{_free_port()}", "pinned") is None


@pytest.mark.parametrize("shape", [OPENAI_SHAPE, LLAMACPP_SHAPE])
def test_both_shapes_produce_a_usable_backend(shape):
    httpd, port = _serve(shape)
    try:
        found = _probe(f"http://127.0.0.1:{port}", "pinned")
        assert found is not None and found.model
    finally:
        httpd.shutdown()
