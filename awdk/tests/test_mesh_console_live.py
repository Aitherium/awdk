"""LIVE proof that the central console admin proxy forwards CONFIG MANAGEMENT to
a REAL remote agent over a socket (not a mock).

Boots a second agent's /admin API on a real port, points discovery at it, then
drives the console proxy (/mesh/agents/{name}/admin/*) end-to-end:
  - GET  llm/status  -> forwarded to the remote, returns its backend/model
  - POST llm/keys    -> sets a key on the remote; response is MASKED (value never echoed)
  - POST cli/exec    -> denied by the allowlist, never forwarded
"""

import asyncio
import json
import socket
import sys
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def remote_agent_server(tmp_path, monkeypatch):
    """A real second agent whose /admin/llm/* API is served over a live socket."""
    try:
        import uvicorn
        from fastapi import FastAPI
    except ImportError:
        pytest.xfail("uvicorn/fastapi not installed")
    from adk.admin_api import register_admin_routes

    home = tmp_path / ".aither"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AITHER_HOME", str(home))

    # Stateful fake LLM so a proxied backend-switch actually takes effect
    # (MagicMock.switch_backend would be an inert no-op and prove nothing).
    class _FakeLLM:
        def __init__(self):
            self.provider_name = "llamacpp"
            self._model = "Bonsai-27B"

        def switch_backend(self, provider, base_url=None, api_key=None, model=None):
            self.provider_name = provider
            if model:
                self._model = model

    agent = MagicMock()
    agent.name = "remote-peer"
    agent.llm = _FakeLLM()

    async def get_agent():
        return agent

    app = FastAPI()
    register_admin_routes(app, get_agent=get_agent, state={"config": None})

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = Thread(daemon=True, target=lambda: asyncio.run(server.serve()))
    thread.start()

    # Poll for the socket to accept requests. Use a route that does NOT trigger
    # the local-backend preflight probe (llm/status does, and it can take >1s,
    # timing out the readiness poll). Any HTTP response == server is up.
    import httpx
    for _ in range(50):
        try:
            httpx.get(f"{base}/admin/meta", timeout=1)
            break
        except Exception:
            pass
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"remote agent did not start on {port}")

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=2)


class TestMeshConsoleLive:
    async def test_console_proxies_config_to_remote(self, remote_agent_server, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        base = remote_agent_server

        # Point discovery at the running remote agent; keep it fast + hermetic.
        agents = tmp_path / "agents.json"
        agents.write_text(json.dumps({"agents": {"remote-peer": {
            "name": "remote-peer", "invoke_url": base, "provider_hint": "llamacpp",
            "model": "Bonsai-27B", "reach": "lan", "status": "online"}}}), encoding="utf-8")
        monkeypatch.setenv("AITHER_AGENTS_FILE", str(agents))
        monkeypatch.setenv("AITHER_PORTAL_URL", "http://127.0.0.1:9")  # registry fast-fails
        import adk.mesh_discovery as md
        monkeypatch.setattr(md, "_owner_token", lambda: "")  # skip cloud registry

        # Build the bearer-gated console server.
        import os as _os
        with patch.dict(_os.environ, {"AITHER_SERVER_API_KEY": "consolekey"}, clear=False):
            from adk.server import create_app
            from adk.config import Config
            cfg = Config()
            cfg.gateway_url = ""
            cfg.aither_api_key = ""
            ca = MagicMock()
            ca.name = "console"
            ca.llm = MagicMock()
            ca.llm.provider_name = "mock"
            ca._identity = MagicMock()
            ca._identity.name = "console"
            ca._identity.description = ""
            ca._identity.skills = []
            ca._tools = MagicMock()
            ca._tools.list_tools = MagicMock(return_value=[])
            ca._safety = None
            capp = create_app(agent=ca, identity="console", config=cfg)

        client = TestClient(capp)
        h = {"Authorization": "Bearer consolekey"}

        # 1) GET llm/status forwards to the REAL remote agent over a socket.
        r = client.get("/mesh/agents/remote-peer/admin/llm/status", headers=h)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["active_backend"] == "llamacpp"
        assert j["model"] == "Bonsai-27B"

        # 2) POST llm/keys sets a key on the remote agent; value is masked, never echoed.
        secret = "sk-secret-value-123456"
        r2 = client.post("/mesh/agents/remote-peer/admin/llm/keys", headers=h,
                         json={"provider": "openai", "api_key": secret})
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2["ok"] is True and j2["provider"] == "openai"
        assert secret not in json.dumps(j2), "raw key value leaked in response"
        assert j2.get("key_preview") and j2["key_preview"] != secret

        # 3) After setting it, llm/status reports the key as present (masked preview).
        r3 = client.get("/mesh/agents/remote-peer/admin/llm/status", headers=h)
        openai = [p for p in r3.json().get("providers", []) if p["id"] == "openai"]
        assert openai and openai[0]["has_key"] is True
        assert secret not in r3.text

        # 4) POST llm/switch flips the REMOTE agent's backend local->cloud, live.
        r_sw = client.post("/mesh/agents/remote-peer/admin/llm/switch", headers=h,
                           json={"provider": "openai", "model": "gpt-4o", "persist": False})
        assert r_sw.status_code == 200, r_sw.text
        assert r_sw.json()["active_backend"] == "openai"
        # Confirm the switch stuck on the remote (via a fresh forwarded status).
        r_after = client.get("/mesh/agents/remote-peer/admin/llm/status", headers=h)
        assert r_after.json()["active_backend"] == "openai"
        assert r_after.json()["model"] == "gpt-4o"

        # 5) cli/exec through the console is denied by the allowlist (never forwarded).
        r5 = client.post("/mesh/agents/remote-peer/admin/cli/exec", headers=h, json={"cmd": "whoami"})
        assert r5.status_code == 403
        assert "not permitted" in r5.text
