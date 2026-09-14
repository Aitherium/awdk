"""Secrets sync — 2-way sync between AitherOS vault and local encrypted keyring.

When running standalone, ADK agents store secrets locally in ~/.aither/secrets.enc
(encrypted). This module syncs those secrets from the platform vault so standalone
agents can access credentials configured via the portal without manual setup.

Sync sources (in priority order):
1. AitherSecrets service — when sovereign stack is running (via AITHER_SECRETS_URL)
2. Gateway vault (https://gateway.aitherium.com/v1/secrets) — when online
3. Local encrypted keyring (~/.aither/secrets.enc) — always available as fallback

Usage:
    from adk.sync.secrets import SecretsSync

    # Pull secrets from platform vault into encrypted local store
    client = SecretsSync(api_key="...", secrets_url="http://secrets:8111")
    synced = await client.pull()  # Stores in encrypted keyring

    # Push a local secret to the vault
    await client.push("MY_KEY", "value")

    # Bidirectional sync (pull + push local-only secrets)
    await client.sync()
"""

from __future__ import annotations
from adk._tls import tls_verify

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("adk.secrets_sync")


def _extract_secret_names(payload: object) -> list[str]:
    """Extract secret NAMES from a ``GET /secrets`` response, tolerating every
    shape AitherSecrets may return: a list of metadata dicts
    (``[{"name": ...}, ...]`` — the real shape), a list of plain names, a
    ``{"secrets": {...}}`` envelope, or a flat ``{name: value}`` dict.

    This exists because the naive ``list(d.keys()) if "secrets" not in d else []``
    SILENTLY DROPPED a populated ``{"secrets": {...}}`` envelope (pulled zero
    secrets and looked like success — the fail-closed-looks-like-working trap),
    and ``names = names_data`` passed a list of *dicts* straight to the batch
    endpoint. Both are fixed here in one place.
    """
    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out
    if isinstance(payload, dict):
        inner = payload.get("secrets", payload)
        if isinstance(inner, dict):
            return list(inner.keys())
        if isinstance(inner, list):
            return _extract_secret_names(inner)
    logger.warning("Unrecognized /secrets response shape (%s) — 0 names", type(payload).__name__)
    return []


class SecretsSync:
    """Bidirectional secrets sync with AitherOS vault and encrypted local keyring.

    Fetches secrets from the platform vault (AitherSecrets or Gateway) and stores them
    in the local encrypted keyring (~/.aither/secrets.enc). Also supports pushing
    local-only secrets back to the vault.
    """

    def __init__(
        self,
        api_key: str = "",
        secrets_url: str = "",
        gateway_url: str = "",
    ):
        self.api_key = api_key or os.environ.get("AITHER_API_KEY", "")
        self.secrets_url = (
            secrets_url
            or os.environ.get("AITHER_SECRETS_URL", "")
        ).rstrip("/")
        self.gateway_url = (
            gateway_url
            or os.environ.get("AITHER_GATEWAY_URL", "")
            or os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
        ).rstrip("/")

    async def pull(self) -> Dict[str, str]:
        """Pull secrets from vault and store in encrypted local keyring.

        Returns dict of synced secret names (keys only, values not logged).
        Stores all fetched secrets in the encrypted ~/.aither/secrets.enc file.
        """
        import httpx
        from adk.builtin_tools import _load_secrets, _save_secrets

        synced: Dict[str, str] = {}

        # Try AitherSecrets service directly (sovereign stack)
        if self.secrets_url:
            try:
                async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as client:
                    # Step 1: List secret names
                    resp = await client.get(
                        f"{self.secrets_url}/secrets",
                        headers=self._headers(),
                    )
                    if resp.status_code == 200:
                        names_data = resp.json()
                        # Response is a list of secret metadata or dict with 'secrets' key
                        names = _extract_secret_names(names_data)

                        # Step 2: Fetch secret values via batch endpoint (more efficient)
                        if names:
                            batch_resp = await client.post(
                                f"{self.secrets_url}/secrets/batch",
                                json={"names": names},
                                headers=self._headers(),
                            )
                            if batch_resp.status_code == 200:
                                batch_data = batch_resp.json()
                                synced = batch_data.get("secrets", {})
                            else:
                                # Fallback: fetch individually
                                for name in names:
                                    secret_resp = await client.get(
                                        f"{self.secrets_url}/secrets/{name}",
                                        headers=self._headers(),
                                    )
                                    if secret_resp.status_code == 200:
                                        data = secret_resp.json()
                                        if "value" in data:
                                            synced[name] = data["value"]
                        logger.info("Pulled %d secrets from AitherSecrets", len(synced))
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("AitherSecrets pull failed: %s", exc)

        # Fallback: Try gateway vault (online)
        if not synced and self.api_key and self.gateway_url:
            try:
                async with httpx.AsyncClient(
                    timeout=15, verify=tls_verify()
                ) as client:
                    # Gateway uses the same /secrets endpoints
                    resp = await client.get(
                        f"{self.gateway_url}/secrets",
                        headers=self._headers(),
                    )
                    if resp.status_code == 200:
                        names_data = resp.json()
                        names = _extract_secret_names(names_data)

                        if names:
                            batch_resp = await client.post(
                                f"{self.gateway_url}/secrets/batch",
                                json={"names": names},
                                headers=self._headers(),
                            )
                            if batch_resp.status_code == 200:
                                batch_data = batch_resp.json()
                                synced = batch_data.get("secrets", {})
                        logger.info("Pulled %d secrets from gateway vault", len(synced))
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Gateway secrets pull failed: %s", exc)

        # Store synced secrets in encrypted keyring (merge with existing)
        if synced:
            local = _load_secrets()
            local.update(synced)
            _save_secrets(local)
            logger.info("Synced %d secrets to encrypted keyring", len(synced))

        return synced

    async def push(self, key: str, value: str) -> bool:
        """Push a secret to the platform vault.

        Uses POST /secrets with the correct AitherSecrets API format:
        {name, value, secret_type, access_level, allowed_services, expires_in_days}

        Returns True on success, False otherwise.
        """
        import httpx

        if self.secrets_url:
            try:
                async with httpx.AsyncClient(timeout=10, verify=tls_verify()) as client:
                    resp = await client.post(
                        f"{self.secrets_url}/secrets",
                        json={
                            "name": key,
                            "value": value,
                            "secret_type": "api_key",
                            "access_level": "private",
                            "allowed_services": [],
                            "expires_in_days": None,
                        },
                        headers=self._headers(),
                    )
                    if resp.status_code in (200, 201):
                        logger.info("Pushed secret '%s' to AitherSecrets", key)
                        return True
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Failed to push to AitherSecrets: %s", exc)

        # Fallback: Try gateway vault
        if self.api_key and self.gateway_url:
            try:
                async with httpx.AsyncClient(
                    timeout=15, verify=tls_verify()
                ) as client:
                    resp = await client.post(
                        f"{self.gateway_url}/secrets",
                        json={
                            "name": key,
                            "value": value,
                            "secret_type": "api_key",
                            "access_level": "private",
                            "allowed_services": [],
                            "expires_in_days": None,
                        },
                        headers=self._headers(),
                    )
                    if resp.status_code in (200, 201):
                        logger.info("Pushed secret '%s' to gateway vault", key)
                        return True
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Failed to push to gateway vault: %s", exc)

        logger.warning("Failed to push secret '%s' to any vault", key)
        return False

    async def sync(self) -> Dict[str, str]:
        """Bidirectional sync: pull from vault, then push any local-only secrets.

        Returns dict of all synced secrets (both pulled and pushed).
        """
        from adk.builtin_tools import _load_secrets

        # Step 1: Pull from vault into encrypted keyring
        pulled = await self.pull()

        # Step 2: Push any local-only secrets (those in keyring but not in vault)
        # Only attempt push if we have credentials
        pushed: Dict[str, str] = {}
        if self.api_key and (self.secrets_url or self.gateway_url):
            local = _load_secrets()
            # Find keys that are in local keyring but weren't just pulled
            local_only = {k: v for k, v in local.items() if k not in pulled}
            for key, value in local_only.items():
                if await self.push(key, value):
                    pushed[key] = value

        logger.info(
            "Sync complete: pulled %d, pushed %d secrets",
            len(pulled), len(pushed)
        )
        return {**pulled, **pushed}

    def _headers(self) -> Dict[str, str]:
        """Build request headers with auth credentials.

        Uses Bearer token if api_key is present. Does NOT include X-Tenant-ID
        because scoping is handled by the server based on the Bearer token.
        """
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # X-API-Key is supported as a fallback for service-to-service calls
        # if needed, but should not be included by default
        return headers


async def sync_secrets(
    api_key: str = "",
    secrets_url: str = "",
    gateway_url: str = "",
    bidirectional: bool = False,
) -> Dict[str, str]:
    """Convenience function — sync secrets from/to platform vault.

    Args:
        api_key: API key or Bearer token for authentication
        secrets_url: AitherSecrets service URL
        gateway_url: Gateway service URL (fallback)
        bidirectional: If True, also push local-only secrets to vault (default: False)

    Returns:
        Dict of synced secret names (values not logged).
    """
    client = SecretsSync(
        api_key=api_key,
        secrets_url=secrets_url,
        gateway_url=gateway_url,
    )
    if bidirectional:
        return await client.sync()
    return await client.pull()
