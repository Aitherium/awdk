"""
AitherNet Relay — Turn any ADK node into a mesh relay.
========================================================

When an ADK server starts and phones home, it automatically registers
as a relay node on the AitherNet mesh. This enables:

- Node discovery: find other nodes by capability
- Message relay: forward chat/commands through the mesh
- Inference relay: route LLM requests to nodes with GPUs
- Agent relay: discover and call agents on remote nodes
- Heartbeat: maintain presence in the mesh

Architecture::

    awnode (laptop)                awnode (GPU server)
        |                                   |
        | register + heartbeat              | register + heartbeat
        v                                   v
    gateway.aitherium.com  <-- D1 node registry -->
        |                                   |
        | WebSocket relay                   |
        v                                   v
    demo.aitherium.com/ws/relay  (hub for NAT traversal)
        |
        +-- message relay (inter-node chat)
        +-- inference relay (laptop -> GPU node)
        +-- agent relay (cross-node agent calls)

Usage::

    from adk.relay import AitherNetRelay

    relay = AitherNetRelay(api_key="aither_sk_live_...")
    await relay.register()           # Register this node
    await relay.start_heartbeat()    # Background keepalive

    nodes = await relay.discover()   # Find other nodes
    await relay.send("node_abc", {"type": "inference", ...})  # Relay message
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("adk.relay")

_GATEWAY_URL = "https://gateway.aitherium.com"
_RELAY_HUB_URL = "wss://demo.aitherium.com/ws/relay"


@dataclass
class NodeInfo:
    """Information about a mesh node."""
    node_id: str
    name: str
    host: str = ""
    port: int = 0
    capabilities: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    gpu_count: int = 0
    status: str = "online"
    last_seen: str = ""
    version: str = ""
    os: str = ""


@dataclass
class RelayMessage:
    """Message relayed between nodes."""
    msg_id: str
    from_node: str
    to_node: str  # "*" for broadcast
    msg_type: str  # "chat", "inference", "agent_call", "ping", "pong"
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class AitherNetRelay:
    """AitherNet mesh relay client.

    Turns any ADK node into a participant in the AitherNet mesh:
    - Registers with gateway.aitherium.com node registry
    - Sends heartbeats to maintain presence
    - Connects to relay hub for NAT-traversed messaging
    - Handles incoming relay messages (inference, chat, agent calls)
    """

    def __init__(
        self,
        api_key: str = "",
        gateway_url: str = "",
        relay_hub_url: str = "",
        node_name: str = "",
        capabilities: list[str] | None = None,
        agents: list[str] | None = None,
        host: str = "",
        port: int = 0,
        data_dir: str | Path | None = None,
    ):
        self.api_key = api_key or os.getenv("AITHER_API_KEY", "")
        self.gateway_url = (gateway_url or os.getenv("AITHER_GATEWAY_URL", _GATEWAY_URL)).rstrip("/")
        # Support both AITHERNET_RELAY_URL (adk) and AITHERNET_HUB_URL (server-side),
        # with AITHERNET_RELAY_URL taking precedence for backward compat
        self.relay_hub_url = (
            relay_hub_url
            or os.getenv("AITHERNET_RELAY_URL")
            or os.getenv("AITHERNET_HUB_URL", _RELAY_HUB_URL)
        )

        # Override relay hub URL from saved config (for desktop direct relay)
        if (not relay_hub_url and not os.getenv("AITHERNET_RELAY_URL")
                and not os.getenv("AITHERNET_HUB_URL")):
            try:
                from adk.config import load_saved_config  # noqa: lazy import
                saved = load_saved_config()
                elysium_relay = saved.get("elysium_relay_url", "")
                if elysium_relay:
                    self.relay_hub_url = elysium_relay
            except (ImportError, OSError, ValueError):
                pass
        self.node_name = node_name or platform.node() or f"node-{secrets.token_hex(4)}"
        self.capabilities = capabilities or []
        self.agents = agents or []
        self.host = host
        self.port = port

        self._data_dir = Path(data_dir or Path.home() / ".aither")
        self._identity_path = self._data_dir / "node_identity.json"

        self._node_id: str = ""
        self._registered = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._relay_task: Optional[asyncio.Task] = None
        self._relay_ws: Any = None
        self._handlers: dict[str, list[Callable]] = {}
        self._start_time = time.time()
        self._pending_responses: dict[str, dict] = {}

        self._load_identity()

    def _load_identity(self):
        """Load or generate persistent node identity."""
        if self._identity_path.exists():
            try:
                data = json.loads(self._identity_path.read_text())
                self._node_id = data.get("node_id", "")
            except Exception:
                pass

        if not self._node_id:
            self._node_id = str(uuid.uuid4())
            self._save_identity()

    def _save_identity(self):
        """Persist node identity to disk."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._identity_path.write_text(json.dumps({
                "node_id": self._node_id,
                "node_name": self.node_name,
                "created_at": time.time(),
            }, indent=2))
        except Exception as e:
            logger.debug(f"Failed to save node identity: {e}")

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def is_registered(self) -> bool:
        return self._registered

    def _auth_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ─── Node Registration ─────────────────────────────────────────────

    async def register(self) -> dict:
        """Register this node with the gateway mesh registry.

        Returns the registration response including assigned node_id.
        """
        from adk import __version__

        payload = {
            "node_id": self._node_id,
            "name": self.node_name,
            "host": self.host,
            "port": self.port,
            "capabilities": self.capabilities,
            "agents": self.agents,
            "version": __version__,
            "os": platform.system(),
            "arch": platform.machine(),
            "gpu_count": self._detect_gpu_count(),
            "python_version": platform.python_version(),
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, headers=self._auth_headers()) as client:
                resp = await client.post(
                    f"{self.gateway_url}/v1/nodes/register",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            self._registered = True
            if data.get("node_id"):
                self._node_id = data["node_id"]
                self._save_identity()

            logger.info(
                "Registered on AitherNet as %s (node_id=%s)",
                self.node_name, self._node_id[:12],
            )

            bearer_token = data.get("bearer_token", "")
            if bearer_token:
                await self._self_mint_key(bearer_token)

            return data
        except Exception as e:
            logger.warning(f"AitherNet registration failed: {e}")
            return {"ok": False, "error": str(e)}

    async def _self_mint_key(self, bearer_token: str) -> None:
        """Self-service mint a node-scoped avk_ gateway key via AitherSecrets.

        Called right after a successful /v1/nodes/register that returns a
        tenant-scoped capability ``bearer_token`` (see identity_nodes.py's
        register_node). Trades that token for a real, revocable ``avk_...``
        key from AitherSecrets and switches this client to use it for all
        subsequent calls — replacing whatever access token the node started
        with. Best-effort: on any failure the node just keeps using its
        original ``self.api_key``.

        AitherSecrets (8111) is local-only (no public tunnel route) by
        design, so this always targets the local vault, not the gateway
        hostname — a remote (non-local) node would need a gateway-side proxy
        for /api-keys, which doesn't exist yet.
        """
        secrets_url = os.getenv("AITHER_SECRETS_URL", "http://localhost:8111")
        try:
            from adk._tls import tls_verify
            async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
                resp = await client.post(
                    f"{secrets_url.rstrip('/')}/api-keys",
                    json={"name": f"node-{self._node_id}", "scopes": ["endpoint:secrets"]},
                    headers={"Authorization": f"Bearer {bearer_token}"},
                )
            if resp.status_code == 200:
                minted = resp.json().get("api_key", "")
                if minted:
                    self.api_key = minted
                    logger.info("Node self-minted its own gateway key (node_id=%s)", self._node_id[:12])
                    return
            logger.debug("Self-mint key failed (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("Self-mint key failed: %s", e)

    async def heartbeat(self) -> bool:
        """Send a single heartbeat to maintain presence."""
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=self._auth_headers()) as client:
                resp = await client.post(
                    f"{self.gateway_url}/v1/nodes/{self._node_id}/heartbeat",
                    json={
                        "uptime_seconds": int(time.time() - self._start_time),
                        "agents": self.agents,
                        "capabilities": self.capabilities,
                    },
                )
                return resp.status_code < 300
        except Exception:
            return False

    async def start_heartbeat(self, interval: int = 60):
        """Start background heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return

        async def _loop():
            while True:
                await self.heartbeat()
                await asyncio.sleep(interval)

        self._heartbeat_task = asyncio.create_task(_loop())
        logger.info("AitherNet heartbeat started (every %ds)", interval)

    async def stop_heartbeat(self):
        """Stop the heartbeat loop."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def deregister(self) -> bool:
        """Remove this node from the mesh."""
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=self._auth_headers()) as client:
                resp = await client.delete(
                    f"{self.gateway_url}/v1/nodes/{self._node_id}",
                )
                self._registered = False
                return resp.status_code < 300
        except Exception:
            return False

    # ─── Discovery ─────────────────────────────────────────────────────

    async def discover(
        self,
        capability: str = "",
        status: str = "online",
        limit: int = 50,
    ) -> list[NodeInfo]:
        """Discover other nodes on the mesh."""
        params: dict = {"limit": limit}
        if capability:
            params["capability"] = capability
        if status:
            params["status"] = status

        try:
            async with httpx.AsyncClient(timeout=15.0, headers=self._auth_headers()) as client:
                resp = await client.get(
                    f"{self.gateway_url}/v1/nodes",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

            return [
                NodeInfo(
                    node_id=n.get("node_id", ""),
                    name=n.get("name", ""),
                    host=n.get("host", ""),
                    port=n.get("port", 0),
                    capabilities=n.get("capabilities", []),
                    agents=n.get("agents", []),
                    gpu_count=n.get("gpu_count", 0),
                    status=n.get("status", "unknown"),
                    last_seen=n.get("last_seen", ""),
                    version=n.get("version", ""),
                    os=n.get("os", ""),
                )
                for n in data.get("nodes", [])
            ]
        except Exception as e:
            logger.warning(f"Node discovery failed: {e}")
            return []

    async def find_inference_nodes(self) -> list[NodeInfo]:
        """Find nodes that can serve inference (have GPUs)."""
        return await self.discover(capability="inference")

    async def find_agent(self, agent_name: str) -> Optional[NodeInfo]:
        """Find which node hosts a specific agent."""
        nodes = await self.discover()
        for node in nodes:
            if agent_name in node.agents:
                return node
        return None

    # ─── Message Relay ─────────────────────────────────────────────────

    async def send(self, to_node: str, msg_type: str, payload: dict) -> bool:
        """Send a relay message to another node via the gateway.

        For NAT-traversed delivery, messages route through the gateway.
        The target node picks them up via polling or WebSocket.
        """
        message = {
            "msg_id": str(uuid.uuid4()),
            "from_node": self._node_id,
            "to_node": to_node,
            "msg_type": msg_type,
            "payload": payload,
            "timestamp": time.time(),
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, headers=self._auth_headers()) as client:
                resp = await client.post(
                    f"{self.gateway_url}/v1/nodes/{to_node}/relay",
                    json=message,
                )
                return resp.status_code < 300
        except Exception as e:
            logger.warning(f"Relay send failed: {e}")
            return False

    async def broadcast(self, msg_type: str, payload: dict) -> bool:
        """Broadcast a message to all nodes."""
        return await self.send("*", msg_type, payload)

    async def poll_messages(self) -> list[RelayMessage]:
        """Poll for relay messages addressed to this node."""
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=self._auth_headers()) as client:
                resp = await client.get(
                    f"{self.gateway_url}/v1/nodes/{self._node_id}/messages",
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()

            return [
                RelayMessage(
                    msg_id=m.get("msg_id", ""),
                    from_node=m.get("from_node", ""),
                    to_node=m.get("to_node", ""),
                    msg_type=m.get("msg_type", ""),
                    payload=m.get("payload", {}),
                    timestamp=m.get("timestamp", 0),
                )
                for m in data.get("messages", [])
            ]
        except Exception:
            return []

    # ─── WebSocket Relay (Real-time) ───────────────────────────────────

    async def connect_relay_hub(self):
        """Connect to the AitherNet relay hub for real-time messaging.

        Uses WebSocket for low-latency relay between nodes that can't
        reach each other directly (NAT traversal).
        """
        try:
            import websockets
        except ImportError:
            logger.info("websockets not installed — using HTTP polling for relay")
            return

        if self._relay_task and not self._relay_task.done():
            return

        async def _relay_loop():
            while True:
                try:
                    async with websockets.connect(self.relay_hub_url) as ws:
                        self._relay_ws = ws
                        logger.info("Connected to AitherNet relay hub at %s", self.relay_hub_url)

                        # Announce ourselves
                        await ws.send(json.dumps({
                            "type": "join",
                            "node_id": self._node_id,
                            "name": self.node_name,
                            "capabilities": self.capabilities,
                            "agents": self.agents,
                        }))

                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                await self._handle_relay_message(msg)
                            except json.JSONDecodeError:
                                pass

                except Exception as e:
                    logger.warning(f"Relay hub disconnected: {e}. Reconnecting in 10s...")
                    self._relay_ws = None
                    await asyncio.sleep(10)

        self._relay_task = asyncio.create_task(_relay_loop())

    async def _handle_relay_message(self, msg: dict):
        """Process an incoming relay message."""
        msg_type = msg.get("msg_type", msg.get("type", ""))

        # Dispatch to registered handlers
        for handler in self._handlers.get(msg_type, []):
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"Relay handler error for {msg_type}: {e}")

        # Built-in handlers
        if msg_type == "ping":
            if self._relay_ws:
                await self._relay_ws.send(json.dumps({
                    "type": "pong",
                    "from_node": self._node_id,
                    "to_node": msg.get("from_node"),
                }))

        # Response messages — store for pending request-response waiters
        payload = msg.get("payload", {})
        req_id = payload.get("_request_id")
        if msg_type in ("mcp_response", "agent_response", "inference_response") and req_id:
            self._pending_responses[req_id] = payload

        # Inbound MCP tool call — execute locally and send response back
        if msg_type == "mcp_call" and req_id:
            await self._handle_inbound_mcp_call(msg)

        # Inbound MCP list — return local tools
        if msg_type == "mcp_list" and req_id:
            await self._handle_inbound_mcp_list(msg)

        # Inbound inference — run through local LLM and return result
        if msg_type == "inference" and req_id:
            await self._handle_inbound_inference(msg)

        # Inbound agent call — route to local agent
        if msg_type == "agent_call" and req_id:
            await self._handle_inbound_agent_call(msg)

    async def _handle_inbound_mcp_call(self, msg: dict):
        """Handle an incoming MCP tool call from another node."""
        payload = msg.get("payload", {})
        from_node = msg.get("from_node", payload.get("_return_to", ""))
        request_id = payload.get("_request_id", "")
        try:
            from adk.mcp_server import MCPServer
            mcp = self._get_local_mcp_server()
            if not mcp:
                response_payload = {"ok": False, "error": "no_mcp_server"}
            else:
                result = await mcp.handle({
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": payload.get("name", ""), "arguments": payload.get("arguments", {})},
                    "id": 1,
                })
                if "result" in result:
                    content = result["result"].get("content", [])
                    text = content[0]["text"] if content else ""
                    response_payload = {"ok": True, "result": text, "_request_id": request_id}
                else:
                    response_payload = {"ok": False, "error": result.get("error", {}).get("message", "unknown"),
                                        "_request_id": request_id}
        except Exception as exc:
            response_payload = {"ok": False, "error": str(exc), "_request_id": request_id}

        if from_node:
            await self.send(from_node, "mcp_response", response_payload)

    async def _handle_inbound_mcp_list(self, msg: dict):
        """Handle an incoming MCP tools/list request from another node."""
        payload = msg.get("payload", {})
        from_node = msg.get("from_node", payload.get("_return_to", ""))
        request_id = payload.get("_request_id", "")
        try:
            mcp = self._get_local_mcp_server()
            if not mcp:
                response_payload = {"ok": False, "tools": [], "_request_id": request_id}
            else:
                result = await mcp.handle({
                    "jsonrpc": "2.0", "method": "tools/list", "id": 1,
                })
                tools = result.get("result", {}).get("tools", [])
                response_payload = {"ok": True, "tools": tools, "_request_id": request_id}
        except Exception as exc:
            response_payload = {"ok": False, "tools": [], "error": str(exc), "_request_id": request_id}

        if from_node:
            await self.send(from_node, "mcp_response", response_payload)

    async def _handle_inbound_inference(self, msg: dict):
        """Handle an incoming inference request from another node.

        Runs the request through the local LLMRouter and returns the response
        via relay. This enables the desktop to offload inference to this node
        when it has available GPU capacity.
        """
        payload = msg.get("payload", {})
        from_node = msg.get("from_node", payload.get("_return_to", ""))
        request_id = payload.get("_request_id", "")

        try:
            from adk.llm import LLMRouter, Message

            router = LLMRouter()
            messages = [
                Message(role=m.get("role", "user"), content=m.get("content", ""))
                for m in payload.get("messages", [])
            ]
            model = payload.get("model", "")
            resp = await router.chat(messages, model=model or None)
            response_payload = {
                "ok": True,
                "content": resp.content,
                "model": resp.model,
                "tokens_used": resp.tokens_used,
                "_request_id": request_id,
            }
        except Exception as exc:
            response_payload = {"ok": False, "error": str(exc), "_request_id": request_id}

        if from_node:
            await self.send(from_node, "inference_response", response_payload)

    async def _handle_inbound_agent_call(self, msg: dict):
        """Handle an incoming agent call from another node."""
        payload = msg.get("payload", {})
        from_node = msg.get("from_node", payload.get("_return_to", ""))
        request_id = payload.get("_request_id", "")
        agent_name = payload.get("agent", "")
        message = payload.get("message", "")

        try:
            from adk.agent import AitherAgent
            agent = AitherAgent(name=agent_name, identity=agent_name)
            resp = await agent.chat(message)
            response_payload = {
                "ok": True,
                "response": resp.content,
                "agent": agent_name,
                "model": resp.model,
                "tokens_used": resp.tokens_used,
                "_request_id": request_id,
            }
        except Exception as exc:
            response_payload = {"ok": False, "error": str(exc), "_request_id": request_id}

        if from_node:
            await self.send(from_node, "agent_response", response_payload)

    def _get_local_mcp_server(self):
        """Get the local MCP server instance (set by server.py startup)."""
        return getattr(self, "_local_mcp_server", None)

    def set_local_mcp_server(self, mcp_server):
        """Register the local MCP server for handling inbound mesh tool calls."""
        self._local_mcp_server = mcp_server

    def on(self, msg_type: str, handler: Callable):
        """Register a handler for a relay message type.

        Usage::

            relay.on("inference", handle_inference_request)
            relay.on("chat", handle_chat_message)
            relay.on("agent_call", handle_agent_call)
        """
        self._handlers.setdefault(msg_type, []).append(handler)

    async def disconnect_relay_hub(self):
        """Disconnect from the relay hub."""
        if self._relay_task:
            self._relay_task.cancel()
            self._relay_task = None
        if self._relay_ws:
            try:
                await self._relay_ws.close()
            except Exception:
                pass
            self._relay_ws = None

    # ─── Inference Relay ───────────────────────────────────────────────

    async def relay_inference(
        self,
        messages: list[dict],
        model: str = "",
        target_node: str = "",
        timeout: float = 30.0,
    ) -> dict:
        """Relay an inference request to a GPU node on the mesh.

        If target_node is empty, auto-discovers a node with inference capability.
        Uses request-response pattern: sends request, polls for response.
        """
        if not target_node:
            gpu_nodes = await self.find_inference_nodes()
            if not gpu_nodes:
                return {"ok": False, "error": "no_inference_nodes", "message": "No GPU nodes on mesh"}
            target_node = gpu_nodes[0].node_id

        return await self._request_response(target_node, "inference", {
            "messages": messages,
            "model": model,
        }, timeout=timeout)

    # ─── MCP Tool Relay (cross-node tool calling) ─────────────────────

    async def call_remote_tool(
        self,
        node_id: str,
        tool_name: str,
        arguments: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Call an MCP tool on a remote node via the mesh.

        Sends a mcp_call message and waits for the response.
        """
        return await self._request_response(node_id, "mcp_call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=timeout)

    async def list_remote_tools(
        self,
        node_id: str,
        timeout: float = 10.0,
    ) -> list[dict]:
        """List MCP tools available on a remote node."""
        result = await self._request_response(node_id, "mcp_list", {}, timeout=timeout)
        if result.get("ok"):
            return result.get("tools", [])
        return []

    async def call_remote_agent(
        self,
        agent_name: str,
        message: str,
        target_node: str = "",
        timeout: float = 60.0,
    ) -> dict:
        """Call an agent on a remote node.

        If target_node is empty, auto-discovers which node hosts the agent.
        """
        if not target_node:
            node = await self.find_agent(agent_name)
            if not node:
                return {"ok": False, "error": "agent_not_found",
                        "message": f"Agent '{agent_name}' not found on any mesh node"}
            target_node = node.node_id

        return await self._request_response(target_node, "agent_call", {
            "agent": agent_name,
            "message": message,
        }, timeout=timeout)

    async def discover_mesh_tools(self) -> list[dict]:
        """Discover all MCP tools across all mesh nodes.

        Returns a flat list of tools with node_id and node_name metadata.
        """
        nodes = await self.discover(capability="mcp")
        all_tools = []
        for node in nodes:
            if node.node_id == self._node_id:
                continue  # Skip self
            try:
                tools = await self.list_remote_tools(node.node_id, timeout=5.0)
                for t in tools:
                    t["_node_id"] = node.node_id
                    t["_node_name"] = node.name
                all_tools.extend(tools)
            except Exception:
                pass
        return all_tools

    # ─── Request-Response Pattern ─────────────────────────────────────

    async def _request_response(
        self,
        to_node: str,
        msg_type: str,
        payload: dict,
        timeout: float = 30.0,
    ) -> dict:
        """Send a request and wait for a response via polling.

        Attaches a unique request_id so we can match response to request.
        """
        request_id = str(uuid.uuid4())
        payload["_request_id"] = request_id
        payload["_return_to"] = self._node_id

        sent = await self.send(to_node, msg_type, payload)
        if not sent:
            return {"ok": False, "error": "relay_failed"}

        # Poll for response with timeout
        deadline = time.time() + timeout
        poll_interval = 0.5
        while time.time() < deadline:
            # Check pending responses (populated by _handle_relay_message)
            if request_id in self._pending_responses:
                return self._pending_responses.pop(request_id)
            await asyncio.sleep(min(poll_interval, deadline - time.time()))
            # Also poll gateway for messages
            messages = await self.poll_messages()
            for msg in messages:
                if msg.payload.get("_request_id") == request_id:
                    self._pending_responses[request_id] = msg.payload
                elif msg.payload.get("_request_id"):
                    # Store for other waiters
                    self._pending_responses[msg.payload["_request_id"]] = msg.payload
                else:
                    await self._handle_relay_message({
                        "msg_type": msg.msg_type,
                        "payload": msg.payload,
                        "from_node": msg.from_node,
                    })
            if request_id in self._pending_responses:
                return self._pending_responses.pop(request_id)
            poll_interval = min(poll_interval * 1.5, 3.0)  # Backoff

        return {"ok": False, "error": "timeout", "message": f"No response within {timeout}s"}

    # ─── Status ────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Current relay status."""
        return {
            "node_id": self._node_id,
            "node_name": self.node_name,
            "registered": self._registered,
            "heartbeat_active": self._heartbeat_task is not None and not self._heartbeat_task.done(),
            "relay_hub_connected": self._relay_ws is not None,
            "capabilities": self.capabilities,
            "agents": self.agents,
            "uptime_seconds": int(time.time() - self._start_time),
        }

    # ─── Utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _detect_gpu_count() -> int:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return len(result.stdout.strip().split("\n"))
        except Exception:
            pass
        return 0


# ─── Module-level singleton ────────────────────────────────────────────────

_relay: Optional[AitherNetRelay] = None


def get_relay(**kwargs) -> AitherNetRelay:
    """Get or create the singleton relay instance."""
    global _relay
    if _relay is None:
        _relay = AitherNetRelay(**kwargs)
    return _relay
