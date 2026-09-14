"""A2A Gateway service client (port 8766)."""

import uuid
from typing import Optional

from adk.client._base import ServiceClient


class A2AClient(ServiceClient):
    """Client for the A2A Gateway service."""

    async def services(self) -> dict:
        """Discover all available AitherNet services."""
        return await self._get("/services")

    async def services_status(self) -> dict:
        """Get real-time status of all services."""
        return await self._get("/services/status", timeout=15.0)

    async def agents(self) -> dict:
        """Get all registered A2A agents."""
        return await self._get("/agents")

    async def call_service(self, service: str, skill: str, params: Optional[dict] = None) -> dict:
        """Call a skill on an AitherNet service."""
        return await self._post(
            "/call",
            json={"service": service, "skill": skill, "params": params or {}},
            timeout=60.0,
        )

    async def send_task(self, agent_id: str, message: str) -> dict:
        """Send a task to a specific A2A agent via JSON-RPC."""
        client = await self._get_client()
        resp = await client.post(
            f"{self._base_url}/rpc",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "message/send",
                "params": {
                    "id": str(uuid.uuid4()),
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": message}],
                    },
                },
            },
            headers={"X-Agent-ID": agent_id},
            timeout=60.0,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def tools(self) -> dict:
        """List all available MCP tools."""
        return await self._get("/tools", timeout=15.0)

    async def tool_schema(self, name: str) -> dict:
        """Get a specific tool's schema."""
        return await self._get(f"/tools/{name}")

    async def call_tool(self, name: str, params: Optional[dict] = None) -> dict:
        """Call an MCP tool directly."""
        return await self._post(
            "/tools/call",
            json={"tool_name": name, "arguments": params or {}},
            timeout=30.0,
        )

    async def flux_state(self) -> dict:
        """Get Flux nervous system state."""
        return await self._get("/flux/state")
