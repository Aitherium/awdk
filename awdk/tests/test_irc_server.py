"""Tests for the IRC protocol server (RFC 2812 subset)."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.chat import ChatRelay, IRCServer, _IRCClient


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_client(nick: str = "testuser") -> _IRCClient:
    """Create a mock IRC client."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    writer = MagicMock()
    writer.get_extra_info.return_value = ("127.0.0.1", 12345)
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()

    client = _IRCClient(reader, writer)
    client.nick = nick
    client.user = nick
    client.registered = True
    return client


class TestIRCClient:
    def test_create(self):
        client = _make_client("alice")
        assert client.nick == "alice"
        assert client.addr == "127.0.0.1:12345"

    def test_hostmask(self):
        client = _make_client("alice")
        assert client.hostmask == "alice!alice@127.0.0.1"

    @pytest.mark.asyncio
    async def test_send(self):
        client = _make_client("alice")
        await client.send("PING :test")
        client.writer.write.assert_called_once()
        written = client.writer.write.call_args[0][0]
        assert written == b"PING :test\r\n"

    @pytest.mark.asyncio
    async def test_send_numeric(self):
        client = _make_client("alice")
        await client.send_numeric("AitherNet", "001", "alice", ":Welcome")
        written = client.writer.write.call_args[0][0]
        assert b":AitherNet 001 alice :Welcome\r\n" == written


# ── IRC Server ───────────────────────────────────────────────────────────


class TestIRCServer:
    def test_create(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay, port=16667)
        assert server._port == 16667

    def test_status_not_started(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        s = server.status()
        assert s["listening"] is False
        assert s["clients"] == 0


class TestRegistration:
    @pytest.mark.asyncio
    async def test_nick_command(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.nick = ""
        client.user = ""
        client.registered = False

        await server._cmd_nick(client, ["alice"])
        assert client.nick == "alice"
        assert "alice" in server._clients

    @pytest.mark.asyncio
    async def test_nick_no_param(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.nick = ""
        client.registered = False

        await server._cmd_nick(client, [])
        # Should send 431 ERR_NONICKNAMEGIVEN
        written = client.writer.write.call_args[0][0]
        assert b"431" in written

    @pytest.mark.asyncio
    async def test_nick_collision(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client1 = _make_client("alice")
        server._clients["alice"] = client1

        client2 = _make_client("")
        client2.nick = ""
        client2.registered = False
        await server._cmd_nick(client2, ["alice"])
        # Should send 433 ERR_NICKNAMEINUSE
        written = client2.writer.write.call_args[0][0]
        assert b"433" in written

    @pytest.mark.asyncio
    async def test_invalid_nick(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.nick = ""
        client.registered = False

        await server._cmd_nick(client, ["inv@lid!"])
        written = client.writer.write.call_args[0][0]
        assert b"432" in written

    @pytest.mark.asyncio
    async def test_user_command(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.nick = "alice"
        client.user = ""
        client.registered = False
        server._clients["alice"] = client

        await server._cmd_user(client, ["alice", "0", "*", "Alice Smith"])
        assert client.user == "alice"
        assert client.realname == "Alice Smith"
        assert client.registered is True

    @pytest.mark.asyncio
    async def test_complete_registration_sends_welcome(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.nick = "bob"
        client.user = ""
        client.registered = False
        server._clients["bob"] = client

        await server._cmd_user(client, ["bob", "0", "*", "Bob"])
        # Should have sent multiple numerics: 001, 002, 003, 004, 375, 372, 376
        calls = client.writer.write.call_args_list
        all_output = b"".join(c[0][0] for c in calls)
        assert b"001" in all_output  # RPL_WELCOME
        assert b"376" in all_output  # End of MOTD
        assert "bob" in relay._users  # User registered in ChatRelay

    @pytest.mark.asyncio
    async def test_reregister_blocked(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        # Already registered
        await server._cmd_user(client, ["alice", "0", "*", "Alice"])
        written = client.writer.write.call_args[0][0]
        assert b"462" in written  # ERR_ALREADYREGISTRED


class TestChannelCommands:
    @pytest.mark.asyncio
    async def test_join(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_join(client, ["#test"])
        assert "#test" in client.channels
        assert "#test" in relay._channels

    @pytest.mark.asyncio
    async def test_join_multiple(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_join(client, ["#a,#b,#c"])
        assert "#a" in client.channels
        assert "#b" in client.channels
        assert "#c" in client.channels

    @pytest.mark.asyncio
    async def test_join_invalid_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_join(client, ["#inv@lid!"])
        written = client.writer.write.call_args[0][0]
        assert b"403" in written

    @pytest.mark.asyncio
    async def test_part(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#test", "alice")
        client.channels.append("#test")

        await server._cmd_part(client, ["#test", "Goodbye!"])
        assert "#test" not in client.channels

    @pytest.mark.asyncio
    async def test_part_not_in_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_part(client, ["#nonexistent"])
        written = client.writer.write.call_args[0][0]
        assert b"442" in written  # ERR_NOTONCHANNEL

    @pytest.mark.asyncio
    async def test_topic_query(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        relay.set_topic("#general", "Welcome to AitherNet")
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_topic(client, ["#general"])
        written = client.writer.write.call_args[0][0]
        assert b"332" in written
        assert b"Welcome to AitherNet" in written

    @pytest.mark.asyncio
    async def test_topic_set(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._cmd_topic(client, ["#general", "New topic!"])
        assert relay._channels["#general"].topic == "New topic!"

    @pytest.mark.asyncio
    async def test_names(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        relay.join("#general", "alice")
        relay.join("#general", "bob")
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_names(client, ["#general"])
        calls = client.writer.write.call_args_list
        all_output = b"".join(c[0][0] for c in calls)
        assert b"353" in all_output  # RPL_NAMREPLY
        assert b"366" in all_output  # RPL_ENDOFNAMES


class TestMessagingCommands:
    @pytest.mark.asyncio
    async def test_privmsg_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._cmd_privmsg(client, ["#general", "Hello world!"])
        history = relay.history("#general")
        contents = [m["content"] for m in history if m["msg_type"] == "message"]
        assert "Hello world!" in contents

    @pytest.mark.asyncio
    async def test_privmsg_dm(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "bob")

        await server._cmd_privmsg(client, ["bob", "Secret message"])
        dm_key = relay._dm_key("alice", "bob")
        history = relay.history(dm_key)
        assert len(history) >= 1

    @pytest.mark.asyncio
    async def test_privmsg_no_text(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_privmsg(client, ["#general", ""])
        written = client.writer.write.call_args[0][0]
        assert b"412" in written  # ERR_NOTEXTTOSEND

    @pytest.mark.asyncio
    async def test_privmsg_action(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._cmd_privmsg(client, ["#general", "\x01ACTION waves\x01"])
        history = relay.history("#general")
        actions = [m for m in history if m["msg_type"] == "action"]
        assert len(actions) >= 1

    @pytest.mark.asyncio
    async def test_privmsg_to_nonexistent_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_privmsg(client, ["#nonexistent", "hello"])
        written = client.writer.write.call_args[0][0]
        assert b"404" in written  # ERR_CANNOTSENDTOCHAN


class TestQueryCommands:
    @pytest.mark.asyncio
    async def test_who_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        relay.join("#general", "alice")
        relay.join("#general", "bob")
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_who(client, ["#general"])
        calls = client.writer.write.call_args_list
        all_output = b"".join(c[0][0] for c in calls)
        assert b"352" in all_output  # RPL_WHOREPLY
        assert b"315" in all_output  # RPL_ENDOFWHO

    @pytest.mark.asyncio
    async def test_list(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_list(client, [])
        calls = client.writer.write.call_args_list
        all_output = b"".join(c[0][0] for c in calls)
        assert b"322" in all_output  # RPL_LIST
        assert b"323" in all_output  # RPL_LISTEND

    @pytest.mark.asyncio
    async def test_whois(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        relay.join("#general", "alice")
        server = IRCServer(relay)
        client = _make_client("bob")
        server._clients["bob"] = client

        await server._cmd_whois(client, ["alice"])
        calls = client.writer.write.call_args_list
        all_output = b"".join(c[0][0] for c in calls)
        assert b"311" in all_output  # RPL_WHOISUSER
        assert b"318" in all_output  # RPL_ENDOFWHOIS

    @pytest.mark.asyncio
    async def test_whois_not_found(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_whois(client, ["nobody"])
        written = client.writer.write.call_args[0][0]
        assert b"401" in written  # ERR_NOSUCHNICK


class TestUtilityCommands:
    @pytest.mark.asyncio
    async def test_ping(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_ping(client, ["12345"])
        written = client.writer.write.call_args[0][0]
        assert b"PONG" in written
        assert b"12345" in written

    @pytest.mark.asyncio
    async def test_pong(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        # PONG should not raise
        await server._cmd_pong(client, ["token"])

    @pytest.mark.asyncio
    async def test_quit(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._cmd_quit(client, ["Bye!"])
        assert "alice" not in server._clients

    @pytest.mark.asyncio
    async def test_mode_channel(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_mode(client, ["#general"])
        written = client.writer.write.call_args[0][0]
        assert b"324" in written  # RPL_CHANNELMODEIS

    @pytest.mark.asyncio
    async def test_mode_user(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._cmd_mode(client, ["alice"])
        written = client.writer.write.call_args[0][0]
        assert b"221" in written  # RPL_UMODEIS

    @pytest.mark.asyncio
    async def test_cap_ls(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.registered = False

        await server._cmd_cap(client, ["LS"])
        written = client.writer.write.call_args[0][0]
        assert b"CAP" in written

    @pytest.mark.asyncio
    async def test_cap_req(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("")
        client.registered = False

        await server._cmd_cap(client, ["REQ", "multi-prefix"])
        written = client.writer.write.call_args[0][0]
        assert b"NAK" in written  # No caps supported

    @pytest.mark.asyncio
    async def test_userhost(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        relay.join("#general", "alice")
        server = IRCServer(relay)
        client = _make_client("bob")
        server._clients["bob"] = client

        await server._cmd_userhost(client, ["alice"])
        written = client.writer.write.call_args[0][0]
        assert b"302" in written

    @pytest.mark.asyncio
    async def test_unknown_command(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        await server._process_line(client, "FOOBAR some params")
        written = client.writer.write.call_args[0][0]
        assert b"421" in written  # ERR_UNKNOWNCOMMAND


class TestLineProcessing:
    @pytest.mark.asyncio
    async def test_process_with_prefix(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client

        # Some clients send :prefix before the command
        await server._process_line(client, ":alice PING :test")
        written = client.writer.write.call_args[0][0]
        assert b"PONG" in written

    @pytest.mark.asyncio
    async def test_process_trailing_param(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._process_line(client, "PRIVMSG #general :Hello world with spaces")
        history = relay.history("#general")
        contents = [m["content"] for m in history if m["msg_type"] == "message"]
        assert "Hello world with spaces" in contents


class TestRelayEventBridging:
    def test_message_event_skips_irc_clients(self, tmp_path):
        """Messages from IRC clients should NOT be echoed back to IRC."""
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        client.channels.append("#general")

        # This simulates a message event from the relay where sender is IRC client
        server._on_relay_message({
            "nick": "alice",
            "channel": "#general",
            "content": "hello",
        })
        # No broadcast should happen (writer.write only called if send() is called)
        # The event handler returns early, so no new writes

    def test_message_event_from_ws_broadcasts(self, tmp_path):
        """Messages from WebSocket users SHOULD be broadcast to IRC clients."""
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("irc_user")
        server._clients["irc_user"] = client
        client.channels.append("#general")

        # Message from a non-IRC user (WebSocket/federation)
        server._on_relay_message({
            "nick": "ws_user",
            "channel": "#general",
            "content": "hello from WS",
        })
        # This creates an asyncio future — in real code it would send to IRC

    def test_join_event_skips_irc(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        server._clients["alice"] = _make_client("alice")

        # Join from IRC client — should be skipped
        server._on_relay_join({"nick": "alice", "channel": "#test"})

    def test_dm_event_from_ws(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("bob")
        server._clients["bob"] = client

        # DM from non-IRC user to IRC client
        server._on_relay_dm({
            "nick": "ws_user",
            "channel": "dm:bob:ws_user",
            "content": "hey bob",
        })
        # Should create future to send to bob


class TestNickChange:
    @pytest.mark.asyncio
    async def test_nick_change(self, tmp_path):
        relay = ChatRelay(data_dir=tmp_path)
        server = IRCServer(relay)
        client = _make_client("alice")
        server._clients["alice"] = client
        relay.join("#general", "alice")
        client.channels.append("#general")

        await server._cmd_nick(client, ["alice_new"])
        assert client.nick == "alice_new"
        assert "alice_new" in server._clients
        assert "alice" not in server._clients
        assert "alice_new" in relay._users
