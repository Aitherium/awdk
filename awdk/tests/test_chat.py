"""Tests for the AitherNet chat relay system."""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.chat import ChatRelay, ChatMessage, ChatUser, Channel, get_chat_relay


# ── Channel Management ───────────────────────────────────────────────────


class TestChannels:
    def test_default_channels_created(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        channels = chat.list_channels()
        names = [c["name"] for c in channels]
        assert "#general" in names
        assert "#agents" in names
        assert "#dev" in names

    def test_create_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        ch = chat.create_channel("#test", topic="Test channel", mode="private")
        assert ch.name == "#test"
        assert ch.topic == "Test channel"
        assert ch.mode == "private"

    def test_create_duplicate_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        ch1 = chat.create_channel("#dup")
        ch2 = chat.create_channel("#dup")
        assert ch1.name == ch2.name  # Returns existing

    def test_invalid_channel_name(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        with pytest.raises(ValueError):
            chat.create_channel("no-hash")  # Must start with #

    def test_set_topic(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.set_topic("#general", "New topic")
        assert chat._channels["#general"].topic == "New topic"

    def test_list_channels_with_users(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#general", "bob")
        channels = chat.list_channels()
        gen = [c for c in channels if c["name"] == "#general"][0]
        assert gen["users"] == 2


# ── User Management ──────────────────────────────────────────────────────


class TestUsers:
    def test_join_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        ok = chat.join("#general", "alice")
        assert ok is True
        assert "alice" in chat._users
        assert "#general" in chat._users["alice"].channels

    def test_join_creates_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#newchan", "alice")
        assert "#newchan" in chat._channels

    def test_invalid_nick(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        ok = chat.join("#general", "inv@lid!")
        assert ok is False

    def test_part_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#dev", "alice")
        chat.part("#general", "alice")
        assert "#general" not in chat._users["alice"].channels
        assert "#dev" in chat._users["alice"].channels

    def test_part_last_channel_removes_user(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.part("#general", "alice")
        assert "alice" not in chat._users

    def test_who(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#general", "bob", is_agent=True)
        users = chat.who("#general")
        assert len(users) == 2
        nicks = [u["nick"] for u in users]
        assert "alice" in nicks
        assert "bob" in nicks
        agent = [u for u in users if u["nick"] == "bob"][0]
        assert agent["is_agent"] is True

    def test_online_users(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#dev", "bob")
        online = chat.online_users()
        assert len(online) == 2

    def test_register_agent(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.register_agent("aither")
        assert "aither" in chat._users
        assert chat._users["aither"].is_agent is True
        assert "#general" in chat._users["aither"].channels
        assert "#agents" in chat._users["aither"].channels


# ── Messaging ────────────────────────────────────────────────────────────


class TestMessaging:
    def test_post_message(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        msg = chat.post("#general", "alice", "Hello world!")
        assert msg is not None
        assert msg.content == "Hello world!"
        assert msg.msg_type == "message"
        assert msg.msg_id != ""

    def test_post_to_nonexistent_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        msg = chat.post("#nonexistent", "alice", "hello")
        assert msg is None

    def test_message_truncation(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        long_msg = "x" * 5000
        msg = chat.post("#general", "alice", long_msg)
        assert len(msg.content) == 4000

    def test_post_action(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        msg = chat.post_action("#general", "alice", "waves")
        assert msg is not None
        assert "* alice waves" in msg.content
        assert msg.msg_type == "action"

    def test_post_dm(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#general", "bob")
        msg = chat.post_dm("alice", "bob", "secret message")
        assert msg is not None
        assert msg.channel.startswith("dm:")

    def test_history(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        for i in range(5):
            chat.post("#general", "alice", f"msg {i}")
        history = chat.history("#general", limit=10)
        # Includes join message + 5 posts
        assert len(history) >= 5
        # Messages are in chronological order
        contents = [m["content"] for m in history if m["msg_type"] == "message"]
        assert contents == [f"msg {i}" for i in range(5)]

    def test_history_with_limit(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        for i in range(10):
            chat.post("#general", "alice", f"msg {i}")
        history = chat.history("#general", limit=3)
        assert len(history) == 3

    def test_mention_handler(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.register_agent("aither", mention_handler=lambda msg: received.append(msg))
        chat.join("#general", "alice")
        chat.post("#general", "alice", "Hey @aither help me")
        assert len(received) == 1

    def test_mention_handler_case_insensitive(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.register_agent("Aither", mention_handler=lambda msg: received.append(msg))
        chat.join("#general", "alice")
        chat.post("#general", "alice", "Hey @aither help me")
        assert len(received) == 1


# ── Outbound Relay Handlers ──────────────────────────────────────────────


class TestOutboundHandlers:
    def test_outbound_handler_invoked(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.register_outbound_handler("relay", lambda msg: received.append(msg))
        chat.join("#general", "alice")
        chat.post("#general", "alice", "hello")
        assert len(received) == 1
        assert received[0].content == "hello"

    def test_outbound_handler_error_nonfatal(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.register_outbound_handler("boom", lambda msg: 1 / 0)
        chat.join("#general", "alice")
        # Must not raise and must still return the posted message
        msg = chat.post("#general", "alice", "hello")
        assert msg is not None

    def test_multiple_handlers_isolated(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.register_outbound_handler("boom", lambda msg: 1 / 0)
        chat.register_outbound_handler("ok", lambda msg: received.append(msg))
        chat.join("#general", "alice")
        chat.post("#general", "alice", "hello")
        # The failing handler does not prevent the healthy one
        assert len(received) == 1

    def test_unregister_outbound_handler(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.register_outbound_handler("relay", lambda msg: received.append(msg))
        chat.unregister_outbound_handler("relay")
        chat.join("#general", "alice")
        chat.post("#general", "alice", "hello")
        assert received == []

    @pytest.mark.asyncio
    async def test_async_outbound_handler(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []

        async def handler(msg):
            received.append(msg)

        chat.register_outbound_handler("relay", handler)
        chat.join("#general", "alice")
        chat.post("#general", "alice", "hello")
        await asyncio.sleep(0)  # let the scheduled coroutine run
        assert len(received) == 1


# ── IRC Commands ─────────────────────────────────────────────────────────


class TestIRCCommands:
    def test_join_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "/join #test")
        assert result["action"] == "join"
        assert result["channel"] == "#test"

    def test_part_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        result = chat.parse_irc_command("alice", "/part #general")
        assert result["action"] == "part"

    def test_nick_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        result = chat.parse_irc_command("alice", "/nick bob")
        assert result["action"] == "nick"
        assert result["new"] == "bob"
        assert "bob" in chat._users
        assert "alice" not in chat._users

    def test_who_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        result = chat.parse_irc_command("alice", "/who #general")
        assert result["action"] == "who"
        assert len(result["users"]) == 1

    def test_list_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "/list")
        assert result["action"] == "list"
        assert len(result["channels"]) >= 3  # default channels

    def test_me_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "/me waves")
        assert result["action"] == "action"
        assert result["text"] == "waves"

    def test_msg_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#general", "bob")
        result = chat.parse_irc_command("alice", "/msg bob hello there")
        assert result["action"] == "dm"
        assert result["to"] == "bob"

    def test_help_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "/help")
        assert result["action"] == "help"
        assert len(result["commands"]) > 0

    def test_non_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "just a normal message")
        assert result is None

    def test_topic_command(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        result = chat.parse_irc_command("alice", "/topic #general New topic here")
        assert result["action"] == "topic"
        assert result["topic"] == "New topic here"


# ── Event Handlers ───────────────────────────────────────────────────────


class TestEvents:
    def test_on_message(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.on("message", lambda data: received.append(data))
        chat.join("#general", "alice")
        chat.post("#general", "alice", "hello")
        assert len(received) == 1

    def test_on_join(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.on("join", lambda data: received.append(data))
        chat.join("#general", "alice")
        assert len(received) == 1
        assert received[0]["nick"] == "alice"

    def test_on_part(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        received = []
        chat.on("part", lambda data: received.append(data))
        chat.join("#general", "alice")
        chat.part("#general", "alice")
        assert len(received) == 1

    def test_handler_error_nonfatal(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.on("message", lambda data: 1 / 0)  # Deliberate error
        chat.join("#general", "alice")
        # Should not raise
        chat.post("#general", "alice", "hello")


# ── Federation ───────────────────────────────────────────────────────────


class TestFederation:
    def test_handle_federated_message(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path, node_id="local-node")
        chat.join("#general", "alice")
        chat.handle_federated_message({
            "origin_node": "remote-node",
            "channel": "#general",
            "nick": "bob@remote",
            "content": "Hello from remote!",
            "msg_type": "message",
            "timestamp": time.time(),
        })
        history = chat.history("#general")
        contents = [m["content"] for m in history if m["msg_type"] == "message"]
        assert "Hello from remote!" in contents

    def test_loop_prevention(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path, node_id="local-node")
        initial_count = len(chat.history("#general"))
        chat.handle_federated_message({
            "origin_node": "local-node",  # Same node — should be ignored
            "channel": "#general",
            "nick": "alice",
            "content": "loop!",
        })
        # No new message stored
        assert len(chat.history("#general")) == initial_count

    def test_federated_creates_channel(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path, node_id="local-node")
        chat.handle_federated_message({
            "origin_node": "remote-node",
            "channel": "#remote-only",
            "nick": "bob@remote",
            "content": "First message",
        })
        assert "#remote-only" in chat._channels


# ── WebSocket Handling ───────────────────────────────────────────────────


class TestWebSocket:
    @pytest.mark.asyncio
    async def test_handle_ws_message(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        await chat.handle_ws_message("alice", {
            "type": "message",
            "channel": "#general",
            "content": "ws hello",
        })
        history = chat.history("#general")
        contents = [m["content"] for m in history if m["msg_type"] == "message"]
        assert "ws hello" in contents

    @pytest.mark.asyncio
    async def test_handle_ws_join(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        await chat.handle_ws_message("alice", {
            "type": "join",
            "channel": "#test",
        })
        assert "#test" in chat._users["alice"].channels

    @pytest.mark.asyncio
    async def test_handle_ws_dm(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path)
        chat.join("#general", "alice")
        chat.join("#general", "bob")
        await chat.handle_ws_message("alice", {
            "type": "dm",
            "to": "bob",
            "content": "secret",
        })
        # DM stored
        dm_key = chat._dm_key("alice", "bob")
        history = chat.history(dm_key)
        assert len(history) >= 1


# ── Persistence ──────────────────────────────────────────────────────────


class TestPersistence:
    def test_channels_persist(self, tmp_path):
        chat1 = ChatRelay(data_dir=tmp_path)
        chat1.create_channel("#persist", topic="Persistent!")

        chat2 = ChatRelay(data_dir=tmp_path)
        assert "#persist" in chat2._channels
        assert chat2._channels["#persist"].topic == "Persistent!"

    def test_messages_persist(self, tmp_path):
        chat1 = ChatRelay(data_dir=tmp_path)
        chat1.join("#general", "alice")
        chat1.post("#general", "alice", "persisted message")

        chat2 = ChatRelay(data_dir=tmp_path)
        history = chat2.history("#general")
        contents = [m["content"] for m in history]
        assert "persisted message" in contents


# ── Status ───────────────────────────────────────────────────────────────


class TestStatus:
    def test_status(self, tmp_path):
        chat = ChatRelay(data_dir=tmp_path, node_id="test-node")
        chat.join("#general", "alice")
        chat.register_agent("aither")
        s = chat.status()
        assert s["node_id"] == "test-node"
        assert s["channels"] >= 3
        assert s["users_online"] == 2
        assert s["agents_online"] == 1


# ── Singleton ────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_chat_relay_singleton(self, tmp_path):
        import adk.chat as chat_mod
        chat_mod._chat_relay = None

        r1 = get_chat_relay(data_dir=tmp_path)
        r2 = get_chat_relay(data_dir=tmp_path)
        assert r1 is r2

        chat_mod._chat_relay = None


# ── Export ───────────────────────────────────────────────────────────────


class TestExport:
    def test_exports(self):
        import adk
        assert hasattr(adk, "ChatRelay")
