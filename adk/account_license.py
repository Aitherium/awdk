"""Account-linked licensing: sign in with Aitherium, and the license follows the account.

A purchase is recorded against the buyer's Aitherium account (AitherACTA's
account-entitlements store). Signing in here -- `adk login`, `adk license sync`,
or the "Sign in with Aitherium" button in a product -- fetches that account's
Ed25519-signed license from AitherIdentity (``GET /auth/license``) and saves it
as the ACCOUNT license (``~/.aither/license.json``). Nothing has to be pasted.

Offline licenses (a key pasted from an email on an air-gapped box) live under
``~/.aither/licenses/*.json`` and are never overwritten by a sync:
``adk.licensing`` unions the packs of every verified license.

Everything here fails soft: a sync that cannot reach the account leaves the
license that is already installed exactly as it was.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from adk import licensing as _lic

logger = logging.getLogger("adk.account_license")

#: A license older than this is refreshed when a gated product starts.
DEFAULT_MAX_AGE_HOURS = 12.0
#: Marker the platform puts in an account license's payload (ACTA account_entitlements).
ACCOUNT_MARKER = "account"


def _sync_state_path() -> Path:
    return _lic.license_file_path().parent / "license.sync.json"


def _read_sync_state() -> Dict[str, Any]:
    try:
        return json.loads(_sync_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_sync_state(**fields: Any) -> None:
    state = _read_sync_state()
    state.update(fields)
    path = _sync_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        logger.debug("license sync state not written: %s", exc)


def _payload(envelope: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(base64.b64decode(envelope["payload"]).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _is_account_envelope(envelope: Dict[str, Any]) -> bool:
    return _payload(envelope).get("issued_via") == ACCOUNT_MARKER


def save_account_license(license_key: str, tier_hint: str = "") -> str:
    """Save the base64 outer key from the account as ``~/.aither/license.json``.

    Before the file is replaced, a VERIFIED license already there that did not come
    from the account (an older offline install) is moved to ``licenses/`` so its
    packs stay active. A previous ACCOUNT license is simply replaced: a refund
    removes a pack from the account, and the stale copy must not keep it alive.

    Returns the tier (payload tier, else ``tier_hint``), or "" when nothing usable
    was given or the write failed. Never raises.
    """
    key = (license_key or "").strip()
    if not key:
        return ""
    try:
        envelope = json.loads(base64.b64decode(key).decode("utf-8"))
    except Exception:  # noqa: BLE001 -- legacy HMAC string / garbage: not an envelope
        return ""
    if not (isinstance(envelope, dict) and "payload" in envelope and "signature" in envelope):
        return ""
    envelope = {"payload": envelope["payload"], "signature": envelope["signature"]}
    path = _lic.license_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            _preserve_offline(path, envelope)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 -- never break login over persistence
        logger.debug("account license persistence skipped: %s", exc)
        return ""
    _write_sync_state(synced_at=time.time())
    _lic.reset_license_manager()
    return str(_payload(envelope).get("tier") or tier_hint or "")


def _preserve_offline(path: Path, new_envelope: Dict[str, Any]) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(current, dict) or current.get("payload") == new_envelope.get("payload"):
        return
    if _is_account_envelope(current):
        return  # superseded account license: replace, never preserve
    try:
        verified = _lic._license_from_envelope(current, source="file")
    except Exception:  # noqa: BLE001
        verified = None
    if verified is None:
        return
    keep = _lic.licenses_dir() / f"{_lic.envelope_fingerprint(current)}.json"
    if not keep.exists():
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(json.dumps(current, indent=2), encoding="utf-8")


# ── sync from the account ───────────────────────────────────────────────────

def _saved_token() -> str:
    try:
        from adk.config import load_saved_config
    except Exception:  # noqa: BLE001
        return ""
    return str((load_saved_config() or {}).get("api_key") or "")


def _identity_url(identity_url: str = "") -> str:
    base = (identity_url or os.getenv("AITHER_PORTAL_URL", "")).rstrip("/")
    try:
        from adk.cli import _DEFAULT_IDENTITY_URL, _resolve_identity_url
        return _resolve_identity_url(base or _DEFAULT_IDENTITY_URL).rstrip("/")
    except Exception:  # noqa: BLE001
        return base or "https://api.aitherium.com"


def _http_get_json(url: str, token: str, timeout: float) -> Dict[str, Any]:
    import urllib.request

    try:
        from adk import __version__ as _v
    except Exception:  # noqa: BLE001
        _v = "0"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": f"awdk/{_v} (license-sync)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- https URL
        return json.loads(resp.read())


def sync_account_license(
    identity_url: str = "",
    token: str = "",
    timeout: float = 10.0,
    fetch: Optional[Callable[[str, str, float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch the account's signed license and save it. Never raises.

    Returns ``{ok, tier, packs, error}``. ``fetch(url, token, timeout)`` is the
    HTTP seam for tests.
    """
    token = token or _saved_token()
    if not token:
        return {"ok": False, "tier": "", "packs": [],
                "error": "not signed in -- run `adk login` (or Sign in with Aitherium)"}
    url = f"{_identity_url(identity_url)}/auth/license"
    _write_sync_state(attempted_at=time.time())
    try:
        data = (fetch or _http_get_json)(url, token, timeout)
    except Exception as exc:  # noqa: BLE001 -- offline / 401 / 5xx: keep what is installed
        code = getattr(exc, "code", None)
        err = ("your sign-in has expired -- sign in again" if code in (401, 403)
               else f"could not reach your account ({type(exc).__name__})")
        return {"ok": False, "tier": "", "packs": [], "error": err}
    tier = save_account_license(str(data.get("license_key") or ""), str(data.get("tier") or ""))
    if not tier:
        return {"ok": False, "tier": "", "packs": [],
                "error": "the account returned no signed license"}
    return {"ok": True, "tier": tier, "packs": list(data.get("packs") or []), "error": ""}


def license_age_seconds() -> Optional[float]:
    """Seconds since the account license was last saved, or None if never."""
    synced = _read_sync_state().get("synced_at")
    return None if not synced else max(0.0, time.time() - float(synced))


def maybe_sync(max_age_hours: Optional[float] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Refresh the account license when it is older than *max_age_hours*.

    Called when a gated product starts. A no-op (returns None) when not signed in,
    when ``AITHER_LICENSE_SYNC=0``, or when the last sync -- or the last ATTEMPT,
    so an offline box does not retry on every start -- is recent enough.
    """
    if os.environ.get("AITHER_LICENSE_SYNC", "").strip().lower() in ("0", "false", "no", "off"):
        return None
    if max_age_hours is None:
        try:
            max_age_hours = float(os.environ.get("AITHER_LICENSE_MAX_AGE_HOURS",
                                                 DEFAULT_MAX_AGE_HOURS))
        except ValueError:
            max_age_hours = DEFAULT_MAX_AGE_HOURS
    if not (kwargs.get("token") or _saved_token()):
        return None
    state = _read_sync_state()
    last = max(float(state.get("synced_at") or 0), float(state.get("attempted_at") or 0))
    if last and time.time() - last < max_age_hours * 3600:
        return None
    return sync_account_license(**kwargs)


def maybe_sync_in_background(**kwargs: Any) -> threading.Thread:
    """:func:`maybe_sync` on a daemon thread, so a product's startup never waits on it."""
    t = threading.Thread(target=lambda: _safe(maybe_sync, **kwargs),
                         name="adk-license-sync", daemon=True)
    t.start()
    return t


def _safe(fn: Callable[..., Any], **kwargs: Any) -> Any:
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("license sync failed: %s", exc)
        return None


# ── "Sign in with Aitherium" for a product's web UI ─────────────────────────

class AccountLink:
    """One device-flow sign-in driven by a product server for its browser UI.

    ``start()`` asks AitherIdentity for a code and returns what the UI shows
    (code + verification URL); a daemon thread polls for approval server-side
    (bounded by the code's lifetime) and, on approval, persists the sign-in the
    same way ``adk login`` does and syncs the account license. The UI polls
    ``status()``. Thread-safe; a second ``start()`` while one is pending returns
    the pending code instead of starting another.
    """

    def __init__(self, identity_url: str = "", client_name: str = "adk-app",
                 on_linked: Optional[Callable[[Dict[str, Any]], None]] = None,
                 max_wait_seconds: float = 900.0) -> None:
        self.identity_url = identity_url
        self.client_name = client_name
        self.on_linked = on_linked
        self.max_wait_seconds = max_wait_seconds
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {"state": "idle"}
        self._thread: Optional[threading.Thread] = None

    # seams (tests replace these) -------------------------------------------
    def _request_code(self, identity_url: str) -> Dict[str, Any]:
        from adk.cli import _post_json_resilient

        try:
            from adk import __version__ as _v
        except Exception:  # noqa: BLE001
            _v = "0"
        return _post_json_resilient(
            f"{identity_url}/auth/device/code",
            {"client_name": self.client_name, "scopes": "full"},
            {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": f"awdk/{_v} ({self.client_name})"},
        )

    def _poll(self, identity_url: str, device_code: str, interval: int, expires_in: int) -> Dict[str, Any]:
        from adk.cli import _poll_device_token

        return _poll_device_token(identity_url, device_code, interval, expires_in,
                                  f"awdk ({self.client_name})")

    def _complete(self, identity_url: str, result: Dict[str, Any]) -> str:
        from adk.cli import complete_device_login

        return complete_device_login(identity_url, result, sync=False)

    # API ----------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._state.get("state") == "pending":
                return dict(self._state)
            self._state = {"state": "starting"}
        identity_url = _identity_url(self.identity_url)
        try:
            data = self._request_code(identity_url)
            user_code = str(data["user_code"])
            device_code = str(data["device_code"])
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state = {"state": "error",
                               "error": f"could not reach Aitherium ({type(exc).__name__})"}
                return dict(self._state)
        expires_in = int(min(float(data.get("expires_in", 900)), self.max_wait_seconds))
        interval = max(2, int(data.get("interval", 5)))
        with self._lock:
            self._state = {
                "state": "pending", "user_code": user_code,
                "verification_uri": data.get("verification_uri", ""),
                "verification_uri_complete": (data.get("verification_uri_complete")
                                              or data.get("verification_uri", "")),
                "expires_at": time.time() + expires_in,
            }
            snapshot = dict(self._state)
        self._thread = threading.Thread(
            target=self._run, args=(identity_url, device_code, interval, expires_in),
            name="adk-account-link", daemon=True)
        self._thread.start()
        return snapshot

    def _run(self, identity_url: str, device_code: str, interval: int, expires_in: int) -> None:
        try:
            result = self._poll(identity_url, device_code, interval, expires_in)
            username = self._complete(identity_url, result)
            tier = save_account_license(str(result.get("license_key") or ""),
                                        str(result.get("tier") or ""))
            sync: Dict[str, Any] = {"ok": bool(tier), "tier": tier}
            if not tier:  # an older Identity: fetch the license with the new session
                sync = sync_account_license(identity_url, str(result.get("access_token") or ""))
            final = {"state": "linked", "username": username,
                     "tier": sync.get("tier") or "", "synced": bool(sync.get("ok")),
                     "error": sync.get("error") or ""}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or type(exc).__name__
            final = {"state": "error", "error": f"sign-in did not complete ({msg})"}
        with self._lock:
            self._state = final
        if final["state"] == "linked" and self.on_linked is not None:
            _safe(lambda: self.on_linked(final))  # type: ignore[misc]

    def wait(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Test helper: join the poll thread."""
        if self._thread is not None:
            self._thread.join(timeout)
        return self.status()


def account_summary() -> Dict[str, Any]:
    """What a product UI shows next to its Sign-in button."""
    from adk.config import load_saved_config

    saved = load_saved_config() or {}
    mgr = _lic.LicenseManager()
    age = license_age_seconds()
    return {"signed_in": bool(saved.get("api_key")), "username": saved.get("username", ""),
            "tier": mgr.license.tier.value, "packs": list(mgr.license.packs),
            "synced_seconds_ago": None if age is None else int(age)}
