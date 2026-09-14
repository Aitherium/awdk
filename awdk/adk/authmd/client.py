"""auth.md client — full agent registration & token acquisition flow.

Main entry point for auth.md protocol. Implements all 6 steps:
1. Discover — parse 401, fetch PRM, fetch AS metadata
2. Pick method — identity_assertion, service_auth, anonymous
3. Register — POST /agent/identity
4. Claim ceremony (optional) — surface code to user, poll completion
5. Exchange — POST /oauth2/token with assertion
6. Use — Bearer token, refresh via re-exchange

The client is STATEFUL per registration — one instance per agent/resource pair.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import httpx

from adk._tls import tls_verify

from .consent import ConsentHandler, ConsentRequiredError
from .store import AuthMdStore, StoredCredential

logger = logging.getLogger("adk.authmd.client")


class AuthMdError(Exception):
    """A protocol-level rejection. Carries the error code and HTTP status."""

    def __init__(self, code: str, description: str = "", status_code: int = 400):
        super().__init__(description or code)
        self.code = code
        self.description = description or code
        self.status_code = status_code


@dataclass
class DiscoveredMetadata:
    """Parsed auth.md service metadata."""

    resource: str
    resource_name: str
    resource_logo_uri: str
    scopes_supported: list[str]
    issuer: str
    token_endpoint: str
    revocation_endpoint: str
    grant_types_supported: list[str]
    identity_endpoint: str
    claim_endpoint: str
    identity_types_supported: list[str]
    events_endpoint: str


@dataclass
class Registration:
    """A completed registration — ready for exchange or ceremony."""

    registration_id: str
    registration_type: str  # "identity_assertion", "service_auth", "anonymous"
    identity_assertion: str = ""  # present if already available
    assertion_expires: str = ""  # ISO 8601
    scopes: list[str] = None  # post-claim scopes if claimed

    def __post_init__(self):
        if self.scopes is None:
            self.scopes = []


class AuthMdClient:
    """Orchestrates auth.md agent registration and credential acquisition.

    Usage:
        client = AuthMdClient()
        metadata = await client.discover("https://api.service.example.com/")
        reg = await client.register("anonymous")
        token = await client.exchange_assertion(reg)
    """

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        store: Optional[AuthMdStore] = None,
        consent_handler: Optional[ConsentHandler] = None,
        tenant_id: str = "",
        workspace_id: str = "",
    ):
        """Initialize the client.

        Args:
            http_client: Optional httpx.AsyncClient. If not provided, one is created
                        with internal CA verification.
            store: Credential store. If not provided, an AitherSecrets-backed store is
                   created, scoped to `tenant_id`/`workspace_id`.
            consent_handler: Consent ceremony handler.
            tenant_id / workspace_id: the scope the acquired credentials are filed under.
                These come from the AUTHENTICATED caller. They are what keeps one
                tenant's token for a service out of another tenant's reach — passing
                them is not optional in a multi-tenant process.
        """
        self.http_client = http_client
        self._owns_http_client = http_client is None
        self.store = store or AuthMdStore(tenant=tenant_id, workspace=workspace_id)
        self.consent_handler = consent_handler or ConsentHandler(
            token_endpoint="https://localhost/oauth2/token"  # will be set on discover
        )

        # Discovered metadata (set by discover())
        self.metadata: Optional[DiscoveredMetadata] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Lazily initialize httpx with internal CA verification."""
        if self.http_client is None:
            verify = tls_verify()  # internal CA trust
            self.http_client = httpx.AsyncClient(verify=verify, timeout=30.0)
        return self.http_client

    async def discover(self, prm_url_or_resource: str) -> DiscoveredMetadata:
        """Discover the auth.md service metadata.

        Args:
            prm_url_or_resource: The PRM URL (from WWW-Authenticate header) or
                               a resource URL (will append /.well-known/...).

        Returns:
            DiscoveredMetadata with all endpoints and supported features.

        Raises:
            AuthMdError: If discovery fails at any step.
        """
        if not prm_url_or_resource:
            raise AuthMdError("invalid_request", "prm_url_or_resource required")

        # Normalize: if it looks like a resource URL, append the well-known path
        if prm_url_or_resource.startswith("http"):
            parsed = urlparse(prm_url_or_resource)
            if "/.well-known/" not in prm_url_or_resource:
                prm_url = urljoin(
                    f"{parsed.scheme}://{parsed.netloc}",
                    "/.well-known/oauth-protected-resource",
                )
            else:
                prm_url = prm_url_or_resource
        else:
            prm_url = prm_url_or_resource

        client = await self._get_http_client()

        # Step 1a: Fetch Protected Resource Metadata
        try:
            resp = await client.get(prm_url)
            if resp.status_code != 200:
                raise AuthMdError("invalid_request", f"PRM fetch returned {resp.status_code}")
            prm = resp.json()
        except AuthMdError:
            raise
        except Exception as e:
            raise AuthMdError("invalid_request", f"PRM fetch failed: {e}")

        resource = prm.get("resource", "")
        resource_name = prm.get("resource_name", "Service")
        resource_logo_uri = prm.get("resource_logo_uri", "")
        scopes_supported = prm.get("scopes_supported", [])
        auth_servers = prm.get("authorization_servers", [])

        if not resource or not auth_servers:
            raise AuthMdError("invalid_request", "PRM missing resource or authorization_servers")

        # Step 1b: Fetch Authorization Server metadata
        as_url = auth_servers[0].rstrip("/") + "/.well-known/oauth-authorization-server"
        try:
            resp = await client.get(as_url)
            if resp.status_code != 200:
                raise AuthMdError("invalid_request", f"AS metadata fetch returned {resp.status_code}")
            as_meta = resp.json()
        except AuthMdError:
            raise
        except Exception as e:
            raise AuthMdError("invalid_request", f"AS metadata fetch failed: {e}")

        issuer = as_meta.get("issuer", "")
        token_endpoint = as_meta.get("token_endpoint", "")
        revocation_endpoint = as_meta.get("revocation_endpoint", "")
        grant_types = as_meta.get("grant_types_supported", [])

        agent_auth = as_meta.get("agent_auth", {})
        if not agent_auth:
            raise AuthMdError("invalid_request", "AS metadata missing agent_auth block")

        identity_endpoint = agent_auth.get("identity_endpoint", "")
        claim_endpoint = agent_auth.get("claim_endpoint", "")
        identity_types = agent_auth.get("identity_types_supported", [])
        events_endpoint = agent_auth.get("events_endpoint", "")

        if not identity_endpoint or not token_endpoint:
            raise AuthMdError("invalid_request", "AS metadata missing identity or token endpoint")

        self.metadata = DiscoveredMetadata(
            resource=resource,
            resource_name=resource_name,
            resource_logo_uri=resource_logo_uri,
            scopes_supported=scopes_supported,
            issuer=issuer,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
            grant_types_supported=grant_types,
            identity_endpoint=identity_endpoint,
            claim_endpoint=claim_endpoint,
            identity_types_supported=identity_types,
            events_endpoint=events_endpoint,
        )

        # Update consent handler to use the discovered token endpoint
        self.consent_handler.token_endpoint = token_endpoint

        logger.info(
            "[authmd] discovered: resource=%s issuer=%s", resource, issuer
        )
        return self.metadata

    async def register(
        self,
        method: str,
        id_jag: str = "",
        email: str = "",
    ) -> Registration:
        """Register an agent identity.

        Args:
            method: Registration method: "identity_assertion", "service_auth", "anonymous".
            id_jag: The ID-JAG JWT (required if method="identity_assertion").
            email: User email (required if method="service_auth").

        Returns:
            Registration with registration_id and optional identity_assertion.

        Raises:
            ConsentRequiredError: If the service requires human confirmation (claim ceremony).
            AuthMdError: On protocol errors.
        """
        if not self.metadata:
            raise AuthMdError("invalid_request", "Must call discover() first")

        if method not in self.metadata.identity_types_supported:
            raise AuthMdError(
                f"{method}_not_enabled",
                f"Service does not support {method} registration",
            )

        client = await self._get_http_client()
        body: Dict[str, Any] = {"type": method}

        if method == "identity_assertion":
            if not id_jag:
                raise AuthMdError("invalid_request", "id_jag required for identity_assertion")
            body["assertion_type"] = "urn:ietf:params:oauth:token-type:id-jag"
            body["assertion"] = id_jag
        elif method == "service_auth":
            if not email:
                raise AuthMdError("invalid_request", "email required for service_auth")
            body["login_hint"] = email
        elif method != "anonymous":
            raise AuthMdError("invalid_request", f"unknown registration method: {method}")

        try:
            resp = await client.post(
                self.metadata.identity_endpoint,
                json=body,
            )
        except Exception as e:
            raise AuthMdError("invalid_request", f"Registration POST failed: {e}")

        if resp.status_code == 401:
            # Claim ceremony required
            resp_body = resp.json()
            error = resp_body.get("error", "")
            if error in ("interaction_required", "login_required"):
                if error == "login_required":
                    raise AuthMdError(
                        "login_required",
                        resp_body.get("error_description", "Re-authenticate at the provider"),
                        status_code=401,
                    )
                # interaction_required — start ceremony
                claim_block = resp_body.get("claim", {})
                raise ConsentRequiredError(
                    registration_id=resp_body.get("registration_id", ""),
                    claim_token=resp_body.get("claim_token", ""),
                    user_code=claim_block.get("user_code", ""),
                    verification_uri=claim_block.get("verification_uri", ""),
                    expires_in=claim_block.get("expires_in", 600),
                    interval=claim_block.get("interval", 5),
                )
            raise AuthMdError(error or "interaction_required", resp_body.get("error_description"))

        if resp.status_code != 200:
            body = resp.json()
            raise AuthMdError(
                body.get("error", "invalid_request"),
                body.get("error_description", ""),
                status_code=resp.status_code,
            )

        reg_data = resp.json()
        scopes = reg_data.get("post_claim_scopes", reg_data.get("scopes", []))

        # Check if ceremony is needed
        claim_block = reg_data.get("claim")
        if claim_block:
            raise ConsentRequiredError(
                registration_id=reg_data.get("registration_id", ""),
                claim_token=reg_data.get("claim_token", ""),
                user_code=claim_block.get("user_code", ""),
                verification_uri=claim_block.get("verification_uri", ""),
                expires_in=claim_block.get("expires_in", 600),
                interval=claim_block.get("interval", 5),
            )

        reg = Registration(
            registration_id=reg_data.get("registration_id", ""),
            registration_type=method,
            identity_assertion=reg_data.get("identity_assertion", ""),
            assertion_expires=reg_data.get("assertion_expires", ""),
            scopes=scopes,
        )

        logger.info(
            "[authmd] registered: id=%s type=%s", reg.registration_id, reg.registration_type
        )
        return reg

    async def exchange_assertion(
        self,
        reg: Registration,
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Exchange a service-signed identity_assertion for an access_token.

        If a cached token exists and is still valid, returns it. Otherwise,
        re-exchanges the assertion.

        Args:
            reg: The Registration from register().
            user_id: Optional user context for multi-user agents.

        Returns:
            Token response: {access_token, token_type, expires_in, scope, ...}

        Raises:
            AuthMdError: On exchange failures.
        """
        if not self.metadata:
            raise AuthMdError("invalid_request", "Must call discover() first")
        if not reg.identity_assertion:
            raise AuthMdError("invalid_request", "Registration has no identity_assertion")

        # Check cache first
        cached = await self.store.get(self.metadata.resource, user_id)
        if cached and cached.access_token_valid():
            logger.debug("[authmd] using cached access_token")
            return {
                "access_token": cached.access_token,
                "token_type": "Bearer",
                "expires_in": int(cached.access_token_expires_at),
                "scope": " ".join(reg.scopes),
            }

        client = await self._get_http_client()

        try:
            resp = await client.post(
                self.metadata.token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": reg.identity_assertion,
                    "resource": self.metadata.resource,
                },
            )
        except Exception as e:
            raise AuthMdError("invalid_request", f"Token exchange POST failed: {e}")

        if resp.status_code != 200:
            body = resp.json() if resp.headers.get("content-type") else {}
            raise AuthMdError(
                body.get("error", "invalid_request"),
                body.get("error_description", ""),
                status_code=resp.status_code,
            )

        token = resp.json()
        access_token = token.get("access_token", "")
        if not access_token:
            raise AuthMdError("invalid_request", "Token response missing access_token")

        # Cache the token
        expires_in = int(token.get("expires_in", 3600))
        expires_at = time.time() + expires_in
        cred = StoredCredential(
            access_token=access_token,
            access_token_expires_at=str(expires_at),
            identity_assertion=reg.identity_assertion,
            assertion_expires_at=reg.assertion_expires,
            registration_id=reg.registration_id,
            service_resource=self.metadata.resource,
        )
        await self.store.put(self.metadata.resource, cred, user_id)

        logger.info("[authmd] token exchanged: id=%s expires_in=%ds", reg.registration_id, expires_in)
        return token

    async def poll_ceremony(
        self,
        consent_err: ConsentRequiredError,
        timeout_s: int = 600,
        user_id: str = "",
    ) -> Dict[str, Any]:
        """Poll until the claim ceremony completes, then exchange the token.

        Args:
            consent_err: The ConsentRequiredError raised by register().
            timeout_s: How long to wait for user confirmation (seconds).
            user_id: Optional user context.

        Returns:
            Token response (same as exchange_assertion()).

        Raises:
            TimeoutError: If user doesn't confirm within timeout_s.
            AuthMdError: On protocol errors.
        """
        if not self.metadata:
            raise AuthMdError("invalid_request", "Must call discover() first")

        # Poll until ceremony completes
        token = await self.consent_handler.surface_and_poll(consent_err, timeout_s=timeout_s)

        # The ceremony may return the access_token directly (if claim grant is used)
        # or an identity_assertion for further exchange (if jwt-bearer grant is used).
        access_token = token.get("access_token", "")
        identity_assertion = token.get("identity_assertion", "")

        if not access_token and identity_assertion:
            # Need to exchange the assertion
            reg = Registration(
                registration_id=consent_err.registration_id,
                registration_type="identity_assertion",
                identity_assertion=identity_assertion,
                assertion_expires=token.get("assertion_expires", ""),
            )
            return await self.exchange_assertion(reg, user_id)

        # Cache the token
        if access_token:
            expires_in = int(token.get("expires_in", 3600))
            expires_at = time.time() + expires_in
            cred = StoredCredential(
                access_token=access_token,
                access_token_expires_at=str(expires_at),
                identity_assertion=identity_assertion or consent_err.claim_token,
                assertion_expires_at=token.get("assertion_expires", ""),
                registration_id=consent_err.registration_id,
                service_resource=self.metadata.resource,
            )
            await self.store.put(self.metadata.resource, cred, user_id)

        logger.info("[authmd] ceremony complete: id=%s", consent_err.registration_id)
        return token

    async def revoke_token(
        self,
        access_token: str,
    ) -> bool:
        """Revoke an access_token via the revocation endpoint.

        Args:
            access_token: The token to revoke.

        Returns:
            True on success or if already revoked (idempotent).
        """
        if not self.metadata:
            logger.warning("[authmd] revoke: no metadata, skipping")
            return False

        client = await self._get_http_client()

        try:
            resp = await client.post(
                self.metadata.revocation_endpoint,
                data={
                    "token": access_token,
                    "token_type_hint": "access_token",
                },
            )
            # 200/204 = success, idempotent
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.warning("[authmd] revoke failed: %s", e)
            return False

    async def acquire_token(
        self,
        prm_url_or_resource: str,
        *,
        user_id: str = "",
        email: str = "",
        id_jag: str = "",
        allow_ceremony: bool = True,
    ) -> Optional[str]:
        """One call: discover -> pick a method -> register -> [ceremony] -> access_token.

        This is the method every CONSUMER actually wants (AitherBrowser's 401 handler,
        awnode's tool dispatch). The step-by-step methods above remain for callers
        that need to drive the ceremony themselves.

        Scopes are NOT passed in. They come from the target service's PRM
        (`scopes_supported`) — asking for scopes of our own invention would just earn an
        `invalid_scope`, and the service is the only authority on its own vocabulary.
        (An earlier version of the 401 handler hardcoded `["api.read","api.write"]`,
        which are the WorkOS *example's* placeholders and exist at no real service.)

        Returns the access_token, or None if a credential could not be obtained.
        Never raises for the ordinary "we couldn't get one" case — the caller falls back
        to browsing/calling anonymously.
        """
        try:
            meta = await self.discover(prm_url_or_resource)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[authmd] discovery failed for %s: %s", prm_url_or_resource, exc)
            return None

        # auth.md Step 2 — the decision tree, in the spec's order of preference.
        supported = set(meta.identity_types_supported or [])
        if id_jag and "identity_assertion" in supported:
            method = "identity_assertion"
        elif email and "service_auth" in supported:
            method = "service_auth"
        elif "anonymous" in supported:
            method = "anonymous"
        else:
            logger.warning(
                "[authmd] %s supports none of the methods we can offer (%s)",
                prm_url_or_resource, sorted(supported),
            )
            return None

        try:
            reg = await self.register(method=method, id_jag=id_jag, email=email)
        except ConsentRequiredError as exc:
            # The service wants a human to confirm a 6-digit code (step-up, service_auth,
            # or an anonymous claim). That needs a person, so it is opt-in.
            if not allow_ceremony:
                logger.info("[authmd] %s needs a human ceremony; not attempting", prm_url_or_resource)
                return None
            try:
                token = await self.poll_ceremony(exc)
                return (token or {}).get("access_token") or None
            except Exception as inner:  # noqa: BLE001
                logger.warning("[authmd] ceremony did not complete: %s", inner)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[authmd] registration at %s failed: %s", prm_url_or_resource, exc)
            return None

        if not reg.identity_assertion:
            logger.info(
                "[authmd] registered at %s but got no identity_assertion (a ceremony is "
                "required before this agent can act)", prm_url_or_resource,
            )
            return None

        try:
            tok = await self.exchange_assertion(reg, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[authmd] assertion exchange failed: %s", exc)
            return None
        return (tok or {}).get("access_token") or None

    async def close(self):
        """Close the HTTP client if we own it."""
        if self._owns_http_client and self.http_client:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
