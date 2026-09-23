"""A self-hosted Bonsai (install-bonsai.sh :8080, `adk bonsai-local` :8090) must be
found by the agent path without the user typing `adk backend set`.

Measured 2026-09-12: LLMRouter scanned 8120/8200-8203/8000, `adk status` probed
8209, `adk up` accepted llama-server's /health as its own, and
local_inference.discover_local_endpoint() was called by nothing. These tests pin
the contract: one ladder (SELFHOST_PORTS), /v1/models-gated, honored by the
router, the status command, the up-preflight, and the daemon health poll.

Uses a real loopback HTTP server; no pytest-asyncio needed (asyncio.run).
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _serve(models: list[str] | None, health_body: dict) -> tuple[HTTPServer, int]:
    """Loopback server: /v1/models lists `models` (404 if None), /health returns health_body."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):  # silence
            pass

        def _json(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/v1/models":
                if models is None:
                    self._json(404, {"error": "no models route"})
                else:
                    self._json(200, {"object": "list", "data": [{"id": m} for m in models]})
            elif self.path == "/health":
                self._json(200, health_body)
            else:
                self._json(404, {})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture
def bonsai_like():
    """Looks exactly like llama-server from install-bonsai.sh."""
    srv, port = _serve(["bonsai-selfhost"], {"status": "ok"})
    yield port
    srv.shutdown()


@pytest.fixture
def health_only():
    """A dev web server: /health 200, no /v1/models. Must NOT be adopted."""
    srv, port = _serve(None, {"status": "ok"})
    yield port
    srv.shutdown()


def test_probe_requires_model_for_port_scan(bonsai_like, health_only):
    from adk.local_inference import _probe_endpoint

    ok, model = asyncio.run(_probe_endpoint(f"http://127.0.0.1:{bonsai_like}", require_model=True))
    assert ok and model == "bonsai-selfhost"

    ok, _ = asyncio.run(_probe_endpoint(f"http://127.0.0.1:{health_only}", require_model=True))
    assert not ok, "a /health-only server must not pass the port scan"

    ok, _ = asyncio.run(_probe_endpoint(f"http://127.0.0.1:{health_only}", require_model=False))
    assert ok, "explicit URLs may still accept /health (env override path)"


def test_discover_walks_selfhost_ladder(monkeypatch, bonsai_like, health_only):
    import adk.local_inference as li

    monkeypatch.delenv("AITHER_LOCAL_LLM_URL", raising=False)
    # health-only port first: must be skipped; bonsai port second: must win.
    monkeypatch.setattr(li, "SELFHOST_PORTS", (health_only, bonsai_like))
    found = asyncio.run(li.discover_local_endpoint())
    assert found.found
    assert found.source == "port"
    assert found.endpoint_url == f"http://127.0.0.1:{bonsai_like}"
    assert found.model == "bonsai-selfhost"


def test_router_adopts_selfhost_before_vllm_scan(monkeypatch, bonsai_like):
    import adk.local_inference as li
    from adk.llm import LLMRouter

    monkeypatch.delenv("AITHER_LOCAL_LLM_URL", raising=False)
    monkeypatch.setattr(li, "SELFHOST_PORTS", (bonsai_like,))

    class _Cfg:  # minimal Config stand-in: nothing explicit, no keys
        llm_backend = "auto"
        llm_base_url = ""
        aither_api_key = ""
        cloud_mode = ""
        vllm_extra_ports = ""
        dgx_url = ""
        inference_url = ""
        core_llm_url = ""
        ollama_host = ""
        anthropic_api_key = ""
        openai_api_key = ""
        openai_base_url = ""
        deepseek_api_key = ""

    router = LLMRouter(config=_Cfg())
    provider = asyncio.run(router._try_local_selfhost())
    assert provider is not None
    assert provider.base_url.rstrip("/") == f"http://127.0.0.1:{bonsai_like}/v1"
    assert provider.default_model == "bonsai-selfhost"
    assert router._provider_name == "local"


def test_router_bonsai_aliases_resolve():
    from adk.llm import LLMRouter

    r = LLMRouter(config=None)
    p = r._create_provider("bonsai-local")
    assert p.base_url.rstrip("/") == "http://127.0.0.1:8090/v1"
    assert p.default_model == "bonsai-27b"
    p = r._create_provider("bonsai", base_url="http://127.0.0.1:8081/v1")
    assert p.base_url.rstrip("/") == "http://127.0.0.1:8081/v1"
    assert p.default_model == "bonsai-selfhost"


def test_daemon_health_rejects_llama_server(bonsai_like):
    """`adk up --port 8080` on a Bonsai box must not call llama-server 'healthy'."""
    from adk import agent_daemon as d

    assert d.is_adk_health({"status": "healthy", "agent": "aither", "version": "3.8.17"})
    assert not d.is_adk_health({"status": "ok"})
    assert d.port_owner(bonsai_like) == "other"
    assert d.wait_for_health(bonsai_like, timeout=0.1) is False


def test_preflight_sees_configured_backend(monkeypatch, bonsai_like):
    from adk import shell_launcher as sl

    monkeypatch.delenv("AITHER_LLM_BASE_URL", raising=False)
    monkeypatch.setattr(
        "adk.config.load_saved_config",
        lambda: {"default_backend": "vllm", "inference_url": f"http://127.0.0.1:{bonsai_like}/v1"},
    )
    ok, desc = sl._preflight_check()
    assert ok and desc.startswith("configured:vllm")


def test_login_keeps_user_backend_url(monkeypatch):
    """A re-login must not rewrite inference_url to the cloud when the user set a backend."""
    import adk.cli as cli

    saved_calls: list[dict] = []
    monkeypatch.setattr(cli, "load_saved_config", lambda: {
        "default_backend": "vllm", "inference_url": "http://127.0.0.1:8080/v1",
    })
    monkeypatch.setattr(cli, "save_saved_config", lambda d: saved_calls.append(dict(d)))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: __import__("pathlib").Path("/nonexistent-home-for-test")))

    cli._persist_workspace_endpoints("https://idp.aitherium.com")
    assert saved_calls, "endpoints should still be saved"
    data = saved_calls[0]
    assert "inference_url" not in data
    assert data["gateway_inference_url"] == "https://mcp.aitherium.com/v1"
    assert data["mcp_url"] == "https://mcp.aitherium.com/mcp"


def test_login_sets_url_on_unconfigured_box(monkeypatch):
    import adk.cli as cli

    saved_calls: list[dict] = []
    monkeypatch.setattr(cli, "load_saved_config", lambda: {})
    monkeypatch.setattr(cli, "save_saved_config", lambda d: saved_calls.append(dict(d)))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: __import__("pathlib").Path("/nonexistent-home-for-test")))

    cli._persist_workspace_endpoints("https://idp.aitherium.com")
    assert saved_calls[0]["inference_url"] == "https://mcp.aitherium.com/v1"


def _bare_cfg():
    class _Cfg:  # minimal Config stand-in: nothing explicit, no keys
        llm_backend = "auto"
        llm_base_url = ""
        aither_api_key = ""
        cloud_mode = ""
        vllm_extra_ports = ""
        dgx_url = ""
        inference_url = ""
        core_llm_url = ""
        ollama_host = ""
        anthropic_api_key = ""
        openai_api_key = ""
        openai_base_url = ""
        deepseek_api_key = ""

    return _Cfg()


def test_router_skips_selfhost_that_lacks_the_configured_model(monkeypatch, bonsai_like):
    """Measured 2026-09-23: spine unreachable, discovery adopted Ollama and asked it
    for bonsai2-27b on every turn -> 'model not found' -> 'empty completion'."""
    import adk.local_inference as li
    from adk.llm import LLMRouter

    monkeypatch.delenv("AITHER_LOCAL_LLM_URL", raising=False)
    monkeypatch.setattr(li, "SELFHOST_PORTS", (bonsai_like,))
    router = LLMRouter(config=_bare_cfg(), model="bonsai2-27b")
    assert asyncio.run(router._try_local_selfhost()) is None


def test_router_keeps_selfhost_that_serves_the_configured_model(monkeypatch, bonsai_like):
    import adk.local_inference as li
    from adk.llm import LLMRouter

    monkeypatch.delenv("AITHER_LOCAL_LLM_URL", raising=False)
    monkeypatch.setattr(li, "SELFHOST_PORTS", (bonsai_like,))
    router = LLMRouter(config=_bare_cfg(), model="bonsai-selfhost")
    provider = asyncio.run(router._try_local_selfhost())
    assert provider is not None and provider.default_model == "bonsai-selfhost"
