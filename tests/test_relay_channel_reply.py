"""An agent answers a message ADDRESSED to it in a channel, and only that.

Measured 2026-09-20: a Claude session could already address an agent
(`awrelay send '#agents' ... --to lyra`) and hear an answer inside its own turn,
but nothing on the agent side ever answered -- so every exchange still needed a
human to carry it across. This is the other half of that loop, plus the two ways
it goes wrong: replying to broadcast (noise), and re-answering the whole window
after a restart (an agent that looks stuck).
"""
from __future__ import annotations

import json

import pytest
from adk.relay_client import RelayClient, addressed_to_me, strip_envelope

NICK = "lyra"


def _msg(mid: str, sender: str, text: str, to: list[str] | None = None,
         corr: str = "") -> dict:
    body = json.dumps({"kind": "request", "sender": sender,
                       "payload": {"to": to} if to else {},
                       "correlation_id": corr, "sent_at": ""},
                      ensure_ascii=False, sort_keys=True)
    return {"id": mid, "nick": sender,
            "content": f"[request] {text}\n```awrelay\n{body}\n```"}


class _Agent:
    def __init__(self, answer: str = "on it"):
        self.answer, self.seen = answer, []

    async def chat(self, text: str):
        self.seen.append(text)
        return self.answer


class _FakeHTTP:
    """Serves one channel window and records what was posted back."""

    def __init__(self, rows: list[dict]):
        self.rows, self.posts = rows, []

    async def get(self, url, headers=None):
        return _Resp({"messages": self.rows})

    async def post(self, url, headers=None, json=None, **kw):
        self.posts.append((url, json))
        return _Resp({})


class _Resp:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _client(agent: _Agent) -> RelayClient:
    return RelayClient(base_url="https://relay.invalid/v1", token="t", nick=NICK,
                       agent=agent, channel="#agents", verify=False)


@pytest.mark.parametrize("content,expected", [
    (_msg("1", "david+77db6255", "lyra can you check the vault?", to=["lyra"])["content"], True),
    (_msg("2", "david+77db6255", "anyone seen the vault?")["content"], False),
    ("plain chat, no envelope, mentioning lyra", False),
    ("plain chat pinging @lyra directly", True),
])
def test_only_addressed_messages_count(content, expected):
    assert addressed_to_me(content, NICK) is expected


def test_a_session_suffix_addresses_the_session_not_a_prefix_of_another():
    content = _msg("3", "peer", "hi", to=["77db6255"])["content"]
    assert addressed_to_me(content, "david+77db6255") is True
    assert addressed_to_me(content, "david+77db6000") is False


def test_strip_envelope_leaves_the_human_half():
    assert strip_envelope(_msg("4", "p", "do the thing", to=["lyra"])["content"]) == "do the thing"


@pytest.mark.asyncio
async def test_the_first_pass_only_primes_then_answers_once():
    rows = [_msg("1", "david+77db6255", "lyra: status?", to=["lyra"], corr="c-9")]
    agent = _Agent("all green")
    c, http = _client(agent), _FakeHTTP(rows)
    assert await c.poll_channel_once(http) == 0, "a restart must not re-answer the backlog"
    assert agent.seen == [] and http.posts == []

    http.rows = rows + [_msg("2", "david+77db6255", "lyra: and the db?", to=["lyra"])]
    assert await c.poll_channel_once(http) == 1
    assert agent.seen == ["lyra: and the db?"]
    url, body = http.posts[-1]
    assert url.endswith("/channels/agents/messages")
    assert body["nick"] == NICK and body["agent"] is True
    payload = json.loads(body["content"].split("```awrelay\n", 1)[1].split("\n```", 1)[0])
    assert payload["payload"]["to"] == ["david+77db6255"], "the reply is addressed BACK"
    assert "all green" in body["content"]

    assert await c.poll_channel_once(http) == 0, "one mention, one reply"


@pytest.mark.asyncio
async def test_broadcast_and_self_posts_are_left_alone():
    agent = _Agent()
    c, http = _client(agent), _FakeHTTP([])
    await c.poll_channel_once(http)          # prime on empty
    http.rows = [_msg("10", "david+77db6255", "fleet is rebuilding"),
                 _msg("11", NICK, "lyra talking to lyra", to=["lyra"])]
    assert await c.poll_channel_once(http) == 0
    assert agent.seen == [] and http.posts == []


@pytest.mark.asyncio
async def test_a_failing_turn_still_answers_instead_of_going_silent():
    class _Broken(_Agent):
        async def chat(self, text: str):
            raise RuntimeError("model unreachable")

    agent = _Broken()
    c, http = _client(agent), _FakeHTTP([])
    await c.poll_channel_once(http)
    http.rows = [_msg("20", "david+77db6255", "lyra: ping", to=["lyra"])]
    assert await c.poll_channel_once(http) == 1
    assert "agent error" in http.posts[-1][1]["content"], \
        "a peer waiting on an answer must hear the failure, not silence"


@pytest.mark.asyncio
async def test_the_channel_is_joined_before_the_first_answer():
    """#agents refuses a non-member with the same 403 a door uses, so a loop driven
    without run() posted its answer into a refusal and logged success."""
    agent = _Agent()
    c, http = _client(agent), _FakeHTTP([])
    await c.poll_channel_once(http)
    http.rows = [_msg("30", "david+77db6255", "lyra: ping", to=["lyra"])]
    await c.poll_channel_once(http)
    urls = [u for u, _ in http.posts]
    assert any(u.endswith("/agent/join") for u in urls), "the agent never joined"
    first_join = next(i for i, u in enumerate(urls) if u.endswith("/agent/join"))
    first_post = next(i for i, u in enumerate(urls) if u.endswith("/channels/agents/messages"))
    assert first_join < first_post, "join must precede the answer"
