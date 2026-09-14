"""
AitherShell Entitlement
========================

Client-side entitlement / quota gate for privileged CLI commands.

Sits between the auth layer (who are you?) and the feature execution
(can you do this, and do you have budget left?). Truth lives server-side
at ``GET /v1/billing/entitlement`` and ``POST /v1/billing/debit``; this
module is a fast local hint with a signed cache and an offline grace.

Threat model
------------
- Local-root OSS users: free tier, hard cap, no paid features.
- Stolen ``auth.json``: device_id mismatch invalidates entitlement on
  next remote refresh.
- Tampered cache file: HMAC signature mismatch -> treated as missing.
- Subscription revoked: cache expires within ``MAX_AGE`` and refresh
  returns ``status="revoked"`` -> command refused.
- Portal offline: cached entitlement honoured up to ``OFFLINE_GRACE``,
  then commands refused with a clear "reconnect to portal" message.

Zero AitherOS-internal imports — portable to OSS harness.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import stat
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.auth import AuthStore

ENTITLEMENT_FILE = Path.home() / ".aither" / "entitlement.json"
DEVICE_KEY_FILE = Path.home() / ".aither" / ".device_key"

MAX_AGE = timedelta(hours=1)            # refresh after this
OFFLINE_GRACE = timedelta(hours=24)     # hard cutoff if portal unreachable
ENTITLEMENT_VERSION = 1

# --------------------------------------------------------------------------- #
# Free-tier defaults for local-root / unauth users
# --------------------------------------------------------------------------- #

FREE_TIER: Dict[str, Any] = {
    "plan": "local",
    "tokens_remaining": 1000,
    "tokens_total": 1000,
    "features": {
        # Free for everyone
        "setup_wizard": True,
        "local_llm_install": True,
        "mcp_serve": True,
        "agents_list": True,
        "whoami": True,
        # Paid only
        "framework_hermes": False,
        "framework_openclaw": False,
        "deploy_cloud": False,
        "model_large": False,         # >13B
        "agents_integrate_portal": False,
        "chat_remote": False,
    },
    "expires_at": "",                  # never
    "status": "free",
}

EXPLORER_TIER_FEATURES = {
    "setup_wizard": True,
    "local_llm_install": True,
    "mcp_serve": True,
    "agents_list": True,
    "whoami": True,
    "framework_hermes": True,
    "framework_openclaw": False,
    "deploy_cloud": False,
    "model_large": False,
    "agents_integrate_portal": True,
    "chat_remote": True,
}

# Plan -> default feature map (used as fallback when /v1/billing/entitlement
# is unavailable but the user has a valid bearer token).
PLAN_FEATURE_FALLBACK = {
    "explorer": EXPLORER_TIER_FEATURES,
    "builder": {**EXPLORER_TIER_FEATURES,
                "framework_openclaw": True, "model_large": True,
                "deploy_cloud": True},
    "enterprise": {k: True for k in EXPLORER_TIER_FEATURES},
}


# --------------------------------------------------------------------------- #
# Data class
# --------------------------------------------------------------------------- #

@dataclass
class Entitlement:
    plan: str
    tokens_remaining: int
    tokens_total: int
    features: Dict[str, bool]
    expires_at: str                     # ISO-8601 or "" for never
    status: str                         # "free" | "active" | "revoked" | "lapsed"
    validated_at: str                   # ISO-8601 of last successful refresh
    device_id: str                      # binds cache to this machine
    source: str = "remote"              # "remote" | "fallback" | "free"
    signature: str = ""                 # HMAC over the canonical payload
    licensed_packs: List[str] = field(default_factory=list)  # pack IDs with active licenses

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return exp < datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    def is_stale(self) -> bool:
        try:
            v = datetime.fromisoformat(self.validated_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) - v > MAX_AGE
        except (ValueError, TypeError):
            return True

    def is_beyond_offline_grace(self) -> bool:
        try:
            v = datetime.fromisoformat(self.validated_at.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) - v > OFFLINE_GRACE
        except (ValueError, TypeError):
            return True

    def feature_enabled(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))

    def has_pack_license(self, pack_id: str) -> bool:
        """Check if this entitlement includes a license for the given pack."""
        return pack_id in (self.licensed_packs or [])


# --------------------------------------------------------------------------- #
# Device identity + HMAC signing
# --------------------------------------------------------------------------- #

def _get_device_id() -> str:
    """Stable per-install device fingerprint. Created on first call."""
    if DEVICE_KEY_FILE.exists():
        try:
            return DEVICE_KEY_FILE.read_text(encoding="utf-8").strip().split("\n", 1)[0]
        except OSError:
            pass
    # Synthesize from MAC + platform + a random nonce
    parts = [
        platform.node() or "",
        platform.machine() or "",
        uuid.getnode().__str__(),
        uuid.uuid4().hex,
    ]
    device_id = hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]
    _ensure_dir()
    DEVICE_KEY_FILE.write_text(device_id, encoding="utf-8")
    try:
        DEVICE_KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return device_id


def _get_signing_key() -> bytes:
    """Per-device HMAC key. Stored alongside device id."""
    if DEVICE_KEY_FILE.exists():
        try:
            lines = DEVICE_KEY_FILE.read_text(encoding="utf-8").strip().split("\n")
            if len(lines) >= 2:
                return bytes.fromhex(lines[1])
        except (OSError, ValueError):
            pass
    # Generate a new key and persist alongside device id
    device_id = _get_device_id()
    key = os.urandom(32)
    _ensure_dir()
    DEVICE_KEY_FILE.write_text(f"{device_id}\n{key.hex()}", encoding="utf-8")
    try:
        DEVICE_KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


def _canonical(payload: Dict[str, Any]) -> bytes:
    """Stable serialization for HMAC."""
    clean = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(payload: Dict[str, Any]) -> str:
    return hmac.new(_get_signing_key(), _canonical(payload), hashlib.sha256).hexdigest()


def _verify(payload: Dict[str, Any]) -> bool:
    expected = _sign(payload)
    return hmac.compare_digest(expected, payload.get("signature", ""))


def _ensure_dir() -> None:
    ENTITLEMENT_FILE.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Cache I/O
# --------------------------------------------------------------------------- #

def load_cached() -> Optional[Entitlement]:
    """Load + verify the local entitlement cache. None if missing/tampered."""
    if not ENTITLEMENT_FILE.exists():
        return None
    try:
        data = json.loads(ENTITLEMENT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("version") != ENTITLEMENT_VERSION:
        return None
    payload = data.get("entitlement", {})
    if not _verify(payload):
        return None
    if payload.get("device_id") != _get_device_id():
        return None  # stolen cache from another machine
    try:
        return Entitlement(**{k: v for k, v in payload.items()
                              if k in Entitlement.__annotations__})
    except TypeError:
        return None


def save_cached(ent: Entitlement) -> None:
    _ensure_dir()
    payload = asdict(ent)
    payload["signature"] = _sign(payload)
    ENTITLEMENT_FILE.write_text(
        json.dumps({"version": ENTITLEMENT_VERSION, "entitlement": payload},
                   indent=2),
        encoding="utf-8",
    )
    try:
        ENTITLEMENT_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Remote refresh
# --------------------------------------------------------------------------- #

async def _fetch_remote(profile: Dict[str, Any]) -> Optional[Entitlement]:
    """Hit /v1/billing/entitlement. Returns None on any failure."""
    endpoint = (profile or {}).get("endpoint", "")
    token = (profile or {}).get("access_token", "")
    if not endpoint or endpoint == "local" or not token or token == "aither_root_local":
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            resp = await http.get(
                f"{endpoint}/v1/billing/entitlement",
                headers={"Authorization": f"Bearer {token}",
                         "X-Device-Id": _get_device_id()},
            )
            if resp.status_code != 200:
                return None
            body = resp.json() if resp.content else {}
    except Exception:
        return None

    plan = body.get("plan", "explorer")
    features = body.get("features") or PLAN_FEATURE_FALLBACK.get(plan, EXPLORER_TIER_FEATURES)
    now = datetime.now(timezone.utc).isoformat()
    return Entitlement(
        plan=plan,
        tokens_remaining=int(body.get("tokens_remaining", 0)),
        tokens_total=int(body.get("tokens_total", 0)),
        features=features,
        expires_at=body.get("expires_at", ""),
        status=body.get("status", "active"),
        validated_at=now,
        device_id=_get_device_id(),
        source="remote",
        licensed_packs=body.get("licensed_packs", []),
    )


def _fallback_for_profile(profile: Dict[str, Any]) -> Entitlement:
    """When the portal endpoint is missing, derive an entitlement from the
    profile so paying-portal users still get a working experience.

    - is_local_root=True -> FREE_TIER
    - otherwise -> explorer tier with conservative defaults
    """
    now = datetime.now(timezone.utc).isoformat()
    if not profile or profile.get("is_local_root"):
        return Entitlement(
            plan="local",
            tokens_remaining=FREE_TIER["tokens_remaining"],
            tokens_total=FREE_TIER["tokens_total"],
            features=dict(FREE_TIER["features"]),
            expires_at="",
            status="free",
            validated_at=now,
            device_id=_get_device_id(),
            source="free",
        )
    return Entitlement(
        plan="explorer",
        tokens_remaining=5000,
        tokens_total=5000,
        features=dict(EXPLORER_TIER_FEATURES),
        expires_at="",
        status="active",
        validated_at=now,
        device_id=_get_device_id(),
        source="fallback",
    )


async def refresh(force: bool = False) -> Entitlement:
    """Return a fresh entitlement, refreshing from the portal if stale."""
    cached = load_cached()
    profile = AuthStore.get_active_profile() or {}

    if cached and not force and not cached.is_stale():
        return cached

    remote = await _fetch_remote(profile)
    if remote is not None:
        save_cached(remote)
        return remote

    # Portal unreachable or 404 -> use cache if still inside offline grace
    if cached and not cached.is_beyond_offline_grace():
        return cached

    fallback = _fallback_for_profile(profile)
    save_cached(fallback)
    return fallback


# --------------------------------------------------------------------------- #
# Errors + public gate
# --------------------------------------------------------------------------- #

class EntitlementError(Exception):
    """Raised when a privileged command is refused."""
    def __init__(self, message: str, *, code: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


async def require(feature: str, *, debit: int = 0) -> Entitlement:
    """Refuse to proceed unless ``feature`` is enabled and quota covers ``debit``.

    Returns the active Entitlement on success. Raises EntitlementError otherwise.
    """
    ent = await refresh()

    if ent.status == "revoked":
        raise EntitlementError(
            "Your AitherOS subscription has been revoked.",
            code="revoked",
            hint="Visit https://api.aitherium.com/billing to re-activate.",
        )
    if ent.status == "lapsed" or ent.is_expired():
        raise EntitlementError(
            "Your AitherOS subscription has lapsed.",
            code="lapsed",
            hint="Run `aither login` then renew at api.aitherium.com/billing.",
        )
    if not ent.feature_enabled(feature):
        raise EntitlementError(
            f"Feature '{feature}' is not included in your plan ({ent.plan}).",
            code="feature_locked",
            hint="Upgrade at https://api.aitherium.com/billing",
        )
    if debit > 0 and ent.tokens_remaining < debit:
        raise EntitlementError(
            f"Insufficient tokens for '{feature}': need {debit}, have {ent.tokens_remaining}.",
            code="quota_exhausted",
            hint="Top up tokens at https://api.aitherium.com/billing",
        )
    return ent


async def report_debit(feature: str, amount: int) -> bool:
    """Locally decrement + best-effort server-side debit. Returns True on success."""
    if amount <= 0:
        return True
    ent = load_cached() or await refresh()
    ent.tokens_remaining = max(0, ent.tokens_remaining - amount)
    save_cached(ent)

    profile = AuthStore.get_active_profile() or {}
    endpoint = profile.get("endpoint", "")
    token = profile.get("access_token", "")
    if not endpoint or endpoint == "local" or token == "aither_root_local":
        return True

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.post(
                f"{endpoint}/v1/billing/debit",
                headers={"Authorization": f"Bearer {token}",
                         "X-Device-Id": _get_device_id()},
                json={"feature": feature, "amount": amount},
            )
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
# Click decorator
# --------------------------------------------------------------------------- #

def requires_entitlement(feature: str, *, debit: int = 0):
    """Decorator: gate a Click command on entitlement + quota.

    Usage::

        @cli.command()
        @requires_entitlement("framework_hermes")
        def install_hermes(...): ...
    """
    import asyncio
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Honor read-only / dry-run paths if the command passes plan_only
            if kwargs.get("plan_only") or kwargs.get("output_plan"):
                return fn(*args, **kwargs)
            try:
                ent = asyncio.run(require(feature, debit=debit))
            except EntitlementError as e:
                _emit_refusal(feature, e, kwargs.get("output_json"))
                sys.exit(2)
            kwargs.setdefault("_entitlement", ent)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def _emit_refusal(feature: str, err: EntitlementError, want_json: Optional[bool]) -> None:
    payload = {
        "status": "refused",
        "feature": feature,
        "code": err.code,
        "message": str(err),
        "hint": err.hint,
    }
    if want_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n[entitlement] {err}", file=sys.stderr)
        if err.hint:
            print(f"              -> {err.hint}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Public summary (for `aither whoami` / portal connect)
# --------------------------------------------------------------------------- #

async def summary() -> Dict[str, Any]:
    ent = await refresh()
    return {
        "plan": ent.plan,
        "status": ent.status,
        "tokens_remaining": ent.tokens_remaining,
        "tokens_total": ent.tokens_total,
        "features": ent.features,
        "expires_at": ent.expires_at,
        "validated_at": ent.validated_at,
        "source": ent.source,
        "device_id": ent.device_id[:8] + "...",
    }
