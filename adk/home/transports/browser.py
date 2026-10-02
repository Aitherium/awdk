"""Browser pairing for the ``local`` Hearth channel: a web page talks to YOUR serve.

A page on an allowlisted origin (by default ``https://hearth.aitherium.com``,
``https://aitherium.com`` and ``https://academy.aitherium.com``) can chat with the
``adk home serve`` running on this machine, so the brain answering is your own
model (Bonsai on your CPU or GPU),
not ours. The page never sees ``<home>/local.token``. Instead:

1. ``adk home connect-browser`` (or ``adk home serve --browser``) asks the running
   serve for a short one-time code (``ABCD-EFGH``) and prints it on THIS console.
2. You type the code into the page; it sends ``POST /browser/pair {"code"}`` from
   its origin. The serve exchanges it for a BROWSER bearer: a fresh random token,
   kept only in the serve's memory (a restart revokes every one), bound to the
   origin that paired, valid for :data:`SESSION_TTL_S`.
3. The page calls ``/browser/say``, ``/browser/state`` and ``/browser/approve``
   with that bearer. Approval cards still need a human click; owner rules,
   receipts and the 127.0.0.1-only bind are unchanged.

Codes are single use, expire after :data:`CODE_TTL_S`, and every open code is
burned after :data:`MAX_PAIR_FAILS` wrong guesses. The file token is never
accepted on ``/browser/*`` and the browser bearer is never accepted elsewhere.

``$HEARTH_BROWSER_ORIGINS`` (comma-separated ``scheme://host[:port]``) replaces the
allowlist; ``off`` turns browser access off. ``http://`` is accepted only for
``localhost`` / ``127.0.0.1`` (development pages).

:class:`BrowserGuard` wraps the whole local app: a request whose ``Host`` is not
loopback is refused (DNS rebinding), a request carrying an ``Origin`` that is not
allowlisted is refused on every path, an allowlisted origin may reach
``/browser/*`` only, and CORS answers (with ``Access-Control-Allow-Private-Network``
for Chrome's private-network preflight) are sent for allowlisted origins only.
No cookies are involved, so ``Access-Control-Allow-Credentials`` is never sent.

One path needs no bearer: ``GET /browser/status`` (:data:`STATUS_PATH`). It answers an
allowlisted page that has not paired yet with four facts about the RUNNING serve --
the awdk version, whether the classroom tools are on, whether the model runs on this
computer, whether the home holds a sign-in -- and nothing that names a person, an
agent, a class or a file. It is how a setup page says "running" without a terminal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..config import HomeError

__all__ = [
    "DEFAULT_ORIGINS", "ORIGINS_ENV", "CODE_TTL_S", "SESSION_TTL_S", "MAX_PAIR_FAILS",
    "BROWSER_PREFIX", "STATUS_PATH", "BrowserPairing", "BrowserGuard", "browser_origins",
    "normalize_code",
]

#: The pages allowed to pair with a home serve unless ``$HEARTH_BROWSER_ORIGINS`` says otherwise.
DEFAULT_ORIGINS: Tuple[str, ...] = ("https://hearth.aitherium.com", "https://aitherium.com",
                                    "https://academy.aitherium.com")
ORIGINS_ENV = "HEARTH_BROWSER_ORIGINS"
#: Seconds a pairing code stays valid.
CODE_TTL_S = 300.0
#: Seconds a browser bearer stays valid (a serve restart revokes it sooner).
SESSION_TTL_S = 12 * 3600.0
#: Wrong codes (from anyone) before every open code is burned.
MAX_PAIR_FAILS = 5
#: Codes open at once; minting another drops the oldest.
MAX_OPEN_CODES = 3
#: Browser sessions held at once; pairing another drops the oldest.
MAX_SESSIONS = 8
BROWSER_PREFIX = "/browser/"
#: The one /browser/* path that needs no bearer (see the module doc).
STATUS_PATH = "/browser/status"
#: No 0/O/1/I/L: the code is read off a terminal and typed by hand.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
_ORIGIN_RE = re.compile(r"(https?)://([a-z0-9.-]+)(:[0-9]{1,5})?")
_LOOPBACK_NAMES = {"127.0.0.1", "localhost"}


def browser_origins(value: Optional[str] = None) -> Tuple[str, ...]:
    """The allowlist: ``value`` / ``$HEARTH_BROWSER_ORIGINS``, else :data:`DEFAULT_ORIGINS`.

    ``off`` / ``none`` -> () (browser access off). A malformed entry is a
    :class:`HomeError`: a typo must not silently widen or empty the list.
    """
    raw = os.environ.get(ORIGINS_ENV, "") if value is None else str(value)
    raw = raw.strip()
    if not raw:
        return DEFAULT_ORIGINS
    if raw.lower() in ("off", "none", "0", "false"):
        return ()
    out: List[str] = []
    for part in raw.split(","):
        origin = part.strip().rstrip("/").lower()
        if not origin:
            continue
        m = _ORIGIN_RE.fullmatch(origin)
        if not m:
            raise HomeError(f"{ORIGINS_ENV}: {part.strip()!r} is not scheme://host[:port]")
        if m.group(1) == "http" and m.group(2) not in _LOOPBACK_NAMES:
            raise HomeError(f"{ORIGINS_ENV}: {origin} is plain http; only https origins "
                            "(or http://localhost for development) may pair")
        if origin not in out:
            out.append(origin)
    return tuple(out)


def normalize_code(code: Any) -> str:
    """``abcd efgh`` / ``ABCD-EFGH`` -> ``ABCDEFGH``; "" when it cannot be a code."""
    raw = re.sub(r"[\s-]", "", str(code or "")).upper()
    if len(raw) != CODE_LEN or any(c not in CODE_ALPHABET for c in raw):
        return ""
    return raw


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class BrowserPairing:
    """One-time codes and the browser bearers they were exchanged for (memory only)."""

    def __init__(self, origins: Optional[Tuple[str, ...]] = None, *,
                 clock: Any = time.time):
        self.origins: Tuple[str, ...] = browser_origins() if origins is None else tuple(
            o.rstrip("/").lower() for o in origins)
        self._clock = clock
        #: normalized code -> expiry
        self._codes: "OrderedDict[str, float]" = OrderedDict()
        self._fails = 0
        #: sha256(token) -> {"origin", "expires"}
        self._sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def __repr__(self) -> str:  # never show a code or a token
        return f"BrowserPairing(origins={list(self.origins)}, sessions={len(self._sessions)})"

    @property
    def enabled(self) -> bool:
        return bool(self.origins)

    def origin_allowed(self, origin: str) -> bool:
        return bool(origin) and origin.rstrip("/").lower() in self.origins

    # ── codes ───────────────────────────────────────────────────────────────
    def new_code(self) -> Tuple[str, float]:
        """A fresh code (``ABCD-EFGH``) and its TTL in seconds."""
        if not self.enabled:
            raise HomeError(f"browser pairing is off ({ORIGINS_ENV}=off)")
        self._prune()
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        self._codes[raw] = self._clock() + CODE_TTL_S
        while len(self._codes) > MAX_OPEN_CODES:
            self._codes.popitem(last=False)
        self._fails = 0
        return f"{raw[:4]}-{raw[4:]}", CODE_TTL_S

    def open_codes(self) -> int:
        self._prune()
        return len(self._codes)

    def pair(self, code: Any, origin: str) -> Tuple[Optional[str], str]:
        """(bearer, "") on success; (None, why) otherwise. The code dies on use."""
        if not self.origin_allowed(origin):
            return None, "origin not allowed"
        self._prune()
        given = normalize_code(code)
        match = ""
        for open_code in list(self._codes):
            # Constant time per candidate; every candidate is compared.
            if hmac.compare_digest(given.encode("ascii") or b"-", open_code.encode("ascii")):
                match = open_code
        if not match:
            self._fails += 1
            if self._fails >= MAX_PAIR_FAILS:
                self._codes.clear()
                self._fails = 0
                return None, ("too many wrong codes: every open code was cancelled; run "
                              "`adk home connect-browser` for a new one")
            return None, "wrong or expired code"
        del self._codes[match]
        self._fails = 0
        token = secrets.token_urlsafe(32)
        self._sessions[_digest(token)] = {"origin": origin.rstrip("/").lower(),
                                          "expires": self._clock() + SESSION_TTL_S}
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return token, ""

    # ── bearers ─────────────────────────────────────────────────────────────
    def session_for(self, token: str, origin: str) -> Optional[Dict[str, Any]]:
        """The live session ``token`` names, used from the origin it paired on."""
        if not token:
            return None
        self._prune()
        sess = self._sessions.get(_digest(token))
        if sess is None:
            return None
        if not hmac.compare_digest(str(sess["origin"]).encode("utf-8"),
                                   origin.rstrip("/").lower().encode("utf-8")):
            return None
        return sess

    def forget(self, token: str) -> bool:
        return self._sessions.pop(_digest(token), None) is not None

    def revoke_all(self) -> int:
        n = len(self._sessions)
        self._sessions.clear()
        self._codes.clear()
        return n

    def sessions(self) -> int:
        self._prune()
        return len(self._sessions)

    def _prune(self) -> None:
        now = self._clock()
        for code, exp in list(self._codes.items()):
            if exp <= now:
                del self._codes[code]
        for key, sess in list(self._sessions.items()):
            if sess["expires"] <= now:
                del self._sessions[key]


def _host_name(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


class BrowserGuard:
    """ASGI middleware: loopback ``Host`` only, the origin allowlist, and CORS."""

    def __init__(self, app: Any, pairing: BrowserPairing):
        self.app = app
        self.pairing = pairing

    async def _refuse(self, send: Any, status: int, error: str) -> None:
        body = json.dumps({"error": error}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii")),
                                (b"vary", b"Origin")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        path = str(scope.get("path") or "")
        if _host_name(headers.get("host", "")) not in _LOOPBACK_NAMES:
            # A page that rebinds its own name to 127.0.0.1 still sends that name.
            await self._refuse(send, 403, "host not allowed")
            return
        origin = headers.get("origin", "")
        if not origin:
            await self.app(scope, receive, send)       # a local client, not a browser
            return
        if not self.pairing.origin_allowed(origin):
            await self._refuse(send, 403, "origin not allowed")
            return
        if not path.startswith(BROWSER_PREFIX):
            await self._refuse(send, 403, "a browser may use /browser/* only")
            return
        cors = [(b"access-control-allow-origin", origin.encode("latin-1")),
                (b"vary", b"Origin")]
        if scope.get("method") == "OPTIONS":
            out = cors + [
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                (b"access-control-allow-headers", b"authorization, content-type"),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"0"),
            ]
            if headers.get("access-control-request-private-network", "").lower() == "true":
                out.append((b"access-control-allow-private-network", b"true"))
            await send({"type": "http.response.start", "status": 204, "headers": out})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = [h for h in (message.get("headers") or [])
                                      if h[0].lower() != b"vary"] + cors
            await send(message)

        await self.app(scope, receive, send_with_cors)
