"""Channel-specific authentication helpers for verifying caller identity.

Reusable primitives for channel adapters (Telegram, Slack, etc.) to verify
inbound signatures and sign approval decisions using stdlib crypto (HMAC-SHA256,
constant-time comparison). Framework-agnostic; the app wires them into its
webhook handlers. Kept as a flat module (not a ``channels`` package) to avoid
shadowing the existing ``adk/channels.py`` adapter module.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


def verify_telegram_secret(received_token: str, expected_token: str) -> bool:
    """Verify a Telegram webhook's secret token using constant-time comparison.

    Telegram delivers an ``X-Telegram-Bot-Api-Secret-Token`` header on webhook
    calls. Compare it safely against the configured token to prevent timing
    attacks.

    Returns True only on an exact match.
    """
    return hmac.compare_digest(received_token, expected_token)


def is_ceo(from_id: Any, ceo_chat_id: Any) -> bool:
    """Check whether a caller's chat/user ID matches the configured CEO ID.

    String-normalized equality, so int and str IDs compare equal. Used to gate
    high-authority operations (e.g. approval callbacks) to a single known ID.
    """
    return str(from_id).strip() == str(ceo_chat_id).strip()


def sign_approval(secret: str, request_id: str, decision: str, ts: str) -> str:
    """Sign an approval decision with HMAC-SHA256.

    Binds an approval non-repudiably to (request_id, decision, ts) so a database
    ``human_verified`` flag can be set ONLY by a verified signed callback, never
    by the agent itself. Returns the hexdigest signature.
    """
    message = f"{request_id}:{decision}:{ts}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_approval(
    secret: str,
    request_id: str,
    decision: str,
    ts: str,
    signature: str,
) -> bool:
    """Verify a signed approval decision using constant-time comparison.

    Recomputes the expected HMAC over (request_id, decision, ts) and compares it
    to the claimed signature. Including the timestamp + request id guards against
    forgery and replay. Returns True only when the signature is valid.
    """
    expected = sign_approval(secret, request_id, decision, ts)
    return hmac.compare_digest(signature, expected)
