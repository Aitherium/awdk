"""Email as a Hearth transport: IMAP in, SMTP out, stdlib only.

Inbound. Every ``poll_interval`` seconds the transport logs in to IMAP (in a
worker thread), reads up to :data:`MAX_PER_POLL` ``UNSEEN`` messages from the
mailbox (``BODY.PEEK[]``, then ``\\Seen``), and hands each one to the core as
``("email", <lowercased bare From address>, <reply text>)``. The reply text is
the ``text/plain`` part (or de-tagged HTML, parsed in linear time and capped at
:data:`MAX_HTML_CHARS`) with quoted history and the signature cut off by
:func:`strip_quoted_reply`. Parsing runs in a worker thread, never on the loop.

Spoofing. Anyone can write any ``From:`` -- and any ``Authentication-Results:``.
A message whose From is the bound owner is accepted only when the RECEIVING
server vouched for it: the TOP-MOST ``Authentication-Results`` header must carry
the receiving server's authserv-id (``HEARTH_MAIL_AUTHSERV_ID``, REQUIRED -- the
transport refuses to start without it) and show ``dmarc=pass`` (for the From
domain), or ``spf=pass`` AND ``dkim=pass`` both aligned to the From domain
(:func:`sender_authenticated`). Nothing below the top-most header is read, and
a top-most header with any other authserv-id fails: on a server that stamps no
header at all, the top-most one is the sender's forgery, so without the pin the
gate would trust the attacker. Find the id in the ``Authentication-Results:``
line of a mail your own server delivered (the first token, e.g.
``mx.google.com``). A message that fails the gate is treated as a stranger's: it
never reaches the owner path, and a right pairing code from it does not bind
(``verify_identity``). Each unauthenticated sender is logged once.

Outbound. Replies thread: ``Re: <subject>``, ``In-Reply-To`` the owner's last
message and ``References`` its chain. SMTP is implicit TLS on port 465, else
STARTTLS -- a server that does not offer STARTTLS gets no password.

Configuration (environment only; the password never comes from argv and is never
logged)::

    HEARTH_IMAP_HOST  HEARTH_IMAP_PORT (993)  HEARTH_IMAP_USER
    HEARTH_SMTP_HOST (= IMAP host)  HEARTH_SMTP_PORT (587)
    HEARTH_MAIL_PASSWORD  -- or the OS keychain: service ``aither_adk``,
                             key ``HEARTH_MAIL_PASSWORD``
    HEARTH_MAIL_FROM (= IMAP user)  HEARTH_MAIL_POLL (30)  HEARTH_MAIL_MAILBOX (INBOX)
    HEARTH_MAIL_AUTHSERV_ID  -- REQUIRED: your receiving server's authserv-id
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import html
import imaplib
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage, Message
from email.utils import getaddresses, make_msgid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config import HomeError

logger = logging.getLogger("adk.home.transports.mail")

CHANNEL = "email"
KEYCHAIN_SERVICE = "aither_adk"
PASSWORD_ENV = "HEARTH_MAIL_PASSWORD"
DEFAULT_POLL_S = 30.0
#: Messages read per poll; the rest wait for the next one.
MAX_PER_POLL = 20
#: Characters of reply text handed to the core.
MAX_TEXT = 8000
#: Raw message bytes read; a larger message is skipped (marked seen).
MAX_RAW_BYTES = 2_000_000
DEFAULT_SUBJECT = "Aither Hearth"
#: Characters of an HTML-only body that are parsed; the rest is ignored.
MAX_HTML_CHARS = 256 * 1024
AUTHSERV_ENV = "HEARTH_MAIL_AUTHSERV_ID"

UNVERIFIED_REPLY = ("That code is right, but your mail server's checks (DMARC, or "
                    "SPF and DKIM) did not pass for this message, so it was not paired.")


# ── Authentication-Results ────────────────────────────────────────────────────

def _strip_comments(value: str) -> str:
    """Drop RFC 5322 ``(comments)`` (nested) from a header value."""
    out: List[str] = []
    depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def parse_auth_results(value: str) -> Tuple[str, List[Dict[str, str]]]:
    """``(authserv_id, [{"method": "dkim", "result": "pass", "header.d": ...}, ...])``."""
    parts = [p.strip() for p in _strip_comments(value).split(";")]
    authserv = parts[0].split()[0].lower() if parts and parts[0].split() else ""
    results: List[Dict[str, str]] = []
    for part in parts[1:]:
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].partition("=")
        entry = {"method": method.strip().lower(), "result": result.strip().lower()}
        for tok in tokens[1:]:
            key, eq, val = tok.partition("=")
            if eq:
                entry[key.strip().lower()] = val.strip().strip('"').lower()
        results.append(entry)
    return authserv, results


def _domain_of(value: str) -> str:
    value = (value or "").strip().strip("<>").lower()
    return value.rpartition("@")[2].rstrip(".")


def aligned(domain: str, from_domain: str) -> bool:
    """Relaxed DMARC alignment, approximated without a public-suffix list: equal,
    or one a subdomain of the other where the parent has at least two labels."""
    a, b = domain.lower().rstrip("."), from_domain.lower().rstrip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return "." in short and long_.endswith("." + short)


def sender_authenticated(msg: Message, from_domain: str,
                         authserv_id: str = "") -> bool:
    """True when the receiving server vouched for ``from_domain`` (see module doc).

    Fails closed: no ``authserv_id`` pinned, no header, or a top-most header
    stamped by anyone else is False. Headers further down are never consulted.
    """
    pinned = (authserv_id or "").strip().lower()
    headers = [str(h) for h in (msg.get_all("Authentication-Results") or [])]
    if not pinned or not headers or not from_domain:
        return False
    server, chosen = parse_auth_results(headers[0])   # top-most: the receiving server's
    if server != pinned or not chosen:
        return False

    def passed(method: str, *keys: str) -> bool:
        for r in chosen or []:
            if r["method"] != method or r["result"] != "pass":
                continue
            domains = [_domain_of(r[k]) for k in keys if r.get(k)]
            if domains and aligned(domains[0], from_domain):
                return True
        return False

    return (passed("dmarc", "header.from")
            or (passed("spf", "smtp.mailfrom")
                and passed("dkim", "header.d", "header.i")))


# ── quoted-reply stripping ────────────────────────────────────────────────────

_ATTRIBUTION_RE = re.compile(
    r"^\s*(on\b.{0,200}\bwrote:|le\b.{0,200}\ba écrit\s?:|am\b.{0,200}\bschrieb\b.*:)\s*$",
    re.I)
_CUT_LINE_RE = re.compile(
    r"^\s*(-{2,}\s*original message\s*-{2,}|-{2,}\s*forwarded message\s*-{2,}"
    r"|_{10,}|begin forwarded message:)\s*$", re.I)
_HEADER_BLOCK_RE = re.compile(r"^\s*(from|de|von):\s.+$", re.I)
_HEADER_NEXT_RE = re.compile(r"^\s*(sent|date|to|subject|envoyé|gesendet):\s", re.I)
_SIGNOFF_RE = re.compile(
    r"^\s*(sent from my \w+|get outlook for \w+|sent from (yahoo )?mail for \w+)", re.I)


def strip_quoted_reply(text: str) -> str:
    """The new part of an email reply: drop ``>`` quotes, everything from an
    attribution (``On ... wrote:``, also wrapped over two lines), an Outlook header
    block or ``Original Message`` separator on, the ``-- `` signature and mobile
    sign-offs."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: List[str] = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.rstrip() in ("--", "-- ") or line == "-- ":
            break
        if _CUT_LINE_RE.match(line) or _ATTRIBUTION_RE.match(line):
            break
        if (line.strip().lower().startswith("on ") and not line.rstrip().endswith(":")
                and nxt.rstrip().lower().endswith("wrote:")
                and _ATTRIBUTION_RE.match(line + " " + nxt)):
            break
        if _HEADER_BLOCK_RE.match(line) and _HEADER_NEXT_RE.match(nxt):
            break
        if _SIGNOFF_RE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


_TAG_NAME_RE = re.compile(r"\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")
#: Tags whose whole content is dropped (quoted history lives in ``blockquote``).
_SKIP_TAGS = frozenset({"script", "style", "head", "blockquote"})
#: Tags whose end (or, for ``br``, whose start) is a line break.
_BREAK_TAGS = frozenset({"br", "p", "div", "li", "tr"})


def _html_to_text(value: str) -> str:
    """De-tagged text of an HTML body, in ONE linear pass over at most
    :data:`MAX_HTML_CHARS` characters.

    Hand-rolled on purpose: backtracking regexes (``<script.*?</script>``) and
    :class:`html.parser.HTMLParser` both go quadratic on tags that are opened and
    never closed, which any stranger can mail. Each ``<`` is resolved with one
    forward ``find`` to the next ``>`` and the scan resumes after it; the first
    ``<`` with no ``>`` after it ends the scan (no later one can have one either).
    """
    value = value[:MAX_HTML_CHARS]
    out: List[str] = []
    skip = 0
    i, n = 0, len(value)
    while i < n:
        lt = value.find("<", i)
        if lt < 0:
            if not skip:
                out.append(value[i:])
            break
        if lt > i and not skip:
            out.append(value[i:lt])
        if value.startswith("<!--", lt):
            close = value.find("-->", lt + 4)
            if close < 0:
                break
            i = close + 3
            continue
        gt = value.find(">", lt + 1)
        if gt < 0:
            break                               # an unterminated tag: nothing after it
        m = _TAG_NAME_RE.match(value, lt + 1, gt)
        if m:
            closing, tag = bool(m.group(1)), m.group(2).lower()
            if tag in _SKIP_TAGS:
                skip = max(0, skip - 1) if closing else skip + 1
            elif not skip and tag in _BREAK_TAGS and (closing or tag == "br"):
                out.append("\n")
        i = gt + 1
    return html.unescape("".join(out))


def body_text(msg: Message) -> str:
    """The first non-attachment ``text/plain`` part, else de-tagged ``text/html``."""
    plain = rich = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, AssertionError):
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and rich is None:
            rich = text
    if plain is not None:
        return plain
    return _html_to_text(rich) if rich is not None else ""


# ── the transport ─────────────────────────────────────────────────────────────

def _sender(msg: Message) -> str:
    """The one bare From address, lowercased; ``""`` for none or several."""
    addrs = [a for _, a in getaddresses([str(v) for v in msg.get_all("From") or []]) if a]
    if len(addrs) != 1:
        return ""
    addr = addrs[0].strip().lower()
    if "@" not in addr or any(c in addr for c in "\r\n <>,;"):
        return ""
    return addr


def _is_automated(msg: Message) -> bool:
    auto = str(msg.get("Auto-Submitted", "no")).strip().lower()
    prec = str(msg.get("Precedence", "")).strip().lower()
    return (auto not in ("", "no") or prec in ("bulk", "list", "junk")
            or bool(msg.get("List-Id")))


class EmailTransport:
    """IMAP polling + SMTP replies for :class:`adk.home.hearth.HearthCore`."""

    name = CHANNEL
    unverified_reply = UNVERIFIED_REPLY

    def __init__(self, *, imap_host: str, user: str, password: str,
                 imap_port: int = 993, smtp_host: str = "", smtp_port: int = 587,
                 from_addr: str = "", mailbox: str = "INBOX",
                 poll_interval: float = DEFAULT_POLL_S, authserv_id: str = "",
                 imap_factory: Optional[Callable[[], Any]] = None,
                 smtp_factory: Optional[Callable[[], Any]] = None):
        if not imap_host or not user:
            raise HomeError("email: set HEARTH_IMAP_HOST and HEARTH_IMAP_USER")
        if not (authserv_id or "").strip():
            raise HomeError(f"email: set {AUTHSERV_ENV} to your receiving mail server's "
                            "authserv-id (the first token of the Authentication-Results "
                            "header it adds); without it a forged header could pass as "
                            "the owner")
        if not password:
            raise HomeError(f"email: set {PASSWORD_ENV} (or store it in the keychain, "
                            f"service {KEYCHAIN_SERVICE!r}, key {PASSWORD_ENV!r})")
        self.imap_host, self.imap_port = imap_host, int(imap_port)
        self.smtp_host, self.smtp_port = smtp_host or imap_host, int(smtp_port)
        self.user = user
        self.__password = password
        self.from_addr = (from_addr or user).strip()
        self.mailbox = mailbox or "INBOX"
        self.poll_interval = max(5.0, float(poll_interval))
        self.authserv_id = (authserv_id or "").strip().lower()
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp
        self.core: Any = None
        self._task: Optional["asyncio.Task[None]"] = None
        #: user_id -> {"message_id", "references", "subject"} of their last message.
        self._threads: Dict[str, Dict[str, str]] = {}
        #: user_id -> did the message being handled pass the auth gate.
        self._verified: Dict[str, bool] = {}
        self._warned: Set[str] = set()
        self._seen_ids: Set[str] = set()

    def __repr__(self) -> str:   # never the password
        return (f"EmailTransport(imap={self.imap_host}:{self.imap_port}, "
                f"smtp={self.smtp_host}:{self.smtp_port}, user={self.user!r})")

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None, **kw: Any) -> "EmailTransport":
        e = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return (e.get(name) or default).strip()

        password = get(PASSWORD_ENV) or _keychain_password()
        try:
            return cls(imap_host=get("HEARTH_IMAP_HOST"), user=get("HEARTH_IMAP_USER"),
                       password=password,
                       imap_port=int(get("HEARTH_IMAP_PORT", "993")),
                       smtp_host=get("HEARTH_SMTP_HOST"),
                       smtp_port=int(get("HEARTH_SMTP_PORT", "587")),
                       from_addr=get("HEARTH_MAIL_FROM"),
                       mailbox=get("HEARTH_MAIL_MAILBOX", "INBOX"),
                       poll_interval=float(get("HEARTH_MAIL_POLL", str(DEFAULT_POLL_S))),
                       authserv_id=get(AUTHSERV_ENV), **kw)
        except ValueError as exc:
            raise HomeError(f"email: bad port or poll interval ({exc})") from None

    # ── connections (run in a worker thread) ─────────────────────────────────
    def _default_imap(self) -> Any:
        ctx = ssl.create_default_context()
        if self.imap_port == 143:
            conn = imaplib.IMAP4(self.imap_host, self.imap_port)
            conn.starttls(ssl_context=ctx)
            return conn
        return imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ctx)

    def _default_smtp(self) -> Any:
        ctx = ssl.create_default_context()
        if self.smtp_port == 465:
            return smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=30)
        conn = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
        conn.ehlo()
        if not conn.has_extn("starttls"):
            conn.quit()
            raise smtplib.SMTPNotSupportedError("server does not offer STARTTLS")
        conn.starttls(context=ctx)
        conn.ehlo()
        return conn

    def _fetch_unseen(self) -> List[bytes]:
        """Up to :data:`MAX_PER_POLL` raw UNSEEN messages, each marked seen."""
        conn = self._imap_factory()
        raws: List[bytes] = []
        try:
            conn.login(self.user, self.__password)
            conn.select(self.mailbox)
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return raws
            for uid in data[0].split()[:MAX_PER_POLL]:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = b""
                for item in parts or []:
                    if isinstance(item, tuple) and len(item) > 1:
                        raw = bytes(item[1])
                        break
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                if typ == "OK" and raw and len(raw) <= MAX_RAW_BYTES:
                    raws.append(raw)
                elif raw:
                    logger.warning("hearth: email: skipped an oversized message")
        finally:
            try:
                conn.logout()
            except Exception as exc:  # noqa: BLE001 - already done with it
                logger.debug("hearth: email: IMAP logout failed: %s", exc)
        return raws

    def _smtp_send(self, msg: EmailMessage) -> None:
        conn = self._smtp_factory()
        try:
            conn.login(self.user, self.__password)
            conn.send_message(msg)
        finally:
            try:
                conn.quit()
            except Exception as exc:  # noqa: BLE001 - the message already went
                logger.debug("hearth: email: SMTP quit failed: %s", exc)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self, core: Any) -> None:
        self.core = core
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("hearth: email loop cancelled")
            except Exception as exc:  # noqa: BLE001 - stopping; report, do not raise
                logger.debug("hearth: email loop ended with %s", exc)

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad poll must not end the loop
                logger.error("hearth: email poll failed: %s", type(exc).__name__)
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> int:
        """Read the mailbox once; returns how many messages reached the core."""
        raws = await asyncio.to_thread(self._fetch_unseen)
        handled = 0
        for raw in raws:
            if await self.handle_raw(raw):
                handled += 1
        return handled

    # ── inbound ───────────────────────────────────────────────────────────────
    async def handle_raw(self, raw: bytes) -> bool:
        """Gate and dispatch one raw message. True when it reached the core.

        Parsing (headers, then the body) runs in a worker thread: a hostile
        message must never stall the event loop every channel shares."""
        if self.core is None:
            return False
        msg = await asyncio.to_thread(email.message_from_bytes, raw,
                                      policy=email.policy.compat32)
        sender = _sender(msg)
        if not sender:
            logger.warning("hearth: email: dropped a message without exactly one From")
            return False
        if sender == self.from_addr.lower() or _is_automated(msg):
            return False                      # our own mail, bounces, lists: no loops
        msg_id = str(msg.get("Message-ID", "")).strip()
        if msg_id and msg_id in self._seen_ids:
            return False
        if msg_id:
            self._seen_ids.add(msg_id)
        verified = sender_authenticated(msg, _domain_of(sender), self.authserv_id)
        if not verified and sender not in self._warned:
            self._warned.add(sender)
            logger.warning("hearth: email from %s failed DMARC/SPF+DKIM at the "
                           "receiving server -- treated as a stranger", sender)
        if not verified and self.core.is_owner(self.name, sender):
            return False                      # a forged owner never reaches the owner path
        if not verified and not getattr(self.core, "pair_code", ""):
            return False                      # a stranger with no pairing open: body unread
        text = await asyncio.to_thread(_reply_text, msg)
        if verified:
            refs = str(msg.get("References", "")).split()
            if msg_id:
                refs.append(msg_id)
            self._threads[sender] = {
                "message_id": msg_id,
                "references": " ".join(refs[-20:]),
                "subject": " ".join(str(msg.get("Subject", "")).split())[:200],
            }
        self._verified[sender] = verified
        try:
            await self.core.on_message(self.name, sender, text)
        finally:
            self._verified.pop(sender, None)
        return True

    async def verify_identity(self, user_id: str) -> bool:
        """The core asks this before binding on a right pairing code."""
        return bool(self._verified.get(user_id, False))

    # ── outbound ──────────────────────────────────────────────────────────────
    def build_reply(self, user_id: str, text: str) -> EmailMessage:
        thread = self._threads.get(user_id, {})
        subject = thread.get("subject") or DEFAULT_SUBJECT
        if not re.match(r"(?i)^\s*re\s*:", subject):
            subject = f"Re: {subject}"
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = user_id
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=_domain_of(self.from_addr) or None)
        msg["Auto-Submitted"] = "auto-replied"
        if thread.get("message_id"):
            msg["In-Reply-To"] = thread["message_id"]
            msg["References"] = thread.get("references") or thread["message_id"]
        msg.set_content(text)
        return msg

    async def send(self, user_id: str, text: str) -> bool:
        if not user_id or "@" not in user_id or any(c in user_id for c in "\r\n"):
            return False
        try:
            msg = self.build_reply(user_id, text)
            await asyncio.to_thread(self._smtp_send, msg)
        except Exception as exc:  # noqa: BLE001 - a failed send is a False, not a crash
            logger.error("hearth: email send failed: %s", type(exc).__name__)
            return False
        return True


def _reply_text(msg: Message) -> str:
    return strip_quoted_reply(body_text(msg))[:MAX_TEXT]


def _keychain_password() -> str:
    try:
        from adk.core.secrets import KeyringStore

        return KeyringStore(KEYCHAIN_SERVICE).get(PASSWORD_ENV)
    except Exception:  # noqa: BLE001 - no keyring package, no entry: just unset
        return ""


def build_email_transport(**kw: Any) -> EmailTransport:
    """``adk home serve --channels email``: the transport configured from the env."""
    return EmailTransport.from_env(**kw)


__all__ = ["EmailTransport", "build_email_transport", "parse_auth_results",
           "sender_authenticated", "strip_quoted_reply", "body_text", "aligned"]
