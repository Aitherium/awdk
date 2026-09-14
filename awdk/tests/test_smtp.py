"""Tests for the AitherNet mail relay system."""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.smtp import MailRelay, Email, Mailbox, PROVIDER_PRESETS, get_mail_relay


# ── Configuration ────────────────────────────────────────────────────────


class TestConfiguration:
    def test_default_not_configured(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        assert mail.is_configured is False

    def test_configure_manual(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(
            host="smtp.example.com", port=587,
            username="user@example.com", password="secret",
        )
        assert mail.is_configured is True
        cfg = mail.get_config()
        assert cfg["host"] == "smtp.example.com"
        assert cfg["port"] == "587"

    def test_configure_provider_preset(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(provider="gmail", username="user@gmail.com", password="app-pass")
        cfg = mail.get_config(redact=False)
        assert cfg["host"] == "smtp.gmail.com"
        assert cfg["provider"] == "gmail"

    def test_config_redaction(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="secret123")
        cfg = mail.get_config(redact=True)
        assert cfg["password"] == "***"

    def test_config_unredacted(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="real-pass")
        cfg = mail.get_config(redact=False)
        assert cfg["password"] == "real-pass"

    def test_config_persists(self, tmp_path):
        mail1 = MailRelay(data_dir=tmp_path)
        mail1.configure(host="smtp.test.com", username="u", password="p")

        mail2 = MailRelay(data_dir=tmp_path)
        assert mail2.is_configured is True
        cfg = mail2.get_config(redact=False)
        assert cfg["host"] == "smtp.test.com"

    def test_provider_presets(self):
        assert "gmail" in PROVIDER_PRESETS
        assert "sendgrid" in PROVIDER_PRESETS
        assert "resend" in PROVIDER_PRESETS
        assert PROVIDER_PRESETS["gmail"]["host"] == "smtp.gmail.com"


# ── Email Queuing ────────────────────────────────────────────────────────


class TestQueuing:
    @pytest.mark.asyncio
    async def test_queue_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        mail.configure(host="smtp.test.com", username="u", password="p")
        result = await mail.send(
            to="user@example.com", subject="Test", body="Hello!",
        )
        assert result["ok"] is True
        assert result["status"] == "queued"
        assert result["email_id"] != ""

    @pytest.mark.asyncio
    async def test_queued_email_in_sent(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        mail.configure(host="smtp.test.com", username="u", password="p")
        await mail.send(to="user@example.com", subject="Test", body="Hello!")
        sent = mail.sent()
        assert len(sent) == 1
        assert sent[0]["to_addr"] == "user@example.com"
        assert sent[0]["status"] == "queued"

    @pytest.mark.asyncio
    async def test_send_with_agent(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="p")
        await mail.send(to="user@example.com", subject="Agent mail", body="Hi", agent="aither")
        sent = mail.sent(agent="aither")
        assert len(sent) == 1
        assert sent[0]["agent"] == "aither"

    @pytest.mark.asyncio
    async def test_send_with_html(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="p")
        await mail.send(
            to="user@example.com", subject="HTML",
            body="Plain text", html="<b>Bold</b>",
        )
        email_list = mail.sent()
        assert email_list[0]["html"] == "<b>Bold</b>"


# ── Direct Send ──────────────────────────────────────────────────────────


class TestDirectSend:
    def test_send_not_configured(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        success, error = mail._send_direct({
            "from_addr": "a@b.com", "to_addr": "c@d.com",
            "subject": "test", "body": "body",
            "attachments": "[]", "agent": "",
        })
        assert success is False
        assert "not configured" in error.lower()

    def test_send_success(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", port=587, username="u", password="p")

        with patch("smtplib.SMTP") as MockSMTP:
            server = MagicMock()
            MockSMTP.return_value = server

            success, error = mail._send_direct({
                "from_addr": "u@test.com", "to_addr": "r@test.com",
                "subject": "test", "body": "hello",
                "html": "", "attachments": "[]", "agent": "",
            })

        assert success is True
        assert error == ""
        server.login.assert_called_once()
        server.sendmail.assert_called_once()

    def test_send_with_ssl(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", port=465, username="u", password="p",
                       use_ssl=True, use_tls=False)

        with patch("smtplib.SMTP_SSL") as MockSMTP:
            server = MagicMock()
            MockSMTP.return_value = server

            success, error = mail._send_direct({
                "from_addr": "u@test.com", "to_addr": "r@test.com",
                "subject": "test", "body": "hello",
                "html": "", "attachments": "[]", "agent": "",
            })

        assert success is True
        MockSMTP.assert_called_once()

    def test_send_failure(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", port=587, username="u", password="p")

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.side_effect = Exception("connection refused")
            success, error = mail._send_direct({
                "from_addr": "u@test.com", "to_addr": "r@test.com",
                "subject": "test", "body": "hello",
                "html": "", "attachments": "[]", "agent": "",
            })

        assert success is False
        assert "connection refused" in error

    def test_send_with_attachments(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", port=587, username="u", password="p")

        with patch("smtplib.SMTP") as MockSMTP:
            server = MagicMock()
            MockSMTP.return_value = server

            attachments = json.dumps([{
                "filename": "test.txt",
                "content_base64": "SGVsbG8=",  # "Hello" in base64
                "mime_type": "text/plain",
            }])

            success, error = mail._send_direct({
                "from_addr": "u@test.com", "to_addr": "r@test.com",
                "subject": "test", "body": "see attached",
                "html": "", "attachments": attachments, "agent": "",
            })

        assert success is True

    def test_send_with_html_body(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", port=587, username="u", password="p")

        with patch("smtplib.SMTP") as MockSMTP:
            server = MagicMock()
            MockSMTP.return_value = server

            success, _ = mail._send_direct({
                "from_addr": "u@test.com", "to_addr": "r@test.com",
                "subject": "test", "body": "plain", "html": "<b>bold</b>",
                "attachments": "[]", "agent": "",
            })

        assert success is True
        # Verify sendmail was called with MIME message containing alternative part
        call_args = server.sendmail.call_args[0]
        msg_str = call_args[2]
        assert "multipart/alternative" in msg_str


# ── Mesh Routing ─────────────────────────────────────────────────────────


class TestMeshRouting:
    @pytest.mark.asyncio
    async def test_mesh_mail_route(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="local-node")

        with patch("adk.smtp.get_mail_relay") as _:
            mock_relay = MagicMock()
            mock_relay.is_registered = True
            mock_relay.find_agent = AsyncMock(return_value=MagicMock(node_id="remote-node"))
            mock_relay.send = AsyncMock(return_value=True)

            with patch("adk.relay.get_relay", return_value=mock_relay):
                result = await mail.send(
                    to="aither@aithernet", subject="Mesh mail", body="Hello mesh!",
                    agent="lyra",
                )

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_mesh_mail_broadcast_fallback(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="local-node")

        mock_relay = MagicMock()
        mock_relay.is_registered = True
        mock_relay.find_agent = AsyncMock(return_value=None)  # Agent not found
        mock_relay.broadcast = AsyncMock(return_value=True)

        with patch("adk.relay.get_relay", return_value=mock_relay):
            result = await mail.send(
                to="unknown@aithernet", subject="Broadcast", body="Anyone?",
            )

        assert result["ok"] is True
        assert result["status"] == "broadcast"

    @pytest.mark.asyncio
    async def test_mesh_mail_no_relay(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="local-node")

        with patch("adk.relay.get_relay", return_value=None):
            result = await mail.send(
                to="agent@aithernet", subject="No relay", body="fail",
            )

        assert result["ok"] is False

    def test_receive_mesh_mail(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="local-node")
        email_obj = mail.receive_mesh_mail({
            "from_agent": "lyra",
            "from_node": "remote-node-id",
            "to_agent": "aither",
            "subject": "Mesh incoming",
            "body": "Hello from the mesh!",
        })
        assert email_obj.direction == "inbound"
        assert email_obj.agent == "aither"

        inbox = mail.inbox(agent="aither")
        assert len(inbox) == 1
        assert inbox[0]["subject"] == "Mesh incoming"


# ── Mailbox Management ──────────────────────────────────────────────────


class TestMailboxes:
    def test_provision_mailbox(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="abcd1234")
        result = mail.provision_mailbox("aither")
        assert result["status"] == "ok"
        assert "aither@" in result["email"]

    def test_provision_duplicate(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="abcd1234")
        mail.provision_mailbox("aither")
        result = mail.provision_mailbox("aither")
        assert result["status"] == "exists"

    def test_provision_custom_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        result = mail.provision_mailbox(
            "aither", email_address="aither@custom.domain", domain="custom.domain",
        )
        assert result["email"] == "aither@custom.domain"

    def test_get_mailbox(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="abcd1234")
        mail.provision_mailbox("aither", display_name="Aither Agent")
        mbx = mail.get_mailbox("aither")
        assert mbx is not None
        assert mbx["display_name"] == "Aither Agent"

    def test_get_nonexistent_mailbox(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        assert mail.get_mailbox("nobody") is None

    def test_list_mailboxes(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="abcd1234")
        mail.provision_mailbox("aither")
        mail.provision_mailbox("lyra")
        mailboxes = mail.list_mailboxes()
        assert len(mailboxes) == 2
        names = [m["username"] for m in mailboxes]
        assert "aither" in names
        assert "lyra" in names


# ── Query ────────────────────────────────────────────────────────────────


class TestQuery:
    @pytest.mark.asyncio
    async def test_inbox_empty(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        assert mail.inbox() == []

    @pytest.mark.asyncio
    async def test_sent_empty(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        assert mail.sent() == []

    @pytest.mark.asyncio
    async def test_get_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="p")
        result = await mail.send(to="user@test.com", subject="Find me", body="body")
        email_id = result["email_id"]
        email_obj = mail.get_email(email_id)
        assert email_obj is not None
        assert email_obj["subject"] == "Find me"

    @pytest.mark.asyncio
    async def test_get_nonexistent_email(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        assert mail.get_email("nonexistent") is None

    @pytest.mark.asyncio
    async def test_sent_by_agent(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path)
        mail.configure(host="smtp.test.com", username="u", password="p")
        await mail.send(to="a@b.com", subject="Agent 1", body="", agent="aither")
        await mail.send(to="c@d.com", subject="Agent 2", body="", agent="lyra")
        aither_sent = mail.sent(agent="aither")
        assert len(aither_sent) == 1
        assert aither_sent[0]["agent"] == "aither"


# ── Status ───────────────────────────────────────────────────────────────


class TestStatus:
    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        mail = MailRelay(data_dir=tmp_path, node_id="test-node")
        mail.configure(host="smtp.test.com", username="u", password="p")
        await mail.send(to="a@b.com", subject="Q1", body="body")
        mail.receive_mesh_mail({
            "from_agent": "lyra", "from_node": "remote",
            "to_agent": "aither", "subject": "In", "body": "hi",
        })

        s = mail.status()
        assert s["node_id"] == "test-node"
        assert s["configured"] is True
        assert s["counts"]["queued"] == 1
        assert s["counts"]["received"] == 1
        assert "gmail" in s["providers_available"]


# ── Singleton ────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_mail_relay_singleton(self, tmp_path):
        import adk.smtp as smtp_mod
        smtp_mod._mail_relay = None

        r1 = get_mail_relay(data_dir=tmp_path)
        r2 = get_mail_relay(data_dir=tmp_path)
        assert r1 is r2

        smtp_mod._mail_relay = None


# ── Export ───────────────────────────────────────────────────────────────


class TestExport:
    def test_exports(self):
        import adk
        assert hasattr(adk, "MailRelay")
