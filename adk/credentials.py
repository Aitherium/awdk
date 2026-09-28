"""One login per machine: the single writer and reader of the user's bearer.

Two files hold the person's sign-in on a machine:

* ``~/.aither/auth.json`` -- the multi-profile store every CLI reads. Each
  sign-in lands in a profile keyed by the issuer's HOST (``idp.aitherium.com``),
  so signing in to a second issuer adds a profile instead of overwriting one.
  The profile name ``local`` is reserved for the built-in local root account.
* ``~/.aither/session-bearer`` -- the bare bearer the MCP stdio bridge and the
  desktop read. Same token, mode 600.

Before this module each tool wrote whichever of the two it knew about, under a
profile name of its own choosing, and the tools that READ a bearer picked up
whatever was nearest -- including a gateway API key exported in the shell. A
machine could be signed in three different ways at once.

:func:`save_login` writes both files. :func:`user_bearer` returns the person's
bearer and nothing else: a session token, a personal access token
(``aither_pat_``) or an OIDC token. It never returns an API key
(``aither_sk_live_``), an extension key (``aither_ext_``), the local-root
placeholder, or the value of an API-key environment variable. Past a token's
half-life it asks the issuer to extend it (``POST <issuer>/auth/refresh``) and
rewrites both files.

Standard library only: this module is imported by tools that run before any
dependency is installed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

AUTH_VERSION = 1
AUTH_FILE: Path = Path.home() / ".aither" / "auth.json"
BEARER_FILE: Path = Path.home() / ".aither" / "session-bearer"

#: Reserved profile name: the built-in local root account.
LOCAL_PROFILE = "local"

#: Token kinds that identify a PERSON and may be handed out as the bearer.
USER_KINDS = frozenset({"session", "pat", "oidc", "bearer"})

#: Prefixes of credentials that are NOT a person's login and must never be
#: returned as the user bearer.
NON_USER_PREFIXES = ("aither_sk_", "aither_ext_", "aither_root_local", "sk-")

#: Environment variables that carry API keys. Their values are never a user
#: bearer, even when the same string was stored in a profile.
API_KEY_ENVS = ("AITHERIUM_API_KEY", "AITHER_API_KEY", "AITHER_GATEWAY_KEY")

#: POST <issuer> + this path extends a session.
REFRESH_PATH = "/auth/refresh"

# (url, token, timeout) -> (status, body dict). Injected by tests.
Refresher = Callable[[str, str, float], Tuple[int, Dict[str, Any]]]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    """ISO-8601 string or epoch seconds -> aware UTC datetime; None if absent."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError, OSError, OverflowError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def issuer_key(issuer: str) -> str:
    """The profile name for an issuer: its lower-cased host.

    Raises ValueError for an issuer with no host, or one whose host is the
    reserved ``local`` name.
    """
    raw = (issuer or "").strip()
    host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower()
    if not host:
        raise ValueError(f"issuer has no host: {issuer!r}")
    if host == LOCAL_PROFILE:
        raise ValueError("the profile name 'local' is reserved for the local root account")
    return host


def is_user_token(token: str, kind: str = "") -> bool:
    """True when ``token`` may be handed out as the person's bearer."""
    tok = (token or "").strip()
    if not tok:
        return False
    if kind and kind.lower() not in USER_KINDS:
        return False
    if tok.startswith(NON_USER_PREFIXES):
        return False
    for env in API_KEY_ENVS:
        val = os.environ.get(env, "").strip()
        if val and val == tok:
            return False
    return True


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        if os.name == "posix":  # Windows ACLs ignore POSIX modes; POSIX must not
            raise


def _load_store(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != AUTH_VERSION:
        return {}
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    return data


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def save_login(
    issuer: str,
    token: str,
    expires_at: Any = "",
    kind: str = "session",
    user: Optional[Dict[str, Any]] = None,
    *,
    extra: Optional[Dict[str, Any]] = None,
    auth_path: Optional[Path] = None,
    bearer_path: Optional[Path] = None,
) -> str:
    """Persist one sign-in. Returns the profile name it was stored under.

    Writes the ``auth.json`` profile keyed by the issuer host and makes it the
    active profile. When the token identifies a person (see
    :func:`is_user_token`) the same token is also written to the session-bearer
    file with mode 600; an API key is stored in its profile but never becomes
    the machine's session bearer.
    """
    tok = (token or "").strip()
    if not tok:
        raise ValueError("save_login needs a token")
    name = issuer_key(issuer)
    kind = (kind or "session").lower()
    apath = auth_path or AUTH_FILE
    bpath = bearer_path or BEARER_FILE

    exp = _parse_time(expires_at)
    profile: Dict[str, Any] = dict(extra or {})
    profile.update({
        "issuer": issuer.rstrip("/"),
        "endpoint": profile.get("endpoint") or issuer.rstrip("/"),
        "token_type": profile.get("token_type") or kind,
        "kind": kind,
        "access_token": tok,
        "issued_at": _iso(_now()),
        "expires_at": _iso(exp) if exp else "",
        "user": dict(user or {}),
    })

    store = _load_store(apath) or {"version": AUTH_VERSION, "profiles": {}}
    store["version"] = AUTH_VERSION
    store["profiles"][name] = profile
    store["active_profile"] = name
    _write_private(apath, json.dumps(store, indent=2, default=str))

    if is_user_token(tok, kind):
        _write_private(bpath, tok)
    return name


def write_session_bearer(token: str, *, bearer_path: Optional[Path] = None) -> None:
    """Write an already-stored user bearer to the session-bearer file."""
    if not is_user_token(token):
        raise ValueError("refusing to write a non-user credential as the session bearer")
    _write_private(bearer_path or BEARER_FILE, token.strip())


def clear_active_profile(*, auth_path: Optional[Path] = None) -> bool:
    """Sign out: no profile is active. Returns True when auth.json changed."""
    apath = auth_path or AUTH_FILE
    store = _load_store(apath)
    if not store or not store.get("active_profile"):
        return False
    store["active_profile"] = ""
    _write_private(apath, json.dumps(store, indent=2, default=str))
    return True


# ---------------------------------------------------------------------------
# read (+ refresh)
# ---------------------------------------------------------------------------


def _default_refresher(url: str, token: str, timeout: float) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- https issuer
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _usable(profile: Dict[str, Any], now: datetime) -> bool:
    if not isinstance(profile, dict):
        return False
    if not is_user_token(str(profile.get("access_token") or ""), str(profile.get("kind") or "")):
        return False
    exp = _parse_time(profile.get("expires_at"))
    return exp is None or exp > now


def _past_half_life(profile: Dict[str, Any], now: datetime) -> bool:
    exp = _parse_time(profile.get("expires_at"))
    issued = _parse_time(profile.get("issued_at"))
    if exp is None or issued is None or exp <= issued:
        return False
    return now >= issued + (exp - issued) / 2


def _refresh(
    name: str,
    profile: Dict[str, Any],
    refresher: Refresher,
    auth_path: Path,
    bearer_path: Path,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """Extend ``profile`` at its issuer. Returns the new profile, or None."""
    issuer = str(profile.get("issuer") or profile.get("endpoint") or "").rstrip("/")
    if not issuer.startswith(("https://", "http://")):
        return None
    token = str(profile.get("access_token") or "")
    try:
        status, body = refresher(issuer + REFRESH_PATH, token, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    new_token = str(body.get("access_token") or token)
    new_exp = body.get("expires_at")
    if not new_exp and body.get("expires_in"):
        try:
            from datetime import timedelta

            new_exp = _iso(_now() + timedelta(seconds=int(body["expires_in"])))
        except (TypeError, ValueError):
            new_exp = ""
    keep = {k: v for k, v in profile.items()
            if k not in ("access_token", "expires_at", "issued_at")}
    save_login(
        issuer,
        new_token,
        new_exp or profile.get("expires_at", ""),
        str(profile.get("kind") or "session"),
        profile.get("user") if isinstance(profile.get("user"), dict) else {},
        extra=keep,
        auth_path=auth_path,
        bearer_path=bearer_path,
    )
    return _load_store(auth_path).get("profiles", {}).get(issuer_key(issuer))


def user_bearer(
    *,
    auth_path: Optional[Path] = None,
    bearer_path: Optional[Path] = None,
    refresher: Optional[Refresher] = None,
    refresh: bool = True,
    timeout: float = 10.0,
) -> str:
    """The person's bearer on this machine, or "" when signed out.

    Order: the active auth.json profile, then any other issuer profile (the
    most recently issued first), then the session-bearer file. Only user tokens
    that have not expired qualify. A token past half-life is extended at its
    issuer first; if that fails the still-valid token is returned.
    """
    apath = auth_path or AUTH_FILE
    bpath = bearer_path or BEARER_FILE
    now = _now()
    store = _load_store(apath)
    profiles: Dict[str, Any] = store.get("profiles", {}) if store else {}
    active = str(store.get("active_profile") or "") if store else ""

    ordered = []
    if active in profiles:
        ordered.append(active)
    others = [n for n in profiles if n != active and n != LOCAL_PROFILE]
    others.sort(key=lambda n: _iso(_parse_time((profiles[n] or {}).get("issued_at"))), reverse=True)
    ordered.extend(others)

    for name in ordered:
        profile = profiles.get(name) or {}
        if not _usable(profile, now):
            continue
        if refresh and _past_half_life(profile, now):
            renewed = _refresh(name, profile, refresher or _default_refresher,
                               apath, bpath, timeout)
            if renewed and _usable(renewed, _now()):
                return str(renewed["access_token"]).strip()
        return str(profile["access_token"]).strip()

    try:
        tok = bpath.read_text(encoding="utf-8").strip()
    except OSError:
        tok = ""
    # A bearer file carries no expiry. When auth.json holds the same token, the
    # profile already judged it (expired / not a user token), so do not revive it.
    if any(isinstance(p, dict) and str(p.get("access_token") or "").strip() == tok
           for p in profiles.values()):
        return ""
    return tok if is_user_token(tok) else ""


def main(argv: Optional[list] = None) -> int:
    """``python -m adk.credentials``: say whether a user bearer exists (never print it)."""
    tok = user_bearer(refresh=False)
    if tok:
        print(f"signed in: user bearer present ({len(tok)} chars)")
        return 0
    print("not signed in: run `adk login`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
