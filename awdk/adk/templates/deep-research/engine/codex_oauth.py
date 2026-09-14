"""Codex / ChatGPT OAuth (EXPERIMENTAL, fast-follow).

The default, fully-tested auth paths are API keys (Anthropic / OpenAI) and local
Ollama. This module scaffolds "sign in with your ChatGPT/Codex account" using the
RFC-8628 device-authorization flow, mirroring adk/auth.py's existing device-code
pattern. The token it obtains is fed to the OpenAI-compatible provider as a bearer.

⚠️ This is scaffolding: the OpenAI device-flow client_id and endpoints below are
placeholders. Wire real values (or your gateway's OAuth) before relying on it.
It is intentionally NOT on the default demo path so the demo never blocks on it.

Usage (when configured):
    creds = login_device_flow()           # prints a URL + user code, polls
    os.environ["OPENAI_API_KEY"] = creds.access_token
    os.environ["DEEP_RESEARCH_PROVIDER"] = "openai"
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("deep_research.codex_oauth")

# Placeholders — set via env to point at OpenAI's (or your gateway's) OAuth.
_AUTH_BASE = os.getenv("CODEX_OAUTH_BASE", "https://auth.openai.com")
_CLIENT_ID = os.getenv("CODEX_OAUTH_CLIENT_ID", "")
_SCOPE = os.getenv("CODEX_OAUTH_SCOPE", "openid profile offline_access")
_TOKEN_PATH = Path(os.path.expanduser("~/.aither")) / "codex_oauth.json"


@dataclass
class Credentials:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and (self.expires_at == 0 or time.time() < self.expires_at)


def load_saved() -> Credentials | None:
    """Load previously-saved Codex credentials, if any and still valid."""
    if not _TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        creds = Credentials(**{k: data[k] for k in data if k in Credentials.__dataclass_fields__})
        return creds if creds.valid else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("load_saved failed: %s", exc)
        return None


def _save(creds: Credentials) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(json.dumps(creds.__dict__), encoding="utf-8")
    try:
        os.chmod(_TOKEN_PATH, 0o600)
    except (OSError, AttributeError):
        pass


def login_device_flow(timeout: float = 300.0) -> Credentials:
    """RFC-8628 device-authorization flow. Prints a URL + code; polls for the token.

    Returns Credentials on success. Raises RuntimeError if not configured or on
    timeout. EXPERIMENTAL — see module docstring.
    """
    if not _CLIENT_ID:
        raise RuntimeError(
            "Codex OAuth not configured. Set CODEX_OAUTH_CLIENT_ID (and "
            "CODEX_OAUTH_BASE if not the default). For the demo, use an API key "
            "(ANTHROPIC_API_KEY / OPENAI_API_KEY) or local Ollama instead."
        )
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(
            f"{_AUTH_BASE}/oauth/device/code",
            data={"client_id": _CLIENT_ID, "scope": _SCOPE},
        )
        resp.raise_for_status()
        dev = resp.json()
        verification = dev.get("verification_uri_complete") or dev.get("verification_uri")
        print("\n  Sign in to continue:")
        print(f"    Open: {verification}")
        print(f"    Code: {dev.get('user_code')}\n")

        interval = float(dev.get("interval", 5))
        device_code = dev["device_code"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            tok = client.post(
                f"{_AUTH_BASE}/oauth/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": _CLIENT_ID,
                },
            )
            body = tok.json()
            if tok.status_code == 200 and body.get("access_token"):
                creds = Credentials(
                    access_token=body["access_token"],
                    refresh_token=body.get("refresh_token", ""),
                    expires_at=time.time() + float(body.get("expires_in", 3600)),
                )
                _save(creds)
                return creds
            if body.get("error") not in ("authorization_pending", "slow_down"):
                raise RuntimeError(f"OAuth failed: {body.get('error')}")
    raise RuntimeError("Codex OAuth timed out waiting for sign-in.")
