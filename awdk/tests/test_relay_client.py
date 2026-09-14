"""Unit tests for the AitherRelay client (join + DM self-reply loop)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.relay_client import RelayClient


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = "ok"

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async httpx stand-in that scripts GET responses + records POSTs."""

    def __init__(self, get_map):
        self._get_map = get_map          # path-suffix -> payload
        self.posts = []                  # (url, json) recorded

    async def get(self, url, headers=None):
        for suffix, payload in self._get_map.items():
            if url.endswith(suffix):
                return _Resp(payload)
        return _Resp([], status=404)

    async def post(self, url, headers=None, params=None, json=None):
        self.posts.append((url, json))
        return _Resp({"success": True})


class _FakeAgent:
    def __init__(self, reply="hello back"):
        self.reply = reply
        self.seen = []

    async def chat(self, message):
        self.seen.append(message)
        class _R:
            content = self.reply
        return _R()


def _client(nick="optiplex-agent", agent=None):
    return RelayClient(base_url="https://relay.example/api/relay/v1",
                       token="tok", nick=nick, agent=agent or _FakeAgent())


@pytest.mark.asyncio
async def test_join_posts_to_agent_join():
    rc = _client()
    fc = _FakeClient({})
    ok = await rc.join(fc)
    assert ok is True
    assert fc.posts and fc.posts[0][0].endswith("/agent/join")


@pytest.mark.asyncio
async def test_replies_to_new_human_dm_on_own_inference():
    agent = _FakeAgent(reply="pong")
    rc = _client(agent=agent)
    fc = _FakeClient({
        "/dms/partners": {"partners": [{"nick": "david"}]},
        "/dms/david": {"messages": [{"id": "m1", "from_nick": "david", "content": "hi agent"}]},
    })
    sent = await rc.poll_once(fc)
    assert sent == 1
    assert agent.seen == ["hi agent"]                       # ran on its own inference
    dm_posts = [p for p in fc.posts if p[0].endswith("/dms")]
    assert dm_posts and dm_posts[-1][1] == {"to_nick": "david", "content": "pong"}


@pytest.mark.asyncio
async def test_does_not_reply_twice_to_same_message():
    rc = _client()
    fc = _FakeClient({
        "/dms/partners": {"partners": [{"nick": "david"}]},
        "/dms/david": {"messages": [{"id": "m1", "from_nick": "david", "content": "hi"}]},
    })
    assert await rc.poll_once(fc) == 1
    assert await rc.poll_once(fc) == 0                       # same last_id -> already seen


@pytest.mark.asyncio
async def test_ignores_own_outbound_message():
    rc = _client(nick="optiplex-agent")
    fc = _FakeClient({
        "/dms/partners": {"partners": [{"nick": "david"}]},
        "/dms/david": {"messages": [{"id": "m2", "from_nick": "optiplex-agent", "content": "my own reply"}]},
    })
    assert await rc.poll_once(fc) == 0                       # last msg is ours -> no reply
