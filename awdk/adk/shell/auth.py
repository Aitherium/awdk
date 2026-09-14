"""
AitherShell Authentication
===========================

Shared auth module for CLI authentication against AitherIdentity.
Zero AitherOS-internal imports — portable to open-source harness.

Manages ~/.aither/auth.json with multi-profile support.
"""

import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

AUTH_FILE = Path.home() / ".aither" / "auth.json"
AUTH_VERSION = 1


# ---------------------------------------------------------------------------
# AuthStore — read/write ~/.aither/auth.json
# ---------------------------------------------------------------------------

class AuthStore:
    """Read/write ~/.aither/auth.json with multi-profile support."""

    @staticmethod
    def load() -> Optional[Dict[str, Any]]:
        """Load the auth store from disk. Returns None if missing/invalid."""
        if not AUTH_FILE.exists():
            return None
        try:
            data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != AUTH_VERSION:
                return None
            return data
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def save(data: Dict[str, Any]) -> None:
        """Write auth store to disk with restrictive permissions."""
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        data.setdefault("version", AUTH_VERSION)
        AUTH_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        # chmod 0600 — owner read/write only (best effort on Windows)
        try:
            AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def get_active_profile() -> Optional[Dict[str, Any]]:
        """Return the active profile dict, or None."""
        store = AuthStore.load()
        if not store:
            return None
        active = store.get("active_profile", "local")
        return store.get("profiles", {}).get(active)

    @staticmethod
    def get_active_token() -> Optional[str]:
        """Return the active access_token, or None if missing/expired."""
        profile = AuthStore.get_active_profile()
        if not profile or not profile.get("access_token"):
            return None
        # Check expiry
        expires = profile.get("expires_at")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp_dt < datetime.now(timezone.utc):
                    return None  # expired
            except (ValueError, TypeError):
                pass
        return profile["access_token"]

    @staticmethod
    def get_active_user() -> Optional[Dict[str, Any]]:
        """Return the user dict from the active profile."""
        profile = AuthStore.get_active_profile()
        if not profile:
            return None
        return profile.get("user")

    @staticmethod
    def set_profile(name: str, profile: Dict[str, Any]) -> None:
        """Set a named profile in the store."""
        store = AuthStore.load() or {
            "version": AUTH_VERSION,
            "active_profile": name,
            "profiles": {},
        }
        store.setdefault("profiles", {})[name] = profile
        store["active_profile"] = name
        AuthStore.save(store)

    @staticmethod
    def clear_profile(name: str) -> None:
        """Remove a profile from the store."""
        store = AuthStore.load()
        if not store:
            return
        store.get("profiles", {}).pop(name, None)
        if store.get("active_profile") == name:
            remaining = list(store.get("profiles", {}).keys())
            store["active_profile"] = remaining[0] if remaining else ""
        AuthStore.save(store)


# ---------------------------------------------------------------------------
# Built-in root account — like Linux UID 0
# ---------------------------------------------------------------------------

# The root profile is auto-provisioned for local sessions when no auth is
# configured. This means `aither` just works out of the box, like `su -`.
# Cloud deployments override this with a real login.

ROOT_PROFILE: Dict[str, Any] = {
    "endpoint": "local",
    "genesis_url": "https://localhost:8001",
    "token_type": "local",
    "access_token": "aither_root_local",
    "expires_at": "",  # never expires for local root
    "is_local_root": True,  # entitlement layer flags this as unpaid OSS user
    "user": {
        "id": "root",
        "username": "root",
        "display_name": "root",
        "email": "",
        "roles": ["local_root"],  # NOT admin — entitlement decides feature access
        "tenant_id": "",
        "tenant_slug": "",
    },
}


def _is_root_provisioning_allowed() -> bool:
    """Local-root auto-provision is allowed unless explicitly disabled.

    Disable with AITHER_REQUIRE_AUTH=1 to force `aither login` for every user.
    Paying-user deployments (portal-managed) should set this.
    """
    return os.environ.get("AITHER_REQUIRE_AUTH", "").lower() not in ("1", "true", "yes")


def ensure_root_profile() -> Optional[Dict[str, Any]]:
    """Ensure a profile exists for local sessions.

    If a real authenticated profile exists and is unexpired, return it.
    Otherwise auto-provision the built-in local-root profile (unpaid tier),
    unless AITHER_REQUIRE_AUTH=1 in which case returns None and callers must
    direct the user to ``aither login``.

    The local-root profile carries ``is_local_root=True`` so the entitlement
    layer can gate paid features (frameworks, large models, deploy) on real
    authentication while still letting free commands work out of the box.
    """
    store = AuthStore.load()

    # Already have a valid session? Use it.
    if store:
        active = store.get("active_profile", "")
        profile = store.get("profiles", {}).get(active)
        if profile and profile.get("access_token"):
            expires = profile.get("expires_at", "")
            if not expires:
                return profile  # no expiry recorded (e.g. local root) = valid
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp_dt > datetime.now(timezone.utc):
                    return profile  # not expired
            except (ValueError, TypeError):
                return profile  # can't parse = assume valid

    # No valid session — auto-provision local root unless strict mode is on
    if not _is_root_provisioning_allowed():
        return None
    AuthStore.set_profile("local", ROOT_PROFILE)
    return ROOT_PROFILE


# ---------------------------------------------------------------------------
# Auth API calls — uses httpx directly (not AitherSDK)
# ---------------------------------------------------------------------------

async def _get_client():
    """Lazy import httpx to avoid top-level dependency."""
    import httpx
    return httpx.AsyncClient(timeout=15.0)


async def login_password(
    endpoint: str, username: str, password: str
) -> Dict[str, Any]:
    """Authenticate with username/password. Returns profile dict or raises."""
    async with await _get_client() as http:
        resp = await http.post(
            f"{endpoint}/auth/login",
            json={"username": username, "password": password},
        )
        if resp.status_code == 401:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("requires_2fa"):
                return {"requires_2fa": True, "temp_token": data.get("temp_token", "")}
            raise AuthError("Invalid credentials")
        resp.raise_for_status()
        return resp.json()


async def verify_2fa(
    endpoint: str, temp_token: str, code: str
) -> Dict[str, Any]:
    """Verify TOTP 2FA code after initial login."""
    async with await _get_client() as http:
        resp = await http.post(
            f"{endpoint}/auth/2fa/verify",
            json={"temp_token": temp_token, "code": code},
        )
        if resp.status_code == 401:
            raise AuthError("Invalid 2FA code")
        resp.raise_for_status()
        return resp.json()


async def register_user(
    endpoint: str,
    username: str,
    password: str,
    email: str,
    invite_code: str = "",
) -> Dict[str, Any]:
    """Register a new user account."""
    async with await _get_client() as http:
        # Check alpha capacity first
        try:
            cap = await http.get(f"{endpoint}/auth/alpha-capacity")
            if cap.status_code == 200:
                cap_data = cap.json()
                if not cap_data.get("available", True):
                    raise AuthError("Registration is currently closed (alpha capacity reached)")
        except Exception:
            pass  # Identity may not have this endpoint; proceed anyway

        body: Dict[str, Any] = {
            "username": username,
            "password": password,
            "email": email,
        }
        if invite_code:
            body["invite_code"] = invite_code

        resp = await http.post(f"{endpoint}/auth/register", json=body)
        if resp.status_code == 409:
            raise AuthError("Username or email already taken")
        resp.raise_for_status()
        return resp.json()


async def validate_token(
    endpoint: str, token: str
) -> Optional[Dict[str, Any]]:
    """Validate a token by calling GET /auth/me. Returns user dict or None."""
    async with await _get_client() as http:
        resp = await http.get(
            f"{endpoint}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            return resp.json()
        return None


async def logout_session(endpoint: str, token: str) -> None:
    """Invalidate a session server-side."""
    async with await _get_client() as http:
        try:
            await http.post(
                f"{endpoint}/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception:
            pass  # Best effort


def build_profile_from_response(
    endpoint: str,
    genesis_url: str,
    resp_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a profile dict from an auth API response."""
    token = resp_data.get("access_token", resp_data.get("token", ""))
    user = resp_data.get("user", {})
    return {
        "endpoint": endpoint,
        "genesis_url": genesis_url,
        "token_type": resp_data.get("token_type", "session"),
        "access_token": token,
        "expires_at": resp_data.get("expires_at", ""),
        "user": {
            "id": user.get("id", ""),
            "username": user.get("username", ""),
            "display_name": user.get("display_name", user.get("username", "")),
            "email": user.get("email", ""),
            "roles": user.get("roles", []),
            "tenant_id": user.get("tenant_id", ""),
            "tenant_slug": user.get("tenant_slug", ""),
        },
    }


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Authentication error."""
    pass
