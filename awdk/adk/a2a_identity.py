"""A2A Ed25519 identity — persist the endpoint's signing keypair for A2A trust.

At first startup, the agent generates an Ed25519 keypair and persists it locally.
The private key is stored 0600 and NEVER sent. The public key is sent on endpoint
registration so the fleet can verify signed A2A requests (commands, data flows).

The key format matches what the verifier consumes (a2a_trust.py):
_verify_ed25519 does Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)),
so the public key MUST be RAW 32 bytes hex (64 chars), NOT PEM, NOT base64.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("adk.a2a_identity")

_PRIVATE_KEY_NAME = "private_key"    # RAW 32-byte Ed25519 private key (binary)
_PUBLIC_KEY_NAME = "public_key"      # RAW 32-byte Ed25519 public key (hex, 64 chars)


def a2a_identity_dir() -> Path:
    """Directory holding this agent's A2A signing identity.

    Overridable via ``AITHER_A2A_IDENTITY_DIR`` (e.g. for testing).
    Defaults to ``~/.aither/a2a``.
    """
    base = os.environ.get("AITHER_A2A_IDENTITY_DIR")
    if base:
        return Path(base)
    return Path.home() / ".aither" / "a2a"


def _ensure_a2a_identity(*, directory: Optional[Path] = None) -> tuple[str, str]:
    """Generate or load the A2A Ed25519 keypair. Returns (private_hex, public_hex).

    The keypair is persisted on first call, then re-used. Idempotent: never
    regenerates over an existing key.

    Args:
        directory: override the storage dir (defaults to a2a_identity_dir()).

    Returns:
        (private_key_hex, public_key_hex) — both RAW 32 bytes in hex (64 chars each).

    Raises:
        OSError: if directory creation or file I/O fails.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519

    d = directory or a2a_identity_dir()
    d.mkdir(parents=True, exist_ok=True)

    private_key_path = d / _PRIVATE_KEY_NAME
    public_key_path = d / _PUBLIC_KEY_NAME

    # Idempotency: if both files exist, load and return them
    if private_key_path.is_file() and public_key_path.is_file():
        try:
            private_key_hex = private_key_path.read_bytes().hex()
            public_key_hex = public_key_path.read_text(encoding="utf-8").strip()
            # Validate format (defensive: should be 64 chars)
            if len(private_key_hex) == 64 and len(public_key_hex) == 64:
                log.debug("Loaded existing A2A identity from %s", d)
                return private_key_hex, public_key_hex
        except (OSError, ValueError) as e:
            log.warning("Failed to load existing A2A identity: %s (will regenerate)", e)

    # Generate new keypair
    log.info("Generating new A2A Ed25519 keypair for this agent")
    private_key_obj = ed25519.Ed25519PrivateKey.generate()
    public_key_obj = private_key_obj.public_key()

    # Extract raw bytes and convert to hex
    # Ed25519 private key is always 32 bytes in raw form
    private_key_bytes = private_key_obj.private_bytes_raw()
    public_key_bytes = public_key_obj.public_bytes_raw()

    private_key_hex = private_key_bytes.hex()
    public_key_hex = public_key_bytes.hex()

    # Persist
    private_key_path.write_bytes(bytes.fromhex(private_key_hex))
    try:
        private_key_path.chmod(0o600)
    except OSError as exc:  # best-effort on platforms without POSIX perms
        log.debug("chmod private_key failed (non-fatal): %s", exc)

    public_key_path.write_text(public_key_hex + "\n", encoding="utf-8")

    log.info("A2A identity stored at %s", d)
    return private_key_hex, public_key_hex


def get_a2a_public_key(*, directory: Optional[Path] = None) -> str:
    """Get the agent's A2A Ed25519 public key (hex-encoded, 64 chars).

    Generates and persists the keypair on first call. Idempotent.

    Args:
        directory: override the storage dir (defaults to a2a_identity_dir()).

    Returns:
        Public key in hex format (RAW 32 bytes = 64 hex chars).

    Raises:
        OSError: if directory creation or file I/O fails.
    """
    _, public_key_hex = _ensure_a2a_identity(directory=directory)
    return public_key_hex


def get_a2a_private_key(*, directory: Optional[Path] = None) -> str:
    """Get the agent's A2A Ed25519 private key (hex-encoded, 64 chars).

    SECURITY: This key MUST NEVER be logged, sent to the server, or included in
    any telemetry. It is only for signing outbound A2A requests, never for
    registration payloads.

    Generates and persists the keypair on first call. Idempotent.

    Args:
        directory: override the storage dir (defaults to a2a_identity_dir()).

    Returns:
        Private key in hex format (RAW 32 bytes = 64 hex chars).

    Raises:
        OSError: if directory creation or file I/O fails.
    """
    private_key_hex, _ = _ensure_a2a_identity(directory=directory)
    return private_key_hex


def has_a2a_identity(*, directory: Optional[Path] = None) -> bool:
    """True when this agent has a persisted A2A keypair on disk."""
    d = directory or a2a_identity_dir()
    return (d / _PRIVATE_KEY_NAME).is_file() and (d / _PUBLIC_KEY_NAME).is_file()
