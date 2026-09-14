"""A2A inbound signature verification and trust enforcement.

Validates incoming A2A requests with X-Signature headers, ensuring they are:
  1. Cryptographically valid (Ed25519 signature matching X-Public-Key)
  2. From a trusted peer (registered in mesh or allowlist)

This module is OPT-IN via AITHER_A2A_REQUIRE_TRUST env var (default: 'false').
When enabled ('true'), unknown keys are rejected with 403 Forbidden.
When 'audit', signatures are verified but failures only log (non-blocking).

Usage:
  from adk.a2a_trust import verify_a2a_request

  # In your A2A endpoint handler:
  verified = await verify_a2a_request(
      request_body=body_bytes,
      x_signature=request.headers.get("X-Signature"),
      x_public_key=request.headers.get("X-Public-Key"),
      x_node_id=request.headers.get("X-Node-ID"),
  )
  if not verified.trusted:
      return JSONResponse({"error": "Untrusted request"}, status_code=403)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("adk.a2a_trust")

_REQUIRE_TRUST_MODE = os.getenv("AITHER_A2A_REQUIRE_TRUST", "false").lower()

# Module-level nonce cache: maps nonce -> expiry_epoch. Pruned on each call.
# LIMITATION (P1, accepted): this cache is PER-PROCESS. A multi-worker deploy
# (e.g. uvicorn --workers N) would not share it, so a replay could land on a
# different worker within the window. The timestamp window still bounds the
# exposure to AITHER_A2A_REPLAY_WINDOW seconds. For a hardened multi-worker
# deploy, back this with a shared store (Redis SETNX / the mesh KV) keyed by
# nonce with the same TTL. Single-process `adk up` / one-worker serve is exact.
_SEEN_NONCES: dict[str, int] = {}


@dataclass
class A2ATrustResult:
    """Result of A2A trust verification."""
    verified: bool  # Cryptographic signature valid?
    trusted: bool   # Key is in trusted list or registered?
    node_id: Optional[str]  # X-Node-ID from request
    public_key: Optional[str]  # X-Public-Key from request
    reason: str  # Why verified/trusted or failed


def check_replay(body: dict) -> tuple[bool, str]:
    """Check replay protection (timestamp + nonce validation).

    Validates that a request's ts/nonce fields are present and the nonce
    has not been seen before (within the replay window).

    Args:
        body: JSON-RPC request dict (must contain top-level "ts" and "nonce")

    Returns:
        (ok: bool, reason: str) — (True, "ok") if valid, (False, reason) if
        replay detected or fields missing.
    """
    global _SEEN_NONCES

    # Get window from env (default 300s = 5 min). Never let a malformed value
    # raise (that would turn a security check into a 500) — fall back to 300.
    try:
        window = int(os.getenv("AITHER_A2A_REPLAY_WINDOW", "300"))
        if window <= 0:
            window = 300
    except (TypeError, ValueError):
        window = 300
    now = int(time.time())

    # Check ts and nonce presence (fail-closed)
    ts = body.get("ts")
    nonce = body.get("nonce")

    if ts is None or not nonce:
        return (False, "missing ts/nonce in request body")

    # Ensure ts is int
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return (False, "ts is not a valid integer")

    # Check timestamp is within window
    if abs(now - ts_int) > window:
        return (False, f"timestamp outside replay window (delta={abs(now - ts_int)}s, "
                      f"window={window}s)")

    # Prune expired nonces first (bound memory usage)
    expired = [n for n, exp in _SEEN_NONCES.items() if exp < now]
    for n in expired:
        del _SEEN_NONCES[n]

    # Check if nonce was already used
    if nonce in _SEEN_NONCES:
        return (False, "replay detected: nonce already used")

    # Record this nonce as valid until expiry
    _SEEN_NONCES[nonce] = now + window
    return (True, "ok")


async def verify_a2a_request(
    request_body: bytes,
    x_signature: Optional[str],
    x_public_key: Optional[str],
    x_node_id: Optional[str] = None,
) -> A2ATrustResult:
    """Verify A2A request signature and trust status.

    Args:
        request_body: Raw request body bytes (to be hashed for signature check)
        x_signature: X-Signature header (hex-encoded Ed25519 signature)
        x_public_key: X-Public-Key header (hex-encoded Ed25519 public key)
        x_node_id: X-Node-ID header (optional, for mesh peer lookup)

    Returns:
        A2ATrustResult with verified, trusted, and reason fields.
    """
    # If no signature headers, fail-closed (when enforcement is on)
    if not x_signature or not x_public_key:
        return A2ATrustResult(
            verified=False,
            trusted=False,
            node_id=x_node_id,
            public_key=None,
            reason="Missing X-Signature or X-Public-Key header",
        )

    # Verify signature
    try:
        verified = await _verify_ed25519_signature(request_body, x_public_key, x_signature)
    except Exception as e:
        logger.warning(f"Signature verification failed: {e}")
        return A2ATrustResult(
            verified=False,
            trusted=False,
            node_id=x_node_id,
            public_key=x_public_key,
            reason=f"Signature verification error: {e}",
        )

    if not verified:
        return A2ATrustResult(
            verified=False,
            trusted=False,
            node_id=x_node_id,
            public_key=x_public_key,
            reason="Signature does not match public key",
        )

    # Signature is valid; now check if key is trusted
    trusted, reason = await _is_key_trusted(x_public_key, x_node_id)

    return A2ATrustResult(
        verified=True,
        trusted=trusted,
        node_id=x_node_id,
        public_key=x_public_key,
        reason=reason,
    )


async def _verify_ed25519_signature(
    message: bytes,
    public_key_hex: str,
    signature_hex: str,
) -> bool:
    """Verify Ed25519 signature using cryptography library.

    Args:
        message: Message bytes
        public_key_hex: Hex-encoded Ed25519 public key (64 chars = 32 bytes)
        signature_hex: Hex-encoded signature (128 chars = 64 bytes)

    Returns:
        True if signature is valid, False otherwise.

    Raises:
        ValueError: If key/signature format is invalid
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature

        # Decode hex to bytes
        if len(public_key_hex) != 64:
            raise ValueError(
                f"Public key must be 32 bytes (64 hex chars), got {len(public_key_hex)}"
            )
        if len(signature_hex) != 128:
            raise ValueError(
                f"Signature must be 64 bytes (128 hex chars), got {len(signature_hex)}"
            )

        public_key_bytes = bytes.fromhex(public_key_hex)
        signature_bytes = bytes.fromhex(signature_hex)

        # Load public key and verify
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature_bytes, message)
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        logger.warning(f"Signature verification error: {e}")
        raise


# Positive/negative cache of the authoritative registry pubkey set, so we do NOT
# hit the network on every inbound A2A request. Short TTL keeps it fresh enough
# that a newly-enrolled agent becomes trusted within ~TTL, and a registry outage
# fails CLOSED (empty set -> deny) for at most TTL before the next fetch retries.
_REGISTRY_KEYS_CACHE: dict = {"keys": None, "ts": 0.0}
_REGISTRY_KEYS_TTL = float(os.getenv("AITHER_A2A_TRUST_CACHE_TTL", "60") or "60")


async def _fetch_registry_trusted_keys() -> set[str]:
    """Set of a2a public keys from the AUTHORITATIVE platform A2A fleet.

    Uses ``mesh_discovery._fetch_a2a_fleet`` — the server-side A2A fleet endpoint
    ``{portal}/api/genesis/a2a/fleet``, which is the ONLY discovery source that
    actually carries each agent's ``public_key`` (the sibling ``_fetch_registry``
    at ``/agent-endpoints`` deliberately does NOT return pubkeys, so using it here
    would make this lookup INERT — an empty set that silently rejects everything).
    The fleet is served by genesis (not the calling peer), so it cannot be forged.
    Fail-CLOSED: unreachable / no token / no pubkeys -> empty set -> nothing trusted."""
    try:
        from adk.mesh_discovery import _fetch_a2a_fleet
    except Exception:
        return set()
    warnings: list = []
    try:
        agents = await _fetch_a2a_fleet(warnings)
    except Exception as e:  # already best-effort, but be safe on the hot path
        logger.debug("a2a-fleet trusted-key fetch failed: %s", e)
        return set()
    return {a.public_key for a in agents if getattr(a, "public_key", "")}


async def _get_registry_trusted_keys() -> set[str]:
    """Cached wrapper around :func:`_fetch_registry_trusted_keys` (TTL-bounded)."""
    now = time.time()
    cached = _REGISTRY_KEYS_CACHE.get("keys")
    if cached is not None and (now - _REGISTRY_KEYS_CACHE["ts"]) < _REGISTRY_KEYS_TTL:
        return cached
    keys = await _fetch_registry_trusted_keys()
    _REGISTRY_KEYS_CACHE["keys"] = keys
    _REGISTRY_KEYS_CACHE["ts"] = now
    return keys


async def _is_key_trusted(
    public_key_hex: str,
    node_id: Optional[str] = None,
) -> tuple[bool, str]:
    """Check if a public key is in a trusted source.

    Sources (all fail-CLOSED — an unknown key is never trusted):
      1. Static ``AITHER_A2A_TRUSTED_KEYS`` env var (comma-separated hex keys).
      2. Authoritative cloud agent registry (owner-authed, per-tenant) — consulted
         when a mesh peer is identified (``node_id`` present) OR
         ``AITHER_A2A_TRUST_FROM_PORTAL`` is enabled. The registry is the source of
         truth the peer cannot forge (as opposed to its self-served agent card).

    Args:
        public_key_hex: Hex-encoded Ed25519 public key
        node_id: Optional node ID (marks the caller as a mesh peer)

    Returns:
        (trusted: bool, reason: str)
    """
    # 1. Static trusted keys env var
    trusted_keys_str = os.getenv("AITHER_A2A_TRUSTED_KEYS", "").strip()
    if trusted_keys_str:
        trusted_keys = [k.strip() for k in trusted_keys_str.split(",") if k.strip()]
        if public_key_hex in trusted_keys:
            return True, "Key is in AITHER_A2A_TRUSTED_KEYS"
    else:
        logger.debug("No AITHER_A2A_TRUSTED_KEYS configured")

    # 2. Authoritative registry (mesh peer or portal-trust mode). The registry is
    #    keyed on the AUTHENTICATED pubkey set, not on the caller-supplied node_id,
    #    so a peer cannot forge trust by claiming another node's id.
    use_registry = bool(node_id) or \
        os.getenv("AITHER_A2A_TRUST_FROM_PORTAL", "").lower() in ("true", "1")
    if use_registry:
        try:
            registry_keys = await _get_registry_trusted_keys()
        except Exception as e:
            logger.debug("registry trust lookup failed: %s", e)
            registry_keys = set()
        if public_key_hex in registry_keys:
            return True, "Key is registered in the cloud agent registry (authoritative)"

    return False, "Key is not in any trusted source (static or registry)"


def should_require_a2a_trust() -> bool:
    """Check if A2A trust enforcement is enabled.

    Modes:
      'false' (default): No verification
      'audit': Verify but only log failures
      'true': Enforce — return 403 for untrusted keys
    """
    return _REQUIRE_TRUST_MODE == "true"


def should_audit_a2a_trust() -> bool:
    """Check if A2A trust should be audited (logged but not enforced)."""
    return _REQUIRE_TRUST_MODE == "audit"
