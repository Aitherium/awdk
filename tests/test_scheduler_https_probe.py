"""The fleet spine (:8150) serves HTTPS only; adk must still find it.

Measured 2026-11-11: adk.local_inference probed only http://127.0.0.1:8150, got an
empty reply from the HTTPS spine, logged "No local inference endpoint found" and
every awsh question ended in ConnectionError after a silent 120 s. These tests pin
https-first discovery that still falls through to a plain-http scheduler.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _scheduler() -> tuple[HTTPServer, int]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = {"backends": {"bonsai": {"served_model": "aither-orchestrator"}}}
            data = json.dumps(body).encode() if self.path == "/llm/backends/snapshot" else b"{}"
            self.send_response(200 if self.path == "/llm/backends/snapshot" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture
def plain_scheduler():
    srv, port = _scheduler()
    yield port
    srv.shutdown()


def test_https_is_probed_before_http():
    from adk.local_inference import SCHEDULER_URLS

    assert SCHEDULER_URLS[0].startswith("https://127.0.0.1:8150")
    # Owner 2026-11-12: the spine is https only; plain http into it hangs.
    assert all(u.startswith("https://") for u in SCHEDULER_URLS)


def test_discovery_falls_through_a_dead_scheme(monkeypatch, plain_scheduler):
    import adk.local_inference as li

    monkeypatch.delenv("AITHER_LOCAL_LLM_URL", raising=False)
    # An https URL that refuses (port 9 on loopback) first, then the live http one.
    live = f"http://127.0.0.1:{plain_scheduler}"
    monkeypatch.setattr(li, "SCHEDULER_URLS", ("https://127.0.0.1:9", live))
    found = asyncio.run(li.discover_local_endpoint())
    assert found.found and found.endpoint_url == live
    assert found.source == "microscheduler"
    assert found.model == "aither-orchestrator"
