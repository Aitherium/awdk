"""Federation client — connect an ADK agent to a running AitherOS instance.

Handles the full federation lifecycle:
1. Register with AitherIdentity (get credentials)
2. Join AitherMesh (node discovery)
3. Subscribe to FluxEmitter events
4. Use MCP tools
5. Chat via Genesis or Node
6. Operate in isolated tenant context

Two connection modes:

**Gateway mode** (default for external agents):
    All traffic goes through gateway.aitherium.com → AitherExternalGateway:8185
    which handles auth, rate limiting, and proxies to internal services.

    fed = FederationClient("https://gateway.aitherium.com")
    await fed.register("my-agent", api_key="ak_xxx")

**Direct mode** (internal / development):
    Connects to individual service ports on localhost.

    fed = FederationClient("http://localhost", mode="direct")
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("adk.federation")

# Default gateway URL — external agents connect here
GATEWAY_URL = "https://gateway.aitherium.com"


@dataclass
class FederationCredentials:
    """Credentials received from AitherIdentity enrollment."""
    api_key: str = ""
    token: str = ""
    user_id: str = ""
    node_id: str = ""
    mesh_key: str = ""
    wireguard_ip: str = ""
    trust_level: int = 0


@dataclass
class MeshNode:
    """A node in the AitherMesh network."""
    node_id: str = ""
    hostname: str = ""
    role: str = "client"
    capabilities: dict = field(default_factory=dict)
    status: str = "unknown"


class FederationClient:
    """Connect an external ADK agent to a running AitherOS instance.

    Provides the full federation stack:
    - AitherIdentity: Authentication and registration
    - AitherMesh: Node network and discovery
    - AitherFlux: Event routing and pub/sub
    - MCP tools: awnode tool execution
    - Genesis chat: Full pipeline access

    Args:
        host: Gateway URL (default: https://gateway.aitherium.com)
              or localhost URL for direct mode.
        mode: "gateway" (default) routes all calls through the external gateway.
              "direct" connects to individual service ports (dev/internal only).
        timeout: Default request timeout in seconds.
        tenant: Tenant ID for isolation.
    """

    def __init__(
        self,
        host: str = GATEWAY_URL,
        timeout: float = 15.0,
        tenant: str = "public",
        mode: str = "gateway",
    ):
        self.host = host.rstrip("/")
        self._timeout = timeout
        self._tenant = tenant
        self._mode = mode
        self._creds = FederationCredentials()
        self._node_id = f"anode-{uuid.uuid4().hex[:12]}"
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._flux_task: Optional[asyncio.Task] = None
        self._flux_handlers: dict[str, list] = {}

        # Service ports (only used in direct mode)
        self._ports = {
            "identity": 8112,
            "mesh": 8125,
            "flux": 8117,
            "node": 8090,
            "genesis": 8001,
            "pulse": 8081,
            "gateway": 8185,
        }

        # Auto-detect mode: if host looks like a gateway URL, use gateway mode
        if mode == "gateway" or "gateway.aitherium.com" in host:
            self._mode = "gateway"
        elif "localhost" in host or "127.0.0.1" in host:
            # localhost defaults to direct unless explicitly set
            if mode != "gateway":
                self._mode = "direct"

    def _url(self, service: str, path: str) -> str:
        """Build URL for a service endpoint.

        In gateway mode, ALL requests go through the gateway.
        In direct mode, requests go to individual service ports.
        """
        if self._mode == "gateway":
            # Everything routes through the gateway — paths are prefixed
            # gateway.aitherium.com/v1/chat, /v1/mesh/join, etc.
            return f"{self.host}{path}"
        else:
            port = self._ports.get(service, 8001)
            return f"{self.host}:{port}{path}"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Caller-Type": "public",
            "X-Tenant-ID": self._tenant,
        }
        if self._creds.token:
            h["Authorization"] = f"Bearer {self._creds.token}"
        elif self._creds.api_key:
            h["Authorization"] = f"Bearer {self._creds.api_key}"
        return h

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, headers=self._headers())

    # ─── AitherIdentity ───

    async def register(
        self,
        username: str,
        password: str = "",
        api_key: str = "",
        email: str = "",
    ) -> FederationCredentials:
        """Register with AitherIdentity and get credentials.

        Either provide password (for new registration) or api_key (for existing).

        In gateway mode, all calls go through gateway.aitherium.com/v1/auth/*.
        In direct mode, calls go to AitherIdentity:8112/auth/*.
        """
        if api_key:
            # Existing credentials — just verify
            self._creds.api_key = api_key
            try:
                async with self._client() as client:
                    url = (self._url("gateway", "/v1/auth/me")
                           if self._mode == "gateway"
                           else self._url("identity", "/auth/me"))
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        self._creds.user_id = data.get("user_id", data.get("id", ""))
                        self._creds.token = api_key
                        logger.info(f"Authenticated as {username}")
                        return self._creds
            except Exception as e:
                logger.warning(f"API key verification failed: {e}")

        # New registration
        try:
            async with self._client() as client:
                if self._mode == "gateway":
                    # Gateway handles registration in one call
                    resp = await client.post(
                        self._url("gateway", "/v1/auth/register"),
                        json={
                            "display_name": username,
                            "email": email,
                            "password": password,
                            "capabilities": [],
                        },
                    )
                else:
                    # Direct to Identity
                    payload = {"username": username}
                    if password:
                        payload["password"] = password
                    if email:
                        payload["email"] = email
                    resp = await client.post(
                        self._url("identity", "/auth/register"),
                        json=payload,
                    )

                if resp.status_code in (200, 201):
                    data = resp.json()
                    self._creds.token = data.get("token", "")
                    self._creds.api_key = data.get("api_key", self._creds.token)
                    self._creds.user_id = data.get("user_id", data.get("id", ""))
                    logger.info(f"Registered as {username}")
                else:
                    # Try login if already registered
                    login_url = (self._url("gateway", "/v1/auth/login")
                                 if self._mode == "gateway"
                                 else self._url("identity", "/auth/login"))
                    resp = await client.post(
                        login_url,
                        json={"username": username, "password": password or "", "api_key": api_key or ""},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self._creds.token = data.get("token", "")
                        self._creds.api_key = data.get("api_key", self._creds.token)
                        self._creds.user_id = data.get("user_id", "")
                        logger.info(f"Logged in as {username}")
                    else:
                        logger.error(f"Registration/login failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Identity service unreachable: {e}")

        return self._creds

    async def enroll_with_mesh_key(
        self,
        mesh_key: str,
        hostname: str = "",
        capabilities: dict | None = None,
    ) -> FederationCredentials:
        """Enroll using a pre-generated mesh key (no prior auth needed)."""
        import platform as plat

        payload = {
            "mesh_key": mesh_key,
            "node_id": self._node_id,
            "hostname": hostname or plat.node(),
            "platform": plat.system().lower(),
            "arch": plat.machine(),
            "capabilities": capabilities or {},
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url("identity", "/v1/mesh-keys/enroll"),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                self._creds.api_key = data.get("api_key", "")
                self._creds.token = self._creds.api_key
                self._creds.node_id = self._node_id
                self._creds.mesh_key = mesh_key
                self._creds.wireguard_ip = data.get("wireguard_ip", "")
                self._creds.trust_level = data.get("trust_level", 0)
                logger.info(f"Enrolled via mesh key: node={self._node_id}")
            else:
                logger.error(f"Mesh enrollment failed: {resp.status_code} {resp.text}")

        return self._creds

    # ─── AitherMesh ───

    async def join_mesh(
        self,
        capabilities: list[str] | None = None,
        role: str = "client",
    ) -> bool:
        """Join the AitherOS mesh network.

        In gateway mode: POST gateway.aitherium.com/v1/mesh/join
        In direct mode: POST mesh:8125/aithernet/nodes/join (then fallback /join)
        """
        import platform as plat

        if self._mode == "gateway":
            try:
                async with self._client() as client:
                    resp = await client.post(
                        self._url("gateway", "/v1/mesh/join"),
                        json={
                            "node_name": self._node_id,
                            "capabilities": capabilities or [],
                            "endpoint": "",
                        },
                    )
                    if resp.status_code == 200:
                        logger.info(f"Joined mesh via gateway: {self._node_id}")
                        return True
                    logger.warning(f"Gateway mesh join: {resp.status_code} {resp.text}")
            except Exception as e:
                logger.warning(f"Gateway mesh join error: {e}")
            return False

        # Direct mode: try AitherNet first (compound mount at /aithernet/)
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._url("mesh", "/aithernet/nodes/join"),
                    json={
                        "hostname": plat.node(),
                        "wg_public_key": f"adk-{self._node_id}",
                        "services": capabilities or [],
                        "metadata": {
                            "type": "adk_agent",
                            "role": role,
                            "platform": plat.system(),
                        },
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"Joined AitherNet: {data.get('node_id', self._node_id)}")
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    return True
                logger.debug(f"AitherNet join: {resp.status_code}")
        except Exception as e:
            logger.debug(f"AitherNet join error: {e}")

        # Fallback: direct mesh /join
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._url("mesh", "/join"),
                    json={
                        "node_id": self._node_id,
                        "capabilities": {"skills": capabilities or [], "role": role},
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Joined mesh as {self._node_id}")
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    return True
                logger.warning(f"Mesh join failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Mesh join error: {e}")
        return False

    async def list_nodes(self) -> list[MeshNode]:
        """List nodes in the mesh."""
        paths = (["/v1/mesh/nodes"] if self._mode == "gateway"
                 else ["/aithernet/nodes", "/nodes"])
        service = "gateway" if self._mode == "gateway" else "mesh"
        for path in paths:
            try:
                async with self._client() as client:
                    resp = await client.get(self._url(service, path))
                    if resp.status_code == 200:
                        data = resp.json()
                        nodes = []
                        for n in data.get("nodes", []):
                            nodes.append(MeshNode(
                                node_id=n.get("node_id", ""),
                                hostname=n.get("hostname", ""),
                                role=n.get("role", ""),
                                capabilities=n.get("capabilities", {}),
                                status=n.get("status", "unknown"),
                            ))
                        return nodes
            except Exception:
                continue
        return []

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to maintain mesh presence."""
        while True:
            try:
                await asyncio.sleep(30)
                async with self._client() as client:
                    await client.post(
                        self._url("mesh", "/heartbeat"),
                        json={"node_id": self._node_id, "status": "online"},
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ─── AitherFlux (Event Routing) ───

    async def emit_event(self, event_type: str, data: dict) -> bool:
        """Emit an event via AitherFlux."""
        try:
            async with self._client() as client:
                if self._mode == "gateway":
                    resp = await client.post(
                        self._url("gateway", "/v1/events/emit"),
                        json={
                            "event_type": event_type,
                            "data": data,
                            "source": self._node_id,
                        },
                    )
                else:
                    resp = await client.post(
                        self._url("flux", "/emit"),
                        json={
                            "event_type": event_type,
                            "data": data,
                            "source": self._node_id,
                        },
                    )
                return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Flux emit error: {e}")
            return False

    def on_event(self, event_type: str, handler):
        """Register a handler for flux events."""
        if event_type not in self._flux_handlers:
            self._flux_handlers[event_type] = []
        self._flux_handlers[event_type].append(handler)

    async def subscribe_events(self) -> None:
        """Subscribe to AitherFlux SSE event stream."""
        if self._flux_task and not self._flux_task.done():
            return
        self._flux_task = asyncio.create_task(self._flux_sse_loop())

    async def _flux_sse_loop(self):
        """Connect to Pulse SSE and dispatch to registered handlers."""
        url = self._url("pulse", "/events/stream")
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url, headers=self._headers()) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                try:
                                    event = json.loads(line[5:].strip())
                                    etype = event.get("type", event.get("event_type", ""))
                                    handlers = self._flux_handlers.get(etype, [])
                                    for h in handlers:
                                        if asyncio.iscoroutinefunction(h):
                                            await h(event)
                                        else:
                                            h(event)
                                except json.JSONDecodeError:
                                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"SSE reconnecting: {e}")
                await asyncio.sleep(5)

    # ─── MCP Tools ───

    async def _get_mcp_session(self) -> tuple[str, str]:
        """Get an MCP session from awnode SSE transport.

        Returns (messages_url, sse_url).
        awnode uses SSE transport: GET /sse returns endpoint URL,
        then POST to /messages/?session_id=xxx for JSON-RPC.
        Results come back via SSE stream.
        """
        if hasattr(self, "_mcp_messages_url") and self._mcp_messages_url:
            return self._mcp_messages_url, self._mcp_sse_url

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                async with client.stream("GET", self._url("node", "/sse")) as resp:
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            path = line[5:].strip()
                            if "/messages/" in path:
                                msg_url = f"{self.host}:{self._ports['node']}{path}"
                                sse_url = self._url("node", "/sse")
                                self._mcp_messages_url = msg_url
                                self._mcp_sse_url = sse_url
                                return msg_url, sse_url
                            break
        except Exception as e:
            logger.debug(f"MCP SSE session error: {e}")

        self._mcp_messages_url = ""
        self._mcp_sse_url = ""
        return "", ""

    async def _mcp_request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """Send an MCP JSON-RPC request and collect the response via SSE.

        awnode MCP uses SSE transport:
        1. POST JSON-RPC to /messages/?session_id=xxx (returns 202 Accepted)
        2. Response comes back on the SSE stream as a JSON-RPC result
        """
        msg_url, _ = await self._get_mcp_session()
        if not msg_url:
            return {}

        request_id = id(method) % 100000

        # We need to listen on SSE while sending the request
        # Strategy: send request, then read SSE events until we get our response
        try:
            # Send the request (202 means accepted)
            async with httpx.AsyncClient(timeout=10.0, headers=self._headers()) as client:
                payload = {"jsonrpc": "2.0", "method": method, "id": request_id}
                if params:
                    payload["params"] = params
                resp = await client.post(msg_url, json=payload)
                if resp.status_code not in (200, 202):
                    logger.warning(f"MCP request failed: {resp.status_code}")
                    return {}

                # If 200, response is inline
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception:
                        pass

            # For 202, read response from SSE stream
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Get a new SSE connection to read the response
                async with client.stream("GET", self._url("node", "/sse")) as sse_resp:
                    async for line in sse_resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            # Skip endpoint announcements
                            if "/messages/" in data_str:
                                continue
                            try:
                                data = json.loads(data_str)
                                # Check if this is our response
                                if isinstance(data, dict) and ("result" in data or "error" in data):
                                    return data
                            except json.JSONDecodeError:
                                continue
        except asyncio.TimeoutError:
            logger.warning(f"MCP request timed out: {method}")
        except Exception as e:
            logger.warning(f"MCP request error: {e}")
        return {}

    async def list_mcp_tools(self) -> list[dict]:
        """List available MCP tools from awnode."""
        if self._mode == "gateway":
            try:
                async with self._client() as client:
                    resp = await client.get(self._url("gateway", "/v1/mcp/tools"))
                    if resp.status_code == 200:
                        data = resp.json()
                        tools = data.get("tools", [])
                        if tools:
                            logger.info(f"Discovered {len(tools)} MCP tools via gateway")
                        return tools
            except Exception as e:
                logger.warning(f"Gateway MCP tools error: {e}")
            return []

        data = await self._mcp_request("tools/list", timeout=15.0)
        tools = data.get("result", {}).get("tools", [])
        if tools:
            logger.info(f"Discovered {len(tools)} MCP tools")
        else:
            logger.warning("No MCP tools discovered")
        return tools

    async def call_mcp_tool(self, name: str, arguments: dict | None = None) -> str:
        """Call an MCP tool on awnode."""
        if self._mode == "gateway":
            try:
                async with self._client() as client:
                    resp = await client.post(
                        self._url("gateway", "/v1/mcp/call"),
                        json={"name": name, "arguments": arguments or {}},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        parts = data.get("content", [])
                        texts = []
                        for p in parts:
                            if isinstance(p, dict) and p.get("type") == "text":
                                texts.append(p["text"])
                        return "\n".join(texts) if texts else json.dumps(data)
            except Exception as e:
                logger.warning(f"Gateway MCP call error: {e}")
            return ""

        data = await self._mcp_request(
            "tools/call",
            params={"name": name, "arguments": arguments or {}},
        )
        result = data.get("result", {})
        parts = result.get("content", [])
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p["text"])
            elif isinstance(p, str):
                texts.append(p)
        return "\n".join(texts) if texts else (json.dumps(result) if result else "")

    # ─── Chat (Genesis / Node) ───

    async def chat(
        self,
        message: str,
        agent: str = "aither",
        effort: int = 5,
    ) -> dict:
        """Send a chat message via AitherOS.

        In gateway mode: POST gateway.aitherium.com/v1/chat
        In direct mode: Try Genesis:8001 first, then Node:8090.

        Returns {"response": "...", "source": "genesis|gateway|node", ...}
        """
        if self._mode == "gateway":
            try:
                async with self._client() as client:
                    resp = await client.post(
                        self._url("gateway", "/v1/chat"),
                        json={
                            "message": message,
                            "agent": agent,
                            "session_id": f"fed-{self._node_id[:8]}",
                        },
                        timeout=60.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return {
                            "response": data.get("response", data.get("content", "")),
                            "source": "gateway",
                            "model": data.get("model", ""),
                            "agent": agent,
                        }
                    else:
                        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        return {
                            "response": "",
                            "source": "gateway",
                            "error": data.get("detail", f"HTTP {resp.status_code}"),
                        }
            except Exception as e:
                return {"response": "", "source": "none", "error": str(e)}

        # Direct mode: try Genesis first
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._url("genesis", "/chat"),
                    json={
                        "message": message,
                        "agent": agent,
                        "effort": effort,
                        "session_id": f"fed-{self._node_id[:8]}",
                    },
                    timeout=60.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "response": data.get("response", data.get("content", "")),
                        "source": "genesis",
                        "model": data.get("model", ""),
                        "agent": agent,
                    }
        except Exception as e:
            logger.debug(f"Genesis chat failed, trying Node: {e}")

        # Fallback to Node (OpenAI-compatible)
        try:
            async with self._client() as client:
                resp = await client.post(
                    self._url("node", "/v1/chat/completions"),
                    json={
                        "model": "",
                        "messages": [
                            {"role": "system", "content": f"You are {agent}, an AitherOS agent."},
                            {"role": "user", "content": message},
                        ],
                    },
                    timeout=60.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return {
                        "response": content,
                        "source": "node",
                        "model": data.get("model", ""),
                        "agent": agent,
                    }
        except Exception as e:
            logger.warning(f"Node chat also failed: {e}")

        return {"response": "", "source": "none", "error": "All endpoints unreachable"}

    # ─── System Status ───

    async def get_system_status(self) -> dict:
        """Get AitherOS system status."""
        try:
            async with self._client() as client:
                if self._mode == "gateway":
                    resp = await client.get(self._url("gateway", "/v1/status"))
                else:
                    resp = await client.get(self._url("pulse", "/health"))
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return {"status": "unreachable"}

    async def get_service_health(self, service: str) -> dict:
        """Check health of a specific service."""
        port = self._ports.get(service.lower())
        if not port:
            return {"error": f"Unknown service: {service}"}
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.host}:{port}/health")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return {"status": "unreachable", "service": service}

    # ─── Lifecycle ───

    async def disconnect(self):
        """Cleanly disconnect from the mesh."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._flux_task:
            self._flux_task.cancel()
            try:
                await self._flux_task
            except asyncio.CancelledError:
                pass

        # Notify mesh we're leaving
        try:
            async with self._client() as client:
                await client.post(
                    self._url("mesh", "/heartbeat"),
                    json={"node_id": self._node_id, "status": "offline"},
                )
        except Exception:
            pass

        logger.info(f"Disconnected from mesh: {self._node_id}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.disconnect()
