"""Shared auth — interoperates with ``aithershell`` and AitherIdentity.

This module reads and writes the same ``~/.aither/auth.json`` file that
``aithershell`` uses, so ``aither login`` from any tool authenticates the
whole CLI surface (chat, agents, scheduler, adapters, portal sync).

Supported credential types:

- **Local root** — auto-provisioned for offline dev. Token = ``aither_root_local``.
  Mirrors ``aithershell``'s ``ROOT_PROFILE``.
- **ACTA API key** — ``aither_sk_live_*`` bearer tokens issued by
  ``portal.aitherium.com`` (verified by AitherACTA / Identity). Used for
  cloud inference billing and per-tenant agent isolation.
- **OAuth device code** — RFC 8628 flow against AitherIdentity. The user
  visits a URL, types a code, and the CLI polls for the access_token.
  Backed by AitherIdentity's ``/oauth/device/code`` and ``/oauth/token``.

Environment overrides:

- ``AITHERIUM_BASE_URL``   — portal API root (default ``https://api.aitheros.ai``)
- ``AITHERIDENTITY_URL``   — identity service URL (default uses base URL)
- ``AITHER_OIDC_CLIENT_ID`` — OAuth client_id (default ``aither-cli``)
- ``AITHERIUM_API_KEY``    — short-circuit: skip the file store entirely

Principal authority (G23):

- **Principal** — the caller's identity (subject_id, principal_class, role, clearance).
- **AuthContext** — wraps a Principal and evaluates authorization (can(action, ...)).
- **PrincipalResolver(ABC)** — abstract resolver for channel-specific identity verification.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adk.core.logging import get_logger

_log = get_logger("aither_adk.auth")

# ---------------------------------------------------------------------------
# Principal Authority (G23)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Caller's identity and authority metadata.

    Attributes:
        subject_id: Unique caller identifier (user ID, customer ID, agent ID, etc.).
        principal_class: Type of principal ("customer", "supplier", "employee", "ceo",
                        "agent", "system", etc.).
        role: Optional role name (e.g., "admin", "viewer", "editor").
        clearance: Numeric clearance level (0 = no special clearance, higher = more trust).
        allowed_action_types: Frozenset of action classes this principal can perform
                             (empty = no action types allowed unless verified via context).
                             Use "*" for wildcard (all action types).
        channel: Channel/platform this principal came from (e.g., "telegram", "slack").
        verified: Whether this principal's identity has been cryptographically verified.
    """
    subject_id: str
    principal_class: str
    role: str = ""
    clearance: int = 0
    allowed_action_types: frozenset[str] = field(default_factory=frozenset)
    channel: str = ""
    verified: bool = False


class AuthContext:
    """Authorization context wrapping a Principal and policy enforcement.

    Evaluates whether a principal can perform actions based on clearance,
    action class, and verification status. Default-deny for unverified principals.
    """

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    def can(
        self,
        action: str,
        *,
        required_clearance: int = 0,
        action_class: str = "",
    ) -> bool:
        """Evaluate whether the principal is authorized for an action.

        Args:
            action: The action name (currently unused; reserved for future ACL policy).
            required_clearance: Minimum clearance level required (default 0).
            action_class: Action class category (e.g., "write", "delete", "admin").
                         If empty, no action class restriction applies.

        Returns:
            True if the principal is authorized; False otherwise (default-deny).

        Rules:
        - If principal is unverified and action requires clearance (required_clearance > 0)
          or has an action_class, deny.
        - If principal is verified:
          - Clearance must satisfy: principal.clearance >= required_clearance
          - Action class must match: action_class == "" (no restriction) OR
            action_class in principal.allowed_action_types OR
            "*" in principal.allowed_action_types
        """
        if not self.principal.verified:
            # Unverified principals can only do truly public things (no clearance/class reqs)
            return required_clearance == 0 and action_class == ""

        # Verified principal: clearance + action class
        if required_clearance > 0 and self.principal.clearance < required_clearance:
            return False

        if action_class:
            if (action_class not in self.principal.allowed_action_types
                and "*" not in self.principal.allowed_action_types):
                return False

        return True

    @classmethod
    def anonymous(cls) -> AuthContext:
        """Create an unverified, anonymous principal with no authority."""
        return cls(Principal(
            subject_id="anon",
            principal_class="anonymous",
            verified=False,
        ))

    @classmethod
    def system(cls) -> AuthContext:
        """Create a fully trusted system principal (internal/trusted calls)."""
        return cls(Principal(
            subject_id="system",
            principal_class="system",
            role="admin",
            clearance=999,
            allowed_action_types=frozenset(["*"]),
            verified=True,
        ))


class PrincipalResolver(ABC):
    """Abstract base for resolving a raw identity into a verified Principal.

    Implementations verify channel-specific signatures/proofs and return a Principal
    with appropriate clearance, role, and verified flag set.

    Example implementations:
    - TelegramResolver — verify Telegram WebApp initData signature
    - SlackResolver — verify Slack request signature
    - DirectResolver — consume pre-signed JWT or HMAC-signed claims
    """

    @abstractmethod
    async def resolve(
        self,
        channel: str,
        raw_identity: dict,
        signature: str | None = None,
    ) -> Principal:
        """Resolve and verify a caller's identity.

        Args:
            channel: Platform/channel identifier ("telegram", "slack", "api", etc.).
            raw_identity: Unverified claims dict (user_id, username, etc.).
            signature: Optional cryptographic signature for verification (HMAC, JWT, etc.).

        Returns:
            A Principal with verified=True if identity checks pass,
            or verified=False if checks fail (graceful degradation).
        """
        ...


class DenyAllResolver(PrincipalResolver):
    """Default resolver: always returns unverified anonymous principal.

    Used when no app-level resolver is configured. Concrete resolvers
    (e.g., TelegramResolver, SlackResolver) are provided by the app layer.
    """

    async def resolve(
        self,
        channel: str,
        raw_identity: dict,
        signature: str | None = None,
    ) -> Principal:
        """Return an unverified principal (default-deny)."""
        return Principal(
            subject_id=raw_identity.get("user_id", "unknown"),
            principal_class="unknown",
            channel=channel,
            verified=False,
        )


AUTH_FILE = Path.home() / ".aither" / "auth.json"
AUTH_VERSION = 1

# Measured 2026-08-07: `api.aitheros.ai` does not resolve — `getaddrinfo failed`
# on every fresh install. It had been the baked default for the whole device
# flow, so `adk acp login` and the ACP server's `aither-device` method were both
# dead on arrival for anyone who had not set AITHERIDENTITY_URL. Nothing caught
# it because a DNS failure on a login nobody ran is silent, and every developer
# box here has the env var set.
#
# This value is the one AitherIdentity's OWN discovery document advertises:
#   GET https://idp.aitherium.com/.well-known/openid-configuration
#     issuer                        https://idp.aitherium.com/identity
#     device_authorization_endpoint https://idp.aitherium.com/identity/auth/device/code
# Re-derive it from discovery rather than editing this string by hand.
DEFAULT_PORTAL_URL = "https://idp.aitherium.com/identity"
DEFAULT_CLIENT_ID = "aither-cli"

# Same root profile aithershell uses — keeps the two tools in lock-step.
ROOT_PROFILE: dict[str, Any] = {
    "endpoint": "local",
    "genesis_url": os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"),
    "token_type": "local",
    "access_token": "aither_root_local",
    "expires_at": "",
    "user": {
        "id": "root",
        "username": "root",
        "display_name": "root",
        "email": "",
        "roles": ["admin"],
        "tenant_id": "",
        "tenant_slug": "",
    },
}


class AuthError(RuntimeError):
    """Raised on credential lookup / OAuth failures."""


# ---------------------------------------------------------------------------
# Profile + on-disk store
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Credentials:
    """Resolved credentials for a portal/identity round-trip."""

    access_token: str
    token_type: str = "bearer"  # bearer | acta | local
    endpoint: str = ""
    user: dict[str, Any] = field(default_factory=dict)
    expires_at: str = ""

    @property
    def is_local(self) -> bool:
        return self.token_type == "local"

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        return exp < datetime.now(timezone.utc)

    def authorization_header(self) -> str:
        return f"Bearer {self.access_token}"


class AuthStore:
    """File-backed credential store at ``~/.aither/auth.json``.

    Format matches ``aithershell.auth`` so the two share state.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or AUTH_FILE

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != AUTH_VERSION:
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data.setdefault("version", AUTH_VERSION)
        self.path.write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def set_profile(self, name: str, profile: dict[str, Any]) -> None:
        store = self.load() or {
            "version": AUTH_VERSION,
            "active_profile": name,
            "profiles": {},
        }
        store.setdefault("profiles", {})[name] = profile
        store["active_profile"] = name
        self.save(store)

    def get_active_profile(self) -> dict[str, Any] | None:
        store = self.load()
        if not store:
            return None
        active = store.get("active_profile", "local")
        return store.get("profiles", {}).get(active)

    def clear_profile(self, name: str) -> None:
        store = self.load()
        if not store:
            return
        store.get("profiles", {}).pop(name, None)
        if store.get("active_profile") == name:
            remaining = list(store.get("profiles", {}).keys())
            store["active_profile"] = remaining[0] if remaining else ""
        self.save(store)

    def ensure_root(self) -> dict[str, Any]:
        """Provision the local-root profile if no valid session exists.

        Mirrors :func:`aithershell.auth.ensure_root_profile` so a brand-new
        install just works — same behaviour as Linux auto-login as root.
        """
        existing = self.get_active_profile()
        if existing and existing.get("access_token"):
            return existing
        self.set_profile("local", ROOT_PROFILE.copy())
        return ROOT_PROFILE


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def resolve_credentials(
    *,
    store: AuthStore | None = None,
    env_key: str = "AITHERIUM_API_KEY",
) -> Credentials:
    """Resolve credentials in order: env var → auth.json → local root."""
    api_key = os.environ.get(env_key, "").strip()
    if api_key:
        token_type = "acta" if api_key.startswith("aither_sk_") else "bearer"
        return Credentials(
            access_token=api_key,
            token_type=token_type,
            endpoint=os.environ.get(
                "AITHERIUM_BASE_URL", DEFAULT_PORTAL_URL
            ).rstrip("/"),
        )

    s = store or AuthStore()
    profile = s.get_active_profile() or s.ensure_root()
    return Credentials(
        access_token=profile.get("access_token", ""),
        token_type=profile.get("token_type", "bearer"),
        endpoint=profile.get("endpoint", "local"),
        user=profile.get("user", {}),
        expires_at=profile.get("expires_at", ""),
    )


# ---------------------------------------------------------------------------
# OAuth 2.0 Device Authorization Grant (RFC 8628)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeviceCodeChallenge:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int = 5


async def begin_device_login(
    *,
    identity_url: str | None = None,
    client_id: str | None = None,
    scope: str = "openid profile email agents",
) -> DeviceCodeChallenge:
    """Kick off RFC 8628 device-code flow against AitherIdentity.

    Returns the challenge — the caller shows the user ``user_code`` and
    ``verification_uri``, then calls :func:`finish_device_login`.
    """
    import httpx

    url = (
        identity_url
        or os.environ.get(
            "AITHERIDENTITY_URL",
            os.environ.get("AITHERIUM_BASE_URL", DEFAULT_PORTAL_URL),
        )
    ).rstrip("/")
    cid = client_id or os.environ.get("AITHER_OIDC_CLIENT_ID", DEFAULT_CLIENT_ID)

    async with httpx.AsyncClient(timeout=15) as client:
        # `/auth/device/code`, JSON body. Both were wrong here until 2026-08-07:
        # the RFC-8628-style `/oauth/device/code` 404s on AitherIdentity, and a
        # form-encoded body is rejected 422 ("Input should be a valid dictionary")
        # because the endpoint is a FastAPI model. Its sibling
        # `autonomous_agent_login` in this same file has always used the right
        # shape — the two had silently diverged.
        resp = await client.post(
            f"{url}/auth/device/code",
            json={"client_id": cid, "scope": scope},
        )
        if resp.status_code >= 400:
            raise AuthError(
                f"device-code start failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()

    return DeviceCodeChallenge(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        verification_uri_complete=body.get(
            "verification_uri_complete", body["verification_uri"]
        ),
        expires_in=int(body.get("expires_in", 600)),
        interval=int(body.get("interval", 5)),
    )


async def finish_device_login(
    challenge: DeviceCodeChallenge,
    *,
    identity_url: str | None = None,
    client_id: str | None = None,
    store: AuthStore | None = None,
    profile_name: str = "portal",
) -> Credentials:
    """Poll the token endpoint until the user finishes the device flow.

    On success the resulting tokens are persisted to ``~/.aither/auth.json``
    under ``profile_name`` and made the active profile.
    """
    import httpx

    url = (
        identity_url
        or os.environ.get(
            "AITHERIDENTITY_URL",
            os.environ.get("AITHERIUM_BASE_URL", DEFAULT_PORTAL_URL),
        )
    ).rstrip("/")
    cid = client_id or os.environ.get("AITHER_OIDC_CLIENT_ID", DEFAULT_CLIENT_ID)
    deadline = asyncio.get_event_loop().time() + challenge.expires_in
    interval = max(1, challenge.interval)

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise AuthError("device login expired before user approval")
            await asyncio.sleep(interval)
            resp = await client.post(
                f"{url}/auth/device/token",
                json={"device_code": challenge.device_code, "client_id": cid},
            )
            body = resp.json() if resp.content else {}
            if resp.status_code == 200 and body.get("access_token"):
                break
            # AitherIdentity answers a pending poll with HTTP **200** and
            # `{"status": "authorization_pending", "interval": 5}` — not the
            # RFC 8628 `400` + `{"error": "authorization_pending"}`. Reading only
            # `error` left `err` empty on every tick, so the loop raised
            # "device login failed: 200" on the FIRST poll and no device login
            # could ever complete. Accept both shapes; the RFC form is still
            # honoured so a standards-compliant IdP keeps working.
            err = body.get("error") or body.get("status") or ""
            if err == "authorization_pending":
                # A server-suggested interval wins — it is how it asks us to
                # back off without an explicit slow_down.
                interval = max(interval, int(body.get("interval", interval) or interval))
                continue
            if err == "slow_down":
                interval += 5
                continue
            # Carry the server's own explanation. `f"...: {resp.status_code}"`
            # alone produced "device login failed: 400" — a message that names
            # neither the endpoint nor the reason, which is what made this whole
            # path so slow to diagnose.
            detail = body.get("detail") or body.get("error_description") or ""
            why = " ".join(p for p in (err, detail) if p) or resp.text[:200]
            raise AuthError(
                f"device login failed at {url}/auth/device/token: "
                f"HTTP {resp.status_code} {why}".rstrip()
            )

    creds = Credentials(
        access_token=body["access_token"],
        token_type=body.get("token_type", "bearer"),
        endpoint=url,
        expires_at=_expires_in_to_iso(body.get("expires_in")),
    )

    # Fetch /auth/me so the persisted profile mirrors aithershell layout.
    user: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            me = await client.get(
                f"{url}/auth/me",
                headers={"Authorization": creds.authorization_header()},
            )
            if me.status_code == 200:
                user = me.json()
        except Exception as e:  # noqa: BLE001
            _log.warning("auth.me.failed", extra={"err": str(e)})
    creds.user = user

    profile = {
        "endpoint": url,
        "genesis_url": user.get("genesis_url", ""),
        "token_type": creds.token_type,
        "access_token": creds.access_token,
        "expires_at": creds.expires_at,
        "user": user,
    }
    (store or AuthStore()).set_profile(profile_name, profile)
    return creds


def _expires_in_to_iso(expires_in: Any) -> str:
    try:
        secs = int(expires_in)
    except (TypeError, ValueError):
        return ""
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()


async def autonomous_agent_login(
    *,
    identity_url: str | None = None,
    internal_secret: str | None = None,
    client_name: str = "aither-agent",
    email: str = "",
    user_id: str = "",
    store: AuthStore | None = None,
    profile_name: str = "portal",
) -> Credentials:
    """Headless agent login — no browser, for TRUSTED FLEET-SERVICE contexts.

    This is the sanctioned autonomous path so an agent (a tenant bot, a sovereign
    worker, any adk node) can obtain its own edge Bearer WITHOUT a human: it runs
    the device-code flow and self-approves via AitherIdentity's
    ``/auth/device/authorize-internal``, which requires ``AITHER_INTERNAL_SECRET``
    — i.e. proof the caller is a trusted service on the fleet. The endpoint is
    owner/admin-scoped server-side, so the minted token carries only an
    owner/admin identity the service is entitled to act as.

    Uses the identity's real device paths (``/auth/device/{code,authorize-internal,
    token}``) — NOT the RFC-standard ``/oauth/*`` paths that ``begin_device_login``
    targets (that surface is a separate, human-browser flow). The resulting token
    is persisted via :class:`AuthStore` so ``resolve_credentials`` returns it for
    every subsequent edge call (gateway inference + mcp tools).

    Returns the minted :class:`Credentials`. Raises :class:`AuthError` on failure.
    """
    import httpx

    base = (
        identity_url
        or os.environ.get("AITHERIDENTITY_URL", os.environ.get("AITHERIUM_BASE_URL", DEFAULT_PORTAL_URL))
    ).rstrip("/")
    secret = internal_secret or os.environ.get("AITHER_INTERNAL_SECRET", "") or os.environ.get("AITHER_MASTER_KEY", "")
    if not secret:
        raise AuthError("autonomous_agent_login requires AITHER_INTERNAL_SECRET (trusted-service proof)")

    async with httpx.AsyncClient(timeout=15) as client:
        # 1) start the device flow
        r = await client.post(f"{base}/auth/device/code",
                              json={"client_name": client_name, "scopes": "full"})
        if r.status_code >= 400:
            raise AuthError(f"device/code failed: {r.status_code} {r.text[:160]}")
        dc = r.json()
        # 2) self-approve with the internal secret (no browser)
        ra = await client.post(
            f"{base}/auth/device/authorize-internal",
            headers={"X-Internal-Token": secret},
            json={"user_code": dc["user_code"], "email": email, "user_id": user_id},
        )
        if ra.status_code >= 400:
            raise AuthError(f"authorize-internal failed: {ra.status_code} {ra.text[:160]}")
        # 3) exchange for the token
        rt = await client.post(f"{base}/auth/device/token", json={"device_code": dc["device_code"]})
        body = rt.json() if rt.content else {}
        if rt.status_code >= 400 or not body.get("access_token"):
            raise AuthError(f"device/token failed: {rt.status_code} {body.get('detail') or body.get('status')}")

    creds = Credentials(
        access_token=body["access_token"],
        token_type=body.get("token_type", "bearer"),
        endpoint=base,
        expires_at=body.get("expires_at", ""),
    )
    creds.user = body.get("user", {})
    (store or AuthStore()).set_profile(profile_name, {
        "endpoint": base,
        "token_type": creds.token_type,
        "access_token": creds.access_token,
        "expires_at": creds.expires_at,
        "tier": body.get("tier", ""),
        "plan": body.get("plan", ""),
        "user": creds.user,
    })
    return creds


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def logout(profile_name: str | None = None, store: AuthStore | None = None) -> None:
    s = store or AuthStore()
    if profile_name is None:
        data = s.load()
        if not data:
            return
        profile_name = data.get("active_profile") or "local"
    s.clear_profile(profile_name)
