"""Tests for the inbound SMTP server (aiosmtpd handler)."""

import json
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from base64 import b64encode
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.smtp import MailRelay, InboundSMTPHandler


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_envelope(mail_from: str, rcpt_tos: list[str], content: bytes) -> MagicMock:
    env = MagicMock()
    env.mail_from = mail_from
    env.rcpt_tos = rcpt_tos
    env.content = content
    return env


def _build_simple_email(from_addr: str, to_addr: str, subject: str, body: str) -> bytes:
    msg = MIMEText(body, "plain")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    return msg.as_bytes()


def _build_html_email(from_addr: str, to_addr: str, subject: str,
                      plain: str, html: str) -> bytes:
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg.as_bytes()


def _build_email_with_attachment(from_addr: str, to_addr: str, subject: str,
                                 body: str, filename: str, content: bytes) -> bytes:
    msg = MIMEMultipart("mixed")
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(content)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    return msg.as_bytes()


# ── Handler Tests ────────────────────────────────────────────────────────


class TestInboundHandler:
    @pytest.mark.asyncio
    async def test_receive_plain_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        handler = InboundSMTPHandler(mail)

        content = _build_simple_email(
            "sender@example.com", "agent@node.aithernet",
            "Test Subject", "Hello from SMTP!",
        )
        envelope = _make_envelope(
            "sender@example.com", ["agent@node.aithernet"], content,
        )

        result = await handler.handle_DATA(None, None, envelope)
        assert result == "250 OK"

        inbox = mail.inbox()
        assert len(inbox) == 1
        assert inbox[0]["from_addr"] == "sender@example.com"
        assert inbox[0]["subject"] == "Test Subject"
        assert inbox[0]["body"] == "Hello from SMTP!"
        assert inbox[0]["direction"] == "inbound"
        assert inbox[0]["status"] == "received"

    @pytest.mark.asyncio
    async def test_receive_html_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        content = _build_html_email(
            "sender@example.com", "agent@node.aithernet",
            "HTML Test", "Plain text version", "<b>Bold version</b>",
        )
        envelope = _make_envelope(
            "sender@example.com", ["agent@node.aithernet"], content,
        )

        result = await handler.handle_DATA(None, None, envelope)
        assert result == "250 OK"

        inbox = mail.inbox()
        assert inbox[0]["body"] == "Plain text version"
        assert inbox[0]["html"] == "<b>Bold version</b>"

    @pytest.mark.asyncio
    async def test_receive_email_with_attachment(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        file_content = b"Hello, this is a test file."
        content = _build_email_with_attachment(
            "sender@example.com", "agent@node.aithernet",
            "Attachment Test", "See attached.", "test.txt", file_content,
        )
        envelope = _make_envelope(
            "sender@example.com", ["agent@node.aithernet"], content,
        )

        result = await handler.handle_DATA(None, None, envelope)
        assert result == "250 OK"

        inbox = mail.inbox()
        attachments = json.loads(inbox[0]["attachments"])
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "test.txt"
        assert attachments[0]["content_base64"] == b64encode(file_content).decode("ascii")

    @pytest.mark.asyncio
    async def test_multiple_recipients(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        content = _build_simple_email(
            "sender@example.com", "agent1@node, agent2@node",
            "Multi", "To both",
        )
        envelope = _make_envelope(
            "sender@example.com",
            ["agent1@node.aithernet", "agent2@node.aithernet"],
            content,
        )

        result = await handler.handle_DATA(None, None, envelope)
        assert result == "250 OK"

        inbox = mail.inbox()
        assert len(inbox) == 2
        to_addrs = {e["to_addr"] for e in inbox}
        assert "agent1@node.aithernet" in to_addrs
        assert "agent2@node.aithernet" in to_addrs


class TestAgentResolution:
    @pytest.mark.asyncio
    async def test_resolve_by_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        mail.provision_mailbox("aither", email_address="aither@test-node.aithernet")
        handler = InboundSMTPHandler(mail)

        content = _build_simple_email(
            "user@example.com", "aither@test-node.aithernet",
            "Agent Mail", "For Aither",
        )
        envelope = _make_envelope(
            "user@example.com", ["aither@test-node.aithernet"], content,
        )

        await handler.handle_DATA(None, None, envelope)

        inbox = mail.inbox(agent="aither")
        assert len(inbox) == 1
        assert inbox[0]["agent"] == "aither"

    @pytest.mark.asyncio
    async def test_resolve_by_username(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        mail.provision_mailbox("atlas")
        handler = InboundSMTPHandler(mail)

        content = _build_simple_email(
            "user@example.com", "atlas@some-domain.com",
            "Agent Mail", "For Atlas",
        )
        envelope = _make_envelope(
            "user@example.com", ["atlas@some-domain.com"], content,
        )

        await handler.handle_DATA(None, None, envelope)

        inbox = mail.inbox(agent="atlas")
        assert len(inbox) == 1

    @pytest.mark.asyncio
    async def test_no_agent_match(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        content = _build_simple_email(
            "user@example.com", "nobody@nowhere.com",
            "No Agent", "Unmatched",
        )
        envelope = _make_envelope(
            "user@example.com", ["nobody@nowhere.com"], content,
        )

        await handler.handle_DATA(None, None, envelope)

        inbox = mail.inbox()
        assert len(inbox) == 1
        assert inbox[0]["agent"] == ""


class TestHandlerProtocol:
    @pytest.mark.asyncio
    async def test_handle_rcpt(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        envelope = MagicMock()
        envelope.rcpt_tos = []

        result = await handler.handle_RCPT(None, None, envelope, "user@test.com", None)
        assert result == "250 OK"
        assert "user@test.com" in envelope.rcpt_tos

    @pytest.mark.asyncio
    async def test_handle_ehlo(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        responses = ["250-server", "250 OK"]
        result = await handler.handle_EHLO(None, None, None, "client.example.com", responses)
        assert result == responses


class TestInboundServerLifecycle:
    @pytest.mark.asyncio
    async def test_start_without_aiosmtpd(self, tmp_path):
        """When aiosmtpd is not installed, start_inbound_server returns False."""
        import adk.smtp as smtp_mod
        original = smtp_mod._HAS_AIOSMTPD

        smtp_mod._HAS_AIOSMTPD = False
        try:
            mail = MailRelay(data_dir=tmp_path)
            result = await mail.start_inbound_server(port=2525)
            assert result is False
        finally:
            smtp_mod._HAS_AIOSMTPD = original

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, tmp_path):
        """stop_inbound_server should not raise when not started."""
        mail = MailRelay(data_dir=tmp_path)
        await mail.stop_inbound_server()  # Should not raise


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_malformed_content(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        # Send garbage bytes that can still be parsed as a message
        envelope = _make_envelope(
            "sender@example.com", ["agent@node.aithernet"],
            b"Not a valid MIME message but still bytes",
        )

        result = await handler.handle_DATA(None, None, envelope)
        # Should still succeed — email module parses best-effort
        assert result == "250 OK"

    @pytest.mark.asyncio
    async def test_string_content_fallback(self, tmp_path):
        """Some SMTP implementations send content as string instead of bytes."""
        mail = MailRelay(data_dir=tmp_path)
        handler = InboundSMTPHandler(mail)

        envelope = MagicMock()
        envelope.mail_from = "sender@example.com"
        envelope.rcpt_tos = ["agent@node.aithernet"]
        envelope.content = "Subject: String Test\r\n\r\nBody as string"

        result = await handler.handle_DATA(None, None, envelope)
        assert result == "250 OK"

        inbox = mail.inbox()
        assert len(inbox) == 1
        assert inbox[0]["subject"] == "String Test"
