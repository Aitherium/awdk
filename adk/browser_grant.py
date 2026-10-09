"""A signed-in owner's web page may drive this daemon's lend routes, and nobody else.

With ``AITHER_LOCAL_AUTH=required`` every daemon route wants ``~/.aither/daemon-token``,
which no web page can read, so the Lend Context panel on aitherium.com answered 401 on
the owner's own PC. The fix is not a pasted token. It is a grant:

1. The page asks this daemon for a one-time nonce (``GET /local/browser-pair/challenge``).
2. The page asks Identity, under the owner's session, to sign
   ``{node, user, origin, nonce, scope, expiry}`` (``POST /v1/nodes/<node>/browser-grant``).
   Identity signs only for the person who may command this device, with the DEVICE's
   command key (the one ``adk.node_commands`` keeps).
3. The page hands the grant back (``POST /local/browser-pair``). The daemon checks the
   signature with its own copy of the key, that the nonce is one it issued and has not
   seen, that it is fresh, and that the request's Origin is the grant's origin. Only
   then does it mint a browser token: scope ``kvholder``, bound to that origin, hours long.

Anyone else -- another site, another account's session, a local process without the
owner's Identity session -- still gets 401 from every gated route.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from adk import node_commands

__all__ = ["GRANT_FIELDS", "new_nonce", "verify_grant", "verify_extension_grant",
           "issue_token", "token_allows", "TOKEN_TTL_S", "EXTENSION_SCOPE"]

GRANT_FIELDS = ("kind", "node_id", "tenant_id", "user_id", "origin", "nonce", "scope",
                "issued_at", "expires_at")
NONCE_TTL_S = 180
TOKEN_TTL_S = 8 * 3600
FIRST_PARTY_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)*aitherium\.(com|org)$")
#: The scopes a grant can carry, and the paths each opens.
#: ``node``: the owner's page lists this machine's models and runs PLAIN completions on
#: them. Plain only -- server.py forces ``plain`` for a browser-token request, so a page
#: token never reaches the agent loop or its tools.
SCOPE_PATHS = {"kvholder": ("/kvholder/",), "node": ("/v1/models", "/v1/chat/completions")}

_lock = threading.Lock()
_nonces: Dict[str, float] = {}
_tokens: Dict[str, Tuple[str, str, float]] = {}  # token -> (origin, scope, expires)


def _prune(now: float) -> None:
    for n in [n for n, t in _nonces.items() if now - t > NONCE_TTL_S]:
        _nonces.pop(n, None)
    for k in [k for k, v in _tokens.items() if v[2] <= now]:
        _tokens.pop(k, None)


def new_nonce(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    nonce = secrets.token_urlsafe(24)
    with _lock:
        _prune(t)
        if len(_nonces) > 256:  # a page hammering the challenge cannot grow this
            _nonces.pop(next(iter(_nonces)), None)
        _nonces[nonce] = t
    return nonce


def verify_grant(grant: Any, *, origin: str, node_id: str, key_hex: str,
                 now: Optional[float] = None) -> str:
    """'' when the grant is good (and its nonce is now spent); else the reason."""
    return _verify(grant, origin=origin, node_id=node_id, key_hex=key_hex, now=now,
                   origin_ok=bool(FIRST_PARTY_ORIGIN.match(origin or "")),
                   scope_ok=lambda s: s in SCOPE_PATHS)


#: The one scope an extension grant carries: the signed-in awconnect extension pairs
#: with this machine's daemons in the same step as sign-in (the daemon then issues its
#: own narrow extension token; this module never mints a browser token for it).
EXTENSION_SCOPE = "extension"


def verify_extension_grant(grant: Any, *, origin: str, node_id: str, key_hex: str,
                           now: Optional[float] = None) -> str:
    """'' when an Identity-signed grant proves the extension at ``origin`` is signed in
    as someone who may command THIS device; else the reason. Only a first-party
    awconnect origin (adk.extension_id) and only the ``extension`` scope qualify."""
    try:
        from adk.extension_id import allowed_extension_origins

        allowed = set(allowed_extension_origins())
    except Exception:  # noqa: BLE001 -- no allowlist, no extension grant
        allowed = set()
    return _verify(grant, origin=origin, node_id=node_id, key_hex=key_hex, now=now,
                   origin_ok=origin in allowed,
                   scope_ok=lambda s: s == EXTENSION_SCOPE)


def _verify(grant: Any, *, origin: str, node_id: str, key_hex: str,
            now: Optional[float], origin_ok: bool, scope_ok) -> str:
    if not isinstance(grant, dict):
        return "no grant"
    if not key_hex:
        return "this device has no command key yet (enroll it)"
    sig = str(grant.get("sig") or "")
    want = node_commands.sign(key_hex, node_commands.canonical(grant, GRANT_FIELDS))
    if not sig or not hmac.compare_digest(want, sig):
        return "bad signature"
    if grant.get("kind") != "browser-grant/v1":
        return "not a browser grant"
    if grant.get("node_id") != node_id:
        return "grant is for another device"
    if not origin_ok or grant.get("origin") != origin:
        return "origin does not match the grant"
    if not scope_ok(grant.get("scope")):
        return "unknown scope"
    t = time.time() if now is None else now
    try:
        expires = float(grant.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0.0  # unreadable expiry = already expired
    if expires <= t:
        return "grant expired"
    with _lock:
        _prune(t)
        if _nonces.pop(str(grant.get("nonce") or ""), None) is None:
            return "nonce unknown or already used"
    return ""


def issue_token(origin: str, scope: str, now: Optional[float] = None) -> Tuple[str, int]:
    t = time.time() if now is None else now
    token = "abt_" + secrets.token_urlsafe(32)
    with _lock:
        _prune(t)
        # Bounded like _nonces: a page that re-pairs in a loop cannot grow this.
        while len(_tokens) >= 64:
            _tokens.pop(next(iter(_tokens)), None)
        _tokens[token] = (origin, scope, t + TOKEN_TTL_S)
    return token, TOKEN_TTL_S


def token_scope(token: str, now: Optional[float] = None) -> Optional[str]:
    """The scope of a live browser token, else None."""
    t = time.time() if now is None else now
    with _lock:
        rec = _tokens.get(token)
    return rec[1] if rec and rec[2] > t else None


def token_allows(token: str, origin: Optional[str], path: str,
                 now: Optional[float] = None) -> bool:
    """Does this bearer open ``path`` for a request from ``origin``?"""
    if not token or not token.startswith("abt_") or not origin:
        return False
    t = time.time() if now is None else now
    with _lock:
        rec = _tokens.get(token)
    if not rec or rec[2] <= t:
        return False
    tok_origin, scope, _ = rec
    if not hmac.compare_digest(tok_origin, origin):
        return False
    return any(path.startswith(p) for p in SCOPE_PATHS.get(scope, ()))
