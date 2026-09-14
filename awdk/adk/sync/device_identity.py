"""Device mTLS identity — persist the enrolled client cert and load it for drive.

At enrollment, ``POST /v1/nodes/register`` returns a one-time ``mtls`` bundle
(``certificate`` / ``private_key`` / ``chain``, CN ``devcert--<tenant>--<node>``).
The device stores it locally with this module; the DriveClient then presents it
for mTLS so the cloud derives the tenant CRYPTOGRAPHICALLY from the cert CN
rather than from a spoofable ``X-Tenant-ID`` header.

The private key never leaves the device and is stored 0600.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger("adk.sync.device_identity")

_CERT_NAME = "cert.pem"          # leaf certificate (PEM)
_KEY_NAME = "key.pem"            # private key (PEM, 0600)
_CHAIN_NAME = "chain.pem"        # issuer chain (PEM), informational
_FULLCHAIN_NAME = "fullchain.pem"  # leaf + chain, what httpx presents


def identity_dir() -> Path:
    """Directory holding this device's mTLS identity.

    Overridable via ``AITHER_DEVICE_IDENTITY_DIR`` (e.g. for testing).
    Defaults to ``~/.aither/tls``.
    """
    base = os.environ.get("AITHER_DEVICE_IDENTITY_DIR")
    if base:
        return Path(base)
    return Path.home() / ".aither" / "tls"


def save_enrolled_identity(
    bundle: Dict[str, str], *, directory: Optional[Path] = None
) -> Path:
    """Persist the one-time enrollment mTLS bundle to disk.

    Args:
        bundle: the ``mtls`` object from /v1/nodes/register —
            ``{certificate, private_key, chain?}`` (PEM strings).
        directory: override the storage dir (defaults to ``identity_dir()``).

    Returns:
        The directory the identity was written to.

    Raises:
        ValueError: if the bundle is missing the certificate or private key.
    """
    cert = (bundle or {}).get("certificate") or bundle.get("cert_pem", "")
    key = (bundle or {}).get("private_key") or bundle.get("key_pem", "")
    chain = (bundle or {}).get("chain") or bundle.get("chain_pem", "")
    if not cert or not key:
        raise ValueError("enrollment bundle missing certificate or private_key")

    d = directory or identity_dir()
    d.mkdir(parents=True, exist_ok=True)

    (d / _CERT_NAME).write_text(cert, encoding="utf-8")
    key_path = d / _KEY_NAME
    key_path.write_text(key, encoding="utf-8")
    try:
        key_path.chmod(0o600)
    except OSError as exc:  # best-effort on platforms without POSIX perms
        log.debug("chmod key.pem failed (non-fatal): %s", exc)

    if chain:
        (d / _CHAIN_NAME).write_text(chain, encoding="utf-8")
    # Full chain (leaf first) is what the TLS client presents.
    fullchain = cert.rstrip("\n") + ("\n" + chain if chain else "") + "\n"
    (d / _FULLCHAIN_NAME).write_text(fullchain, encoding="utf-8")

    log.info("adk device mTLS identity stored at %s", d)
    return d


def load_device_cert(
    *, directory: Optional[Path] = None
) -> Optional[Tuple[str, str]]:
    """Return ``(cert_path, key_path)`` for the enrolled device cert, or ``None``.

    The returned tuple is in the exact shape httpx accepts for its ``cert=``
    kwarg. Presents the full chain (leaf + issuers) when available so the server
    can build the path even before it has the intermediate cached. Returns
    ``None`` when no identity has been enrolled — the caller then falls back to
    bearer-token auth.
    """
    d = directory or identity_dir()
    key_path = d / _KEY_NAME
    if not key_path.is_file():
        return None
    cert_path = d / _FULLCHAIN_NAME
    if not cert_path.is_file():
        cert_path = d / _CERT_NAME
    if not cert_path.is_file():
        return None
    return (str(cert_path), str(key_path))


def has_device_identity(*, directory: Optional[Path] = None) -> bool:
    """True when this device has an enrolled mTLS identity on disk."""
    return load_device_cert(directory=directory) is not None
