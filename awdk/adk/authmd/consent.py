"""auth.md consent ceremony — human confirmation for agent registration.

When an agent registers without an ID-JAG (service_auth or identity_assertion with
existing account collision), a human must confirm the agent should act on their behalf.
The ceremony surfaces a 6-digit code + verification URL to the human (via Relay DM +
portal inbox), and the agent polls the token endpoint until the human completes it.

claim_token is returned ONCE and must never be persisted — hold it in memory only
during the ceremony, then discard it once /oauth2/token completes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger("adk.authmd.consent")


class ConsentRequiredError(Exception):
    """The agent needs human approval to complete registration.

    Surface verification_uri + user_code to the user via Relay DM or portal inbox,
    wait for confirmation via poll, or abandon if timeout expires.
    """

    def __init__(
        self,
        registration_id: str,
        claim_token: str,
        user_code: str,
        verification_uri: str,
        expires_in: int,
        interval: int,
    ):
        super().__init__(f"user_code {user_code} @ {verification_uri}")
        self.registration_id = registration_id
        self.claim_token = claim_token  # Memory-only, never persisted
        self.user_code = user_code
        self.verification_uri = verification_uri
        self.expires_in = expires_in  # seconds until user_code expires
        self.interval = interval  # polling interval


@dataclass
class CeremonyState:
    """Tracks in-flight ceremony state for polling."""

    registration_id: str
    claim_token: str  # Memory-only
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    deadline: float  # time.time() when ceremony window closes


class ConsentHandler:
    """Orchestrates the human consent ceremony."""

    def __init__(
        self,
        token_endpoint: str,
        relay_surface_fn: Optional[Callable[[str, str, str], Any]] = None,
        http_client: Optional[Any] = None,
    ):
        """Initialize the consent handler.

        Args:
            token_endpoint: The /oauth2/token endpoint URL for polling.
            relay_surface_fn: Optional async callable to surface code+URL to user.
                             Signature: async fn(user_code, verification_uri, registration_id).
                             If None, surfacing is skipped (tests).
            http_client: Optional httpx.AsyncClient to use for polling. If not provided,
                        one is created.
        """
        self.token_endpoint = token_endpoint
        self.relay_surface_fn = relay_surface_fn
        self.http_client = http_client
        self._owns_http_client = http_client is None

    async def surface_and_poll(
        self,
        err: ConsentRequiredError,
        timeout_s: int = 600,
    ) -> Dict[str, Any]:
        """Surface consent request to user and poll until ceremony completes.

        Args:
            err: The ConsentRequiredError raised during registration.
            timeout_s: How long to wait for user confirmation (seconds).
                      Defaults to 10 minutes. Raises TimeoutError if exceeded.

        Returns:
            The OAuth token response: {access_token, token_type, expires_in, scope, ...}

        Raises:
            TimeoutError: If user doesn't confirm within timeout_s.
            RuntimeError: On fatal errors (bad claim_token, registration expired).
        """
        state = CeremonyState(
            registration_id=err.registration_id,
            claim_token=err.claim_token,
            user_code=err.user_code,
            verification_uri=err.verification_uri,
            expires_in=err.expires_in,
            interval=err.interval,
            deadline=time.time() + timeout_s,
        )

        # Surface the request to the user (via Relay DM + portal inbox if available)
        if self.relay_surface_fn:
            try:
                await self.relay_surface_fn(
                    user_code=state.user_code,
                    verification_uri=state.verification_uri,
                    registration_id=state.registration_id,
                )
                logger.info(
                    "[authmd] ceremony surfaced to user: code=%s", state.user_code
                )
            except Exception as e:
                logger.warning("[authmd] failed to surface ceremony: %s", e)
                # Don't fail here — the ceremony can still complete, just surfacing failed
        else:
            logger.debug(
                "[authmd] no relay surface fn, ceremony code=%s uri=%s",
                state.user_code,
                state.verification_uri,
            )

        # Poll the token endpoint honouring interval and expiry
        return await self._poll_until_complete(state)

    async def _poll_until_complete(self, state: CeremonyState) -> Dict[str, Any]:
        """Poll /oauth2/token with the claim grant until complete or timeout."""
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30.0)

        client = self.http_client
        next_poll = time.time()  # poll immediately first

        try:
            while time.time() < state.deadline:
                # Honor polling interval
                now = time.time()
                if now < next_poll:
                    delay = min(next_poll - now, 1.0)  # don't wait >1s at a time
                    await asyncio.sleep(delay)
                    continue

                # Poll the token endpoint
                try:
                    resp = await client.post(
                        self.token_endpoint,
                        data={
                            "grant_type": "urn:workos:agent-auth:grant-type:claim",
                            "claim_token": state.claim_token,
                        },
                    )

                    if resp.status_code == 200:
                        body = resp.json()
                        logger.info(
                            "[authmd] ceremony complete: reg=%s", state.registration_id
                        )
                        return body

                    body = resp.json() if resp.headers.get("content-type") else {}
                    error = body.get("error", "")

                    if error == "authorization_pending":
                        # Still waiting for user to confirm
                        next_poll = time.time() + state.interval
                        logger.debug("[authmd] ceremony pending, next poll in %ds", state.interval)
                        continue

                    if error == "expired_token":
                        # user_code window expired, try to mint a fresh one
                        logger.warning("[authmd] user_code expired, reinitiating")
                        # TODO: re-call /agent/identity/claim to get fresh codes
                        # For now, treat as unrecoverable
                        raise RuntimeError(
                            "user_code expired; re-call /agent/identity/claim "
                            "to get fresh codes"
                        )

                    if error == "slow_down":
                        # Back off polling interval
                        state.interval += 5
                        logger.warning("[authmd] slow_down, interval now %ds", state.interval)
                        next_poll = time.time() + state.interval
                        continue

                    if error == "claim_expired":
                        # The entire claim window is closed
                        raise RuntimeError(
                            "claim window expired; the registration is no longer active. "
                            "Start over at Step 3."
                        )

                    # Unexpected error
                    raise RuntimeError(f"ceremony poll failed: {error} — {body.get('error_description')}")

                except httpx.RequestError as e:
                    logger.warning("[authmd] ceremony poll network error: %s", e)
                    next_poll = time.time() + state.interval
                    continue

            # Timeout
            raise TimeoutError(
                f"user did not confirm within {state.deadline - time.time() + state.interval:.0f}s. "
                "Re-call /agent/identity to create a new registration."
            )

        finally:
            if self._owns_http_client and self.http_client:
                await self.http_client.aclose()
                self.http_client = None
