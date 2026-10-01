"""The ``local`` channel: a window onto ``adk home serve`` from this machine.

Every local surface (``adk home say``, ``adk home events``, the awsh ``/hearth``
command, a desk widget) talks to the ONE running Hearth process through this
transport instead of starting a second agent. It is an HTTP server on
``127.0.0.1`` only -- any other bind address is refused at construction -- on
``$HEARTH_LOCAL_PORT`` (default :data:`DEFAULT_PORT`).

Auth. Every start mints a NEW random bearer and, once the port is bound, writes
it to ``<home>/local.token`` together with that port and the server's pid
(``{"token", "port", "pid"}``); stop removes the file. A token harvested from a
previous run is therefore dead, and a client dials the port the serve holding
this token recorded, not whatever ``$HEARTH_LOCAL_PORT`` says in its own shell.
The file is owner-only: mode 0600 on POSIX; on Windows ``icacls`` strips
inheritance and grants only the current user, applied to the EMPTY file before
the token is written. If that restriction cannot be applied the local channel
refuses to start (fail closed): the home may live outside the profile
(``AITHER_AGENT_HOME`` on another drive) where the inherited ACL admits every
local user.

Every credentialed request carries ``Authorization: Bearer <token>``, compared in
constant time; a missing or wrong token is a 401 and nothing reaches the core.
Before sending it, a client proves the listener is the real serve: it sends a
random nonce to ``GET /hello`` and checks ``HMAC(token, nonce|port)`` for the port
IT dialed (:func:`hello_proof`). A squatter on the port cannot answer, and cannot
relay the question to the real serve on another port (that proof names the other
port), so the bearer is never sent to it.

Owner. The owner on ``local`` is the OS user running the server
(``getpass.getuser()``): holding the token proves read access to the owner's
home, which also holds ``owner.json``, so a valid token binds that user through
:meth:`HearthCore.bind_owner` -- the registry, plus a ``pair`` receipt -- on the
first request. No pairing code is involved.

Endpoints (all but /hello need the bearer):

    GET  /hello?nonce=<hex>  -> {"proof", "port"}: HMAC-SHA256(token,
                   "hearth-local-hello|" nonce "|" port). No credential; it proves
                   the server holds this home's token before a client sends it.

    POST /message  {"text": "..."} -> {"handled": bool, "replies": [{"text", "card"}]}
                   the core's replies for THIS turn, approval cards included
                   (``card`` is the nonce to answer with ``yes <nonce>``). The
                   body is capped at :data:`MAX_BODY` bytes (413 above it).
    GET  /events   server-sent events: every outbound message on this channel,
                   ``kind`` "reply" (answering a /message) or "push" (a
                   follow-up); pushes nobody was listening for are replayed to the
                   next subscriber with ``missed: true``.
    GET  /receipts?n=N  the last N receipts plus the chain/signature verdict.
    POST /browser-code  -> {"code", "expires_in", "origins"}: a one-time code a web
                   page exchanges for a browser bearer (``adk home connect-browser``).

Browser endpoints (:mod:`adk.home.transports.browser`; an allowlisted ``Origin``
and, after pairing, the BROWSER bearer -- never the file token):

    POST /browser/pair     {"code"} -> {"token", "expires_in", "agent"}
    POST /browser/say      {"text"} -> as /message
    GET  /browser/state    pending approval card, reminders, receipts + verdict
    POST /browser/approve  {"nonce", "allow"} -> the human's click, as `yes <nonce>`
    POST /browser/forget   drop this browser's bearer

A push with no subscriber returns False from :meth:`send`, so the core falls back
to another bound channel instead of claiming delivery.
"""

from __future__ import annotations

import asyncio
import contextvars
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from ..._private_file import PrivateFileError, restrict_owner_only
from ..config import HomeError, home_dir
from ._webhook import UvicornRunner, read_capped
from .browser import SESSION_TTL_S, BrowserGuard, BrowserPairing, browser_origins

logger = logging.getLogger("adk.home.transports.local")

__all__ = [
    "CHANNEL", "DEFAULT_PORT", "PORT_ENV", "TOKEN_NAME", "MAX_BODY", "LOOPBACK",
    "LocalTransport", "token_path", "new_token", "write_endpoint", "read_endpoint",
    "read_token", "remove_endpoint", "hello_proof", "local_port",
]

CHANNEL = "local"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8363
PORT_ENV = "HEARTH_LOCAL_PORT"
TOKEN_NAME = "local.token"
#: Largest /message body accepted (bytes).
MAX_BODY = 16 * 1024
#: Most receipts one /receipts call returns.
MAX_RECEIPTS = 200
#: Pushes kept for the next /events subscriber when nobody is listening.
BACKLOG = 50
#: Messages queued per /events subscriber before the oldest is dropped.
SUBSCRIBER_QUEUE = 100
#: Seconds between SSE keep-alive comments.
KEEPALIVE_S = 15.0
#: An approval card as :meth:`HearthCore._card` writes it.
CARD_RE = re.compile(r"Reply `yes ([0-9a-f]{8})` to allow")
#: The nonce a browser click answers a card with.
CARD_NONCE_RE = re.compile(r"[0-9a-f]{8}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
#: A /hello challenge nonce (lowercase hex, 128-512 bits).
NONCE_RE = re.compile(r"[0-9a-f]{32,128}")

#: The reply list of the /message request whose turn is running (per task).
_collector: "contextvars.ContextVar[Optional[List[Dict[str, Any]]]]" = \
    contextvars.ContextVar("hearth_local_collector", default=None)


# ── token + port ──────────────────────────────────────────────────────────────

def token_path(root: Optional[Path] = None) -> Path:
    return Path(root or home_dir()) / TOKEN_NAME


def _restrict(path: Path) -> None:
    """Owner-only access, or :class:`HomeError` -- never a silently open file.

    POSIX: chmod 600. Windows: ``icacls /inheritance:r /grant:r <user>:F`` so the
    file does not keep whatever its folder hands down (a home outside the profile
    inherits read access for every local user from the drive root). The one
    implementation is :func:`adk._private_file.restrict_owner_only`, shared with
    ``owner.json``, the approval store and the receipts key and log.
    """
    try:
        restrict_owner_only(path)
    except PrivateFileError as exc:
        raise HomeError(f"local: {exc}; refusing to write the local token where other "
                        "users could read it") from exc


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_endpoint(token: str, port: int, root: Optional[Path] = None) -> Path:
    """Publish ``{"token", "port", "pid"}`` owner-only, atomically.

    The temp file is created EMPTY and restricted before the token is written,
    so the token never sits in a file carrying the folder's inherited ACL. A
    failure to restrict raises :class:`HomeError` and leaves no file behind.
    """
    if not _TOKEN_RE.fullmatch(token or ""):
        raise HomeError("local: refusing to publish a malformed token")
    path = token_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    os.close(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        _restrict(tmp)
        payload = json.dumps({"token": token, "port": int(port), "pid": os.getpid()})
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read_endpoint(root: Optional[Path] = None) -> Tuple[str, int]:
    """(token, port) the running serve published; :class:`HomeError` when none."""
    path = token_path(root)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise HomeError(f"no {path}: nothing is serving; start `adk home serve` "
                        "(the local channel writes it once it is listening)") from None
    except OSError as exc:
        raise HomeError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    token = data.get("token") if isinstance(data, dict) else None
    port = data.get("port") if isinstance(data, dict) else None
    if (not isinstance(token, str) or not _TOKEN_RE.fullmatch(token)
            or isinstance(port, bool) or not isinstance(port, int)
            or not 0 < port <= 65535):
        raise HomeError(f"{path} is malformed; restart `adk home serve` to rewrite it")
    return token, port


def read_token(root: Optional[Path] = None) -> str:
    """The bearer a local client sends; :class:`HomeError` when serve is not up."""
    return read_endpoint(root)[0]


def remove_endpoint(token: str, root: Optional[Path] = None) -> None:
    """Delete the token file if it still records ``token`` (a newer serve's stays)."""
    path = token_path(root)
    try:
        current = json.loads(path.read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError, AttributeError) as exc:
        logger.debug("hearth: local token file not removed: %s", type(exc).__name__)
        return
    if isinstance(current, str) and hmac.compare_digest(current.encode("utf-8"),
                                                        token.encode("utf-8")):
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("hearth: could not remove %s: %s", path, exc)


def hello_proof(token: str, nonce: str, port: int) -> str:
    """What the real serve answers to ``GET /hello``: HMAC over nonce AND port."""
    msg = f"hearth-local-hello|{nonce}|{int(port)}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def local_port(value: Optional[str] = None) -> int:
    """``$HEARTH_LOCAL_PORT`` (or ``value``), else :data:`DEFAULT_PORT`."""
    raw = (os.environ.get(PORT_ENV, "") if value is None else str(value)).strip()
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 <= int(raw) <= 65535:
        raise HomeError(f"{PORT_ENV}={raw!r} is not a port number")
    return int(raw)


# ── the transport ────────────────────────────────────────────────────────────

class LocalTransport:
    """A :class:`adk.home.hearth.Transport` for this machine's own surfaces."""

    name = CHANNEL
    #: No credential in the environment: the token file IS the credential.
    REQUIRED_ENV: tuple = ()

    def __init__(self, *, host: str = LOOPBACK, port: Optional[int] = None,
                 root: Optional[Path] = None, user_id: str = "",
                 token: str = "", origins: Optional[Tuple[str, ...]] = None):
        if host != LOOPBACK:
            raise HomeError(f"local: refusing to listen on {host!r}; the local channel "
                            f"binds {LOOPBACK} only")
        self.host = host
        self.port = local_port() if port is None else int(port)
        self.root = root
        self.user_id = user_id or getpass.getuser()
        # Fresh every run; published (with the bound port) only once listening.
        self._token = token or new_token()
        if not _TOKEN_RE.fullmatch(self._token):
            raise HomeError("local: the token must be 32-128 url-safe characters")
        self.core: Any = None
        self._subscribers: Set["asyncio.Queue[Optional[Dict[str, Any]]]"] = set()
        self._backlog: Deque[Dict[str, Any]] = deque(maxlen=BACKLOG)
        self._runner = UvicornRunner(host, self.port)
        self._published = False
        #: One-time codes and browser bearers (memory only: a restart revokes them).
        self.browser = BrowserPairing(browser_origins() if origins is None else origins)
        self.app = self.build_app()

    def __repr__(self) -> str:  # never show the token
        return f"LocalTransport({self.host}:{self.port})"

    @classmethod
    def from_env(cls) -> "LocalTransport":
        return cls()

    @classmethod
    def is_configured(cls) -> bool:
        return True

    @property
    def bound_port(self) -> int:
        return self._runner.bound_port or self.port

    @property
    def bound_hosts(self) -> List[str]:
        """Every address a listening socket is actually bound to ([] = not up)."""
        return self._runner.bound_hosts

    # ── Transport ─────────────────────────────────────────────────────────────
    async def start(self, core: Any) -> None:
        self.core = core
        await self._runner.start(self.app, CHANNEL, public=False)
        try:
            write_endpoint(self._token, self.bound_port, self.root)
        except BaseException:
            await self._runner.stop()            # fail closed: no token file, no channel
            raise
        self._published = True

    async def stop(self) -> None:
        for q in list(self._subscribers):
            self._offer(q, None)                 # end every open /events stream
        await self._runner.stop()
        if self._published:
            remove_endpoint(self._token, self.root)
            self._published = False

    async def send(self, user_id: str, text: str) -> bool:
        if str(user_id) != self.user_id:
            logger.error("hearth: local send refused: not the local owner")
            return False
        msg: Dict[str, Any] = {"text": text, "card": self._card_nonce(text),
                               "ts": time.time()}
        bucket = _collector.get()
        if bucket is not None:
            bucket.append(msg)
            self._publish({**msg, "kind": "reply"})
            return True
        if self._publish({**msg, "kind": "push"}):
            return True
        self._backlog.append({**msg, "kind": "push", "missed": True})
        logger.warning("hearth: local push had no listener (kept for the next one)")
        return False

    # ── delivery ──────────────────────────────────────────────────────────────
    def _card_nonce(self, text: str) -> Optional[str]:
        m = CARD_RE.search(text or "")
        if not m:
            return None
        waiting = getattr(self.core, "awaiting", None) or {}
        return m.group(1) if waiting.get("nonce") == m.group(1) else None

    @staticmethod
    def _offer(q: "asyncio.Queue[Optional[Dict[str, Any]]]",
               item: Optional[Dict[str, Any]]) -> None:
        if q.full():
            try:
                q.get_nowait()                  # a slow reader loses the oldest
            except asyncio.QueueEmpty:
                logger.debug("hearth: local subscriber queue drained concurrently")
        q.put_nowait(item)

    def _publish(self, item: Dict[str, Any]) -> bool:
        for q in list(self._subscribers):
            self._offer(q, item)
        return bool(self._subscribers)

    def _authorized(self, request: Any) -> bool:
        header = str(request.headers.get("authorization") or "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return False
        return hmac.compare_digest(given.strip().encode("utf-8"),
                                   self._token.encode("utf-8"))

    def _browser_session(self, request: Any) -> Optional[Dict[str, Any]]:
        """The browser session this request's bearer + Origin name (None = refuse)."""
        header = str(request.headers.get("authorization") or "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not given.strip():
            return None
        return self.browser.session_for(given.strip(),
                                        str(request.headers.get("origin") or ""))

    def new_browser_code(self) -> Tuple[str, float]:
        """A one-time pairing code for a web page (printed by the CLI, never logged)."""
        return self.browser.new_code()

    def browser_state(self) -> Dict[str, Any]:
        """What the paired page shows: the card waiting, reminders, agent + model."""
        core = self.core
        waiting = getattr(core, "awaiting", None) or {}
        pending = None
        if waiting.get("nonce"):
            pending = {"nonce": str(waiting["nonce"]),
                       "channel": str(waiting.get("channel") or ""),
                       "age_s": round(time.time() - float(waiting.get("at") or time.time())),
                       "calls": [{"tool": str(p.get("tool") or ""),
                                  "args": p.get("args") if isinstance(p.get("args"), dict)
                                  else {}}
                                 for p in waiting.get("pending") or []
                                 if isinstance(p, dict)]}
        reminders: List[Dict[str, Any]] = []
        store = getattr(core, "store", None)
        if store is not None:
            try:
                rows = store.rows()
            except Exception as exc:  # noqa: BLE001 - shown as empty, never fatal
                logger.warning("hearth: reminders unreadable: %s", type(exc).__name__)
                rows = []
            reminders = [{k: r.get(k) for k in ("id", "kind", "text", "when_ts", "recurring")}
                         for r in rows if r.get("status") == "pending"]
        agent = getattr(core, "agent", None)
        llm = getattr(agent, "llm", None)
        model = ""
        for attr in ("model", "_model", "model_name"):
            val = getattr(llm, attr, None)
            if isinstance(val, str) and val:
                model = val
                break
        return {"agent": str(getattr(agent, "name", "") or ""), "model": model,
                "pending": pending, "reminders": reminders}

    def _ensure_owner(self) -> None:
        if not self.core.registry.is_owner(CHANNEL, self.user_id):
            self.core.bind_owner(CHANNEL, self.user_id, "local-token")

    async def _turn(self, text: str) -> Dict[str, Any]:
        replies: List[Dict[str, Any]] = []
        mark = _collector.set(replies)
        try:
            # The task copies this context: its sends land in ``replies``. The
            # turn is shielded, so a client that hangs up does not cancel it.
            task = asyncio.ensure_future(self.core.on_message(CHANNEL, self.user_id, text))
        finally:
            _collector.reset(mark)
        handled = await asyncio.shield(task)
        return {"handled": bool(handled), "replies": replies}

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response, StreamingResponse
        from starlette.routing import Route

        transport = self

        def refuse(status: int, error: str) -> Response:
            return JSONResponse({"error": error}, status_code=status)

        async def read_json(request: Any) -> Any:
            """The JSON object body, or a refusal Response."""
            body = await read_capped(request, MAX_BODY)
            if body is None:
                return refuse(413, f"body over {MAX_BODY} bytes")
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return refuse(400, "body is not JSON")
            return data if isinstance(data, dict) else refuse(400, "send a JSON object")

        async def run_text(text: str) -> Response:
            if transport.core is None:
                return refuse(503, "hearth is not started")
            transport._ensure_owner()
            try:
                out = await transport._turn(text)
            except Exception as exc:  # noqa: BLE001 - reported, the server lives on
                logger.error("hearth: local turn failed: %s", type(exc).__name__)
                return refuse(500, f"turn failed: {type(exc).__name__}")
            return JSONResponse(out)

        async def say_from(request: Any) -> Response:
            data = await read_json(request)
            if isinstance(data, Response):
                return data
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                return refuse(400, "send {\"text\": \"...\"}")
            return await run_text(text)

        async def message(request: Any) -> Response:
            if not transport._authorized(request):
                logger.warning("hearth: local request without a valid token refused")
                return refuse(401, "unauthorized")
            return await say_from(request)

        async def browser_code(request: Any) -> Response:
            if not transport._authorized(request):
                return refuse(401, "unauthorized")
            if not transport.browser.enabled:
                return refuse(409, "browser pairing is off (HEARTH_BROWSER_ORIGINS=off)")
            code, ttl = transport.new_browser_code()
            return JSONResponse({"code": code, "expires_in": int(ttl),
                                 "origins": list(transport.browser.origins)})

        async def browser_pair(request: Any) -> Response:
            origin = str(request.headers.get("origin") or "")
            if not origin:
                return refuse(403, "pairing is for a web page: no Origin was sent")
            data = await read_json(request)
            if isinstance(data, Response):
                return data
            token, why = transport.browser.pair(data.get("code"), origin)
            if token is None:
                logger.warning("hearth: browser pairing refused (%s)", why)
                return refuse(401, why)
            logger.info("hearth: a browser on %s paired with the local channel", origin)
            agent = getattr(transport.core, "agent", None)
            return JSONResponse({"token": token, "expires_in": int(SESSION_TTL_S),
                                 "agent": str(getattr(agent, "name", "") or "")})

        async def browser_say(request: Any) -> Response:
            if transport._browser_session(request) is None:
                return refuse(401, "unauthorized: pair this page first")
            return await say_from(request)

        async def browser_state(request: Any) -> Response:
            if transport._browser_session(request) is None:
                return refuse(401, "unauthorized: pair this page first")
            if transport.core is None:
                return refuse(503, "hearth is not started")
            from adk import receipts

            path = transport.core.receipts_file
            rows = await asyncio.to_thread(receipts.tail, 12, path)
            code, reason = await asyncio.to_thread(receipts.check, path)
            verdict = {0: "intact", 1: "TAMPERED"}.get(code, "cannot judge")
            out = transport.browser_state()
            out["receipts"] = {"rows": rows,
                               "verify": {"code": code, "verdict": verdict,
                                          "reason": reason}}
            return JSONResponse(out)

        async def browser_approve(request: Any) -> Response:
            if transport._browser_session(request) is None:
                return refuse(401, "unauthorized: pair this page first")
            data = await read_json(request)
            if isinstance(data, Response):
                return data
            nonce = str(data.get("nonce") or "").lower()
            allow = data.get("allow")
            if not CARD_NONCE_RE.fullmatch(nonce) or not isinstance(allow, bool):
                return refuse(400, "send {\"nonce\": \"<8 hex>\", \"allow\": true|false}")
            return await run_text(f"{'yes' if allow else 'no'} {nonce}")

        async def browser_forget(request: Any) -> Response:
            if transport._browser_session(request) is None:
                return refuse(401, "unauthorized")
            header = str(request.headers.get("authorization") or "")
            transport.browser.forget(header.partition(" ")[2].strip())
            return JSONResponse({"ok": True})

        async def events(request: Any) -> Response:
            if not transport._authorized(request):
                return refuse(401, "unauthorized")
            q: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue(SUBSCRIBER_QUEUE)
            while transport._backlog:
                transport._offer(q, transport._backlog.popleft())
            transport._subscribers.add(q)

            async def stream() -> Any:
                try:
                    yield ": connected\n\n"
                    while True:
                        try:
                            item = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        if item is None:
                            return
                        yield f"event: message\ndata: {json.dumps(item, default=str)}\n\n"
                finally:
                    transport._subscribers.discard(q)

            return StreamingResponse(stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache"})

        async def receipts_view(request: Any) -> Response:
            if not transport._authorized(request):
                return refuse(401, "unauthorized")
            if transport.core is None:
                return refuse(503, "hearth is not started")
            raw = str(request.query_params.get("n") or "10")
            n = max(1, min(MAX_RECEIPTS, int(raw))) if raw.isdigit() else 10
            from adk import receipts

            path = transport.core.receipts_file
            rows = await asyncio.to_thread(receipts.tail, n, path)
            code, reason = await asyncio.to_thread(receipts.check, path)
            verdict = {0: "intact", 1: "TAMPERED"}.get(code, "cannot judge")
            return JSONResponse({"rows": rows, "path": str(path),
                                 "verify": {"code": code, "verdict": verdict,
                                            "reason": reason}})

        async def hello(request: Any) -> Response:
            nonce = str(request.query_params.get("nonce") or "")
            if not NONCE_RE.fullmatch(nonce):
                return refuse(400, "send ?nonce=<32-128 lowercase hex>")
            port = transport.bound_port
            return JSONResponse({"proof": hello_proof(transport._token, nonce, port),
                                 "port": port})

        app = Starlette(routes=[
            Route("/hello", hello, methods=["GET"]),
            Route("/message", message, methods=["POST"]),
            Route("/events", events, methods=["GET"]),
            Route("/receipts", receipts_view, methods=["GET"]),
            Route("/browser-code", browser_code, methods=["POST"]),
            Route("/browser/pair", browser_pair, methods=["POST"]),
            Route("/browser/say", browser_say, methods=["POST"]),
            Route("/browser/state", browser_state, methods=["GET"]),
            Route("/browser/approve", browser_approve, methods=["POST"]),
            Route("/browser/forget", browser_forget, methods=["POST"]),
        ])
        return BrowserGuard(app, transport.browser)


def build_local_transport(**overrides: Any) -> LocalTransport:
    """What ``adk home serve`` calls: port from ``$HEARTH_LOCAL_PORT``."""
    return LocalTransport(**overrides)
