"""Pack artifact verification (client-side, adk SDK).

Standalone verification for downloaded pack tarballs.
Uses Ed25519 (same as server-side lib.marketplace.pack_signer).
No AitherOS imports — designed to run in adk CLI/SDK context only.

Security policy:
  - AITHER_PACK_REQUIRE_SIGNING: If "true" (default in production), unsigned packs
    are rejected. If "false" or unset in testing, unsigned packs are accepted for
    backward compatibility.
  - AITHER_PACK_PUBLIC_KEY: Public key for verifying signatures. If not set, defaults
    to placeholder (disabled verification).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("pack_verifier")

# Aitherium pack-signing public key (provisioned 2026-06-13). Safe to ship — it
# only VERIFIES signatures. The matching private seed lives in the vault as
# AITHER_PACK_SIGNING_KEY (server-side only). Override at runtime with
# AITHER_PACK_PUBLIC_KEY=<hex> for self-hosted/sovereign signing roots.
_DEFAULT_PUBLIC_KEY_HEX = "77b08e2aac48efe9287138c6d80b6e4c5c4b8f815f68236ccb40b09c8880bccb"


def get_pack_public_key() -> Optional[str]:
    """Resolve the pack signature public key (client-side).

    Returns hex-encoded Ed25519 public key, or None if not configured.
    Order:
        1. AITHER_PACK_PUBLIC_KEY env var
        2. None (placeholder disabled)
    """
    key = os.environ.get("AITHER_PACK_PUBLIC_KEY", "").strip()
    if key:
        return key
    return None


def verify_bytes(
    data: bytes,
    signature_hex: str,
    public_key_hex: Optional[str] = None,
) -> bool:
    """Verify a signature over data.

    Args:
        data: Original bytes that were signed
        signature_hex: Signature as hex string
        public_key_hex: Public key as hex string. If None, uses env/config.

    Returns:
        True if signature is valid, False otherwise

    Never raises — returns False on any error.
    """
    # Import in its own guarded block: if the import lived inside the main try,
    # a missing `cryptography` would reach `except InvalidSignature:` with that
    # name unbound and crash with UnboundLocalError instead of failing closed.
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        logger.debug("Cannot verify signature: cryptography not installed")
        return False

    try:
        pub_hex = public_key_hex or get_pack_public_key()
        if not pub_hex:
            # No public key configured — cannot verify
            logger.debug("Cannot verify signature: public key not configured")
            return False

        # Load public key from hex
        pub_key_bytes = bytes.fromhex(pub_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_key_bytes)

        # Verify signature
        sig_bytes = bytes.fromhex(signature_hex)
        pub_key.verify(sig_bytes, data)
        return True
    except InvalidSignature:
        logger.debug("Pack signature verification failed: invalid signature")
        return False
    except Exception as e:
        logger.debug("Pack signature verification error: %s", e)
        return False


def verify_pack_tarball(
    tarball_bytes: bytes,
    signature_hex: Optional[str],
    public_key_hex: Optional[str] = None,
) -> tuple[bool, str]:
    """Verify a downloaded pack tarball.

    Args:
        tarball_bytes: The pack tarball bytes
        signature_hex: Signature from response header (X-Aither-Pack-Signature)
                       If None, behavior depends on AITHER_PACK_REQUIRE_SIGNING
        public_key_hex: Public key (optional, uses config if not provided)

    Returns:
        Tuple of (verified, message)
        - verified=True if signature is valid
        - verified=False if signature is missing (when required) or verification fails
        - message describes the result or error

    Never raises.

    Security policy:
    - AITHER_PACK_REQUIRE_SIGNING explicitly set ("true"/"false") wins outright.
    - Otherwise enforcement derives from key presence: if a public key is
      configured the deployment expects signed packs, so an unsigned pack is
      rejected (defeats signature-strip downgrade attacks). With no key
      configured nothing is verifiable anyway, so unsigned packs are accepted
      for backward compat (the 54 pre-signing packs still install).
    """
    pub_hex = public_key_hex or get_pack_public_key()
    require_env = os.environ.get("AITHER_PACK_REQUIRE_SIGNING")
    if require_env is not None:
        require_signing = require_env.lower() in ("true", "1", "yes")
    else:
        require_signing = bool(pub_hex)

    if not signature_hex:
        if require_signing:
            logger.warning(
                "Pack verification failed: signature required but not provided"
            )
            return False, "Pack signature required but not provided"
        # Backward compat: accept unsigned packs if not in strict mode
        logger.debug("Pack verification skipped: no signature provided (unsafe mode)")
        return True, "No signature provided (unsigned pack, unsafe mode)"

    if not verify_bytes(tarball_bytes, signature_hex, pub_hex):
        logger.warning("Pack signature verification failed: invalid signature")
        return False, "Pack signature verification failed"

    logger.debug("Pack signature verified successfully")
    return True, "Pack signature verified"
