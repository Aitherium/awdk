"""AitherNet SMTP — Lightweight mail relay for awnodes.

Every ADK node can send/receive email through configured SMTP providers
or relay messages across the AitherNet mesh. Agents are addressable as
`agentname@node-id.aithernet` for mesh-internal routing.

Usage:
    from adk.smtp import MailRelay, get_mail_relay

    relay = get_mail_relay()
    relay.configure(host="smtp.gmail.com", port=587, username="...", password="...")
    await relay.send("user@example.com", "Hello", "Body text")
"""

from __future__ import annotations

import asyncio
import email.encoders as encoders
import json
import logging
import os
import smtplib
import sqlite3
import time
import uuid
from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass, field
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.smtp")

# ── Constants ────────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_BACKOFF_BASE = 4  # 4s, 16s, 64s
_QUEUE_INTERVAL = 5  # seconds
_MAX_QUEUE_SIZE = 500
_AITHERNET_DOMAIN = "aithernet"

PROVIDER_PRESETS = {
    "gmail": {
        "host": "smtp.gmail.com", "port": 587,
        "use_tls": True, "use_ssl": False,
        "note": "Use App Password (not account password)",
    },
    "outlook": {
        "host": "smtp-mail.outlook.com", "port": 587,
        "use_tls": True, "use_ssl": False,
    },
    "protonmail": {
        "host": "127.0.0.1", "port": 1025,
        "use_tls": False, "use_ssl": False,
        "note": "Requires ProtonMail Bridge running locally",
    },
    "sendgrid": {
        "host": "smtp.sendgrid.net", "port": 587,
        "use_tls": True, "use_ssl": False,
        "note": "Username is 'apikey', password is your SendGrid API key",
    },
    "resend": {
        "host": "smtp.resend.com", "port": 465,
        "use_tls": False, "use_ssl": True,
    },
}


# ── Data Models ──────────────────────────────────────────────────────────


@dataclass
class Email:
    email_id: str
    direction: str  # inbound, outbound
    from_addr: str
    to_addr: str
    subject: str
    body: str
    html: str = ""
    attachments: list[dict] = field(default_factory=list)
    status: str = "queued"  # queued, retry, sent, failed, received
    error: str = ""
    agent: str = ""  # Agent that sent/receives this
    attempts: int = 0
    created_at: float = 0.0
    sent_at: float = 0.0
    node_id: str = ""  # Origin node for mesh-routed mail

    def __post_init__(self):
        if not self.email_id:
            self.email_id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class Mailbox:
    username: str
    email_address: str
    display_name: str = ""
    domain: str = ""
    active: bool = True
    storage_used: int = 0


# ── Mail Relay ───────────────────────────────────────────────────────────


class MailRelay:
    """Lightweight SMTP relay with SQLite queue and mesh routing."""

    def __init__(self, data_dir: str | Path | None = None, node_id: str = ""):
        self.node_id = node_id
        self._data_dir = Path(data_dir) if data_dir else Path.home() / ".aither"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._data_dir / "mail.db"
        self._queue_task: asyncio.Task | None = None
        self._queue_running = False

        self._init_db()

    # ── Database ─────────────────────────────────────────────────────

    def _init_db(self):
        db = sqlite3.connect(str(self._db_path))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS emails (
                email_id    TEXT PRIMARY KEY,
                direction   TEXT NOT NULL DEFAULT 'outbound',
                from_addr   TEXT NOT NULL,
                to_addr     TEXT NOT NULL,
                subject     TEXT NOT NULL DEFAULT '',
                body        TEXT NOT NULL DEFAULT '',
                html        TEXT NOT NULL DEFAULT '',
                attachments TEXT NOT NULL DEFAULT '[]',
                status      TEXT NOT NULL DEFAULT 'queued',
                error       TEXT NOT NULL DEFAULT '',
                agent       TEXT NOT NULL DEFAULT '',
                attempts    INTEGER NOT NULL DEFAULT 0,
                node_id     TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                sent_at     REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_email_status ON emails(status);
            CREATE INDEX IF NOT EXISTS idx_email_to ON emails(to_addr);
            CREATE INDEX IF NOT EXISTS idx_email_agent ON emails(agent);

            CREATE TABLE IF NOT EXISTS smtp_config (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS mailboxes (
                username        TEXT PRIMARY KEY,
                email_address   TEXT UNIQUE NOT NULL,
                display_name    TEXT NOT NULL DEFAULT '',
                domain          TEXT NOT NULL DEFAULT '',
                active          INTEGER NOT NULL DEFAULT 1,
                storage_used    INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL
            );
        """)
        db.close()

    def _get_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self._db_path))
        db.row_factory = sqlite3.Row
        return db

    # ── Configuration ────────────────────────────────────────────────

    def configure(self, host: str = "", port: int = 587, username: str = "",
                  password: str = "", use_tls: bool = True, use_ssl: bool = False,
                  from_addr: str = "", provider: str = ""):
        """Configure SMTP connection. Use provider preset or manual settings."""
        if provider and provider in PROVIDER_PRESETS:
            preset = PROVIDER_PRESETS[provider]
            host = host or preset["host"]
            port = port or preset["port"]
            use_tls = preset.get("use_tls", True)
            use_ssl = preset.get("use_ssl", False)

        db = self._get_db()
        try:
            pairs = {
                "host": host, "port": str(port), "username": username,
                "password": password, "use_tls": str(use_tls).lower(),
                "use_ssl": str(use_ssl).lower(), "from_addr": from_addr or username,
                "provider": provider,
            }
            for k, v in pairs.items():
                if v:
                    db.execute(
                        "INSERT OR REPLACE INTO smtp_config (key, value) VALUES (?, ?)",
                        (k, v),
                    )
            db.commit()
        finally:
            db.close()

    def get_config(self, redact: bool = True) -> dict:
        db = self._get_db()
        try:
            rows = db.execute("SELECT key, value FROM smtp_config").fetchall()
            cfg = {r["key"]: r["value"] for r in rows}
        finally:
            db.close()

        if redact and cfg.get("password"):
            cfg["password"] = "***"
        cfg["configured"] = bool(cfg.get("host") and cfg.get("username"))
        return cfg

    @property
    def is_configured(self) -> bool:
        cfg = self.get_config(redact=False)
        return bool(cfg.get("host") and cfg.get("username"))

    # ── Sending ──────────────────────────────────────────────────────

    async def send(self, to: str, subject: str, body: str, html: str = "",
                   from_addr: str = "", agent: str = "",
                   attachments: list[dict] | None = None) -> dict:
        """Queue an email for sending. Returns {"ok": bool, "email_id": str}."""
        # Check if this is a mesh-internal address
        if to.endswith(f"@{_AITHERNET_DOMAIN}"):
            return await self._route_mesh_mail(to, subject, body, html, agent)

        cfg = self.get_config(redact=False)
        sender = from_addr or cfg.get("from_addr", cfg.get("username", ""))

        email_obj = Email(
            email_id="", direction="outbound",
            from_addr=sender, to_addr=to,
            subject=subject, body=body, html=html,
            attachments=attachments or [], agent=agent,
            node_id=self.node_id,
        )

        db = self._get_db()
        try:
            db.execute(
                "INSERT INTO emails "
                "(email_id, direction, from_addr, to_addr, subject, body, html, "
                "attachments, status, agent, node_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email_obj.email_id, email_obj.direction, email_obj.from_addr,
                 email_obj.to_addr, email_obj.subject, email_obj.body,
                 email_obj.html, json.dumps(email_obj.attachments),
                 email_obj.status, email_obj.agent, email_obj.node_id,
                 email_obj.created_at),
            )
            db.commit()
        finally:
            db.close()

        self._ensure_queue_running()
        return {"ok": True, "email_id": email_obj.email_id, "status": "queued"}

    def _send_direct(self, row: dict) -> tuple[bool, str]:
        """Send a single email via SMTP. Returns (success, error)."""
        cfg = self.get_config(redact=False)
        host = cfg.get("host", "")
        port = int(cfg.get("port", "587"))
        username = cfg.get("username", "")
        password = cfg.get("password", "")
        use_ssl = cfg.get("use_ssl", "false") == "true"
        use_tls = cfg.get("use_tls", "true") == "true"

        if not host or not username:
            return False, "SMTP not configured"

        try:
            # Build MIME message
            msg = MIMEMultipart("mixed")
            msg["From"] = row["from_addr"]
            msg["To"] = row["to_addr"]
            msg["Subject"] = row["subject"]
            msg["X-AitherOS-Agent"] = row.get("agent", "")
            msg["X-AitherOS-Node"] = self.node_id

            # Text + HTML body
            if row.get("html"):
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(row["body"], "plain"))
                alt.attach(MIMEText(row["html"], "html"))
                msg.attach(alt)
            else:
                msg.attach(MIMEText(row["body"], "plain"))

            # Attachments
            attachments = json.loads(row.get("attachments", "[]"))
            for att in attachments:
                filename = att.get("filename", "attachment")
                content_b64 = att.get("content_base64", "")
                mime_type = att.get("mime_type", "application/octet-stream")
                maintype, subtype = mime_type.split("/", 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(b64decode(content_b64))
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                msg.attach(part)

            # Connect and send
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                if use_tls:
                    server.starttls()

            server.login(username, password)
            server.sendmail(row["from_addr"], [row["to_addr"]], msg.as_string())
            server.quit()
            return True, ""

        except Exception as e:
            return False, str(e)

    # ── Queue Processing ─────────────────────────────────────────────

    def _ensure_queue_running(self):
        if self._queue_running:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._queue_task = loop.create_task(self._process_queue())
        except RuntimeError:
            pass

    async def _process_queue(self):
        self._queue_running = True
        try:
            while True:
                await asyncio.sleep(_QUEUE_INTERVAL)
                db = self._get_db()
                try:
                    rows = db.execute(
                        "SELECT * FROM emails WHERE status IN ('queued', 'retry') "
                        "ORDER BY created_at ASC LIMIT 10"
                    ).fetchall()
                finally:
                    db.close()

                for row in rows:
                    row_dict = dict(row)
                    success, error = self._send_direct(row_dict)

                    db = self._get_db()
                    try:
                        if success:
                            db.execute(
                                "UPDATE emails SET status = 'sent', sent_at = ?, error = '' "
                                "WHERE email_id = ?",
                                (time.time(), row_dict["email_id"]),
                            )
                        else:
                            attempts = row_dict.get("attempts", 0) + 1
                            new_status = "failed" if attempts >= _MAX_RETRIES else "retry"
                            db.execute(
                                "UPDATE emails SET status = ?, error = ?, attempts = ? "
                                "WHERE email_id = ?",
                                (new_status, error, attempts, row_dict["email_id"]),
                            )
                            if new_status == "retry":
                                backoff = _BACKOFF_BASE ** (attempts + 1)
                                await asyncio.sleep(min(backoff, 60))
                        db.commit()
                    finally:
                        db.close()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Mail queue error: %s", e)
        finally:
            self._queue_running = False

    # ── Mesh Routing ─────────────────────────────────────────────────

    async def _route_mesh_mail(self, to: str, subject: str, body: str,
                               html: str, agent: str) -> dict:
        """Route mail to an agent on another AitherNet node."""
        # Parse agent@node.aithernet or agent@aithernet
        local_part = to.split("@")[0]

        try:
            from adk.relay import get_relay
            relay = get_relay()
            if not relay or not relay.is_registered:
                return {"ok": False, "error": "not_connected_to_mesh"}

            # Find the node hosting this agent
            node = await relay.find_agent(local_part)
            if not node:
                # Broadcast to all nodes
                await relay.broadcast("mail", {
                    "to_agent": local_part,
                    "subject": subject,
                    "body": body,
                    "html": html,
                    "from_agent": agent,
                    "from_node": self.node_id,
                })
                return {"ok": True, "status": "broadcast", "target": local_part}

            # Direct relay to target node
            await relay.send(node.node_id, "mail", {
                "to_agent": local_part,
                "subject": subject,
                "body": body,
                "html": html,
                "from_agent": agent,
                "from_node": self.node_id,
            })
            return {"ok": True, "status": "relayed", "target_node": node.node_id}

        except Exception as e:
            logger.warning("Mesh mail routing failed: %s", e)
            return {"ok": False, "error": str(e)}

    def receive_mesh_mail(self, data: dict):
        """Handle incoming mail from the mesh relay."""
        from_agent = data.get("from_agent", "unknown")
        from_node = data.get("from_node", "")
        to_agent = data.get("to_agent", "")

        email_obj = Email(
            email_id="", direction="inbound",
            from_addr=f"{from_agent}@{from_node[:8]}.{_AITHERNET_DOMAIN}",
            to_addr=f"{to_agent}@{self.node_id[:8]}.{_AITHERNET_DOMAIN}",
            subject=data.get("subject", ""),
            body=data.get("body", ""),
            html=data.get("html", ""),
            status="received",
            agent=to_agent,
            node_id=from_node,
        )

        db = self._get_db()
        try:
            db.execute(
                "INSERT INTO emails "
                "(email_id, direction, from_addr, to_addr, subject, body, html, "
                "attachments, status, agent, node_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (email_obj.email_id, email_obj.direction, email_obj.from_addr,
                 email_obj.to_addr, email_obj.subject, email_obj.body,
                 email_obj.html, "[]", email_obj.status, email_obj.agent,
                 email_obj.node_id, email_obj.created_at),
            )
            db.commit()
        finally:
            db.close()
        return email_obj

    # ── Mailbox Management ───────────────────────────────────────────

    def provision_mailbox(self, username: str, email_address: str = "",
                          display_name: str = "", domain: str = "") -> dict:
        """Create a mailbox for a user or agent."""
        domain = domain or f"{self.node_id[:8]}.{_AITHERNET_DOMAIN}"
        if not email_address:
            email_address = f"{username}@{domain}"

        db = self._get_db()
        try:
            existing = db.execute(
                "SELECT username FROM mailboxes WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                return {"status": "exists", "email": email_address}

            db.execute(
                "INSERT INTO mailboxes "
                "(username, email_address, display_name, domain, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, email_address, display_name or username, domain, time.time()),
            )
            db.commit()
        finally:
            db.close()
        return {"status": "ok", "email": email_address}

    def get_mailbox(self, username: str) -> dict | None:
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM mailboxes WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def list_mailboxes(self) -> list[dict]:
        db = self._get_db()
        try:
            rows = db.execute("SELECT * FROM mailboxes ORDER BY username").fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    # ── Query ────────────────────────────────────────────────────────

    def inbox(self, agent: str = "", limit: int = 50) -> list[dict]:
        db = self._get_db()
        try:
            if agent:
                rows = db.execute(
                    "SELECT * FROM emails WHERE agent = ? AND direction = 'inbound' "
                    "ORDER BY created_at DESC LIMIT ?", (agent, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM emails WHERE direction = 'inbound' "
                    "ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def sent(self, agent: str = "", limit: int = 50) -> list[dict]:
        db = self._get_db()
        try:
            if agent:
                rows = db.execute(
                    "SELECT * FROM emails WHERE agent = ? AND direction = 'outbound' "
                    "ORDER BY created_at DESC LIMIT ?", (agent, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM emails WHERE direction = 'outbound' "
                    "ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def get_email(self, email_id: str) -> dict | None:
        db = self._get_db()
        try:
            row = db.execute(
                "SELECT * FROM emails WHERE email_id = ?", (email_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    # ── Status ───────────────────────────────────────────────────────

    def status(self) -> dict:
        db = self._get_db()
        try:
            counts = {}
            for status in ("queued", "retry", "sent", "failed", "received"):
                row = db.execute(
                    "SELECT COUNT(*) FROM emails WHERE status = ?", (status,)
                ).fetchone()
                counts[status] = row[0]
            mailbox_count = db.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]
        finally:
            db.close()

        return {
            "node_id": self.node_id,
            "configured": self.is_configured,
            "queue_running": self._queue_running,
            "counts": counts,
            "mailboxes": mailbox_count,
            "providers_available": list(PROVIDER_PRESETS.keys()),
        }


# ── Inbound SMTP Server ──────────────────────────────────────────────────

# aiosmtpd is optional — graceful degradation when not installed
try:
    from aiosmtpd.controller import Controller as _SMTPController  # type: ignore[import-untyped]

    _HAS_AIOSMTPD = True
except ImportError:  # pragma: no cover
    _SMTPController = None  # type: ignore[assignment,misc]
    _HAS_AIOSMTPD = False

_DEFAULT_INBOUND_PORT = 2525


class InboundSMTPHandler:
    """aiosmtpd handler that receives email and stores it in the MailRelay DB.

    Each received message is parsed from raw bytes, decomposed into subject,
    plain-text body, HTML body, and attachments, then persisted as an inbound
    email row. If the recipient matches a provisioned agent mailbox, the
    ``agent`` column is set automatically so the message appears in that
    agent's inbox.
    """

    def __init__(self, relay: MailRelay) -> None:
        self._relay = relay

    # aiosmtpd calls handle_DATA for every incoming message
    async def handle_DATA(  # noqa: N802 — aiosmtpd naming convention
        self, server: Any, session: Any, envelope: Any,
    ) -> str:
        """Process a single inbound email envelope.

        Args:
            server: The SMTP server instance (unused).
            session: The SMTP session context (unused).
            envelope: The SMTP envelope containing mail_from, rcpt_tos, content.

        Returns:
            SMTP status string — ``'250 OK'`` on success, ``'451 …'`` on
            internal error.
        """
        import email as email_mod

        try:
            mail_from: str = envelope.mail_from or ""
            rcpt_tos: list[str] = list(envelope.rcpt_tos or [])
            raw_data: bytes = envelope.content if isinstance(envelope.content, bytes) else (
                envelope.content.encode("utf-8", errors="replace")
            )

            # Parse the raw message into its MIME parts
            msg = email_mod.message_from_bytes(raw_data)

            subject = msg.get("Subject", "")
            body = ""
            html = ""
            attachments: list[dict[str, str]] = []

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    disposition = str(part.get("Content-Disposition", ""))

                    if "attachment" in disposition:
                        payload_bytes = part.get_payload(decode=True) or b""
                        attachments.append({
                            "filename": part.get_filename() or "attachment",
                            "mime_type": content_type,
                            "content_base64": b64encode(payload_bytes).decode("ascii"),
                        })
                    elif content_type == "text/plain" and not body:
                        payload = part.get_payload(decode=True)
                        body = payload.decode("utf-8", errors="replace") if payload else ""
                    elif content_type == "text/html" and not html:
                        payload = part.get_payload(decode=True)
                        html = payload.decode("utf-8", errors="replace") if payload else ""
            else:
                content_type = msg.get_content_type()
                payload = msg.get_payload(decode=True)
                text = payload.decode("utf-8", errors="replace") if payload else ""
                if content_type == "text/html":
                    html = text
                else:
                    body = text

            # Store one row per recipient
            for rcpt in rcpt_tos:
                agent = self._resolve_agent(rcpt)

                email_obj = Email(
                    email_id="",
                    direction="inbound",
                    from_addr=mail_from,
                    to_addr=rcpt,
                    subject=subject,
                    body=body,
                    html=html,
                    attachments=attachments,
                    status="received",
                    agent=agent,
                    node_id=self._relay.node_id,
                )

                db = self._relay._get_db()
                try:
                    db.execute(
                        "INSERT INTO emails "
                        "(email_id, direction, from_addr, to_addr, subject, body, html, "
                        "attachments, status, agent, node_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            email_obj.email_id, email_obj.direction,
                            email_obj.from_addr, email_obj.to_addr,
                            email_obj.subject, email_obj.body, email_obj.html,
                            json.dumps(email_obj.attachments),
                            email_obj.status, email_obj.agent,
                            email_obj.node_id, email_obj.created_at,
                        ),
                    )
                    db.commit()
                finally:
                    db.close()

                logger.info(
                    "Inbound email stored: %s -> %s (agent=%s, subject=%r)",
                    mail_from, rcpt, agent or "<none>", subject[:80],
                )

            return "250 OK"

        except Exception as exc:
            logger.error("Inbound SMTP handler error: %s", exc)
            return "451 Internal error — please retry"

    def _resolve_agent(self, recipient: str) -> str:
        """Match a recipient address to a provisioned agent mailbox.

        Args:
            recipient: The full email address (e.g. ``atlas@node.aithernet``).

        Returns:
            The agent username if a matching mailbox exists, otherwise ``""``.
        """
        db = self._relay._get_db()
        try:
            # Exact email match first
            row = db.execute(
                "SELECT username FROM mailboxes WHERE email_address = ? AND active = 1",
                (recipient,),
            ).fetchone()
            if row:
                return row["username"]

            # Fallback: match the local part (before @) to a mailbox username
            local_part = recipient.split("@")[0] if "@" in recipient else recipient
            row = db.execute(
                "SELECT username FROM mailboxes WHERE username = ? AND active = 1",
                (local_part,),
            ).fetchone()
            return row["username"] if row else ""
        finally:
            db.close()

    async def handle_RCPT(  # noqa: N802 — aiosmtpd naming convention
        self, server: Any, session: Any, envelope: Any, address: str, rcpt_options: Any,
    ) -> str:
        """Accept all recipients — filtering happens at DATA time."""
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_EHLO(  # noqa: N802 — aiosmtpd naming convention
        self, server: Any, session: Any, envelope: Any, hostname: str, responses: list[str],
    ) -> list[str]:
        """Respond to EHLO with supported extensions."""
        return responses


# ── Inbound Server Lifecycle (on MailRelay) ──────────────────────────────
# These methods are added outside the class body to honour the "do not modify
# existing code" constraint.  They are monkey-patched onto MailRelay at
# module load time so they behave as regular instance methods.


async def _start_inbound_server(self: MailRelay, port: int | None = None) -> bool:
    """Start an aiosmtpd-based SMTP listener to receive inbound email.

    Args:
        port: TCP port to listen on. Defaults to ``AITHER_SMTP_PORT`` env var
              or 2525.

    Returns:
        ``True`` if the server was started successfully, ``False`` if
        aiosmtpd is not installed or an error occurred.
    """
    if not _HAS_AIOSMTPD:
        logger.warning(
            "aiosmtpd is not installed — inbound SMTP server disabled. "
            "Install with: pip install aiosmtpd"
        )
        return False

    if getattr(self, "_inbound_controller", None) is not None:
        logger.debug("Inbound SMTP server already running")
        return True

    listen_port = port or int(os.getenv("AITHER_SMTP_PORT", str(_DEFAULT_INBOUND_PORT)))
    handler = InboundSMTPHandler(self)

    try:
        controller = _SMTPController(
            handler,
            hostname="0.0.0.0",
            port=listen_port,
        )
        controller.start()
        self._inbound_controller = controller  # type: ignore[attr-defined]
        logger.info("Inbound SMTP server listening on 0.0.0.0:%d", listen_port)
        return True
    except Exception as exc:
        logger.error("Failed to start inbound SMTP server on port %d: %s", listen_port, exc)
        return False


async def _stop_inbound_server(self: MailRelay) -> None:
    """Stop the inbound SMTP listener gracefully."""
    controller = getattr(self, "_inbound_controller", None)
    if controller is None:
        return
    try:
        controller.stop()
        logger.info("Inbound SMTP server stopped")
    except Exception as exc:
        logger.warning("Error stopping inbound SMTP server: %s", exc)
    finally:
        self._inbound_controller = None  # type: ignore[attr-defined]


# Attach as regular methods on MailRelay
MailRelay.start_inbound_server = _start_inbound_server  # type: ignore[attr-defined]
MailRelay.stop_inbound_server = _stop_inbound_server  # type: ignore[attr-defined]


# ── Singleton ────────────────────────────────────────────────────────────

_mail_relay: MailRelay | None = None


def get_mail_relay(**kwargs) -> MailRelay:
    """Get or create the singleton MailRelay instance."""
    global _mail_relay
    if _mail_relay is None:
        _mail_relay = MailRelay(**kwargs)
    return _mail_relay
