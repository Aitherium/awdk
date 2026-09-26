"""This device's identity: its signing key, and the sync it unlocks.

``adk enroll`` gives every device its own Ed25519 key (awseal). The public half
goes into the enrollment record; the user's other devices trust what it signs
for as long as the device stays enrolled, so removing the device is the
revocation. Today that covers settings sync (awsettings); the same key is meant
to sign the device's mesh peer key next.

Everything here is best effort: a device without awseal still enrolls, it just
cannot sign, and says so.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

SETTINGS_PATH = "/api/settings/preferences"
SEAL_KEYS_PATH = "/v1/nodes/seal-keys"


def seal_public_key(create: bool = True) -> str:
    """This device's public key (hex), generating the key once. "" without awseal."""
    try:
        from awseal import keys
    except Exception:  # noqa: BLE001 -- optional dependency (`pip install awdk[seal]`)
        return ""
    try:
        from pathlib import Path
        env = (os.getenv(keys.KEY_PATH_ENV) or "").strip()
        target = Path(env) if env else Path(keys.DEFAULT_KEY_PATH)
        if create and not target.exists():
            keys.generate(target)
        return keys.public_key_hex(path=target)
    except Exception:  # noqa: BLE001 -- an unreadable key must not block enrollment
        return ""


def token_command() -> str:
    """The credential helper awsettings runs to get the current login's bearer."""
    return f'"{sys.executable}" -m adk.sync.token'


def configure_settings_sync(portal_url: str, identity_url: str,
                            install_hooks: bool = True) -> tuple[int, str]:
    """Point awsettings at this user's store and device list, and install the
    Claude Code hooks at user level (every project, every awsh session).

    Returns (exit code, one line to print). Never raises.
    """
    if (os.getenv("AITHER_SETTINGS_SYNC") or "").strip().lower() in ("0", "false", "off", "no"):
        return 0, "Settings sync: off (AITHER_SETTINGS_SYNC=0)"
    try:
        from awsettings.cli import main as awsettings_main
    except Exception as exc:  # noqa: BLE001
        return 2, f"Settings sync: not configured (awsettings unavailable: {exc})"
    argv = [
        "--user", "--quiet", "enroll",
        "--url", portal_url.rstrip("/") + SETTINGS_PATH,
        "--keys-url", identity_url.rstrip("/") + SEAL_KEYS_PATH,
        "--token-command", token_command(),
    ]
    if not install_hooks:
        argv.append("--no-hooks")
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = awsettings_main(argv)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        return 2, f"Settings sync: not configured ({exc})"
    if rc == 0:
        return 0, "Settings sync: on (signed; Claude Code hooks installed at user level)"
    return rc, f"Settings sync: not configured (awsettings enroll exited {rc})"


def registration_fields() -> dict:
    """Extra fields for the enrollment payload. Empty when awseal is absent."""
    pub: Optional[str] = seal_public_key()
    return {"seal_pubkey": pub} if pub else {}
