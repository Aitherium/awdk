"""
Elysium — Connect your ADK agent to AitherOS cloud inference.
================================================================

The Elysium client is the onramp for external agents to register with
the AitherOS platform and use its inference pipeline, scoped to their
tenant's plan and token balance.

Quick start::

    from adk import AitherAgent
    from adk.elysium import Elysium

    # Connect to Elysium (authenticates + registers your agent)
    elysium = await Elysium.connect(api_key="aither_sk_live_...")

    # Create an agent that uses Elysium inference
    agent = AitherAgent("my_agent", llm=elysium.router)
    response = await agent.chat("Hello from my custom agent!")

    # Or connect an existing agent
    agent = AitherAgent("my_agent")
    await elysium.attach(agent)
    response = await agent.chat("Now using Elysium inference!")

Authentication::

    # Register a new account
    elysium = Elysium()
    result = await elysium.register(email="you@example.com", password="...")

    # Login to get a JWT
    result = await elysium.login(email="you@example.com", password="...")
    # result["token"] is your session token

    # Or use an ACTA API key (from demo.aitherium.com)
    elysium = await Elysium.connect(api_key="aither_sk_live_xxx")

All inference is routed through mcp.aitherium.com/v1/chat/completions
with ACTA token metering.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from adk.agent import AitherAgent
    from adk.llm import LLMRouter

logger = logging.getLogger("adk.elysium")

# Elysium endpoints — both route to awnode MCP Gateway
# gateway.aitherium.com = auth + billing + inference (unified)
# mcp.aitherium.com = legacy inference-only path (still works)
_GATEWAY_URL = "https://gateway.aitherium.com"
_INFERENCE_URL = "https://gateway.aitherium.com/v1"


@dataclass
class ElysiumStatus:
    """Connection status for an Elysium-connected agent."""
    connected: bool = False
    user_id: str = ""
    plan: str = ""
    tier: str = ""
    token_balance: int = 0
    models_available: list = field(default_factory=list)
    agent_id: str = ""


class Elysium:
    """Client for connecting ADK agents to AitherOS Elysium cloud inference.

    Provides:
    - User registration and authentication (gateway.aitherium.com)
    - Agent registration with the network
    - Cloud inference via mcp.aitherium.com (OpenAI-compatible)
    - Model discovery and tier-based access
    - ACTA token balance checking

    All features are optional — agents work fully offline without Elysium.
    """

    def __init__(
        self,
        api_key: str = "",
        gateway_url: str = "",
        inference_url: str = "",
        timeout: float = 30.0,
    ):
        # Resolve API key: explicit param > AITHER_API_KEY env > saved config
        resolved_key = api_key or os.getenv("AITHER_API_KEY", "")
        if not resolved_key:
            try:
                from adk.config import load_saved_config  # noqa: E402
                saved = load_saved_config()
                resolved_key = saved.get("api_key", "")
            except (ImportError, OSError, ValueError):
                pass
        self.api_key = resolved_key
        self.gateway_url = (gateway_url or os.getenv("AITHER_GATEWAY_URL", _GATEWAY_URL)).rstrip("/")
        self.inference_url = (inference_url or os.getenv("AITHER_INFERENCE_URL", _INFERENCE_URL)).rstrip("/")
        self._timeout = timeout
        self._status = ElysiumStatus()
        self._router: Optional[LLMRouter] = None
        self._jwt_token: str = ""  # Session token from login

    def _auth_headers(self) -> dict[str, str]:
        """Build auth headers using API key or JWT token."""
        h: dict[str, str] = {"Content-Type": "application/json"}
        token = self.api_key or self._jwt_token
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _gateway_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )

    def _inference_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._auth_headers(),
        )

    # ─── Authentication ───────────────────────────────────────────────

    async def register(self, email: str, password: str, display_name: str = "") -> dict:
        """Register a new Aitherium account.

        Returns {"ok": True, "user_id": "..."} on success.
        After registration, verify your email, then login to get a JWT.
        """
        async with self._gateway_client() as client:
            resp = await client.post(
                f"{self.gateway_url}/v1/auth/register",
                json={
                    "email": email,
                    "password": password,
                    "display_name": display_name or None,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def login(self, email: str, password: str) -> dict:
        """Login to get a JWT session token.

        Returns {"ok": True, "token": "...", "expires_at": ...}.
        The token is automatically stored for subsequent API calls.
        """
        async with self._gateway_client() as client:
            resp = await client.post(
                f"{self.gateway_url}/v1/auth/login",
                json={"email": email, "password": password},
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("ok") and data.get("token"):
            self._jwt_token = data["token"]
            logger.info("Logged in to Aitherium as %s", email)

        return data

    async def verify_email(self, email: str, token: str) -> dict:
        """Verify email with the token from the verification email."""
        async with self._gateway_client() as client:
            resp = await client.post(
                f"{self.gateway_url}/v1/auth/verify",
                json={"email": email, "token": token},
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_tenant_info(self) -> dict:
        """Fetch the current user's tenant and account info.

        Hits ``/v1/auth/me`` to retrieve user profile, tenant, tier, and
        permissions.  Returns an empty dict (not an exception) when the
        endpoint is unreachable or the caller is unauthenticated, so that
        the CLI can degrade gracefully.

        Returns:
            A dict with keys ``user_id``, ``tenant_id``, ``tier``, ``plan``,
            ``role``, ``permissions`` (when available), or ``{}`` on failure.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._auth_headers(),
            ) as client:
                resp = await client.get(f"{self.gateway_url}/v1/auth/me")
                if resp.status_code == 200:
                    data = resp.json()
                    # Normalise: the endpoint may nest under "user" or return flat
                    if "user" in data and isinstance(data["user"], dict):
                        data = {**data, **data.pop("user")}
                    return data
                logger.debug("fetch_tenant_info: %s returned %s", self.gateway_url, resp.status_code)
        except Exception as exc:
            logger.debug("fetch_tenant_info failed: %s", exc)
        return {}

    # ─── Connection ───────────────────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        api_key: str = "",
        gateway_url: str = "",
        inference_url: str = "",
    ) -> "Elysium":
        """Connect to Elysium and verify credentials.

        Args:
            api_key: ACTA API key (aither_sk_live_...) or JWT token.
            gateway_url: Override gateway URL (default: gateway.aitherium.com).
            inference_url: Override inference URL (default: mcp.aitherium.com/v1).

        Returns:
            Connected Elysium instance with a configured LLM router.

        Raises:
            ConnectionError: If credentials are invalid or gateway unreachable.
        """
        instance = cls(
            api_key=api_key,
            gateway_url=gateway_url,
            inference_url=inference_url,
        )
        await instance._verify_connection()
        return instance

    async def _verify_connection(self) -> None:
        """Verify credentials and fetch available models."""
        if not self.api_key and not self._jwt_token:
            raise ConnectionError(
                "No API key or JWT token provided.\n\n"
                "  Get an API key: https://demo.aitherium.com\n"
                "  Or login:       await elysium.login(email, password)\n"
                "  Or set env:     AITHER_API_KEY=aither_sk_live_..."
            )

        # Check health
        try:
            async with self._inference_client() as client:
                resp = await client.get(f"{self.inference_url.rsplit('/v1', 1)[0]}/health")
                if resp.status_code != 200:
                    raise ConnectionError(f"Elysium health check failed: {resp.status_code}")
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot reach mcp.aitherium.com. Check your internet connection."
            )

        # Fetch available models (validates auth implicitly via the inference endpoint)
        try:
            async with self._inference_client() as client:
                resp = await client.get(f"{self.inference_url}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    self._status.models_available = [
                        m["id"] for m in data.get("data", [])
                        if m.get("accessible", True)
                    ]
        except Exception:
            pass  # Models endpoint is public, non-fatal

        self._status.connected = True
        logger.info(
            "Connected to Elysium (%s). Models: %s",
            self.inference_url,
            ", ".join(self._status.models_available) or "discovering...",
        )

    @property
    def router(self) -> "LLMRouter":
        """Get an LLMRouter configured for Elysium inference.

        This router points at mcp.aitherium.com/v1 with your API key,
        speaking standard OpenAI-compatible format.
        """
        if self._router is None:
            from adk.llm import LLMRouter
            self._router = LLMRouter(
                provider="gateway",
                base_url=self.inference_url,
                api_key=self.api_key or self._jwt_token,
                model="aither-orchestrator",
            )
        return self._router

    @property
    def status(self) -> ElysiumStatus:
        """Current connection status (sync property, not callable).

        Use ``elysium.status`` (no parens), not ``elysium.status()``.
        For an async version that refreshes, use ``await elysium.get_status()``.
        """
        return self._status

    async def get_status(self) -> ElysiumStatus:
        """Refresh and return connection status (async method).

        Unlike the ``status`` property, this re-checks the connection
        and updates balance/model info.
        """
        checks = await self.preflight()
        if checks.get("checks", {}).get("inference_reachable", {}).get("ok"):
            self._status.connected = True
        balance = checks.get("checks", {}).get("balance", {})
        if balance.get("ok"):
            self._status.token_balance = balance.get("balance", 0)
            self._status.plan = balance.get("plan", "")
        return self._status

    # ─── Agent Integration ────────────────────────────────────────────

    async def attach(self, agent: "AitherAgent") -> None:
        """Attach Elysium inference to an existing agent.

        Replaces the agent's LLM router with one pointing at Elysium.
        The agent's custom tools, identity, and memory are preserved —
        only inference is redirected to the cloud.
        """
        if not self._status.connected:
            await self._verify_connection()

        agent.llm = self.router
        agent._provider_name = "elysium"
        logger.info("Attached Elysium inference to agent '%s'", agent.name)

    # ─── Agent Registry ───────────────────────────────────────────────

    async def register_agent(
        self,
        agent_name: str,
        capabilities: list[str] | None = None,
        description: str = "",
        tools: list[str] | None = None,
    ) -> dict:
        """Register your agent with the Aitherium network.

        Makes your agent discoverable by others on the platform.
        """
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._auth_headers(),
        ) as client:
            resp = await client.post(
                f"{self.gateway_url}/v1/agents/register",
                json={
                    "name": agent_name,
                    "capabilities": capabilities or [],
                    "description": description,
                    "tools": tools or [],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        self._status.agent_id = data.get("agent_id", "")
        return data

    async def my_agents(self) -> list[dict]:
        """List your registered agents."""
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._auth_headers(),
        ) as client:
            resp = await client.get(f"{self.gateway_url}/v1/agents/mine")
            if resp.status_code == 404:
                # Endpoint may not exist yet on older gateways
                return []
            resp.raise_for_status()
            return resp.json().get("agents", [])

    async def discover_agents(
        self,
        capability: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Discover other agents on the Aitherium network."""
        params: dict = {"limit": limit}
        if capability:
            params["capability"] = capability
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._auth_headers(),
        ) as client:
            resp = await client.get(
                f"{self.gateway_url}/v1/agents/discover",
                params=params,
            )
            resp.raise_for_status()
            return resp.json().get("agents", [])

    # ─── Utilities ────────────────────────────────────────────────────

    async def models(self) -> list[dict]:
        """List available inference models with tier and pricing info."""
        async with self._inference_client() as client:
            resp = await client.get(f"{self.inference_url}/models")
            resp.raise_for_status()
            return resp.json().get("data", [])

    async def health(self) -> bool:
        """Check if Elysium is reachable."""
        try:
            async with self._inference_client() as client:
                base = self.inference_url.rsplit("/v1", 1)[0]
                resp = await client.get(f"{base}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def preflight(self) -> dict:
        """Run comprehensive health checks for agent self-diagnosis.

        Returns a dict with check results that agents can use to validate
        their setup before starting operations::

            status = await ely.preflight()
            if status["ready"]:
                # All systems go
            else:
                # Check status["checks"] for failures

        Checks performed:
        - gateway_reachable: Can we reach gateway.aitherium.com?
        - inference_reachable: Can we reach mcp.aitherium.com?
        - auth_valid: Is our API key / JWT accepted?
        - balance: Current token balance and plan info
        - models: Available models for our tier
        - agents: Our registered agents
        """
        checks: dict = {
            "ready": False,
            "checks": {},
        }

        # 1. Gateway health
        try:
            async with self._gateway_client() as client:
                resp = await client.get(f"{self.gateway_url}/health")
                checks["checks"]["gateway_reachable"] = {
                    "ok": resp.status_code == 200,
                    "status_code": resp.status_code,
                    "url": f"{self.gateway_url}/health",
                }
        except Exception as e:
            checks["checks"]["gateway_reachable"] = {
                "ok": False, "error": str(e),
                "url": f"{self.gateway_url}/health",
            }

        # 2. Inference health
        try:
            async with self._inference_client() as client:
                base = self.inference_url.rsplit("/v1", 1)[0]
                resp = await client.get(f"{base}/health")
                checks["checks"]["inference_reachable"] = {
                    "ok": resp.status_code == 200,
                    "status_code": resp.status_code,
                    "url": f"{base}/health",
                }
        except Exception as e:
            checks["checks"]["inference_reachable"] = {
                "ok": False, "error": str(e),
                "url": self.inference_url,
            }

        # 3. Auth validation + balance (via billing endpoint)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=self._auth_headers()
            ) as client:
                resp = await client.get(f"{self.gateway_url}/v1/billing/balance")
                if resp.status_code == 200:
                    data = resp.json()
                    checks["checks"]["auth_valid"] = {"ok": True}
                    checks["checks"]["balance"] = {
                        "ok": True,
                        "balance": data.get("balance", 0),
                        "plan": data.get("plan", "unknown"),
                    }
                elif resp.status_code == 401:
                    checks["checks"]["auth_valid"] = {
                        "ok": False, "error": "Invalid API key or token",
                    }
                    checks["checks"]["balance"] = {"ok": False, "error": "Auth failed"}
                else:
                    checks["checks"]["auth_valid"] = {
                        "ok": False, "status_code": resp.status_code,
                    }
                    checks["checks"]["balance"] = {"ok": False, "status_code": resp.status_code}
        except Exception as e:
            checks["checks"]["auth_valid"] = {"ok": False, "error": str(e)}
            checks["checks"]["balance"] = {"ok": False, "error": str(e)}

        # 4. Models
        try:
            async with self._inference_client() as client:
                resp = await client.get(f"{self.inference_url}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    model_list = data.get("data", [])
                    accessible = [m["id"] for m in model_list if m.get("accessible", True)]
                    checks["checks"]["models"] = {
                        "ok": len(accessible) > 0,
                        "total": len(model_list),
                        "accessible": accessible,
                    }
                else:
                    checks["checks"]["models"] = {"ok": False, "status_code": resp.status_code}
        except Exception as e:
            checks["checks"]["models"] = {"ok": False, "error": str(e)}

        # 5. Registered agents
        try:
            agents = await self.my_agents()
            checks["checks"]["agents"] = {
                "ok": True,
                "count": len(agents),
                "names": [a.get("agent_name", a.get("name", "")) for a in agents],
            }
        except Exception as e:
            checks["checks"]["agents"] = {"ok": False, "error": str(e)}

        # Overall readiness
        critical = ["gateway_reachable", "inference_reachable"]
        checks["ready"] = all(
            checks["checks"].get(c, {}).get("ok", False) for c in critical
        )

        return checks
