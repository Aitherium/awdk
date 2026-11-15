"""NVIDIA CUDA docs pack — endpoint, OAuth credential store and redaction.

The CUDA documentation MCP server is NOT keyless. Every unauthenticated call
answers 401, and its authorisation server advertises only ``authorization_code``
+ ``refresh_token`` (no device grant), so one human browser approval with a free
NVIDIA Developer account is unavoidable. After that approval the refresh token
carries headless runs.

The credential lives in a 0600 JSON file under ``~/.aither`` — a file source,
not an environment variable alone, because env vars are transient and a pack
that only reads them breaks the moment a process starts without one. An
environment variable is honoured as an override for a short-lived bearer.

Fail-soft: every function returns a value (an empty string at worst), never
raises into the agent loop.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("cuda_pack")

MCP_URL = "https://api.copilot.nsight.ngc.nvidia.com/mcp/cuda-docs"
TOKEN_FILE = Path.home() / ".aither" / "cuda_docs_token.json"
ACCESS_TOKEN_ENV = "NVIDIA_CUDA_MCP_TOKEN"

FIX = (
    "Call cuda_docs_login() once with a free NVIDIA Developer account, or set "
    "NVIDIA_CUDA_MCP_TOKEN to a current bearer. The refresh token is stored 0600 "
    "under ~/.aither/."
)

# Refresh a little before the advertised expiry: the server is the authority,
# but arriving early costs one request and arriving late costs a 401.
_EXPIRY_MARGIN_SECONDS = 60


def redact(value: Any) -> str:
    """Strip every known credential from text headed for a log or an agent.

    Reads the store directly rather than calling ``access_token()``: redaction
    must not trigger a token refresh as a side effect of logging.
    """
    store = _load_store()
    text = str(value)
    secrets = (
        os.environ.get(ACCESS_TOKEN_ENV, "").strip(),
        str(store.get("access_token") or ""),
        str(store.get("refresh_token") or ""),
    )
    for secret in secrets:
        if secret and len(secret) > 8:
            text = text.replace(secret, "***REDACTED***")
    return text


def _load_store() -> dict:
    """Read the credential store. A missing or unreadable file is an empty dict."""
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_store(store: dict) -> bool:
    """Write the credential store 0600. Returns False when it could not be written."""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(store), encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
        return True
    except OSError as exc:
        logger.warning("cuda_docs: could not write %s: %s", TOKEN_FILE, exc)
        return False


def clear_token() -> bool:
    """Forget the stored credential. Returns True when nothing is left behind."""
    try:
        TOKEN_FILE.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("cuda_docs: could not remove %s: %s", TOKEN_FILE, exc)
        return False


def http_request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    """One HTTP round-trip. Never raises.

    Returns ``{"status": int, "headers": dict, "text": str}`` on a response, or
    ``{"status": 0, "error": str}`` when the request never completed.
    """
    req = urlrequest.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {
                "status": int(getattr(resp, "status", 200) or 200),
                "headers": dict(resp.headers.items()),
                "text": raw,
            }
    except urlerror.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — an unreadable error body is still an error
            raw = ""
        return {
            "status": int(exc.code),
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "text": raw,
        }
    except Exception as exc:  # noqa: BLE001 — a network failure is a status, not a crash
        return {"status": 0, "error": f"{type(exc).__name__}: {exc}"}


def _refresh(store: dict) -> str:
    """Exchange a refresh token for an access token. Returns "" on any failure."""
    refresh_token = str(store.get("refresh_token") or "")
    token_endpoint = str(store.get("token_endpoint") or "")
    client_id = str(store.get("client_id") or "")
    if not (refresh_token and token_endpoint and client_id):
        return ""
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode("utf-8")
    response = http_request(
        "POST",
        token_endpoint,
        body=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20.0,
    )
    if response.get("status") != 200:
        logger.info("cuda_docs: token refresh failed (status %s)", response.get("status"))
        return ""
    try:
        data = json.loads(response.get("text") or "{}")
    except ValueError:
        return ""
    token = str(data.get("access_token") or "")
    if not token:
        return ""
    store["access_token"] = token
    if data.get("refresh_token"):
        store["refresh_token"] = str(data["refresh_token"])
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)):
        store["expires_at"] = time.time() + float(expires_in)
    _save_store(store)
    return token


def access_token() -> str:
    """Return a usable bearer, or "" when none is configured.

    Order: the environment override (a short-lived bearer), then a cached access
    token from the file, then a refresh against the stored refresh token.
    """
    env_token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    store = _load_store()
    cached = str(store.get("access_token") or "")
    expires_at = store.get("expires_at")
    if cached and (
        not isinstance(expires_at, (int, float))
        or float(expires_at) > time.time() + _EXPIRY_MARGIN_SECONDS
    ):
        return cached
    return _refresh(store)


def credential_source() -> str:
    """Name where the bearer came from — never the bearer itself."""
    if os.environ.get(ACCESS_TOKEN_ENV, "").strip():
        return "env:" + ACCESS_TOKEN_ENV
    store = _load_store()
    if str(store.get("access_token") or ""):
        return "file:" + str(TOKEN_FILE) + " (access token)"
    if str(store.get("refresh_token") or ""):
        return "file:" + str(TOKEN_FILE) + " (refresh token)"
    return "none"
