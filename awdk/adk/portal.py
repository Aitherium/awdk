"""Tiny client for api.aitherium.com — pull agent specs, push telemetry.

Designed for the binary-install path: a single ``aither`` binary on a
laptop can fetch an agent spec from the portal, run it locally with a
local orchestrator model, and stream back telemetry/usage events to the
portal so the user sees their fleet in one place.

Credentials are resolved by :mod:`aither_adk.auth` — env var first, then
the shared ``~/.aither/auth.json`` store (same file ``aithershell`` uses),
then a no-op local-root profile. If the resolved credentials are local-only
the methods quietly no-op so offline use stays offline.

Environment:

- ``AITHERIUM_API_KEY``  — ACTA bearer or session token. Overrides the file.
- ``AITHERIUM_BASE_URL`` — defaults to ``https://api.aitheros.ai``
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from adk.auth import Credentials, resolve_credentials
from adk.core.logging import get_logger

_log = get_logger("aither_adk.portal")

DEFAULT_BASE_URL = "https://api.aitheros.ai"


@dataclass(slots=True)
class PortalConfig:
    base_url: str = DEFAULT_BASE_URL
    credentials: Credentials | None = None
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "PortalConfig":
        creds = resolve_credentials()
        base = os.environ.get("AITHERIUM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        # If creds came from a real OAuth/ACTA login, prefer that endpoint.
        if creds.endpoint and creds.endpoint != "local":
            base = creds.endpoint
        return cls(
            base_url=base,
            credentials=creds,
            timeout=float(os.environ.get("AITHERIUM_TIMEOUT", "30")),
        )

    @property
    def configured(self) -> bool:
        c = self.credentials
        return bool(c and c.access_token and not c.is_local and not c.is_expired)

    # Legacy compat — older callers built PortalConfig with api_key=
    @property
    def api_key(self) -> str | None:
        return self.credentials.access_token if self.credentials else None


class PortalClient:
    """Minimal portal HTTPS client. Offline-safe: methods no-op when not
    configured (local-root profile, no API key)."""

    def __init__(
        self,
        config: PortalConfig | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        if config is None:
            config = PortalConfig.from_env()
        if api_key:
            # Explicit override; treat as a bearer token.
            config = PortalConfig(
                base_url=config.base_url,
                credentials=Credentials(access_token=api_key, token_type="bearer"),
                timeout=config.timeout,
            )
        self.config = config

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.config.configured:
            _log.debug("portal.skip", extra={"reason": "no credentials"})
            return None
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "PortalClient requires httpx. "
                "Install with: pip install 'awdk[full]'"
            ) from e
        url = f"{self.config.base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}
        assert self.config.credentials is not None
        headers.setdefault(
            "Authorization", self.config.credentials.authorization_header()
        )
        headers.setdefault("Accept", "application/json")
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/json"):
                return resp.json()
            return resp.text

    # ---- read paths ------------------------------------------------------

    async def whoami(self) -> dict[str, Any] | None:
        """Confirm credentials by calling AitherIdentity's ``/auth/me``."""
        return await self._request("GET", "/auth/me")

    async def get_agent_spec(self, name: str) -> dict[str, Any] | None:
        """Pull a published agent card from the portal by name."""
        return await self._request("GET", f"/v1/agents/{name}/spec")

    async def list_agents(self) -> list[dict[str, Any]] | None:
        return await self._request("GET", "/v1/agents")

    async def list_orchestrator_models(self) -> list[dict[str, Any]] | None:
        """List orchestrator models the portal offers (e.g. nemotron)."""
        return await self._request("GET", "/v1/orchestrator/models")

    # ---- write paths -----------------------------------------------------

    async def report_run(
        self,
        *,
        agent_name: str,
        prompt: str,
        output: str,
        steps: int,
        finish_reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Push a single agent run as telemetry to api.aitherium.com."""
        payload = {
            "agent": agent_name,
            "prompt": prompt,
            "output": output,
            "steps": steps,
            "finish_reason": finish_reason,
            "extra": extra or {},
        }
        await self._request("POST", "/v1/telemetry/agent_run", json=payload)

    async def register_local_agent(
        self, spec: dict[str, Any], *, endpoint: str | None = None
    ) -> dict[str, Any] | None:
        """Tell the portal we are running this agent locally.

        Mirrors the auto-onboard flow described in ``AGENTS.md`` — gives
        the user a unified view of cloud + local agents in the portal UI.
        """
        body = {"spec": spec, "endpoint": endpoint}
        return await self._request("POST", "/v1/agents/register-local", json=body)

    # ---- marketplace: browse / discover / apply --------------------------
    #
    # Programmatic equivalent of the `adk pack` / `adk explore` CLI commands
    # (see adk/cli.py:_cmd_pack, :_load_pack_catalog) so a local agent loop
    # can browse and install agent/skill/tool packs without shelling out.
    # Marketplace endpoints live on portal.aitherium.com specifically, which
    # may differ from self.config.base_url (that defaults to the ACTA/agent
    # API host) — resolved the same way the CLI does.

    def _marketplace_base_url(self) -> str:
        return (
            os.environ.get("AITHER_PORTAL_URL")
            or os.environ.get("AITHER_ELYSIUM_URL")
            or "https://api.aitherium.com"
        ).rstrip("/")

    async def list_packs(self) -> list[dict[str, Any]]:
        """List all agent/skill/tool packs in the marketplace catalog.

        Tries the live catalog endpoint first, falls back to the bundled
        offline snapshot (``adk/data/packs_catalog.json``) so this always
        returns something, even offline — same fallback chain as
        ``adk pack list``.
        """
        base = self._marketplace_base_url()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                for path in ("/api/v1/catalog/packs", "/v1/packs/catalog"):
                    resp = await client.get(f"{base}{path}")
                    if resp.status_code == 200:
                        return resp.json().get("packs", [])
        except Exception as e:  # noqa: BLE001
            _log.debug("portal.list_packs.live_failed", extra={"error": str(e)})

        try:
            from pathlib import Path

            bundled = Path(__file__).parent / "data" / "packs_catalog.json"
            if bundled.exists():
                import json as _json

                return _json.loads(bundled.read_text(encoding="utf-8")).get("packs", [])
        except Exception as e:  # noqa: BLE001
            _log.debug("portal.list_packs.bundled_failed", extra={"error": str(e)})
        return []

    async def search_packs(self, query: str) -> list[dict[str, Any]]:
        """Search the pack catalog by name, description, id, or tag."""
        q = query.lower()
        return [
            p
            for p in await self.list_packs()
            if q in p.get("name", "").lower()
            or q in p.get("description", "").lower()
            or q in p.get("id", "").lower()
            or any(q in tag.lower() for tag in p.get("tags", []))
        ]

    def _marketplace_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.config.api_key
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Aither-Api-Key"] = token
        return headers

    async def negotiate_pack(
        self, pack_id: str, offer_credits: int, rationale: str = ""
    ) -> dict[str, Any]:
        """Negotiate a price for a paid pack. Returns a decision, and a
        ``negotiation_token`` to redeem via :meth:`purchase_pack` if accepted."""
        import httpx

        base = self._marketplace_base_url()
        body = {
            "listing_id": pack_id,
            "offer_credits": int(offer_credits),
            "rationale": rationale,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/marketplace/negotiate",
                json=body,
                headers=self._marketplace_headers(),
            )
            return resp.json() if resp.content else {}

    async def purchase_pack(
        self, pack_id: str, negotiation_token: str | None = None
    ) -> dict[str, Any]:
        """Purchase a pack listing (no-op cost for free packs)."""
        import httpx

        base = self._marketplace_base_url()
        body: dict[str, Any] = {"listing_id": pack_id}
        if negotiation_token:
            body["negotiation_token"] = negotiation_token
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/v1/marketplace/purchase",
                json=body,
                headers=self._marketplace_headers(),
            )
            data = resp.json() if resp.content else {}
            data.setdefault("_status_code", resp.status_code)
            return data

    def install_pack(self, pack_id: str) -> str:
        """Download, verify, and apply an already-licensed pack.

        Synchronous — delegates to :class:`~adk.shell.plugins.builtins.packs.PacksPlugin`,
        the same download→:mod:`adk.pack_verifier`-verify→extract path
        ``adk pack sync``/``adk pack buy --install`` use, so packs land in
        ``~/.aitheros/packs`` and are immediately usable by local agent loops.
        """
        from adk.shell.plugins.builtins.packs import PacksPlugin

        plugin = PacksPlugin()
        plugin._base_url = self._marketplace_base_url()
        token = self.config.api_key
        if token:
            tenant_id = (
                self.config.credentials.user.get("tenant_id")
                if self.config.credentials
                else None
            ) or os.environ.get("AITHER_TENANT_ID")
            plugin.auth.set_auth(token, tenant_id)
        return plugin._sync([pack_id] if pack_id else [])

