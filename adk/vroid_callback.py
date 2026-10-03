"""The VRoid Hub loopback catcher: bounce ``code``/``state`` back to Persona, nothing else.

The VRoid app's ONE registered redirect URI is ``http://127.0.0.1:47835/callback``
(``lib/integrations/vroid_hub.py`` ``DEFAULT_REDIRECT_URI``); nothing listened there.
Genesis already brokers the link (``routers/vroid_hub.py``): ``POST
/avatars/vroid/authorize`` keeps the state and the PKCE verifier in the caller's
lockbox, and ``POST /avatars/vroid/link {code, state}`` exchanges the code server side.

This module closes the gap between the two, and it is deliberately dumb:

* it binds a LOOPBACK address only (``127.0.0.1`` / ``::1``); any other host is refused
  before a socket is opened;
* it answers exactly ``GET /callback?code=&state=`` (or VRoid's ``?error=&state=``)
  with a 302 to the first-party Persona deep link
  ``<origin>/?app=persona&vroid_code=<code>&vroid_state=<state>``;
* it never sees a token, never calls VRoid, never logs the code;
* the return origin is allowlisted (the apex, ``desktop.aitherium.com``, localhost
  dev), so a callback cannot be bounced to a stranger's site;
* code/state must be short url-safe strings, anything else is a 400;
* it stops after one callback, or after ``timeout_s`` (10 minutes), whichever is first.

The daemon starts it on demand (``POST /avatars/vroid/catcher`` in ``adk/server.py``).
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

logger = logging.getLogger("adk.vroid_callback")

CALLBACK_PORT = 47835
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"
DEFAULT_TIMEOUT_S = 600
DEFAULT_RETURN_ORIGIN = "https://aitherium.com"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
RETURN_ORIGINS = frozenset({
    "https://aitherium.com",
    "https://www.aitherium.com",
    "https://desktop.aitherium.com",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
})

# OAuth codes and our state (secrets.token_urlsafe) are RFC 3986 unreserved characters.
_PARAM = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")
_ERROR = re.compile(r"^[a-z_]{1,64}$")


class CatcherError(RuntimeError):
    """The catcher could not start (port busy, bad bind host, bad origin)."""


def resolve_return_origin(requested: Optional[str] = None) -> str:
    """The origin the callback bounces to: the asking page if allowlisted, else config.

    ``AITHER_VROID_RETURN_ORIGIN`` overrides the default, and is held to the SAME
    allowlist -- a config typo can never send a code off-platform.
    """
    if requested and requested in RETURN_ORIGINS:
        return requested
    configured = (os.getenv("AITHER_VROID_RETURN_ORIGIN") or DEFAULT_RETURN_ORIGIN).strip().rstrip("/")
    if configured not in RETURN_ORIGINS:
        raise CatcherError(f"return origin {configured!r} is not a first-party Persona surface")
    return configured


def persona_url(origin: str, params: Dict[str, str]) -> str:
    """``<origin>/?app=persona&vroid_...`` for an allowlisted origin only."""
    if origin not in RETURN_ORIGINS:
        raise CatcherError("return origin not allowlisted")
    query = {"app": "persona"}
    query.update({f"vroid_{k}": v for k, v in params.items()})
    return f"{origin}/?{urlencode(query)}"


def handle_callback(target: str, origin: str) -> Tuple[int, Dict[str, str], bool]:
    """Answer one request line target. Returns (status, headers, consumed).

    ``consumed`` is True only for a well-formed callback: a probe or a malformed
    request does not use up the one callback the catcher exists for.
    """
    parts = urlsplit(target)
    if parts.path != CALLBACK_PATH:
        return 404, {}, False
    raw = parse_qs(parts.query, keep_blank_values=True)
    if any(len(v) != 1 for v in raw.values()):
        return 400, {}, False
    q = {k: v[0] for k, v in raw.items()}
    state = q.get("state", "")
    if not _PARAM.match(state):
        return 400, {}, False
    if "error" in q:
        # VRoid's own refusal (the person pressed cancel): Persona says so.
        if not _ERROR.match(q["error"]) or "code" in q:
            return 400, {}, False
        bounce = {"error": q["error"], "state": state}
    else:
        code = q.get("code", "")
        if not _PARAM.match(code):
            return 400, {}, False
        bounce = {"code": code, "state": state}
    return 302, {"Location": persona_url(origin, bounce)}, True


class _Handler(BaseHTTPRequestHandler):
    server_version = "adk-vroid-catcher"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 -- http.server's spelling
        catcher: "VRoidCallbackCatcher" = self.server.catcher  # type: ignore[attr-defined]
        status, headers, consumed = handle_callback(self.path, catcher.origin)
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers.items():
            self.send_header(k, v)
        body = b"" if status == 302 else (b"bad request" if status == 400 else b"not found")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
        if consumed:
            catcher.stop_soon()

    def log_message(self, fmt: str, *args: Any) -> None:
        # The request line carries the code. Never log it.
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr: Tuple[str, int], catcher: "VRoidCallbackCatcher") -> None:
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.catcher = catcher
        super().__init__(addr, _Handler)


class VRoidCallbackCatcher:
    """One loopback listener, alive for one callback or ``timeout_s``."""

    def __init__(self, origin: str, host: str = "127.0.0.1", port: int = CALLBACK_PORT,
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        if host not in LOOPBACK_HOSTS:
            raise CatcherError(f"the VRoid catcher binds loopback only, not {host!r}")
        if origin not in RETURN_ORIGINS:
            raise CatcherError("return origin not allowlisted")
        self.origin = origin
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._server: Optional[_Server] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            try:
                server = _Server((self.host, self.port), self)
            except OSError as exc:
                raise CatcherError(f"port {self.port} is busy ({exc.__class__.__name__})") from exc
            self._server = server
            threading.Thread(target=server.serve_forever, name="vroid-catcher", daemon=True).start()
            self._timer = threading.Timer(self.timeout_s, self.stop)
            self._timer.daemon = True
            self._timer.start()
        logger.info("vroid catcher listening on %s:%s for %ss", self.host, self.bound_port, self.timeout_s)

    def stop_soon(self) -> None:
        # shutdown() blocks until serve_forever returns; never call it on the handler thread.
        threading.Thread(target=self.stop, daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if server is not None:
            server.shutdown()
            server.server_close()
            logger.info("vroid catcher stopped")


_current: Optional[VRoidCallbackCatcher] = None
_current_lock = threading.Lock()


def start_catcher(requested_origin: Optional[str] = None, *, port: int = CALLBACK_PORT,
                  timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Start (or re-arm) the one process-wide catcher; return what the page needs."""
    global _current
    origin = resolve_return_origin(requested_origin)
    with _current_lock:
        if _current is not None and _current.running:
            # A second click: same listener, now bouncing to the newest asking page.
            _current.origin = origin
        else:
            catcher = VRoidCallbackCatcher(origin, port=port, timeout_s=timeout_s)
            catcher.start()
            _current = catcher
        live = _current
    return {
        "listening": True,
        "redirect_uri": f"http://127.0.0.1:{live.bound_port}{CALLBACK_PATH}",
        "return_origin": live.origin,
        "expires_in": int(live.timeout_s),
    }


def stop_catcher() -> None:
    global _current
    with _current_lock:
        live, _current = _current, None
    if live is not None:
        live.stop()
