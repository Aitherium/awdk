"""
AitherOS Registry Authentication
==================================

Handles GHCR token exchange — validates API key against gateway,
receives short-lived GHCR read token for image pulls.

Phase 1: GHCR packages are public (no token needed for pull).
Phase 2: Gateway exchanges API key → scoped GHCR token.
"""

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GHCR_TOKEN_FILE = Path.home() / ".aither" / "ghcr-token.json"
GATEWAY_URL = "https://gateway.aitherium.com"


class GHCRCredentials:
    """Short-lived GHCR pull token."""

    def __init__(self, token: str, expires_at: str, username: str = "aither"):
        self.token = token
        self.expires_at = expires_at
        self.username = username

    @property
    def is_valid(self) -> bool:
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return exp > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    def save(self) -> None:
        GHCR_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        GHCR_TOKEN_FILE.write_text(
            json.dumps({
                "token": self.token,
                "username": self.username,
                "expires_at": self.expires_at,
            }, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> Optional["GHCRCredentials"]:
        if not GHCR_TOKEN_FILE.exists():
            return None
        try:
            data = json.loads(GHCR_TOKEN_FILE.read_text(encoding="utf-8"))
            creds = cls(
                token=data["token"],
                expires_at=data["expires_at"],
                username=data.get("username", "aither"),
            )
            return creds if creds.is_valid else None
        except (json.JSONDecodeError, KeyError):
            return None


class RegistryAuth:
    """Registry authentication manager."""

    def __init__(self, gateway_url: str = GATEWAY_URL):
        self.gateway_url = gateway_url

    async def exchange_token(self, api_key: str) -> Optional[GHCRCredentials]:
        """
        Exchange AitherOS API key for a short-lived GHCR read token.

        POST gateway.aitherium.com/v1/registry/token
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.gateway_url}/v1/registry/token",
                    json={"api_key": api_key},
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    creds = GHCRCredentials(
                        token=data["token"],
                        expires_at=data["expires_at"],
                        username=data.get("username", "aither"),
                    )
                    creds.save()
                    return creds
                else:
                    logger.warning(
                        "Token exchange failed (%d): %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None
        except Exception as e:
            logger.debug("Token exchange error: %s", e)
            return None

    def docker_login(self, credentials: GHCRCredentials) -> bool:
        """Write ghcr.io auth via docker login subprocess."""
        try:
            proc = subprocess.run(
                ["docker", "login", "ghcr.io",
                 "-u", credentials.username,
                 "--password-stdin"],
                input=credentials.token,
                capture_output=True, text=True, timeout=15,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_cached_credentials(self) -> Optional[GHCRCredentials]:
        """Return cached GHCR credentials if still valid."""
        return GHCRCredentials.load()

    async def ensure_authenticated(self, api_key: str) -> bool:
        """
        Ensure Docker is authenticated to GHCR.
        Uses cached token if valid, otherwise exchanges API key.

        Returns True if authenticated (or registry is public).
        """
        # Check cached
        cached = self.get_cached_credentials()
        if cached and cached.is_valid:
            return True

        # Phase 1: GHCR is public, no auth needed for pulls
        # Try exchange but don't fail if gateway isn't available
        creds = await self.exchange_token(api_key)
        if creds:
            self.docker_login(creds)
            return True

        # Public registry fallback — pulls work without auth
        logger.info("Registry auth unavailable — using public pull (Phase 1)")
        return True
