"""auth.md credential store — workspace-scoped vault persistence.

Stores auth.md credentials in AitherSecrets with tenant+workspace scope
so credentials across tenants never cross-match. Each registration stores:
- access_token (expires hourly, refreshed via re-exchange)
- identity_assertion (expires daily, exchanged at /oauth2/token)

NEVER persists claim_token — the spec requires it to be held IN MEMORY ONLY
during the ceremony. Once the ceremony completes, discard the claim_token.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.authmd.store")


@dataclass(frozen=True)
class StoredCredential:
    """A cached auth.md credential pair."""

    access_token: str
    access_token_expires_at: str  # ISO 8601
    identity_assertion: str
    assertion_expires_at: str  # ISO 8601
    registration_id: str
    service_resource: str
    cached_at: str = ""  # ISO 8601, defaults to now

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if not d["cached_at"]:
            d["cached_at"] = datetime.now(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoredCredential":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def access_token_valid(self, now: Optional[datetime] = None) -> bool:
        """Check if access_token is still valid (not within 1 minute of expiry)."""
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.access_token_expires_at.replace("Z", "+00:00"))
            return now < exp - timedelta(minutes=1)
        except (ValueError, AttributeError):
            return False

    def assertion_valid(self, now: Optional[datetime] = None) -> bool:
        """Check if identity_assertion is still valid (not within 1 hour of expiry)."""
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.assertion_expires_at.replace("Z", "+00:00"))
            return now < exp - timedelta(hours=1)
        except (ValueError, AttributeError):
            return False


class AuthMdStore:
    """Persist auth.md credentials in workspace-scoped vault.

    Vault key format: f"authmd:{tenant}:{workspace}:{service_resource}:{user_id}"
    This prevents same-name services across tenants from cross-matching.
    """

    def __init__(
        self,
        tenant: str = "",
        workspace: str = "",
        vault_url: str = "",
        vault_token: str = "",
    ):
        """Initialize the credential store.

        Args:
            tenant: Tenant ID, from the AUTHENTICATED caller (or AITHER_TENANT for a
                    single-tenant self-hosted node, which the operator sets at deploy time).
            workspace: Workspace ID. Same provenance.
            vault_url: AitherSecrets URL. Defaults to env AITHER_SECRETS_URL, else
                      a local "http://127.0.0.1:8111" (managed deployments set the
                      env to point at their own vault).
            vault_token: Internal auth token. Defaults to env AITHER_INTERNAL_SECRET.

        Raises:
            ValueError: if no tenant can be determined.

        The vault key is (tenant, workspace, service, user) — the tenant is the FIRST
        component and the thing that keeps tenant A's Stripe token out of tenant B's
        reach. Defaulting it to the literal string "default" (as this once did) collapses
        every tenant into ONE shared namespace, which is a cross-tenant credential leak
        dressed up as a convenience. If we cannot name the tenant, we must not store or
        retrieve a credential at all.
        """
        self.tenant = tenant or os.getenv("AITHER_TENANT", "")
        self.workspace = workspace or os.getenv("AITHER_WORKSPACE", "") or "_default"
        if not self.tenant:
            raise ValueError(
                "AuthMdStore requires a tenant. Pass tenant= from the authenticated "
                "caller, or set AITHER_TENANT on a single-tenant node. Refusing to fall "
                "back to a shared namespace."
            )
        self.vault_url = (vault_url or os.getenv("AITHER_SECRETS_URL", "")).rstrip("/") or (
            "http://127.0.0.1:8111"
        )
        self.vault_token = vault_token or os.getenv("AITHER_INTERNAL_SECRET", "")

        # Lazy-loaded httpx client
        self._http_client: Optional[Any] = None

    def _get_http_client(self) -> Any:
        """Lazily initialize an httpx client with internal CA verification."""
        if self._http_client is None:
            import httpx

            from adk._tls import tls_verify

            verify = tls_verify()  # internal CA trust
            self._http_client = httpx.Client(verify=verify, timeout=10.0)
        return self._http_client

    def _vault_key(self, service_resource: str, user_id: str = "") -> str:
        """Construct the vault key. Tenant+workspace-first for isolation."""
        user_part = f":{user_id}" if user_id else ""
        return f"authmd:{self.tenant}:{self.workspace}:{service_resource}{user_part}"

    async def get(self, service_resource: str, user_id: str = "") -> Optional[StoredCredential]:
        """Retrieve a cached credential from the vault.

        Args:
            service_resource: The resource URL (e.g., "https://mcp.aitherium.com/")
            user_id: Optional user context for multi-user agents.

        Returns:
            StoredCredential if found and valid; None otherwise.
        """
        if not self.vault_token:
            logger.warning("[authmd] AITHER_INTERNAL_SECRET unset, cannot read vault")
            return None

        key = self._vault_key(service_resource, user_id)
        try:
            client = self._get_http_client()
            resp = client.get(
                f"{self.vault_url}/v1/secrets/get/{key}",
                headers={"X-API-Key": self.vault_token},
            )
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                logger.warning("[authmd] vault get failed: %s %s", resp.status_code, resp.text)
                return None

            data = resp.json().get("data", {})
            cred = StoredCredential.from_dict(data)
            if cred.access_token_valid():
                return cred
            logger.debug("[authmd] cached credential expired")
            return None
        except Exception as e:
            logger.warning("[authmd] vault read error: %s", e)
            return None

    async def put(self, service_resource: str, cred: StoredCredential, user_id: str = "") -> bool:
        """Store a credential in the vault.

        Args:
            service_resource: The resource URL.
            cred: The StoredCredential to persist.
            user_id: Optional user context.

        Returns:
            True on success, False on error.
        """
        if not self.vault_token:
            logger.warning("[authmd] AITHER_INTERNAL_SECRET unset, cannot write vault")
            return False

        key = self._vault_key(service_resource, user_id)
        try:
            client = self._get_http_client()
            resp = client.post(
                f"{self.vault_url}/v1/secrets/set/{key}",
                json={"data": cred.to_dict()},
                headers={"X-API-Key": self.vault_token},
            )
            if resp.status_code not in (200, 201):
                logger.warning("[authmd] vault put failed: %s %s", resp.status_code, resp.text)
                return False
            logger.debug("[authmd] credential persisted")
            return True
        except Exception as e:
            logger.warning("[authmd] vault write error: %s", e)
            return False

    async def delete(self, service_resource: str, user_id: str = "") -> bool:
        """Revoke a stored credential (post-logout or registration revocation).

        Args:
            service_resource: The resource URL.
            user_id: Optional user context.

        Returns:
            True on success, False on error.
        """
        if not self.vault_token:
            return False

        key = self._vault_key(service_resource, user_id)
        try:
            client = self._get_http_client()
            resp = client.delete(
                f"{self.vault_url}/v1/secrets/delete/{key}",
                headers={"X-API-Key": self.vault_token},
            )
            if resp.status_code not in (200, 204):
                logger.warning("[authmd] vault delete failed: %s", resp.status_code)
                return False
            logger.debug("[authmd] credential revoked")
            return True
        except Exception as e:
            logger.warning("[authmd] vault delete error: %s", e)
            return False
