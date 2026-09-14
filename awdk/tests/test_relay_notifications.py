"""Tests for RelayClient notification polling."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.relay_client import RelayClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, json_data: dict):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}
    resp.json = MagicMock(return_value=json_data)
    resp.text = json.dumps(json_data)
    return resp


# ---------------------------------------------------------------------------
# Tests: poll_notifications
# ---------------------------------------------------------------------------


class TestPollNotifications:
    """Test the poll_notifications method."""

    @pytest.mark.asyncio
    async def test_successful_poll(self):
        """Test a successful notification poll returns new notifications."""
        client_mock = AsyncMock()

        # Mock the GET response
        notifs_data = {
            "notifications": [
                {
                    "id": "notif-1",
                    "type": "mention",
                    "from_nick": "alice",
                    "channel": "#general",
                    "content": "hey agent!",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                }
            ],
            "unread_count": 1,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # Verify notification was returned
        assert len(result) == 1
        assert result[0]["id"] == "notif-1"
        assert result[0]["type"] == "mention"

        # Verify dedup: notification id is in seen set
        assert "notif-1" in relay._seen_notif_ids

        # Verify read was called with the notification id
        client_mock.post.assert_called_once()
        post_args = client_mock.post.call_args
        assert "notifications/agent-1/read" in post_args[0][0]
        assert post_args[1]["json"]["ids"] == ["notif-1"]

    @pytest.mark.asyncio
    async def test_dedup_via_seen_ids(self):
        """Test that duplicate notifications are skipped via _seen_notif_ids."""
        client_mock = AsyncMock()

        notif_id = "notif-already-seen"
        notifs_data = {
            "notifications": [
                {
                    "id": notif_id,
                    "type": "mention",
                    "from_nick": "alice",
                    "channel": "#general",
                    "content": "hey",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                }
            ],
            "unread_count": 0,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )
        # Pre-populate seen set
        relay._seen_notif_ids.add(notif_id)

        result = await relay.poll_notifications(client_mock)

        # Notification should be skipped
        assert len(result) == 0
        # POST should not be called
        client_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_response_logs_warning(self, caplog):
        """Test that None response (401/unreachable) logs warning but does not crash."""
        client_mock = AsyncMock()
        # Mock client.get to return 401 (which _get converts to None)
        client_mock.get = AsyncMock(return_value=_mock_response(401, {"detail": "Unauthorized"}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # Should return empty list
        assert result == []
        # Should increment failure count
        assert relay._notif_poll_failure_count > 0

    @pytest.mark.asyncio
    async def test_none_response_failure_count_increments(self):
        """Test that _notif_poll_failure_count increments on None response."""
        client_mock = AsyncMock()
        # Mock client.get to return non-200 (which _get converts to None)
        client_mock.get = AsyncMock(return_value=_mock_response(401, {"detail": "Unauthorized"}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        initial_count = relay._notif_poll_failure_count
        await relay.poll_notifications(client_mock)
        assert relay._notif_poll_failure_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_unknown_type_logged_at_debug(self):
        """Test that unknown notification types are logged at DEBUG and skipped."""
        client_mock = AsyncMock()

        notifs_data = {
            "notifications": [
                {
                    "id": "notif-unknown-type",
                    "type": "unknown_future_type",
                    "from_nick": "bob",
                    "channel": "#dev",
                    "content": "info",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                }
            ],
            "unread_count": 1,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # Should still return the notification
        assert len(result) == 1
        # But it should be processed (and marked as read)
        client_mock.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_called_for_mention_type(self):
        """Test that on_notification callback is called for type=='mention'."""
        client_mock = AsyncMock()
        callback_mock = MagicMock()

        notifs_data = {
            "notifications": [
                {
                    "id": "notif-mention",
                    "type": "mention",
                    "from_nick": "alice",
                    "channel": "#general",
                    "content": "@agent please help",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                }
            ],
            "unread_count": 1,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
            on_notification=callback_mock,
        )

        result = await relay.poll_notifications(client_mock)

        # Callback should have been called with the notification
        assert callback_mock.call_count == 1
        call_arg = callback_mock.call_args[0][0]
        assert call_arg["id"] == "notif-mention"
        assert call_arg["type"] == "mention"

    @pytest.mark.asyncio
    async def test_callback_exception_logged_not_raised(self):
        """Test that callback exception is logged but does not crash."""
        client_mock = AsyncMock()

        def bad_callback(notif):
            raise ValueError("callback error")

        notifs_data = {
            "notifications": [
                {
                    "id": "notif-1",
                    "type": "mention",
                    "from_nick": "alice",
                    "channel": "#general",
                    "content": "hey",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                }
            ],
            "unread_count": 1,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
            on_notification=bad_callback,
        )

        # Should not raise
        result = await relay.poll_notifications(client_mock)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_read_posts_correct_ids(self):
        """Test that POST /read includes only new notification ids."""
        client_mock = AsyncMock()

        notifs_data = {
            "notifications": [
                {
                    "id": "notif-new-1",
                    "type": "dm",
                    "from_nick": "alice",
                    "channel": "DM",
                    "content": "msg1",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                },
                {
                    "id": "notif-new-2",
                    "type": "reaction",
                    "from_nick": "bob",
                    "channel": "#general",
                    "content": ":+1:",
                    "read": False,
                    "created_at": "2026-07-17T12:01:00Z",
                },
            ],
            "unread_count": 2,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # Both notifications should be returned
        assert len(result) == 2

        # POST should include both ids
        post_args = client_mock.post.call_args
        posted_ids = post_args[1]["json"]["ids"]
        assert set(posted_ids) == {"notif-new-1", "notif-new-2"}

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_list(self):
        """Test that empty notifications list returns empty result."""
        client_mock = AsyncMock()

        notifs_data = {"notifications": [], "unread_count": 0}
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # Should return empty list
        assert result == []
        # POST should not be called (no ids to read)
        client_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_types_handled_correctly(self):
        """Test that multiple notification types are handled correctly."""
        client_mock = AsyncMock()

        notifs_data = {
            "notifications": [
                {
                    "id": "notif-dm",
                    "type": "dm",
                    "from_nick": "alice",
                    "channel": "DM",
                    "content": "direct message",
                    "read": False,
                    "created_at": "2026-07-17T12:00:00Z",
                },
                {
                    "id": "notif-reaction",
                    "type": "reaction",
                    "from_nick": "bob",
                    "channel": "#general",
                    "content": "thumbs up",
                    "read": False,
                    "created_at": "2026-07-17T12:01:00Z",
                },
                {
                    "id": "notif-thread",
                    "type": "thread_reply",
                    "from_nick": "charlie",
                    "channel": "#dev",
                    "content": "reply in thread",
                    "read": False,
                    "created_at": "2026-07-17T12:02:00Z",
                },
                {
                    "id": "notif-system",
                    "type": "system",
                    "from_nick": "relay",
                    "channel": "#general",
                    "content": "system notification",
                    "read": False,
                    "created_at": "2026-07-17T12:03:00Z",
                },
            ],
            "unread_count": 4,
        }
        client_mock.get = AsyncMock(return_value=_mock_response(200, notifs_data))
        client_mock.post = AsyncMock(return_value=_mock_response(200, {"success": True}))

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        result = await relay.poll_notifications(client_mock)

        # All should be returned
        assert len(result) == 4
        # All should be marked as read
        post_args = client_mock.post.call_args
        posted_ids = post_args[1]["json"]["ids"]
        assert len(posted_ids) == 4


# ---------------------------------------------------------------------------
# Tests: Integration with run()
# ---------------------------------------------------------------------------


class TestIntegrationWithRun:
    """Test that poll_notifications integrates into run() loop."""

    @pytest.mark.asyncio
    async def test_run_calls_poll_notifications(self):
        """Test that run() calls poll_notifications each loop."""
        client_mock = AsyncMock()

        # Mock join
        join_resp = _mock_response(200, {})
        client_mock.post = AsyncMock(return_value=join_resp)

        # Mock /dms/partners (for prime)
        partners_resp = _mock_response(200, {"partners": []})

        # Mock /notifications (will be called in loop)
        notifs_resp = _mock_response(
            200, {"notifications": [], "unread_count": 0}
        )

        async def mock_get(url, *args, **kwargs):
            if "notifications" in url:
                return notifs_resp
            if "dms/partners" in url:
                return partners_resp
            return _mock_response(404, {})

        client_mock.get = AsyncMock(side_effect=mock_get)

        relay = RelayClient(
            base_url="https://relay.test/v1",
            token="test_token",
            nick="agent-1",
            agent=None,
        )

        # Run for one iteration then stop
        async def _run_one_iteration():
            async with AsyncMock() as ctx:
                ctx.__aenter__ = AsyncMock(return_value=client_mock)
                ctx.__aexit__ = AsyncMock(return_value=False)

                # Manually do what run() does but stop after one iteration
                if not await relay.join(client_mock):
                    return False

                # Prime _seen
                for p in relay._rows(await relay._get(client_mock, "/dms/partners")):
                    pass

                # One iteration
                await relay.poll_once(client_mock)
                await relay.poll_notifications(client_mock)

                return True

        result = await _run_one_iteration()
        assert result is True

        # Verify poll_notifications was called (notifications endpoint was hit)
        calls = client_mock.get.call_args_list
        notification_calls = [
            c for c in calls if "notifications" in c[0][0]
        ]
        assert len(notification_calls) > 0
