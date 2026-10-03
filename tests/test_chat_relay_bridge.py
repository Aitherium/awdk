"""Node rooms <-> platform relay bridge (adk.chat + adk.relay_client.RoomBridge).

The relay is a stub served through ``httpx.MockTransport`` that behaves like the
real channel API: POST stores a row and returns its id, GET returns the newest
rows -- INCLUDING the ones this bridge posted, so echo suppression is exercised
for real rather than assumed.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.chat import ChatRelay, room_bridge_config  # noqa: E402
from adk.relay_client import RoomBridge  # noqa: E402

BASE = "https://relay.test/api/relay/v1"


class StubRelay:
    def __init__(self, return_ids: bool = True):
        self.rows: dict[str, list[dict]] = {}
        self.posts: list[dict] = []
        self.next_id = 1
        self.return_ids = return_ids
        self.down = False

    def add(self, channel: str, nick: str, content: str) -> dict:
        row = {"id": str(self.next_id), "nick": nick, "content": content,
               "timestamp": 1000.0 + self.next_id}
        self.next_id += 1
        self.rows.setdefault(channel, []).append(row)
        return row

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("relay down", request=request)
        parts = request.url.path.split("/")
        channel = "#" + parts[parts.index("channels") + 1]
        if request.method == "POST":
            import json
            body = json.loads(request.content)
            self.posts.append({"channel": channel, **body})
            row = self.add(channel, body["nick"], body["content"])
            return httpx.Response(201, json={"id": row["id"]} if self.return_ids else {"ok": True})
        return httpx.Response(200, json={"messages": list(self.rows.get(channel, []))[-30:]})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _bridge(rooms=None) -> RoomBridge:
    return RoomBridge(BASE, "tok", "node-bridge", rooms or {"#general": "#acme-room"},
                      verify=True)


def _local_rows(chat: ChatRelay, channel: str = "#general") -> list[dict]:
    return [m for m in chat.history(channel, limit=100) if m["msg_type"] == "message"]


async def _cycle(bridge, chat, relay, passes=1):
    async with relay.client() as client:
        for _ in range(passes):
            await bridge.flush(client)
            await bridge.pull_once(client, chat)


@pytest.mark.parametrize("return_ids", [True, False])
def test_local_post_reaches_relay_once_and_never_echoes_back(tmp_path, return_ids):
    relay = StubRelay(return_ids=return_ids)
    chat = ChatRelay(data_dir=tmp_path)
    bridge = _bridge()
    chat.attach_room_bridge(bridge)
    asyncio.run(_cycle(bridge, chat, relay))          # primes the (empty) channel

    chat.post("#general", "alice", "hello from the node")
    asyncio.run(_cycle(bridge, chat, relay, passes=3))

    assert [p["content"] for p in relay.posts] == ["<alice> hello from the node"]
    assert relay.posts[0]["channel"] == "#acme-room"
    # The relay's copy of our own post is NOT pulled back in as a second line.
    assert [m["content"] for m in _local_rows(chat)] == ["hello from the node"]


def test_relay_message_appears_locally_once_and_is_not_sent_back(tmp_path):
    relay = StubRelay()
    chat = ChatRelay(data_dir=tmp_path)
    bridge = _bridge()
    chat.attach_room_bridge(bridge)
    relay.add("#acme-room", "old", "backlog before the node started")
    asyncio.run(_cycle(bridge, chat, relay))          # priming pass: backlog skipped

    relay.add("#acme-room", "dana", "hi from Commons")
    asyncio.run(_cycle(bridge, chat, relay, passes=3))

    local = _local_rows(chat)
    assert [(m["nick"], m["content"]) for m in local] == [("dana", "hi from Commons")]
    assert local[0]["node_id"] == "relay"
    assert relay.posts == []                          # no echo back out

    # A second bridge reading the same row (e.g. after a restart without priming
    # state) cannot duplicate it either: the local id derives from the relay id.
    assert chat.ingest_bridged("#general", "dana", "hi from Commons",
                               remote_id="#acme-room:2") is None


def test_bridge_off_sends_nothing(tmp_path):
    relay = StubRelay()
    chat = ChatRelay(data_dir=tmp_path)
    assert room_bridge_config(saved={"api_key": "k"}, env={}) is None
    assert asyncio.run(chat.start_room_bridge(config={})) is None
    chat.post("#general", "alice", "local only")
    assert relay.posts == []
    assert chat._outbound_handlers == {}


def test_bridge_requires_enrolment(tmp_path):
    assert room_bridge_config(saved={}, env={"AITHER_ROOM_BRIDGE": "1"}) is None


def test_config_maps_rooms_explicitly_or_asks_the_relay():
    cfg = room_bridge_config(
        saved={"api_key": "k", "username": "alice"},
        env={"AITHER_ROOM_BRIDGE": "1", "AITHER_ROOM_BRIDGE_ROOMS": "#general=#acme-room,dev=#acme-dev"},
        lookup=lambda *_: pytest.fail("explicit rooms must not ask the relay"))
    assert cfg["rooms"] == {"#general": "#acme-room", "#dev": "#acme-dev"}
    assert cfg["nick"] == "alice" and cfg["token"] == "k"

    asked = room_bridge_config(saved={"api_key": "k"}, env={"AITHER_ROOM_BRIDGE": "1"},
                               lookup=lambda base, token: ("#acme-room", ""))
    assert asked["rooms"] == {"#general": "#acme-room"}
    # No configured nick: send none, so the relay posts as the bearer's identity
    # (an invented nick the bearer does not own is a 403 on every line).
    assert asked["nick"] == ""

    none = room_bridge_config(saved={"api_key": "k"}, env={"AITHER_ROOM_BRIDGE": "1"},
                              lookup=lambda base, token: ("", "no room"))
    assert none is None


def test_offline_node_keeps_working_and_catches_up(tmp_path):
    relay = StubRelay()
    chat = ChatRelay(data_dir=tmp_path)
    bridge = _bridge()
    chat.attach_room_bridge(bridge)
    asyncio.run(_cycle(bridge, chat, relay))

    relay.down = True
    assert chat.post("#general", "alice", "written offline") is not None
    asyncio.run(_cycle(bridge, chat, relay))          # must not raise
    assert relay.posts == [] and bridge.status()["pending"] == 1
    assert [m["content"] for m in _local_rows(chat)] == ["written offline"]

    relay.down = False
    asyncio.run(_cycle(bridge, chat, relay, passes=2))
    assert [p["content"] for p in relay.posts] == ["<alice> written offline"]
    assert bridge.status()["pending"] == 0


def test_unmapped_rooms_and_system_lines_stay_local(tmp_path):
    relay = StubRelay()
    chat = ChatRelay(data_dir=tmp_path)
    bridge = _bridge()
    chat.attach_room_bridge(bridge)
    chat.join("#general", "alice")                    # join line: not a message
    chat.post("#dev", "alice", "not bridged")
    asyncio.run(_cycle(bridge, chat, relay))
    assert relay.posts == []


def test_start_room_bridge_attaches_the_outbound_handler(tmp_path, monkeypatch):
    async def _no_network(self, chat):
        return None

    monkeypatch.setattr(RoomBridge, "run", _no_network)
    chat = ChatRelay(data_dir=tmp_path)

    async def _go():
        bridge = await chat.start_room_bridge(
            {"base_url": BASE, "token": "t", "nick": "n", "rooms": {"#room1": "#acme-room"}})
        bridge.stop()
        return bridge

    bridge = asyncio.run(_go())
    assert bridge is not None and "#room1" in chat._channels
    assert "platform-relay-bridge" in chat._outbound_handlers
    chat.detach_room_bridge()
    assert chat._outbound_handlers == {}
