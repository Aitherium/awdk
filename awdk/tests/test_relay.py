"""Tests for the AitherNet relay mesh system."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.relay import AitherNetRelay, NodeInfo, RelayMessage, get_relay


# ── NodeInfo ─────────────────────────────────────────────────────────────


class TestNodeInfo:
    def test_create_default(self):
        node = NodeInfo(node_id="abc", name="test-node")
        assert node.node_id == "abc"
        assert node.capabilities == []
        assert node.agents == []
        assert node.gpu_count == 0
        assert node.status == "online"

    def test_create_full(self):
        node = NodeInfo(
            node_id="abc", name="gpu-node",
            capabilities=["inference", "gpu"],
            agents=["demiurge", "athena"],
            gpu_count=2, host="10.0.0.5", port=8080,
        )
        assert node.gpu_count == 2
        assert "inference" in node.capabilities


# ── Relay Creation ───────────────────────────────────────────────────────


class TestRelayCreation:
    def test_create_default(self):
        relay = AitherNetRelay()
        assert relay.api_key == ""
        assert relay.node_name != ""
        assert relay.node_id != ""  # Auto-generated
        assert relay.is_registered is False

    def test_create_with_api_key(self):
        relay = AitherNetRelay(api_key="aither_sk_live_test")
        assert relay.api_key == "aither_sk_live_test"

    def test_create_from_env(self):
        with patch.dict(os.environ, {"AITHER_API_KEY": "env_key"}):
            relay = AitherNetRelay()
            assert relay.api_key == "env_key"

    def test_node_id_persistent(self, tmp_path):
        relay1 = AitherNetRelay(data_dir=tmp_path)
        node_id = relay1.node_id

        relay2 = AitherNetRelay(data_dir=tmp_path)
        assert relay2.node_id == node_id

    def test_custom_capabilities(self):
        relay = AitherNetRelay(capabilities=["inference", "gpu"], agents=["aither"])
        assert relay.capabilities == ["inference", "gpu"]
        assert relay.agents == ["aither"]


# ── Registration ─────────────────────────────────────────────────────────


class TestRelayRegistration:
    @pytest.mark.asyncio
    async def test_register_success(self):
        relay = AitherNetRelay(api_key="test_key", node_name="test-node")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "node_id": relay.node_id}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await relay.register()

        assert result["ok"] is True
        assert relay.is_registered is True

    @pytest.mark.asyncio
    async def test_register_failure(self):
        relay = AitherNetRelay(api_key="test_key")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=Exception("network error"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            result = await relay.register()

        assert result["ok"] is False
        assert relay.is_registered is False


# ── Heartbeat ────────────────────────────────────────────────────────────


class TestRelayHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_success(self):
        relay = AitherNetRelay(api_key="test_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            assert await relay.heartbeat() is True

    @pytest.mark.asyncio
    async def test_heartbeat_failure(self):
        relay = AitherNetRelay(api_key="test_key")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=Exception("timeout"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            assert await relay.heartbeat() is False


# ── Discovery ────────────────────────────────────────────────────────────


class TestRelayDiscovery:
    @pytest.mark.asyncio
    async def test_discover_nodes(self):
        relay = AitherNetRelay(api_key="test_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "nodes": [
                {"node_id": "n1", "name": "gpu-server", "gpu_count": 2,
                 "capabilities": ["inference"], "agents": ["aither"], "status": "online"},
                {"node_id": "n2", "name": "laptop", "gpu_count": 0,
                 "capabilities": ["chat"], "agents": ["lyra"], "status": "online"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            nodes = await relay.discover()

        assert len(nodes) == 2
        assert isinstance(nodes[0], NodeInfo)
        assert nodes[0].name == "gpu-server"
        assert nodes[0].gpu_count == 2

    @pytest.mark.asyncio
    async def test_discover_empty(self):
        relay = AitherNetRelay(api_key="test_key")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=Exception("timeout"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            nodes = await relay.discover()
            assert nodes == []

    @pytest.mark.asyncio
    async def test_find_inference_nodes(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.discover = AsyncMock(return_value=[
            NodeInfo(node_id="n1", name="gpu", capabilities=["inference"]),
        ])
        nodes = await relay.find_inference_nodes()
        relay.discover.assert_called_once_with(capability="inference")
        assert len(nodes) == 1

    @pytest.mark.asyncio
    async def test_find_agent(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.discover = AsyncMock(return_value=[
            NodeInfo(node_id="n1", name="node1", agents=["aither", "lyra"]),
            NodeInfo(node_id="n2", name="node2", agents=["demiurge"]),
        ])
        node = await relay.find_agent("demiurge")
        assert node is not None
        assert node.node_id == "n2"

    @pytest.mark.asyncio
    async def test_find_agent_not_found(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.discover = AsyncMock(return_value=[
            NodeInfo(node_id="n1", name="node1", agents=["aither"]),
        ])
        node = await relay.find_agent("nonexistent")
        assert node is None


# ── Message Relay ────────────────────────────────────────────────────────


class TestMessageRelay:
    @pytest.mark.asyncio
    async def test_send_message(self):
        relay = AitherNetRelay(api_key="test_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            ok = await relay.send("target_node", "chat", {"content": "hello"})
            assert ok is True

    @pytest.mark.asyncio
    async def test_send_failure(self):
        relay = AitherNetRelay(api_key="test_key")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=Exception("error"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            ok = await relay.send("target", "chat", {})
            assert ok is False

    @pytest.mark.asyncio
    async def test_broadcast(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.send = AsyncMock(return_value=True)
        ok = await relay.broadcast("announcement", {"msg": "hello all"})
        relay.send.assert_called_once_with("*", "announcement", {"msg": "hello all"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_poll_messages(self):
        relay = AitherNetRelay(api_key="test_key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "messages": [
                {"msg_id": "m1", "from_node": "n1", "to_node": relay.node_id,
                 "msg_type": "chat", "payload": {"content": "hello"}, "timestamp": time.time()},
            ]
        }

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            messages = await relay.poll_messages()

        assert len(messages) == 1
        assert isinstance(messages[0], RelayMessage)
        assert messages[0].msg_type == "chat"


# ── Inference Relay ──────────────────────────────────────────────────────


class TestInferenceRelay:
    @pytest.mark.asyncio
    async def test_relay_inference_auto_discover(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.find_inference_nodes = AsyncMock(return_value=[
            NodeInfo(node_id="gpu-1", name="gpu-server", capabilities=["inference"]),
        ])

        fixed_id = "test-req-id-12345"

        async def fake_send(to_node, msg_type, payload):
            # Simulate remote node responding by seeding the pending responses
            relay._pending_responses[fixed_id] = {
                "ok": True,
                "relayed_to": to_node,
                "_request_id": fixed_id,
            }
            return True

        relay.send = AsyncMock(side_effect=fake_send)
        relay.poll_messages = AsyncMock(return_value=[])

        with patch("adk.relay.uuid.uuid4", return_value=type("FakeUUID", (), {"__str__": lambda s: fixed_id, "hex": "a" * 32})()):
            result = await relay.relay_inference(
                messages=[{"role": "user", "content": "hello"}],
                model="aither-orchestrator",
            )

        assert result["ok"] is True
        assert result["relayed_to"] == "gpu-1"
        relay.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_relay_inference_no_gpu_nodes(self):
        relay = AitherNetRelay(api_key="test_key")
        relay.find_inference_nodes = AsyncMock(return_value=[])

        result = await relay.relay_inference(messages=[{"role": "user", "content": "hello"}])
        assert result["ok"] is False
        assert result["error"] == "no_inference_nodes"


# ── Handler Registration ────────────────────────────────────────────────


class TestHandlers:
    @pytest.mark.asyncio
    async def test_register_handler(self):
        relay = AitherNetRelay()
        received = []

        def on_chat(msg):
            received.append(msg)

        relay.on("chat", on_chat)
        await relay._handle_relay_message({"msg_type": "chat", "content": "hi"})
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_async_handler(self):
        relay = AitherNetRelay()
        received = []

        async def on_inference(msg):
            received.append(msg)

        relay.on("inference", on_inference)
        await relay._handle_relay_message({"msg_type": "inference", "model": "aither-small"})
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_handler_error_nonfatal(self):
        relay = AitherNetRelay()

        def bad_handler(msg):
            raise RuntimeError("handler exploded")

        relay.on("chat", bad_handler)
        # Should not raise
        await relay._handle_relay_message({"msg_type": "chat"})


# ── Status ───────────────────────────────────────────────────────────────


class TestRelayStatus:
    def test_status(self):
        relay = AitherNetRelay(
            api_key="test", capabilities=["chat", "gpu"], agents=["aither"],
        )
        s = relay.status()
        assert s["registered"] is False
        assert s["heartbeat_active"] is False
        assert s["relay_hub_connected"] is False
        assert "chat" in s["capabilities"]
        assert "aither" in s["agents"]


# ── Singleton ────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_relay_singleton(self):
        import adk.relay as relay_mod
        relay_mod._relay = None  # Reset

        r1 = get_relay(api_key="test1")
        r2 = get_relay(api_key="test2")  # Should return same instance
        assert r1 is r2
        assert r1.api_key == "test1"

        relay_mod._relay = None  # Cleanup


# ── Export ───────────────────────────────────────────────────────────────


class TestExport:
    def test_exports(self):
        import adk
        assert hasattr(adk, "AitherNetRelay")
