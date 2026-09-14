"""
AitherOS Gateway Client
========================

Client for gateway.aitherium.com — auth, agent registration, discovery.
Shared between AitherShell and ADK.
"""

from __future__ import annotations

import os
import logging
from typing import Optional, List

import httpx

logger = logging.getLogger("adk.client.gateway")

DEFAULT_GATEWAY = "https://gateway.aitherium.com"


class GatewayClient:
    """Client for the AitherOS cloud gateway.

    Usage:
        from adk.client import GatewayClient

        gw = GatewayClient(api_key="aither_pat_...")
        agents = await gw.discover_agents(capability="code")
    """

    def __init__(
        self,
        gateway_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.gateway_url = (gateway_url or os.environ.get("AITHER_GATEWAY_URL") or DEFAULT_GATEWAY).rstrip("/")
        self.api_key = api_key or os.environ.get("AITHER_API_KEY", "")
        self._timeout = timeout

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
            if self.api_key.startswith("aither_pat_"):
                h["X-API-Key"] = self.api_key
        return h

    async def register(self, email: str, password: str) -> dict:
        """Register a new account."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.post(f"{self.gateway_url}/v1/auth/register", json={"email": email, "password": password})
            r.raise_for_status()
            return r.json()

    async def login(self, email: str, password: str) -> dict:
        """Login and get a token."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.post(f"{self.gateway_url}/v1/auth/login", json={"email": email, "password": password})
            r.raise_for_status()
            data = r.json()
        if data.get("token"):
            self.api_key = data["token"]
        return data

    async def register_agent(
        self,
        name: str,
        owner_email: str = "",
        description: str = "",
        framework: str = "adk",
        **kwargs  # Ignore legacy fields (capabilities, tools)
    ) -> dict:
        """Register an agent with the gateway.

        Per the MCP gateway contract, registration requires name and owner_email.
        Args:
            name: Agent name (required)
            owner_email: Contact email for the agent owner (required)
            description: Brief description of the agent
            framework: Framework type (e.g., 'adk', 'custom')
        """
        # Require owner_email per contract
        if not owner_email:
            raise ValueError("owner_email is required for agent registration")

        async with httpx.AsyncClient(timeout=self._timeout) as c:
            # Registration is ANONYMOUS (no auth required) per contract
            r = await c.post(
                f"{self.gateway_url}/v1/agents/register",
                json={
                    "name": name,
                    "owner_email": owner_email,
                    "description": description,
                    "framework": framework,
                },
            )
            if r.status_code == 400:
                # Contract specifies 400 for rate limit, body size, invalid json, missing fields
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                raise ValueError(
                    f"Registration failed (400): {data.get('error', r.text[:200])}"
                )
            r.raise_for_status()
            return r.json()

    async def discover_agents(self, capability: str = None, limit: int = 20) -> List[dict]:
        """Find agents on the network."""
        params = {"limit": limit}
        if capability:
            params["capability"] = capability
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.get(f"{self.gateway_url}/v1/agents/discover", params=params)
            r.raise_for_status()
            return r.json().get("agents", [])

    async def my_agents(self) -> List[dict]:
        """List my registered agents."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.get(f"{self.gateway_url}/v1/agents/mine")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            return r.json().get("agents", [])

    async def unregister_agent(self, agent_id: str) -> dict:
        """Remove an agent from the network."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.delete(f"{self.gateway_url}/v1/agents/{agent_id}")
            r.raise_for_status()
            return r.json()

    async def verify_email(self, token: str) -> dict:
        """Verify email address with a token from the registration email."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as c:
            r = await c.post(f"{self.gateway_url}/v1/auth/verify", json={"token": token})
            r.raise_for_status()
            return r.json()

    async def inference(
        self,
        messages: list[dict],
        model: str = "aither-orchestrator",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream: bool = False,
        inference_url: str = "",
        **kwargs,
    ) -> dict:
        """Send a chat completion request to Elysium inference."""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
            **kwargs,
        }
        url = inference_url or f"{self.gateway_url}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=120.0, headers=self._headers()) as c:
            r = await c.post(url, json=payload)
            r.raise_for_status()
            return r.json()

    async def health(self) -> bool:
        """Check if the gateway is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.gateway_url}/health")
                return r.status_code == 200
        except Exception:
            return False

    @classmethod
    def from_pat(cls, pat: str, **kwargs) -> "GatewayClient":
        """Create a GatewayClient from a Personal Access Token."""
        return cls(api_key=pat, **kwargs)
