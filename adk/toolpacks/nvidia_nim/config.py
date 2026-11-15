"""NVIDIA NIM (hosted build.nvidia.com API) pack configuration and credential resolution.

Resolution order for the NGC API key:

    NVIDIA_NGC_API_KEY | NGC_API_KEY   (environment override)
      -> nim_api_key under ~/.aither/  (0600, written by the installer)
      -> adk pack settings

The key is never hardcoded, never logged and never returned to the agent.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("nim_pack")

API_BASE = "https://integrate.api.nvidia.com/v1"
TOKEN_FILE = Path.home() / ".aither" / "nim_api_key"
FIX = (
    "Set workspace secret nim_api_key (a free key from build.nvidia.com) "
    "or export NVIDIA_NGC_API_KEY"
)


def api_key() -> str:
    """Return the NGC API key, or an empty string when none is configured."""
    for env in ("NVIDIA_NGC_API_KEY", "NGC_API_KEY"):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    try:
        value = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError as exc:
        logger.debug("nim_pack: token file unavailable: %s", exc)
    try:
        from adk.config import load_saved_config  # type: ignore

        settings = (load_saved_config().get("packs_settings") or {}).get("nvidia_nim") or {}
        value = str(settings.get("nim_api_key") or "").strip()
        if value:
            return value
    except Exception as exc:  # noqa: BLE001 — the config store is optional context
        logger.debug("nvidia_nim: pack settings unavailable: %s", exc)
    return ""


def credential_source() -> str:
    """Name where the key came from, never the key itself."""
    for env in ("NVIDIA_NGC_API_KEY", "NGC_API_KEY"):
        if os.environ.get(env, "").strip():
            return "env:" + env
    try:
        if TOKEN_FILE.read_text(encoding="utf-8").strip():
            return "file:" + str(TOKEN_FILE)
    except OSError as exc:
        logger.debug("nim_pack: token file unavailable: %s", exc)
    return "none"


def request(method: str, path: str, payload: dict | None, key: str) -> dict:
    """Call the hosted endpoint. Never raises; every failure is a dict."""
    url = API_BASE.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
        return {"status": "ok", "data": json.loads(body) if body else {}}
    except urlerror.HTTPError as exc:
        if exc.code in (401, 403):
            return {
                "error": "not_authenticated",
                "status_code": exc.code,
                "fix": "The NGC key was rejected; mint a new one at build.nvidia.com.",
            }
        return {"error": "request_failed", "status_code": exc.code, "message": exc.reason}
    except Exception as exc:  # noqa: BLE001 — a network failure is a status, not a crash
        return {"error": "request_failed", "message": str(exc)}
