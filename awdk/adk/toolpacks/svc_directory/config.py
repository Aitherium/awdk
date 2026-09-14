"""AitherOS directory pack — config and auth.

AUTO-GENERATED from _service_spec/services-spec.yaml. Do not edit by hand:
check_service_pack_parity will report your change as drift. Edit the spec and
regenerate instead.

Handles authentication, credential storage, and endpoint configuration.
This pack is OPTIONAL — failures to authenticate are reported as a status
dict with a fix, never as an exception.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("directory_pack")

# ── endpoints ───────────────────────────────────────────────────────────

BASE_URL = "https://aitheros-directory:8111"
PORT = 8111

# ── auth config ─────────────────────────────────────────────────────────

# Auth types: internal_key, oauth_device_flow, none
AUTH_TYPE = "internal_key"

# Internal services authenticate via X-Internal-Key header.
# The key is read fresh on every call so a rotation is picked up without restart.
def get_internal_key() -> str:
    """Retrieve the internal API key from env or config."""
    # Prefer env var; fallback to config file
    env_key = os.environ.get("AITHER_INTERNAL_SECRET", "").strip()
    if env_key:
        return env_key
    # Fallback to config file (if it exists)
    config_file = Path.home() / ".aither" / "internal_key.txt"
    try:
        return config_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def redact(value) -> str:
    """Strip API keys from text headed for logs or the agent."""
    text = str(value)
    key = get_internal_key()
    if key and len(key) > 8:
        text = text.replace(key, "***REDACTED***")
    return text
