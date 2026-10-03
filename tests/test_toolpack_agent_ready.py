"""The agent_ready toolpack against a REAL local HTTP server.

Two sites are served from a ThreadingHTTPServer on 127.0.0.1: one that does
everything right and one that serves nothing. agent_ready_probe must pass every
check on the first and fail every check on the second, so neither verdict can
be a constant.
"""
from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from adk.toolpacks.agent_ready import register
from adk.toolpacks.agent_ready import tools as ar

SKILL = b"---\nname: demo\ndescription: d\n---\n# Demo\n"
ISSUER = {"issuer": "https://idp.example", "authorization_endpoint": "a", "token_endpoint": "t"}

GOOD = {
    "/robots.txt": ("text/plain", b"User-agent: *\nContent-Signal: search=yes, ai-train=no\n"),
    "/.well-known/api-catalog": ("application/linkset+json",
                                 json.dumps({"linkset": [{"anchor": "https://api.example"}]}).encode()),
    "/.well-known/openid-configuration": ("application/json", json.dumps(ISSUER).encode()),
    "/.well-known/oauth-protected-resource": (
        "application/json",
        json.dumps({"resource": "x", "authorization_servers": ["https://idp.example"]}).encode()),
    "/auth.md": ("text/markdown", b"# Example auth.md\n"),
    "/.well-known/mcp/server-card.json": ("application/json",
                                          json.dumps({"serverInfo": {"name": "s"}}).encode()),
    "/.well-known/agent-card.json": ("application/json",
                                     json.dumps({"name": "a", "url": "https://a2a.example"}).encode()),
    "/.well-known/agent-skills/index.json": ("application/json", json.dumps({"skills": [{
        "name": "demo", "type": "skill-md", "description": "d", "url": "u",
        "digest": "sha256:" + hashlib.sha256(SKILL).hexdigest()}]}).encode()),
}


class _Handler(BaseHTTPRequestHandler):
    good = True

    def log_message(self, *_a):  # keep pytest output clean
        return

    def do_GET(self):  # noqa: N802 — http.server API
        if not self.good:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>404</h1>")
            return
        if self.path == "/":
            md = "text/markdown" in (self.headers.get("Accept") or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown" if md else "text/html")
            self.send_header("Link", '</.well-known/api-catalog>; rel="api-catalog"')
            self.end_headers()
            self.wfile.write(b"# Home\n" if md else b"<h1>Home</h1>")
            return
        hit = GOOD.get(self.path)
        if not hit:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", hit[0])
        self.end_headers()
        self.wfile.write(hit[1])


@pytest.fixture(params=[True, False], ids=["agent-ready-site", "bare-site"])
def site(request):
    handler = type("H", (_Handler,), {"good": request.param})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    yield request.param, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_probe_verdicts_follow_the_site(site):
    good, url = site
    out = ar.agent_ready_probe(url)
    statuses = {k: v["status"] for k, v in out["checks"].items()}
    if good:
        assert all(s == "pass" for s in statuses.values()), statuses
        assert out["passed"] == out["total"] == 10
    else:
        assert all(s == "fail" for s in statuses.values()), statuses
        assert all(v["fix"] for v in out["checks"].values())


def test_unreachable_site_is_a_verdict_not_a_crash():
    out = ar.agent_ready_probe("http://127.0.0.1:9")  # discard port: nothing listens
    assert out["passed"] == 0
    assert "unreachable" in out["checks"]["linkHeaders"]["evidence"]


def test_worker_template_ships_with_the_pack():
    t = ar.agent_ready_worker_template()
    assert "export default" in t["source"] and "wantsMarkdown" in t["source"]
    assert "proxied" in " ".join(t["notes"]).lower()


def test_register_counts_every_tool():
    class Reg:
        def __init__(self):
            self.names = []

        def register(self, fn):
            self.names.append(fn.__name__)

    r = Reg()
    assert register(r) == 3
    assert sorted(r.names) == ["agent_ready_probe", "agent_ready_scan", "agent_ready_worker_template"]
