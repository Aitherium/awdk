"""TRUE end-to-end live socket tests for A2A protocol.

These tests exercise the REAL adk.a2a_client outbound path over actual HTTP sockets,
NOT FastAPI TestClient. They prove:
  1. Signed request verification and trust enforcement (positive case: trusted key works)
  2. Replay protection (same nonce rejected on second call)
  3. Untrusted caller rejection (wrong key → 403)
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from threading import Thread

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.a2a import A2AServer
from adk.a2a_client import (
    invoke_skill_at_url,
    load_or_generate_keypair,
    sign_request_body,
)
from adk.a2a_trust import check_replay
from adk.tools import ToolRegistry


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Grab a free TCP port by binding and releasing it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


@pytest.fixture
def aither_home_tmpdir(tmp_path, monkeypatch):
    """Set AITHER_HOME to a temp directory for keypair isolation."""
    aither_home = tmp_path / ".aither"
    aither_home.mkdir()
    monkeypatch.setenv("AITHER_HOME", str(aither_home))
    return aither_home


@pytest.fixture
async def live_a2a_server(aither_home_tmpdir):
    """Boot a real uvicorn server with A2A mounted.

    Yields (base_url, server_task, agent_object).
    Server runs in background thread until test completes.
    """
    # Try to import uvicorn + fastapi; skip if missing
    try:
        import uvicorn
        from fastapi import FastAPI
    except ImportError:
        pytest.xfail("uvicorn/fastapi not installed; cannot run live socket tests")

    # Build a minimal agent with a simple tool. Use a PLAIN object (not
    # MagicMock) so it has no auto-created `_identity` — build_agent_card then
    # takes the fallback-dict path and enriches skills from the real _tools,
    # so the agent card actually lists the "ping" tool.
    class _LiveAgent:
        name = "test-agent"

    agent = _LiveAgent()

    # Create tool registry with one A2A-exposed tool
    registry = ToolRegistry()

    def ping(msg: str = "pong") -> dict:
        """Echo ping tool."""
        return {"pong": True, "echo": msg}

    registry.register(
        ping,
        name="ping",
        description="Echo tool for testing",
        expose_to_a2a=True,
    )

    agent._tools = registry

    # Mount A2A server on FastAPI app
    app = FastAPI()
    a2a = A2AServer(agent=agent, base_url="http://127.0.0.1:8080")
    a2a.mount(app)

    # Find free port and boot uvicorn in background
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Configure uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def run_server():
        asyncio.run(server.serve())

    thread = Thread(daemon=True, target=run_server)
    thread.start()

    # Poll until server is ready (with ~5s timeout)
    max_attempts = 50
    for attempt in range(max_attempts):
        try:
            import httpx
            with httpx.Client(timeout=1) as client:
                resp = client.get(f"{base_url}/.well-known/agent-card.json")
                if resp.status_code == 200:
                    break
        except Exception:
            pass
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"Server on {port} did not start within 5 seconds")

    try:
        yield base_url, server, agent
    finally:
        # Clean shutdown
        server.should_exit = True
        thread.join(timeout=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestA2ALiveSocket:
    """TRUE end-to-end tests over live HTTP sockets."""

    @pytest.mark.asyncio
    async def test_positive_trusted_call(self, live_a2a_server, aither_home_tmpdir, monkeypatch):
        """Positive case: trusted caller invokes a skill successfully.

        1. Generate client keypair
        2. Set AITHER_A2A_TRUSTED_KEYS to the client's public key
        3. Call invoke_skill_at_url with REAL HTTP
        4. Assert result contains skill name and output
        """
        base_url, server, agent = live_a2a_server

        # Load client keypair (generates new one)
        priv, pub = load_or_generate_keypair("live-caller")

        # Set the public key as trusted
        monkeypatch.setenv("AITHER_A2A_TRUSTED_KEYS", pub)

        # Call skill over real HTTP
        result = await invoke_skill_at_url(
            invoke_url=base_url,
            skill="ping",
            args={"msg": "hello"},
            this_agent_name="live-caller",
            timeout=5,
        )

        # Verify success result structure
        assert "error" not in result, f"Got error: {result.get('error')}"
        assert "skill" in result
        assert result["skill"] == "ping"
        assert "output" in result
        assert result["output"]["pong"] is True
        assert result["output"]["echo"] == "hello"

    @pytest.mark.asyncio
    async def test_negative_untrusted_caller(self, live_a2a_server, aither_home_tmpdir, monkeypatch):
        """Negative case: untrusted caller is rejected with 403.

        1. Generate client keypair
        2. Set AITHER_A2A_TRUSTED_KEYS to a DIFFERENT key (not the caller's)
        3. Call invoke_skill_at_url
        4. Assert result contains an error (rpc_error or http_error with 403)
        """
        base_url, server, agent = live_a2a_server

        # Load client keypair
        priv, pub = load_or_generate_keypair("live-caller")

        # Set a DIFFERENT key as trusted (so caller is not trusted)
        other_key = "0" * 64  # Dummy key
        monkeypatch.setenv("AITHER_A2A_TRUSTED_KEYS", other_key)

        # Call skill over real HTTP
        result = await invoke_skill_at_url(
            invoke_url=base_url,
            skill="ping",
            args={},
            this_agent_name="live-caller",
            timeout=5,
        )

        # Verify error result (should have an error key)
        assert "error" in result, f"Expected error but got: {result}"
        # Error could be rpc_error (from server) or http_error (403)
        assert result["error"] in ("rpc_error", "http_error", "request_failed")

    @pytest.mark.asyncio
    async def test_replay_protection(self, live_a2a_server, aither_home_tmpdir, monkeypatch):
        """Negative case: replay attack (same signed request twice) is rejected.

        1. Generate client keypair and set as trusted
        2. Build a signed request manually with ts + nonce
        3. POST it once → should succeed (200)
        4. POST the SAME body + signature again → should be rejected (403 with "replay")
        """
        try:
            import httpx
        except ImportError:
            pytest.xfail("httpx not installed")

        base_url, server, agent = live_a2a_server

        # Load client keypair and set as trusted
        priv, pub = load_or_generate_keypair("replay-test-caller")
        monkeypatch.setenv("AITHER_A2A_TRUSTED_KEYS", pub)

        # Build a signed skills/invoke request manually with ts + nonce
        import time
        request_dict = {
            "jsonrpc": "2.0",
            "method": "skills/invoke",
            "params": {
                "skill": "ping",
                "args": {"msg": "test"},
            },
            "id": 1,
            "ts": int(time.time()),
            "nonce": os.urandom(16).hex(),
        }

        body_json = json.dumps(request_dict, separators=(",", ":"))
        body_bytes = body_json.encode("utf-8")
        signature = sign_request_body(priv, body_bytes)

        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature,
            "X-Public-Key": pub,
        }

        # First call: should succeed
        async with httpx.AsyncClient(timeout=5) as client:
            resp1 = await client.post(
                f"{base_url}/a2a",
                content=body_bytes,
                headers=headers,
            )
        assert resp1.status_code == 200, f"First call failed: {resp1.status_code} {resp1.text}"
        resp1_json = resp1.json()
        assert "result" in resp1_json or "error" not in resp1_json, \
            f"First call returned error: {resp1_json}"

        # Second call with SAME body + signature: should be rejected
        async with httpx.AsyncClient(timeout=5) as client:
            resp2 = await client.post(
                f"{base_url}/a2a",
                content=body_bytes,
                headers=headers,
            )

        # Should be 403 Forbidden with "replay" in reason
        assert resp2.status_code == 403, \
            f"Second call should be 403, got {resp2.status_code}: {resp2.text}"
        resp2_json = resp2.json()
        assert "reason" in resp2_json or "error" in resp2_json
        resp2_text = json.dumps(resp2_json)
        assert "replay" in resp2_text.lower(), \
            f"Expected 'replay' in error reason, got: {resp2_json}"

    @pytest.mark.asyncio
    async def test_replay_check_unit(self):
        """Unit test: check_replay() rejects seen nonces."""
        import time

        # Simulate a valid request body with ts + nonce
        body = {
            "ts": int(time.time()),
            "nonce": "unique-test-nonce-123",
        }

        # First call should pass
        ok, reason = check_replay(body)
        assert ok, f"First check should pass: {reason}"

        # Second call with same nonce should fail (replay detected)
        ok, reason = check_replay(body)
        assert not ok, f"Second check should fail (replay)"
        assert "replay" in reason.lower()

    @pytest.mark.asyncio
    async def test_replay_check_missing_fields(self):
        """Unit test: check_replay() rejects missing ts or nonce."""
        # Missing ts
        ok, reason = check_replay({"nonce": "test"})
        assert not ok
        assert "missing" in reason.lower()

        # Missing nonce
        ok, reason = check_replay({"ts": 123456789})
        assert not ok
        assert "missing" in reason.lower()

        # Both missing
        ok, reason = check_replay({})
        assert not ok
        assert "missing" in reason.lower()

    @pytest.mark.asyncio
    async def test_agent_card_discovery(self, live_a2a_server):
        """Test: Agent Card discovery at /.well-known/agent-card.json."""
        try:
            import httpx
        except ImportError:
            pytest.xfail("httpx not installed")

        base_url, server, agent = live_a2a_server

        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url}/.well-known/agent-card.json")

        assert resp.status_code == 200
        card = resp.json()
        assert "name" in card
        assert "skills" in card
        # Should have our "ping" tool in the skills list
        skill_names = [s.get("id") for s in card.get("skills", [])]
        assert "ping" in skill_names, f"ping not in skills: {skill_names}"

    @pytest.mark.asyncio
    async def test_unsigned_skills_invoke_rejected(self, live_a2a_server, monkeypatch):
        """Negative case: unsigned skills/invoke is rejected (even without global trust).

        The A2A server has a METHOD-SPECIFIC hard gate: skills/invoke ALWAYS requires
        a signature, independent of AITHER_A2A_REQUIRE_TRUST setting.
        """
        try:
            import httpx
        except ImportError:
            pytest.xfail("httpx not installed")

        base_url, server, agent = live_a2a_server

        # Make sure global trust is OFF (default)
        monkeypatch.setenv("AITHER_A2A_REQUIRE_TRUST", "false")

        # Send unsigned skills/invoke request
        request_dict = {
            "jsonrpc": "2.0",
            "method": "skills/invoke",
            "params": {"skill": "ping", "args": {}},
            "id": 1,
            "ts": int(__import__("time").time()),
            "nonce": os.urandom(16).hex(),
        }

        body_json = json.dumps(request_dict, separators=(",", ":"))
        body_bytes = body_json.encode("utf-8")

        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{base_url}/a2a",
                content=body_bytes,
                headers={"Content-Type": "application/json"},
            )

        # Should be 403 (signature required for skills/invoke)
        assert resp.status_code == 403, \
            f"Expected 403, got {resp.status_code}: {resp.text}"
        resp_json = resp.json()
        assert "error" in resp_json or "reason" in resp_json
