"""Clean-room Google AI Plan OAuth 2.0 (Desktop PKCE) + RFC 9449 DPoP auth.

Implements direct Google Desktop OAuth for personal-quota Gemini access:
- PKCE (S256) + loopback redirect (``http://127.0.0.1:<ephemeral>/oauth2callback``)
  with SSH/headless paste fallback.
- RFC 9449 DPoP proof signing on ``POST https://oauth2.googleapis.com/token`` ONLY
  using ECDSA NIST P-256 (``ES256``), strict 4-field header JWK, and raw 64-byte
  IEEE P1363 ``R || S`` signatures (never ASN.1 DER).
- Isolated credential storage (OS Keyring -> owner-only ``_private_file``) plus
  optional ``awseal`` Ed25519 local device-binding attestation.
- Double-checked in-process + cross-process locked token refresh with 5-minute
  proactive lead window and 2-retry DPoP nonce / clock-skew recovery.

Zero third-party OAuth client credentials are embedded in this module; client
identity is resolved exclusively from explicit arguments, environment variables,
OS keyring, or user config.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import hashlib
import hmac
import http.server
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from adk._private_file import write_private_text

logger = logging.getLogger("adk.llm.google_ai_plan.auth")

# ── OAuth & DPoP Protocol Constants ──────────────────────────────────────────

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

GOOGLE_AI_PLAN_REQUIRED_SCOPE = (
    "https://www.googleapis.com/auth/generative-language.peruserquota"
)
GOOGLE_AI_PLAN_SCOPES = f"openid email profile {GOOGLE_AI_PLAN_REQUIRED_SCOPE}"

CALLBACK_PATH = "/oauth2callback"
DEFAULT_LOGIN_TIMEOUT_S = 600.0
DEFAULT_TOKEN_TIMEOUT_S = 30.0
DEFAULT_REFRESH_LOCK_TIMEOUT_S = 15.0
PROACTIVE_REFRESH_LEAD_S = 300.0
DEFAULT_EXPIRES_IN_S = 3599
ID_TOKEN_CLOCK_SKEW_S = 60.0

KEYRING_SERVICE = "aither-google-ai-plan"
KEYRING_SESSION_ACCOUNT = "oauth-session-v1"
KEYRING_CLIENT_ID_ACCOUNT = "oauth_client_id"
KEYRING_CLIENT_SECRET_ACCOUNT = "oauth_client_secret"

ENV_CLIENT_ID = "AITHER_GOOGLE_AI_PLAN_CLIENT_ID"
ENV_CLIENT_SECRET = "AITHER_GOOGLE_AI_PLAN_CLIENT_SECRET"
ENV_AUTH_PATH = "AITHER_GOOGLE_AI_PLAN_AUTH_PATH"

_AUTH_FILENAME = "google_ai_plan_auth.json"
_BINDING_FILENAME = "google_ai_plan_binding.json"
_LOCK_FILENAME = "google_ai_plan_refresh.lock"

_VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


# ── Exceptions ───────────────────────────────────────────────────────────────


class GoogleAIPlanConfigError(RuntimeError):
    """Raised when Google AI Plan OAuth client credentials are not configured."""


class GoogleAIPlanAuthError(RuntimeError):
    """Raised on OAuth login, token refresh, DPoP, or device-binding failure."""

    def __init__(
        self,
        message: str,
        *,
        dead_grant: bool = False,
        scope_missing: bool = False,
        failover_cooldown_s: float = 300.0,
    ) -> None:
        super().__init__(message)
        self.dead_grant = dead_grant
        self.scope_missing = scope_missing
        self.failover_cooldown_s = failover_cooldown_s
        self.circuit_scope = "*"


# ── Base64url & JWK Helpers ──────────────────────────────────────────────────


def b64url_encode(data: bytes) -> str:
    """RFC 7515 base64url encoding without '=' padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """RFC 7515 base64url decoding accepting unpadded input."""
    raw = text.encode("ascii")
    pad = (-len(raw)) % 4
    return base64.urlsafe_b64decode(raw + (b"=" * pad))


def _compact_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def generate_dpop_jwk() -> dict[str, str]:
    """Generate a fresh ECDSA NIST P-256 key pair as a 5-field JWK dict."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    nums = private_key.private_numbers()
    pub = nums.public_numbers
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url_encode(pub.x.to_bytes(32, "big")),
        "y": b64url_encode(pub.y.to_bytes(32, "big")),
        "d": b64url_encode(nums.private_value.to_bytes(32, "big")),
    }


def validate_dpop_jwk(jwk: Any) -> dict[str, str]:
    """Validate and normalize a stored 5-field P-256 private JWK."""
    if not isinstance(jwk, dict):
        raise GoogleAIPlanAuthError("Stored DPoP key is missing or malformed.")
    required = ("kty", "crv", "x", "y", "d")
    for field in required:
        val = jwk.get(field)
        if not isinstance(val, str) or not val:
            raise GoogleAIPlanAuthError(f"Stored DPoP JWK missing required field {field!r}.")
    if jwk["kty"] != "EC" or jwk["crv"] != "P-256":
        raise GoogleAIPlanAuthError(
            f"Unsupported DPoP JWK curve/type: kty={jwk['kty']!r}, crv={jwk['crv']!r}."
        )
    for coord in ("x", "y", "d"):
        try:
            raw = b64url_decode(jwk[coord])
        except Exception as exc:
            raise GoogleAIPlanAuthError(f"Invalid base64url in DPoP JWK {coord!r}.") from exc
        if len(raw) != 32:
            raise GoogleAIPlanAuthError(
                f"DPoP JWK {coord!r} must be 32 bytes, got {len(raw)}."
            )
    return {k: jwk[k] for k in required}


def public_jwk(jwk: dict[str, str]) -> dict[str, str]:
    """Extract the strict 4-field public JWK for the DPoP JWS header.

    Google rejects extra JWK metadata fields such as ``ext``, ``key_ops``,
    ``alg``, ``kid``, or private scalar ``d``.
    """
    validated = validate_dpop_jwk(jwk)
    return {
        "kty": validated["kty"],
        "crv": validated["crv"],
        "x": validated["x"],
        "y": validated["y"],
    }


def jwk_thumbprint(jwk: dict[str, str]) -> str:
    """Compute the RFC 7638 SHA-256 base64url thumbprint of an EC P-256 JWK."""
    pub = public_jwk(jwk)
    canonical = json.dumps(
        {"crv": pub["crv"], "kty": pub["kty"], "x": pub["x"], "y": pub["y"]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return b64url_encode(hashlib.sha256(canonical).digest())


def _jwk_to_private_key(jwk: dict[str, str]) -> ec.EllipticCurvePrivateKey:
    validated = validate_dpop_jwk(jwk)
    x = int.from_bytes(b64url_decode(validated["x"]), "big")
    y = int.from_bytes(b64url_decode(validated["y"]), "big")
    d = int.from_bytes(b64url_decode(validated["d"]), "big")
    pub_nums = ec.EllipticCurvePublicNumbers(x=x, y=y, curve=ec.SECP256R1())
    priv_nums = ec.EllipticCurvePrivateNumbers(private_value=d, public_numbers=pub_nums)
    return priv_nums.private_key()


def code_exchange_jti(code: str) -> str:
    """Deterministic DPoP ``jti`` required by Google on authorization_code exchange."""
    return b64url_encode(hashlib.sha256(code.encode("utf-8")).digest())


def refresh_jti() -> str:
    """Fresh 24-byte random DPoP ``jti`` for every refresh attempt."""
    return b64url_encode(secrets.token_bytes(24))


def create_dpop_proof(
    jwk: dict[str, str],
    *,
    jti: str,
    htm: str = "POST",
    htu: str = GOOGLE_TOKEN_URL,
    iat: int | None = None,
    nonce: str | None = None,
) -> str:
    """Mint an RFC 9449 ES256 DPoP compact JWS with raw 64-byte IEEE P1363 R||S signature."""
    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": public_jwk(jwk),
    }
    payload: dict[str, Any] = {
        "jti": jti,
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()) if iat is None else int(iat),
    }
    if nonce:
        payload["nonce"] = nonce

    header_b64 = b64url_encode(_compact_json(header))
    payload_b64 = b64url_encode(_compact_json(payload))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    private_key = _jwk_to_private_key(jwk)
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = b64url_encode(raw_sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


# ── PKCE, Authorize URL & OIDC Validation ────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a (code_verifier, code_challenge) pair using S256."""
    verifier = b64url_encode(secrets.token_bytes(32))
    challenge = b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    """Build the Google OAuth 2.0 authorization URL for Desktop AI Plan sign-in."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_AI_PLAN_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def validate_granted_scope(scope_str: str) -> None:
    """Verify Google granted the required peruserquota scope."""
    granted = set((scope_str or "").split())
    if GOOGLE_AI_PLAN_REQUIRED_SCOPE not in granted:
        raise GoogleAIPlanAuthError(
            "Google sign-in did not grant the required scope "
            f"({GOOGLE_AI_PLAN_REQUIRED_SCOPE}). Re-run `adk auth google-ai-plan` "
            "and approve the Gemini personal-quota permission.",
            scope_missing=True,
        )


def validate_id_token(
    id_token: str,
    *,
    client_id: str,
    expected_nonce: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate OIDC ``id_token`` claims received over TLS from Google's token endpoint.

    Per OIDC Core §3.1.3.7, signature verification is skipped because the token
    was retrieved directly from the token endpoint over TLS. Returns the decoded
    claims (callers MUST NOT persist PII such as ``email``).
    """
    if not id_token or id_token.count(".") != 2:
        raise GoogleAIPlanAuthError("Token response missing valid OIDC id_token.")
    try:
        payload_b64 = id_token.split(".")[1]
        claims = json.loads(b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise GoogleAIPlanAuthError("Failed to decode OIDC id_token payload.") from exc
    if not isinstance(claims, dict):
        raise GoogleAIPlanAuthError("OIDC id_token payload is not a JSON object.")

    iss = claims.get("iss")
    if iss not in _VALID_ISSUERS:
        raise GoogleAIPlanAuthError(f"Unexpected OIDC id_token issuer: {iss!r}.")

    aud = claims.get("aud")
    if aud != client_id:
        raise GoogleAIPlanAuthError("OIDC id_token audience does not match client_id.")

    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise GoogleAIPlanAuthError("OIDC id_token nonce mismatch.")

    exp = claims.get("exp")
    check_now = time.time() if now is None else float(now)
    if not isinstance(exp, (int, float)) or float(exp) <= check_now - ID_TOKEN_CLOCK_SKEW_S:
        raise GoogleAIPlanAuthError("OIDC id_token is expired.")

    return claims


# ── Client Identity Resolution (Clean-Room, Zero Hardcoded Secrets) ──────────


def _try_keyring():
    """Return usable ``keyring`` module or None when unavailable/fail backend."""
    try:
        import keyring  # type: ignore[import-not-found]
        backend = keyring.get_keyring()
        backend_name = type(backend).__name__.lower()
        module_name = type(backend).__module__.lower()
        if "fail" in backend_name or "fail" in module_name or "null" in backend_name:
            return None
        return keyring
    except Exception:
        return None


def resolve_oauth_client(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    config: Any = None,
) -> tuple[str, str]:
    """Resolve Google Desktop OAuth (client_id, client_secret) without hardcoded keys.

    Precedence:
    1. Explicit arguments
    2. Environment variables (AITHER_GOOGLE_AI_PLAN_CLIENT_ID / _SECRET)
    3. Provided ``config`` object
    4. OS Keyring (service ``aither-google-ai-plan``)
    5. Saved ``~/.aither/config.json`` for ``google_ai_plan_client_id`` ONLY
       (client_secret is never read from or written to plaintext config.json)
    """
    cid = (client_id or "").strip()
    csec = (client_secret or "").strip()

    if not cid:
        cid = os.environ.get(ENV_CLIENT_ID, "").strip()
    if not csec:
        csec = os.environ.get(ENV_CLIENT_SECRET, "").strip()

    if config is not None:
        if not cid:
            cid = str(getattr(config, "google_ai_plan_client_id", "") or "").strip()
        if not csec:
            csec = str(getattr(config, "google_ai_plan_client_secret", "") or "").strip()

    if not cid or not csec:
        kr = _try_keyring()
        if kr is not None:
            try:
                if not cid:
                    cid = (kr.get_password(KEYRING_SERVICE, KEYRING_CLIENT_ID_ACCOUNT) or "").strip()
                if not csec:
                    csec = (kr.get_password(KEYRING_SERVICE, KEYRING_CLIENT_SECRET_ACCOUNT) or "").strip()
            except Exception:
                pass

    if not cid:
        try:
            from adk.config import load_saved_config
            saved = load_saved_config()
            cid = str(saved.get("google_ai_plan_client_id", "") or "").strip()
        except Exception:
            pass

    if not cid or not csec:
        missing = []
        if not cid:
            missing.append(ENV_CLIENT_ID)
        if not csec:
            missing.append(ENV_CLIENT_SECRET)
        raise GoogleAIPlanConfigError(
            "Google AI Plan OAuth client is not configured "
            f"(missing: {', '.join(missing)}). "
            "Provide your Aitherium Desktop OAuth client via environment variables "
            f"({ENV_CLIENT_ID} / {ENV_CLIENT_SECRET}) or run "
            "`adk auth google-ai-plan --client-id <ID> --client-secret-stdin`."
        )
    return cid, csec


# ── Credential Model, Storage Ladder & Optional awseal Binding ───────────────


@dataclass
class GoogleAIPlanCredential:
    """Persisted Google AI Plan OAuth session (zero user PII)."""

    client_id: str
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    dpop_jwk: dict[str, str]
    dpop_nonce: str = ""
    jwk_thumbprint: str = ""
    bound_at: str = ""
    version: int = 1
    provider: str = "google-ai-plan"

    def __post_init__(self) -> None:
        self.dpop_jwk = validate_dpop_jwk(self.dpop_jwk)
        if not self.jwk_thumbprint:
            self.jwk_thumbprint = jwk_thumbprint(self.dpop_jwk)
        if not self.bound_at:
            self.bound_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "provider": self.provider,
            "client_id": self.client_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": float(self.expires_at),
            "scope": self.scope,
            "dpop_jwk": validate_dpop_jwk(self.dpop_jwk),
            "dpop_nonce": self.dpop_nonce,
            "jwk_thumbprint": self.jwk_thumbprint or jwk_thumbprint(self.dpop_jwk),
            "bound_at": self.bound_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoogleAIPlanCredential":
        if not isinstance(data, dict):
            raise GoogleAIPlanAuthError("Corrupted Google AI Plan credential payload.")
        return cls(
            version=int(data.get("version", 1)),
            provider=str(data.get("provider", "google-ai-plan")),
            client_id=str(data.get("client_id", "")),
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=float(data.get("expires_at", 0.0)),
            scope=str(data.get("scope", "")),
            dpop_jwk=validate_dpop_jwk(data.get("dpop_jwk")),
            dpop_nonce=str(data.get("dpop_nonce", "") or ""),
            jwk_thumbprint=str(data.get("jwk_thumbprint", "") or ""),
            bound_at=str(data.get("bound_at", "") or ""),
        )

    def needs_refresh(self, *, now: float | None = None, lead_s: float = PROACTIVE_REFRESH_LEAD_S) -> bool:
        check_now = time.time() if now is None else float(now)
        return (not self.access_token) or (self.expires_at <= check_now + lead_s)


def _get_machine_id() -> str:
    try:
        from adk.device_identity import machine_id
        return str(machine_id())
    except Exception:
        return hashlib.sha256(f"{os.name}:{sys.platform}".encode("utf-8")).hexdigest()[:32]


def _try_awseal_modules():
    """Return ``(awseal_keys, awseal_seal)`` or ``None`` if ``awseal`` is absent."""
    try:
        from awseal import keys as awseal_keys  # type: ignore[import-not-found]
        from awseal import seal as awseal_seal  # type: ignore[import-not-found]
        return awseal_keys, awseal_seal
    except Exception:
        return None


def _binding_canonical_bytes(payload: dict[str, Any], awseal_seal: Any) -> bytes:
    canonical_fn = getattr(awseal_seal, "canonical", None)
    if callable(canonical_fn):
        res = canonical_fn(payload)
        return res if isinstance(res, bytes) else str(res).encode("utf-8")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class GoogleAIPlanCredentialStore:
    """Isolated credential store (OS keyring -> owner-only file) + optional awseal binding."""

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        auth_path: str | Path | None = None,
        use_keyring: bool | None = None,
    ) -> None:
        env_path = os.environ.get(ENV_AUTH_PATH, "").strip()
        if auth_path is not None:
            self._auth_path = Path(auth_path)
            self._data_dir = self._auth_path.parent
            self._use_keyring = False if use_keyring is None else bool(use_keyring)
        elif env_path:
            self._auth_path = Path(env_path)
            self._data_dir = self._auth_path.parent
            self._use_keyring = False if use_keyring is None else bool(use_keyring)
        else:
            base = Path(data_dir) if data_dir is not None else Path(
                os.environ.get("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
            )
            self._data_dir = base
            self._auth_path = base / _AUTH_FILENAME
            self._use_keyring = True if use_keyring is None else bool(use_keyring)

        self._binding_path = self._auth_path.with_name(_BINDING_FILENAME)
        self._lock_path = self._auth_path.with_name(_LOCK_FILENAME)
        self._storage_backend: str = "none"

    @property
    def auth_path(self) -> Path:
        return self._auth_path

    @property
    def binding_path(self) -> Path:
        return self._binding_path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def storage_backend(self) -> str:
        return self._storage_backend

    def _keyring(self):
        if not self._use_keyring:
            return None
        return _try_keyring()

    def _write_awseal_binding(self, cred: GoogleAIPlanCredential) -> bool:
        mods = _try_awseal_modules()
        if mods is None:
            return False
        awseal_keys, awseal_seal = mods
        try:
            priv = awseal_keys.load_private_key()
        except Exception:
            try:
                from adk.device_identity import seal_public_key
                if not seal_public_key(create=True):
                    return False
                priv = awseal_keys.load_private_key()
            except Exception:
                return False

        pub_hex = awseal_keys.public_key_hex(priv)
        payload = {
            "version": 1,
            "dpop_jwk_thumbprint": cred.jwk_thumbprint,
            "machine_id": _get_machine_id(),
            "client_id": cred.client_id,
            "bound_at": cred.bound_at,
            "public_key": pub_hex,
        }
        sig_bytes = priv.sign(_binding_canonical_bytes(payload, awseal_seal))
        record = dict(payload)
        record["signature"] = sig_bytes.hex()
        self._binding_path.parent.mkdir(parents=True, exist_ok=True)
        self._binding_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return True

    def _verify_awseal_binding(self, cred: GoogleAIPlanCredential) -> str:
        if not self._binding_path.exists():
            return "unsealed"
        try:
            record = json.loads(self._binding_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GoogleAIPlanAuthError(
                "Corrupted awseal device binding record; run `adk auth google-ai-plan`."
            ) from exc
        if not isinstance(record, dict):
            raise GoogleAIPlanAuthError(
                "Invalid awseal device binding record; run `adk auth google-ai-plan`."
            )

        mods = _try_awseal_modules()
        if mods is None:
            raise GoogleAIPlanAuthError(
                "Credential is sealed with awseal, but awseal is not installed "
                "(install `awdk[seal]` or re-authenticate)."
            )
        awseal_keys, awseal_seal = mods

        try:
            host_priv = awseal_keys.load_private_key()
            host_pub_hex = awseal_keys.public_key_hex(host_priv)
        except Exception as exc:
            raise GoogleAIPlanAuthError(
                "Credential is sealed with awseal, but host device key is unavailable; "
                "run `adk auth google-ai-plan`."
            ) from exc

        if record.get("public_key") != host_pub_hex:
            raise GoogleAIPlanAuthError(
                "Credential bound to another device key; run `adk auth google-ai-plan`."
            )
        if record.get("machine_id") != _get_machine_id():
            raise GoogleAIPlanAuthError(
                "Credential bound to another machine_id; run `adk auth google-ai-plan`."
            )
        actual_thumbprint = jwk_thumbprint(cred.dpop_jwk)
        if record.get("dpop_jwk_thumbprint") != actual_thumbprint:
            raise GoogleAIPlanAuthError(
                "DPoP key thumbprint mismatch against device binding; "
                "run `adk auth google-ai-plan`."
            )

        sig_hex = str(record.get("signature", "") or "")
        payload = {k: v for k, v in record.items() if k != "signature"}
        try:
            sig_bytes = bytes.fromhex(sig_hex)
            host_pub = host_priv.public_key()
            host_pub.verify(sig_bytes, _binding_canonical_bytes(payload, awseal_seal))
        except Exception as exc:
            raise GoogleAIPlanAuthError(
                "awseal device binding signature verification failed; "
                "run `adk auth google-ai-plan`."
            ) from exc

        return "verified"

    def save(self, cred: GoogleAIPlanCredential, *, write_binding: bool = True) -> str:
        """Persist credential to OS keyring (preferred) or owner-only private file."""
        serialized = json.dumps(cred.to_dict(), separators=(",", ":"))
        kr = self._keyring()
        saved_backend = ""
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, KEYRING_SESSION_ACCOUNT, serialized)
                saved_backend = "os-keyring"
                # Remove stale file copy if keyring succeeded
                if self._auth_path.exists():
                    try:
                        self._auth_path.unlink()
                    except OSError:
                        pass
            except Exception as exc:
                logger.debug("Keyring save failed, falling back to private file: %s", exc)

        if not saved_backend:
            write_private_text(self._auth_path, json.dumps(cred.to_dict(), indent=2) + "\n")
            saved_backend = "private-file"

        self._storage_backend = saved_backend
        if write_binding:
            self._write_awseal_binding(cred)
        return saved_backend

    def load(self, *, verify_binding: bool = True) -> GoogleAIPlanCredential | None:
        """Load credential from OS keyring or private file; fail closed on bad binding."""
        raw: str | None = None
        backend = "none"
        kr = self._keyring()
        if kr is not None:
            try:
                raw = kr.get_password(KEYRING_SERVICE, KEYRING_SESSION_ACCOUNT)
                if raw:
                    backend = "os-keyring"
            except Exception:
                raw = None

        if not raw and self._auth_path.exists():
            raw = self._auth_path.read_text(encoding="utf-8")
            backend = "private-file"

        if not raw:
            self._storage_backend = "none"
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoogleAIPlanAuthError(
                "Stored Google AI Plan credential is invalid JSON; run `adk auth google-ai-plan`."
            ) from exc

        cred = GoogleAIPlanCredential.from_dict(data)
        self._storage_backend = backend
        if verify_binding:
            self._verify_awseal_binding(cred)
        return cred

    def binding_status(self, cred: GoogleAIPlanCredential | None = None) -> str:
        """Return 'verified', 'unsealed', or 'mismatch: <reason>'."""
        if not self._binding_path.exists():
            return "unsealed"
        target = cred
        if target is None:
            try:
                target = self.load(verify_binding=False)
            except Exception as exc:
                return f"mismatch: {exc}"
        if target is None:
            return "unsealed"
        try:
            return self._verify_awseal_binding(target)
        except Exception as exc:
            return f"mismatch: {exc}"

    def delete(self) -> bool:
        """Delete stored credential from keyring, private file, and binding file."""
        removed = False
        kr = self._keyring()
        if kr is not None:
            try:
                if kr.get_password(KEYRING_SERVICE, KEYRING_SESSION_ACCOUNT) is not None:
                    kr.delete_password(KEYRING_SERVICE, KEYRING_SESSION_ACCOUNT)
                    removed = True
            except Exception:
                pass
        for path in (self._auth_path, self._binding_path):
            if path.exists():
                try:
                    path.unlink()
                    removed = True
                except OSError:
                    pass
        self._storage_backend = "none"
        return removed


# ── Cross-Process + In-Process Refresh Locking ───────────────────────────────

_PROCESS_LOCKS: dict[str, asyncio.Lock] = {}


def _get_async_lock(key: str) -> asyncio.Lock:
    lock = _PROCESS_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROCESS_LOCKS[key] = lock
    return lock


@contextmanager
def _file_lock(lock_path: Path, timeout_s: float = DEFAULT_REFRESH_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Cross-process advisory file lock with bounded acquisition timeout."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout_s
    acquired = False
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GoogleAIPlanAuthError(
                        "Timed out waiting for Google AI Plan refresh lock."
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


# ── Loopback Callback Server & Paste Parsing ─────────────────────────────────


def parse_callback_url(raw_input: str, expected_state: str) -> str:
    """Extract and validate OAuth ``code`` from a loopback callback URL or query string."""
    text = (raw_input or "").strip()
    if not text:
        raise GoogleAIPlanAuthError("Empty OAuth callback input.")
    parsed = urllib.parse.urlparse(text)
    query = parsed.query if parsed.query else (text.lstrip("?") if "=" in text else "")
    params = urllib.parse.parse_qs(query, keep_blank_values=False)

    err = (params.get("error") or [""])[0]
    if err:
        desc = (params.get("error_description") or [""])[0]
        raise GoogleAIPlanAuthError(
            f"Google OAuth returned error {err!r}" + (f": {desc}" if desc else "")
        )

    state = (params.get("state") or [""])[0]
    if not state or not hmac.compare_digest(state, expected_state):
        raise GoogleAIPlanAuthError("OAuth callback state mismatch.")

    code = (params.get("code") or [""])[0]
    if not code:
        raise GoogleAIPlanAuthError("OAuth callback missing authorization code.")
    return code


class LoopbackOAuthServer:
    """Single-use HTTP callback server strictly bound to ``127.0.0.1:0``."""

    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self._event = threading.Event()
        self.code: str | None = None
        self.error: str | None = None

        parent = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    code = parse_callback_url(self.path, parent.expected_state)
                    parent.code = code
                    body = (
                        "<!doctype html><html><body>"
                        "<h2>AitherOS / awdk sign-in complete</h2>"
                        "<p>You may close this tab and return to your terminal.</p>"
                        "</body></html>"
                    ).encode("utf-8")
                    self.send_response(200)
                except GoogleAIPlanAuthError as exc:
                    parent.error = str(exc)
                    body = (
                        "<!doctype html><html><body>"
                        "<h2>AitherOS / awdk sign-in failed</h2>"
                        "<p>Return to your terminal for details.</p>"
                        "</body></html>"
                    ).encode("utf-8")
                    self.send_response(400)

                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                parent._event.set()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout_s: float) -> str | None:
        if not self._event.wait(timeout=timeout_s):
            return None
        if self.error:
            raise GoogleAIPlanAuthError(self.error)
        return self.code

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass


# ── Token Endpoint Exchange, Refresh & Revoke ────────────────────────────────


def _parse_server_date_epoch(date_header: str | None) -> int | None:
    if not date_header:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_token_error(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("error", "") or ""), str(body.get("error_description", "") or "")
    except Exception:
        pass
    return "", resp.text[:300]


async def exchange_authorization_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    expected_nonce: str,
    dpop_jwk: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = DEFAULT_TOKEN_TIMEOUT_S,
) -> GoogleAIPlanCredential:
    """Exchange an authorization code for DPoP-bound Google AI Plan tokens."""
    jwk = validate_dpop_jwk(dpop_jwk) if dpop_jwk is not None else generate_dpop_jwk()
    proof = create_dpop_proof(
        jwk,
        jti=code_exchange_jti(code),
        htm="POST",
        htu=GOOGLE_TOKEN_URL,
        nonce=None,
    )
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "DPoP": proof,
    }

    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=form, headers=headers)

    if resp.status_code != 200:
        err_code, err_desc = _parse_token_error(resp)
        raise GoogleAIPlanAuthError(
            f"Google token exchange failed (HTTP {resp.status_code}"
            + (f", {err_code}" if err_code else "")
            + (f": {err_desc}" if err_desc else "")
            + ")."
        )

    payload = resp.json()
    scope = str(payload.get("scope", "") or "")
    validate_granted_scope(scope)
    validate_id_token(
        str(payload.get("id_token", "") or ""),
        client_id=client_id,
        expected_nonce=expected_nonce,
    )

    access_token = str(payload.get("access_token", "") or "")
    refresh_token = str(payload.get("refresh_token", "") or "")
    if not access_token or not refresh_token:
        raise GoogleAIPlanAuthError(
            "Google token exchange response missing access_token or refresh_token."
        )

    expires_in = int(payload.get("expires_in") or DEFAULT_EXPIRES_IN_S)
    # Intentionally do NOT save DPoP-Nonce from code exchange — refreshes use a separate nonce.
    return GoogleAIPlanCredential(
        client_id=client_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + expires_in,
        scope=scope,
        dpop_jwk=jwk,
        dpop_nonce="",
    )


async def refresh_credential(
    cred: GoogleAIPlanCredential,
    *,
    client_secret: str,
    client_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = DEFAULT_TOKEN_TIMEOUT_S,
) -> GoogleAIPlanCredential:
    """Refresh a DPoP-bound Google AI Plan token with 2-retry nonce/clock-skew recovery."""
    cid = (client_id or cred.client_id).strip()
    if not cid or not client_secret:
        raise GoogleAIPlanConfigError("Missing OAuth client_id or client_secret for refresh.")
    if not cred.refresh_token:
        raise GoogleAIPlanAuthError(
            "No refresh_token stored; run `adk auth google-ai-plan`.",
            dead_grant=True,
        )

    nonce: str | None = cred.dpop_nonce or None
    iat_override: int | None = None
    skew_retried = False
    form = {
        "grant_type": "refresh_token",
        "refresh_token": cred.refresh_token,
        "client_id": cid,
        "client_secret": client_secret,
    }

    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        for attempt in range(3):  # initial attempt + up to 2 retries
            proof = create_dpop_proof(
                cred.dpop_jwk,
                jti=refresh_jti(),
                htm="POST",
                htu=GOOGLE_TOKEN_URL,
                iat=iat_override,
                nonce=nonce,
            )
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "DPoP": proof,
            }
            resp = await client.post(GOOGLE_TOKEN_URL, data=form, headers=headers)

            if resp.status_code == 200:
                payload = resp.json()
                access_token = str(payload.get("access_token", "") or "")
                if not access_token:
                    raise GoogleAIPlanAuthError("Google token refresh returned empty access_token.")
                expires_in = int(payload.get("expires_in") or DEFAULT_EXPIRES_IN_S)
                rotated_refresh = str(payload.get("refresh_token", "") or "") or cred.refresh_token
                resp_nonce = (resp.headers.get("dpop-nonce") or "").strip() or (nonce or "")
                return GoogleAIPlanCredential(
                    client_id=cid,
                    access_token=access_token,
                    refresh_token=rotated_refresh,
                    expires_at=time.time() + expires_in,
                    scope=str(payload.get("scope", "") or cred.scope),
                    dpop_jwk=cred.dpop_jwk,
                    dpop_nonce=resp_nonce,
                    jwk_thumbprint=cred.jwk_thumbprint,
                    bound_at=cred.bound_at,
                )

            err_code, err_desc = _parse_token_error(resp)

            if err_code in ("invalid_grant", "unauthorized_client"):
                raise GoogleAIPlanAuthError(
                    f"Google AI Plan grant is no longer valid ({err_code}). "
                    "Re-authenticate with `adk auth google-ai-plan`.",
                    dead_grant=True,
                )

            if resp.status_code == 400 and err_code == "use_dpop_nonce" and attempt < 2:
                new_nonce = (resp.headers.get("dpop-nonce") or "").strip()
                if new_nonce:
                    nonce = new_nonce
                    continue

            if (
                resp.status_code == 400
                and err_code == "invalid_dpop_proof"
                and not skew_retried
                and attempt < 2
            ):
                server_epoch = _parse_server_date_epoch(resp.headers.get("date"))
                if server_epoch is not None:
                    iat_override = server_epoch
                    skew_retried = True
                    continue

            raise GoogleAIPlanAuthError(
                f"Google token refresh failed (HTTP {resp.status_code}"
                + (f", {err_code}" if err_code else "")
                + (f": {err_desc}" if err_desc else "")
                + ")."
            )

    raise GoogleAIPlanAuthError("Google token refresh exhausted DPoP retries.")


async def get_valid_access_token(
    store: GoogleAIPlanCredentialStore | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    config: Any = None,
    force_refresh: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    lead_s: float = PROACTIVE_REFRESH_LEAD_S,
) -> tuple[str, GoogleAIPlanCredential]:
    """Return a valid access token, refreshing safely under double-checked locks."""
    cred_store = store or GoogleAIPlanCredentialStore()
    cred = cred_store.load(verify_binding=True)
    if cred is None:
        raise GoogleAIPlanAuthError(
            "Not signed in to Google AI Plan. Run `adk auth google-ai-plan` to sign in.",
            dead_grant=True,
        )

    if not force_refresh and not cred.needs_refresh(lead_s=lead_s):
        return cred.access_token, cred

    async_lock = _get_async_lock(str(cred_store.lock_path.resolve()))
    async with async_lock:
        with _file_lock(cred_store.lock_path):
            # Re-read store inside lock: another coroutine/process may have refreshed
            fresh = cred_store.load(verify_binding=True)
            if fresh is None:
                raise GoogleAIPlanAuthError(
                    "Google AI Plan credential was removed; run `adk auth google-ai-plan`.",
                    dead_grant=True,
                )
            if not force_refresh and not fresh.needs_refresh(lead_s=lead_s):
                return fresh.access_token, fresh

            resolved_cid, resolved_csec = resolve_oauth_client(
                client_id=client_id or fresh.client_id,
                client_secret=client_secret,
                config=config,
            )
            refreshed = await refresh_credential(
                fresh,
                client_id=resolved_cid,
                client_secret=resolved_csec,
                transport=transport,
            )
            cred_store.save(refreshed, write_binding=False)
            return refreshed.access_token, refreshed


async def revoke_token(
    token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 15.0,
) -> bool:
    """Best-effort RFC 7009 token revocation against Google's revoke endpoint."""
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
            resp = await client.post(
                GOOGLE_REVOKE_URL,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            return resp.status_code in (200, 400)
    except Exception:
        return False


def _is_headless() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


async def login_google_ai_plan(
    *,
    store: GoogleAIPlanCredentialStore | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    config: Any = None,
    no_browser: bool = False,
    timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
    out: Callable[[str], None] = print,
    paste_reader: Callable[[], str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GoogleAIPlanCredential:
    """Run interactive Desktop PKCE + DPoP OAuth login with loopback + paste capture."""
    cid, csec = resolve_oauth_client(
        client_id=client_id,
        client_secret=client_secret,
        config=config,
    )
    cred_store = store or GoogleAIPlanCredentialStore()
    code_verifier, code_challenge = generate_pkce_pair()
    state = b64url_encode(secrets.token_bytes(32))
    nonce = b64url_encode(secrets.token_bytes(32))
    dpop_jwk = generate_dpop_jwk()

    server = LoopbackOAuthServer(expected_state=state)
    server.start()
    try:
        auth_url = build_authorize_url(
            client_id=cid,
            redirect_uri=server.redirect_uri,
            code_challenge=code_challenge,
            state=state,
            nonce=nonce,
        )
        out("Open this URL in your browser to sign in with Google AI Plan:")
        out(f"  {auth_url}")
        if not no_browser and not _is_headless():
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass

        code: str | None = None
        if no_browser or _is_headless() or paste_reader is not None:
            out("Waiting for loopback callback, or paste the final redirect URL and press Enter:")
            pasted_holder: dict[str, Any] = {}
            paste_done = threading.Event()

            def _read_paste() -> None:
                try:
                    reader = paste_reader or (lambda: input())
                    line = reader()
                    if line and line.strip():
                        pasted_holder["raw"] = line.strip()
                except Exception:
                    pass
                finally:
                    paste_done.set()

            paste_thread = threading.Thread(target=_read_paste, daemon=True)
            paste_thread.start()

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if server._event.wait(timeout=0.2):
                    if server.error:
                        raise GoogleAIPlanAuthError(server.error)
                    code = server.code
                    break
                if paste_done.is_set() and "raw" in pasted_holder:
                    code = parse_callback_url(pasted_holder["raw"], state)
                    break
        else:
            code = await asyncio.to_thread(server.wait, timeout_s)

        if not code:
            raise GoogleAIPlanAuthError(
                f"Timed out after {int(timeout_s)}s waiting for Google OAuth callback."
            )
    finally:
        server.close()

    cred = await exchange_authorization_code(
        code=code,
        code_verifier=code_verifier,
        redirect_uri=server.redirect_uri,
        client_id=cid,
        client_secret=csec,
        expected_nonce=nonce,
        dpop_jwk=dpop_jwk,
        transport=transport,
    )
    cred_store.save(cred, write_binding=True)
    return cred
