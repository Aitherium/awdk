"""SMS (or WhatsApp) for Aither Hearth through Twilio.

Inbound: Twilio POSTs form fields to ``/twilio`` with ``X-Twilio-Signature`` =
base64(HMAC-SHA1(auth token, public URL + every POST field name and value,
sorted by name)). The signature is checked before anything is read from the
form; missing or wrong => 401 and nothing reaches the core. The URL must be the
EXACT public URL typed into the Twilio console (``HEARTH_TWILIO_PUBLIC_URL``,
e.g. the ``awtunnel up --port 8096`` host + ``/twilio``), because that is what
Twilio signed. ``user_id`` = the sender in E.164 digits.

Outbound: ``POST https://api.twilio.com/2010-04-01/Accounts/<sid>/Messages.json``
with basic auth. When ``HEARTH_TWILIO_FROM`` starts with ``whatsapp:`` messages
go over Twilio's WhatsApp sender (channel ``twilio-whatsapp``), else SMS
(channel ``sms``).

Identity. The signature proves TWILIO sent the webhook, not who sent the text.
On WhatsApp the sender is authenticated by WhatsApp itself, so
``twilio-whatsapp`` is a normal owner channel. A plain SMS ``From`` is caller ID,
and some routes let anyone set it to any number: SMS is a LOW-TRUST channel. By
default the ``sms`` channel cannot be paired (``verify_identity`` is False, so a
right code does not bind) and a text from a number already bound as the owner is
dropped before it reaches the core -- no owner turns, no approvals, no ``pair``.
An owner who accepts the risk opts in with ``HEARTH_TWILIO_ALLOW_SMS_OWNER=1``
(logged as a warning on every start).

Environment: HEARTH_TWILIO_ACCOUNT_SID, HEARTH_TWILIO_AUTH_TOKEN (secret),
HEARTH_TWILIO_FROM, HEARTH_TWILIO_PUBLIC_URL, HEARTH_TWILIO_PORT (default 8096),
HEARTH_TWILIO_HOST (default 127.0.0.1), HEARTH_TWILIO_ALLOW_SMS_OWNER (default 0).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl

from ..config import HomeError
from ._webhook import InboundDispatcher, UvicornRunner, e164_digits, read_capped

logger = logging.getLogger("adk.home.transports.twilio")

API_URL = "https://api.twilio.com"
DEFAULT_PORT = 8096
DEFAULT_PATH = "/twilio"
#: One outbound body; Twilio's hard limit is 1600 characters.
MAX_TEXT = 1600
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
ALLOW_SMS_OWNER_ENV = "HEARTH_TWILIO_ALLOW_SMS_OWNER"
SMS_UNVERIFIED_REPLY = ("That code is right, but an SMS sender number can be forged, so "
                        "this channel does not pair by default. Pair on WhatsApp, Telegram, "
                        "email or the relay instead.")


def twilio_signature(auth_token: str, url: str, params: Iterable[Tuple[str, str]]) -> str:
    """The ``X-Twilio-Signature`` Twilio computes for a form POST to ``url``."""
    data = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_signature(auth_token: str, url: str,
                            params: Iterable[Tuple[str, str]],
                            header: Optional[str]) -> bool:
    if not auth_token or not url or not header:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), header.strip())


class TwilioTransport:
    """A :class:`adk.home.hearth.Transport` over Twilio Programmable Messaging."""

    def __init__(self, account_sid: str, auth_token: str, from_number: str,
                 public_url: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 path: str = DEFAULT_PATH, api_url: str = API_URL, http: Any = None,
                 name: str = "", allow_sms_owner: bool = False):
        for value, var in ((account_sid, "HEARTH_TWILIO_ACCOUNT_SID"),
                           (auth_token, "HEARTH_TWILIO_AUTH_TOKEN"),
                           (from_number, "HEARTH_TWILIO_FROM"),
                           (public_url, "HEARTH_TWILIO_PUBLIC_URL")):
            if not value:
                raise HomeError(f"twilio: set {var}")
        self.account_sid = account_sid
        self._auth_token = auth_token
        self.from_number = from_number
        self.whatsapp = from_number.lower().startswith("whatsapp:")
        self.name = name or ("twilio-whatsapp" if self.whatsapp else "sms")
        #: May this channel carry the owner? WhatsApp authenticates its senders;
        #: plain SMS caller ID does not, so SMS needs an explicit opt-in.
        self.owner_allowed = self.whatsapp or bool(allow_sms_owner)
        self.unverified_reply = "" if self.owner_allowed else SMS_UNVERIFIED_REPLY
        if not self.whatsapp and allow_sms_owner:
            logger.warning("hearth: %s owner turns ENABLED (%s=1): an SMS sender number "
                           "can be spoofed, so anyone who fakes yours can run turns",
                           self.name, ALLOW_SMS_OWNER_ENV)
        self.public_url = public_url
        self.path = path
        self.api_url = api_url.rstrip("/")
        self._http = http
        self._own_http = http is None
        self._runner = UvicornRunner(host, port)
        self._inbound = InboundDispatcher(self.name)
        self.app = self.build_app()

    def __repr__(self) -> str:
        return f"TwilioTransport(name={self.name!r})"

    async def start(self, core: Any) -> None:
        self._inbound.core = core
        await self._runner.start(self.app, self.name)

    async def stop(self) -> None:
        await self._runner.stop()
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def send(self, user_id: str, text: str) -> bool:
        digits = e164_digits(user_id)
        if not digits:
            logger.error("hearth: %s send refused: recipient is not an E.164 number",
                         self.name)
            return False
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=20.0)
        to = ("whatsapp:+" if self.whatsapp else "+") + digits
        url = f"{self.api_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        text = text or ""
        for i in range(0, max(len(text), 1), MAX_TEXT):
            try:
                resp = await self._http.post(
                    url, data={"From": self.from_number, "To": to,
                               "Body": text[i:i + MAX_TEXT]},
                    auth=(self.account_sid, self._auth_token))
            except Exception as exc:  # noqa: BLE001 - a failed send is False
                logger.error("hearth: %s send failed: %s", self.name, type(exc).__name__)
                return False
            if not 200 <= resp.status_code < 300:
                logger.error("hearth: %s send refused: HTTP %s", self.name, resp.status_code)
                return False
        return True

    async def drain(self) -> None:
        await self._inbound.drain()

    async def verify_identity(self, user_id: str) -> bool:
        """Pairing binds only where the sender is authenticated (see module doc)."""
        return self.owner_allowed

    def build_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse, Response
        from starlette.routing import Route

        transport = self

        async def receive(request: Any) -> Response:
            body = await read_capped(request)
            if body is None:
                return PlainTextResponse("too large", status_code=413)
            try:
                params: List[Tuple[str, str]] = parse_qsl(body.decode("utf-8"),
                                                          keep_blank_values=True)
            except (UnicodeDecodeError, ValueError):
                params = []
            if not verify_twilio_signature(transport._auth_token, transport.public_url,
                                           params, request.headers.get("x-twilio-signature")):
                logger.warning("hearth: %s webhook with a missing or bad signature refused",
                               transport.name)
                return PlainTextResponse("unauthorized", status_code=401)
            transport._accept(dict(params))
            return Response(EMPTY_TWIML, media_type="text/xml")

        return Starlette(routes=[Route(self.path, receive, methods=["POST"])])

    def _accept(self, form: Dict[str, str]) -> bool:
        if form.get("AccountSid") and form["AccountSid"] != self.account_sid:
            logger.warning("hearth: %s message for another account dropped", self.name)
            return False
        sender = e164_digits(form.get("From"))
        if not sender:
            logger.warning("hearth: %s message with a malformed sender dropped", self.name)
            return False
        if self._inbound.seen(form.get("MessageSid") or form.get("SmsSid") or ""):
            return False
        core = self._inbound.core
        if (not self.owner_allowed and core is not None
                and core.is_owner(self.name, sender)):
            logger.warning("hearth: %s text from the bound owner's number dropped: SMS "
                           "caller ID is not authenticated (set %s=1 to allow)",
                           self.name, ALLOW_SMS_OWNER_ENV)
            return False
        self._inbound.submit(sender, form.get("Body") or "")
        return True


def build_twilio_transport(**overrides: Any) -> TwilioTransport:
    """A :class:`TwilioTransport` from ``HEARTH_TWILIO_*`` env vars."""
    def get(name: str) -> str:
        return (os.environ.get(name) or "").strip()

    port_raw = get("HEARTH_TWILIO_PORT")
    kwargs: Dict[str, Any] = {
        "host": get("HEARTH_TWILIO_HOST") or "127.0.0.1",
        "port": int(port_raw) if port_raw.isdigit() else DEFAULT_PORT,
        "allow_sms_owner": get(ALLOW_SMS_OWNER_ENV).lower() in ("1", "true", "yes"),
    }
    kwargs.update(overrides)
    return TwilioTransport(get("HEARTH_TWILIO_ACCOUNT_SID"), get("HEARTH_TWILIO_AUTH_TOKEN"),
                           get("HEARTH_TWILIO_FROM"), get("HEARTH_TWILIO_PUBLIC_URL"),
                           **kwargs)
