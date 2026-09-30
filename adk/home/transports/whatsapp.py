"""WhatsApp for Aither Hearth, on the Meta WhatsApp Cloud API.

Inbound is a webhook Meta calls:

* ``GET  /whatsapp`` -- the subscribe handshake: ``hub.mode=subscribe`` plus the
  configured verify token (compared in constant time) echoes ``hub.challenge``;
  anything else is 403.
* ``POST /whatsapp`` -- every delivery carries ``X-Hub-Signature-256:
  sha256=<hex>``, an HMAC-SHA256 of the raw body keyed with the app secret. The
  signature is checked BEFORE the body is parsed; missing or wrong => 401 and
  nothing reaches the core. Verified text messages go to
  ``HearthCore.on_message("whatsapp", <wa_id>, text)``, where ``wa_id`` is the
  sender's number in E.164 digits (no ``+``).

Outbound is ``POST https://graph.facebook.com/<version>/<phone_number_id>/messages``
with the access token as a bearer. The token is read from the environment and is
never logged, echoed or put in a receipt.

The webhook needs a public https URL. Run the transport on a local port and
expose it: ``awtunnel up --port 8095``, then set the callback URL in the Meta app
dashboard to ``https://<tunnel-host>/whatsapp`` with the same verify token.

Environment (credentials only from here, never argv):

    HEARTH_WA_TOKEN            access token (system-user token recommended)
    HEARTH_WA_APP_SECRET       app secret, keys the X-Hub-Signature-256 HMAC
    HEARTH_WA_VERIFY_TOKEN     any string you choose; typed into the Meta dashboard
    HEARTH_WA_PHONE_NUMBER_ID  the sending number's id (not a secret)
    HEARTH_WA_PORT             webhook port (default 8095)
    HEARTH_WA_HOST             bind address (default 127.0.0.1; the tunnel dials it)
    HEARTH_WA_API_VERSION      Graph API version (default v21.0)

WhatsApp only allows free-form text within 24 hours of the user's last message.
A follow-up outside that window is refused by Meta (error 131047); :meth:`send`
returns False, the core receipts it as ``failed`` and falls back to another
bound channel.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..config import HomeError
from ._webhook import InboundDispatcher, UvicornRunner, e164_digits, read_capped

logger = logging.getLogger("adk.home.transports.whatsapp")

CHANNEL = "whatsapp"
GRAPH_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
DEFAULT_PORT = 8095
DEFAULT_PATH = "/whatsapp"
#: WhatsApp's limit for one text message body.
MAX_TEXT = 4096
#: A delivery older than this (by the message's own timestamp) is dropped: Meta
#: retries failed deliveries for days, and a stale "yes" must not act late.
MAX_AGE_S = 15 * 60

ENV_TOKEN = "HEARTH_WA_TOKEN"
ENV_APP_SECRET = "HEARTH_WA_APP_SECRET"
ENV_VERIFY_TOKEN = "HEARTH_WA_VERIFY_TOKEN"
ENV_PHONE_NUMBER_ID = "HEARTH_WA_PHONE_NUMBER_ID"


def verify_signature(app_secret: str, body: bytes, header: Optional[str]) -> bool:
    """True only when ``header`` is ``sha256=<hex>`` of HMAC-SHA256(app_secret, body)."""
    if not app_secret or not header:
        return False
    scheme, _, received = header.strip().partition("=")
    if scheme.lower() != "sha256" or not received:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


def sign_body(app_secret: str, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value Meta would send for ``body`` (tests, tools)."""
    return "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def iter_text_messages(payload: Any, phone_number_id: str = ""
                       ) -> Iterator[Tuple[str, str, str, int]]:
    """``(message_id, wa_id, text, timestamp)`` for each text message in a webhook
    payload. Status updates and non-text messages are skipped; so is any change
    addressed to a different phone number id when ``phone_number_id`` is set."""
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            if not isinstance(value, dict):
                continue
            meta_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            if phone_number_id and meta_id and meta_id != phone_number_id:
                continue
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "text":
                    text = str((msg.get("text") or {}).get("body") or "")
                elif msg.get("type") == "button":
                    text = str((msg.get("button") or {}).get("text") or "")
                else:
                    logger.info("hearth: whatsapp %s message ignored (text only)",
                                msg.get("type"))
                    continue
                try:
                    ts = int(msg.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0
                yield str(msg.get("id") or ""), str(msg.get("from") or ""), text, ts


def _chunks(text: str, size: int = MAX_TEXT) -> List[str]:
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class WhatsAppTransport:
    """A :class:`adk.home.hearth.Transport` over the WhatsApp Cloud API."""

    name = CHANNEL

    def __init__(self, token: str, app_secret: str, verify_token: str,
                 phone_number_id: str, *,
                 host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 path: str = DEFAULT_PATH,
                 api_version: str = DEFAULT_API_VERSION,
                 graph_url: str = GRAPH_URL,
                 http: Any = None,
                 max_age_s: int = MAX_AGE_S,
                 clock: Any = time.time):
        if not token:
            raise HomeError(f"whatsapp: set {ENV_TOKEN} (the access token)")
        if not app_secret:
            raise HomeError(f"whatsapp: set {ENV_APP_SECRET} (unsigned webhooks are "
                            "never accepted)")
        if not phone_number_id:
            raise HomeError(f"whatsapp: set {ENV_PHONE_NUMBER_ID}")
        self._token = token
        self._app_secret = app_secret
        self._verify_token = verify_token or ""
        self.phone_number_id = str(phone_number_id)
        self.path = path
        self.api_version = api_version
        self.graph_url = graph_url.rstrip("/")
        self.max_age_s = max_age_s
        self._clock = clock
        self._http = http
        self._own_http = http is None
        self._runner = UvicornRunner(host, port)
        self._inbound = InboundDispatcher(CHANNEL)
        self.app = self.build_app()

    def __repr__(self) -> str:  # never show the credentials
        return f"WhatsAppTransport(phone_number_id={self.phone_number_id!r})"

    # ── Transport ─────────────────────────────────────────────────────────────
    async def start(self, core: Any) -> None:
        self._inbound.core = core
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=20.0)
        await self._runner.start(self.app, CHANNEL)

    async def stop(self) -> None:
        await self._runner.stop()
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def send(self, user_id: str, text: str) -> bool:
        to = e164_digits(user_id)
        if not to:
            logger.error("hearth: whatsapp send refused: recipient is not an E.164 number")
            return False
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=20.0)
        url = f"{self.graph_url}/{self.api_version}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self._token}"}
        for part in _chunks(text):
            body = {"messaging_product": "whatsapp", "recipient_type": "individual",
                    "to": to, "type": "text",
                    "text": {"preview_url": False, "body": part}}
            try:
                resp = await self._http.post(url, json=body, headers=headers)
            except Exception as exc:  # noqa: BLE001 - a failed send is False, not a crash
                logger.error("hearth: whatsapp send failed: %s", type(exc).__name__)
                return False
            if not 200 <= resp.status_code < 300:
                logger.error("hearth: whatsapp send refused: HTTP %s (error %s)",
                             resp.status_code, _graph_error_code(resp))
                return False
        return True

    async def drain(self) -> None:
        """Wait for queued inbound messages to be handled (tests, shutdown)."""
        await self._inbound.drain()

    # ── webhook ───────────────────────────────────────────────────────────────
    def build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, PlainTextResponse, Response
        from starlette.routing import Route

        transport = self

        async def verify(request: Any) -> Response:
            q = request.query_params
            if (q.get("hub.mode") == "subscribe" and transport._verify_token
                    and hmac.compare_digest(str(q.get("hub.verify_token") or ""),
                                            transport._verify_token)):
                return PlainTextResponse(str(q.get("hub.challenge") or ""))
            logger.warning("hearth: whatsapp verify handshake refused")
            return PlainTextResponse("forbidden", status_code=403)

        async def receive(request: Any) -> Response:
            body = await read_capped(request)
            if body is None:
                return PlainTextResponse("too large", status_code=413)
            if not verify_signature(transport._app_secret, body,
                                    request.headers.get("x-hub-signature-256")):
                logger.warning("hearth: whatsapp webhook with a missing or bad signature "
                               "refused")
                return PlainTextResponse("unauthorized", status_code=401)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return PlainTextResponse("bad json", status_code=400)
            accepted = transport._accept(payload)
            return JSONResponse({"ok": True, "accepted": accepted})

        return Starlette(routes=[Route(self.path, verify, methods=["GET"]),
                                 Route(self.path, receive, methods=["POST"])])

    def _accept(self, payload: Dict[str, Any]) -> int:
        now = self._clock()
        accepted = 0
        for msg_id, sender, text, ts in iter_text_messages(payload, self.phone_number_id):
            wa_id = e164_digits(sender)
            if not wa_id:
                logger.warning("hearth: whatsapp message with a malformed sender dropped")
                continue
            if ts and self.max_age_s and now - ts > self.max_age_s:
                logger.warning("hearth: stale whatsapp message dropped (%ds old)",
                               int(now - ts))
                continue
            if self._inbound.seen(msg_id):
                continue
            self._inbound.submit(wa_id, text)
            accepted += 1
        return accepted


def _graph_error_code(resp: Any) -> str:
    try:
        err = (resp.json() or {}).get("error") or {}
        return str(err.get("code") or "?")
    except Exception:  # noqa: BLE001 - diagnostics only
        return "?"


def build_whatsapp_transport(**overrides: Any) -> WhatsAppTransport:
    """A :class:`WhatsAppTransport` configured from ``HEARTH_WA_*`` env vars.

    Raises :class:`HomeError` naming the missing variable (never a value).
    """
    env = os.environ

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    if not get(ENV_VERIFY_TOKEN):
        raise HomeError(f"whatsapp: set {ENV_VERIFY_TOKEN} (the webhook verify token)")
    port_raw = get("HEARTH_WA_PORT")
    kwargs: Dict[str, Any] = {
        "host": get("HEARTH_WA_HOST") or "127.0.0.1",
        "port": int(port_raw) if port_raw.isdigit() else DEFAULT_PORT,
        "api_version": get("HEARTH_WA_API_VERSION") or DEFAULT_API_VERSION,
    }
    kwargs.update(overrides)
    return WhatsAppTransport(get(ENV_TOKEN), get(ENV_APP_SECRET), get(ENV_VERIFY_TOKEN),
                             get(ENV_PHONE_NUMBER_ID), **kwargs)
