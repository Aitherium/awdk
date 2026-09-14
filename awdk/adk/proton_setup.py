"""Proton auto-detection and setup helper.

Checks for local Proton Bridge (IMAP/SMTP), CalDAV calendar,
and rclone protondrive remote. Used during `aither serve --workspace --setup`
first-run wizard.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from typing import Optional

logger = logging.getLogger("adk.proton_setup")

# Default Proton Bridge ports
PROTON_IMAP_HOST = "127.0.0.1"
PROTON_IMAP_PORT = 1143
PROTON_SMTP_HOST = "127.0.0.1"
PROTON_SMTP_PORT = 1025
PROTON_CALDAV_HOST = "127.0.0.1"
PROTON_CALDAV_PORT = 1080


def _probe_port(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def detect_proton_bridge() -> dict:
    """Detect Proton Bridge services running locally.

    Returns a dict with detected services and their status.
    """
    result = {
        "imap": _probe_port(PROTON_IMAP_HOST, PROTON_IMAP_PORT),
        "smtp": _probe_port(PROTON_SMTP_HOST, PROTON_SMTP_PORT),
        "caldav": _probe_port(PROTON_CALDAV_HOST, PROTON_CALDAV_PORT),
        "rclone_protondrive": _detect_rclone_protondrive(),
    }
    result["bridge_running"] = result["imap"] or result["smtp"]
    return result


def _detect_rclone_protondrive() -> bool:
    """Check if rclone has a protondrive remote configured."""
    rclone_path = shutil.which("rclone")
    if not rclone_path:
        return False
    try:
        output = subprocess.check_output(
            [rclone_path, "listremotes"],
            timeout=5,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return any("protondrive" in line.lower() for line in output.splitlines())
    except (subprocess.SubprocessError, OSError):
        return False


def _get_secret(key: str, fallback_env: str = "") -> str:
    """Resolve a secret via ADK's builtin secret_get (vault -> env -> local store).

    Falls back to direct env var if the secrets module isn't available.
    """
    try:
        from adk.builtin_tools import secret_get
        val = secret_get(key)
        # secret_get returns JSON error string on miss
        if val and not val.startswith('{"error'):
            return val
    except (ImportError, Exception):
        pass
    return os.getenv(fallback_env or key, "")


async def auto_connect_mail(
    api_base: str = "http://localhost:8080",
    email: Optional[str] = None,
) -> dict:
    """Auto-connect Proton Bridge mail if detected.

    Credentials are resolved from the ADK secrets store (which checks
    AitherSecrets vault, then env vars, then local keyring).

    Args:
        api_base: Base URL of the workspace server.
        email: Proton email address override.

    Returns:
        Connection result dict.
    """
    detection = detect_proton_bridge()
    if not detection["bridge_running"]:
        return {"ok": False, "reason": "Proton Bridge not detected"}

    email = email or _get_secret("PROTON_EMAIL")
    if not email:
        return {
            "ok": False,
            "reason": "No email configured. Store PROTON_EMAIL in secrets.",
        }

    password = _get_secret("PROTON_BRIDGE_PASSWORD")
    if not password:
        return {
            "ok": False,
            "reason": "No bridge password. Store PROTON_BRIDGE_PASSWORD in secrets.",
        }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{api_base}/api/mail/accounts/connect",
                json={
                    "email": email,
                    "provider": "protonmail",
                    "imap_host": PROTON_IMAP_HOST,
                    "imap_port": PROTON_IMAP_PORT,
                    "imap_security": "starttls",
                    "smtp_host": PROTON_SMTP_HOST,
                    "smtp_port": PROTON_SMTP_PORT,
                    "smtp_security": "starttls",
                    "username": email,
                    "password": password,
                },
            )
            if resp.status_code < 300:
                logger.info("Connected Proton Bridge mail for %s", email)
                return {"ok": True, "email": email}
            return {"ok": False, "reason": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except ImportError:
        return {"ok": False, "reason": "httpx not installed"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


async def auto_connect_calendar(api_base: str = "http://localhost:8080") -> dict:
    """Auto-connect Proton Calendar via CalDAV if detected."""
    if not _probe_port(PROTON_CALDAV_HOST, PROTON_CALDAV_PORT):
        return {"ok": False, "reason": "CalDAV not detected on port 1080"}

    email = _get_secret("PROTON_EMAIL")
    password = _get_secret("PROTON_BRIDGE_PASSWORD")
    if not email or not password:
        return {"ok": False, "reason": "PROTON_EMAIL and PROTON_BRIDGE_PASSWORD not in secrets"}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{api_base}/api/calendar/connect",
                json={
                    "provider": "caldav",
                    "url": f"http://{PROTON_CALDAV_HOST}:{PROTON_CALDAV_PORT}",
                    "username": email,
                    "password": password,
                },
            )
            if resp.status_code < 300:
                logger.info("Connected Proton CalDAV calendar")
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def print_detection_summary() -> None:
    """Print a user-friendly summary of Proton detection results."""
    detection = detect_proton_bridge()
    print("\n  Proton Bridge Detection:")
    print(f"    IMAP (port {PROTON_IMAP_PORT}):  {'found' if detection['imap'] else 'not found'}")
    print(f"    SMTP (port {PROTON_SMTP_PORT}):  {'found' if detection['smtp'] else 'not found'}")
    print(f"    CalDAV (port {PROTON_CALDAV_PORT}): {'found' if detection['caldav'] else 'not found'}")
    print(f"    rclone protondrive:  {'configured' if detection['rclone_protondrive'] else 'not found'}")
    if detection["bridge_running"]:
        print("    Status: Proton Bridge is RUNNING")
    else:
        print("    Status: Proton Bridge NOT detected — start it to enable mail/calendar")
    print()
