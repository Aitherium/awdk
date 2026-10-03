"""llamacpp_setup.status() is "running" only when OUR served model answers.

2026-10-03: another local server held port 8200 and served its own models; status()
reported "Running" with that server's first model, so quickstart and install believed
the orchestrator was up.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adk import llamacpp_setup


def _serve(models):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- http.server API
            body = json.dumps({"object": "list", "data": [{"id": m} for m in models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def server():
    made = []

    def make(models):
        s = _serve(models)
        made.append(s)
        return s.server_address[1]

    yield make
    for s in made:
        s.shutdown()


def test_foreign_server_on_the_port_is_not_running(server):
    port = server(["gemma3npc-1b", "llama-3.2-1b"])
    s = llamacpp_setup.status(port=port)
    assert s.running is False
    assert s.model == ""
    assert "another server" in s.error and "gemma3npc-1b" in s.error
    assert llamacpp_setup.DEFAULT_SERVED_NAME in s.error


def test_our_model_is_running(server):
    port = server(["other", llamacpp_setup.DEFAULT_SERVED_NAME])
    s = llamacpp_setup.status(port=port)
    assert s.running is True
    assert s.model == llamacpp_setup.DEFAULT_SERVED_NAME


def test_nothing_listening_is_not_running():
    s = llamacpp_setup.status(port=1)
    assert s.running is False and s.error
