"""Hearth's Telegram / Discord / Slack transports (adk.home.transports.chat).

No network: the real adk.channels adapters are built with fake SDK internals (so
their own chunking is what runs), or replaced by a fake adapter object.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from adk import channels
from adk.home import entitlement
from adk.home.config import HomeError
from adk.home.hearth import Transport
from adk.home.transports import chat
from adk.licensing import License, LicenseError, LicenseManager, Tier

TG_OWNER = "123456789"
DC_OWNER = "112233445566778899"
SL_OWNER = "U0OWNER01"


class FakeCore:
    def __init__(self):
        self.seen = []

    async def on_message(self, channel, user_id, text):
        self.seen.append((channel, user_id, text))
        return True


class FakeAdapter:
    """Stands in for an adk.channels adapter: records sends, never touches a network."""

    platform = "telegram"

    def __init__(self):
        self.on_message = None
        self.sent = []
        self.started = False
        self._client = None

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def send(self, channel_id, message):
        self.sent.append((channel_id, message))


async def _started(transport, adapter=None):
    core = FakeCore()
    await transport.start(core)
    return core, (adapter or transport.adapter)


def _no_license(monkeypatch, tmp_path, *, tier=Tier.COMMUNITY, packs=()):
    monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "1")
    lic = License(tier=tier, packs=list(packs))
    from adk.licensing import Entitlements

    lic.entitlements = Entitlements.for_tier(tier)
    monkeypatch.setattr(entitlement, "_manager", lambda: LicenseManager(lic))
    import adk.licensing as licensing

    monkeypatch.setattr(licensing, "get_license_manager", lambda: LicenseManager(lic))


# ── protocol ──────────────────────────────────────────────────────────────────

def test_every_chat_transport_is_a_hearth_transport():
    for cls in (chat.TelegramTransport, chat.DiscordTransport, chat.SlackTransport):
        t = cls(FakeAdapter())
        assert isinstance(t, Transport)
        assert t.name == cls.platform and chat.CHAT_TRANSPORTS[t.name] is cls
        assert callable(cls.from_env) and cls.REQUIRED_ENV


# ── Telegram ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_only_private_chats_reach_the_core():
    t = chat.TelegramTransport(FakeAdapter())
    core, adapter = await _started(t)
    assert adapter.started and adapter.on_message == t._on_message
    # a group (negative chat id) and a chat that is not the sender's own are dropped
    assert await adapter.on_message("telegram", "-100777", TG_OWNER, "hi") is None
    await adapter.on_message("telegram", "987", TG_OWNER, "hi")
    assert core.seen == [] and t.dropped == 2
    await adapter.on_message("telegram", TG_OWNER, TG_OWNER, "hello")
    assert core.seen == [("telegram", TG_OWNER, "hello")]


@pytest.mark.asyncio
async def test_user_id_must_be_the_stable_id_never_a_name():
    t = chat.TelegramTransport(FakeAdapter())
    core, adapter = await _started(t)
    for bad in ("unknown", "alice", "@alice", "", "12 34"):
        await adapter.on_message("telegram", bad, bad, "pair 123456")
    assert core.seen == [] and t.dropped == 5
    s = chat.SlackTransport(FakeAdapter())
    s._self_id = "UBOT"
    score, sadapter = await _started(s)
    await sadapter.on_message("slack", "D01", "David Smith", "hi")
    await sadapter.on_message("slack", "D01", "unknown", "hi")
    assert score.seen == []


@pytest.mark.asyncio
async def test_telegram_send_reuses_the_adapter_chunking():
    adapter = channels.TelegramAdapter("tok", licensed=True)
    posted = []

    async def send_message(chat_id, text):
        posted.append((chat_id, text))

    adapter._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    t = chat.TelegramTransport(adapter)
    t.core = FakeCore()
    long = "\n".join("x" * 100 for _ in range(100))  # ~10k chars > 4096
    assert await t.send(TG_OWNER, long) is True  # no route yet: private chat = user id
    assert len(posted) >= 3
    assert all(cid == int(TG_OWNER) and len(txt) <= channels.LIMITS["telegram"]
               for cid, txt in posted)
    assert "".join(txt for _, txt in posted).replace("\n", "") == long.replace("\n", "")


@pytest.mark.asyncio
async def test_a_failed_send_is_false_not_a_crash():
    class Boom(FakeAdapter):
        async def send(self, channel_id, message):
            raise ConnectionError("down")

    t = chat.TelegramTransport(Boom())
    assert await t.send(TG_OWNER, "x") is False


# ── Discord ───────────────────────────────────────────────────────────────────

class FakeDiscordClient:
    def __init__(self, channels_by_id, users=None):
        self.channels_by_id = channels_by_id
        self.users = users or {}

    def get_channel(self, cid):
        return self.channels_by_id.get(cid)

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        return self.users[uid]


def _dm(cid, recipient_id):
    posted = []

    async def send(chunk):
        posted.append(chunk)

    return SimpleNamespace(id=cid, guild=None, recipient=SimpleNamespace(id=int(recipient_id)),
                           send=send, posted=posted)


@pytest.mark.asyncio
async def test_discord_only_the_senders_own_dm_reaches_the_core():
    dm = _dm(555, DC_OWNER)
    guild_channel = SimpleNamespace(id=666, guild=SimpleNamespace(id=1), recipient=None)
    group_dm = SimpleNamespace(id=777, guild=None, recipient=None)
    someone_elses_dm = _dm(888, "999999999999999999")
    adapter = FakeAdapter()
    adapter._client = FakeDiscordClient(
        {555: dm, 666: guild_channel, 777: group_dm, 888: someone_elses_dm})
    t = chat.DiscordTransport(adapter)
    core, _ = await _started(t)
    # an @mention in a server channel is dropped even from the owner
    for cid in ("666", "777", "888", "404", "not-a-number"):
        await adapter.on_message("discord", cid, DC_OWNER, "hi")
    assert core.seen == [] and t.dropped == 5
    await adapter.on_message("discord", "555", DC_OWNER, "hello")
    assert core.seen == [("discord", DC_OWNER, "hello")]
    assert await t.send(DC_OWNER, "reply") is True
    assert adapter.sent == [("555", "reply")]


@pytest.mark.asyncio
async def test_discord_followup_opens_the_dm_and_reuses_adapter_chunking():
    dm = _dm(4242, DC_OWNER)

    async def create_dm():
        return dm

    user = SimpleNamespace(id=int(DC_OWNER), dm_channel=None, create_dm=create_dm)
    adapter = channels.DiscordAdapter("tok", licensed=True)
    adapter._client = FakeDiscordClient({4242: dm}, users={})

    async def fetch_user(uid):
        assert uid == int(DC_OWNER)
        return user

    adapter._client.fetch_user = fetch_user
    t = chat.DiscordTransport(adapter)
    t.core = FakeCore()
    text = "y" * 4500
    assert await t.send(DC_OWNER, text) is True
    assert [len(c) for c in dm.posted] == [2000, 2000, 500]
    assert t._routes[DC_OWNER] == "4242"


# ── Slack ─────────────────────────────────────────────────────────────────────

class FakeSlackClient:
    def __init__(self, bot_user="UBOT"):
        self.posted = []
        self.bot_user = bot_user

    async def auth_test(self):
        return {"user_id": self.bot_user}

    async def chat_postMessage(self, channel, text):  # noqa: N802 - Slack SDK name
        self.posted.append((channel, text))


@pytest.mark.asyncio
async def test_slack_only_ims_reach_the_core_and_its_own_posts_are_dropped():
    adapter = FakeAdapter()
    adapter._bolt_app = SimpleNamespace(client=FakeSlackClient())
    t = chat.SlackTransport(adapter, start_grace=0.01)
    core, _ = await _started(t)
    await adapter.on_message("slack", "C0GENERAL", SL_OWNER, "hi")   # public channel
    await adapter.on_message("slack", "G0PRIVATE", SL_OWNER, "hi")   # private / mpim
    await adapter.on_message("slack", "D0IM", "UBOT", "Paired.")     # the bot's own post
    assert core.seen == [] and t.dropped == 3
    await adapter.on_message("slack", "D0IM", SL_OWNER, "hello")
    assert core.seen == [("slack", SL_OWNER, "hello")]
    await t.stop()


@pytest.mark.asyncio
async def test_slack_send_reuses_adapter_chunking_and_dms_the_user_id():
    adapter = channels.SlackAdapter("tok", app_token="app", licensed=True)
    client = FakeSlackClient()
    adapter._bolt_app = SimpleNamespace(client=client)
    t = chat.SlackTransport(adapter)
    assert await t.send(SL_OWNER, "z" * 9000) is True
    assert [ch for ch, _ in client.posted] == [SL_OWNER] * 3
    assert [len(x) for _, x in client.posted] == [4000, 4000, 1000]


@pytest.mark.asyncio
async def test_slack_start_does_not_wait_on_a_socket_loop_that_never_returns():
    class Forever(FakeAdapter):
        async def start(self):
            self.started = True
            await asyncio.sleep(3600)

    adapter = Forever()
    t = chat.SlackTransport(adapter, start_grace=0.01)
    await asyncio.wait_for(t.start(FakeCore()), timeout=2)
    assert adapter.started
    await t.stop()
    assert t._task.cancelled() or t._task.done()


@pytest.mark.asyncio
async def test_slack_start_failure_surfaces():
    class Bad(FakeAdapter):
        async def start(self):
            raise RuntimeError("slack-bolt is required")

    t = chat.SlackTransport(Bad(), start_grace=1)
    with pytest.raises(RuntimeError):
        await t.start(FakeCore())


# ── licensing + construction ─────────────────────────────────────────────────

def test_owner_channels_are_free_but_general_adapters_stay_licensed(monkeypatch, tmp_path):
    """Pricing: free = bring your own channels. No kit, community tier: the owner's
    own Telegram bot works; a general adk.channels bot still needs a licence."""
    _no_license(monkeypatch, tmp_path)
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "123:secret-token-value")
    t = chat.TelegramTransport.from_env()
    assert isinstance(t, chat.TelegramTransport)
    assert t.adapter.token == "123:secret-token-value"
    with pytest.raises(LicenseError):
        channels.TelegramAdapter("tok")


def test_slack_owner_channel_needs_no_tier(monkeypatch, tmp_path):
    _no_license(monkeypatch, tmp_path)
    monkeypatch.setenv("HEARTH_SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("HEARTH_SLACK_APP_TOKEN", "xapp-fake")
    t = chat.build_chat_transport("slack")
    assert isinstance(t, chat.SlackTransport) and t.adapter.app_token == "xapp-fake"


def test_missing_token_names_the_env_var_not_a_value(monkeypatch):
    for n in ("HEARTH_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    with pytest.raises(HomeError, match="HEARTH_TELEGRAM_TOKEN"):
        chat.build_chat_transport("telegram")
    monkeypatch.setenv("HEARTH_SLACK_BOT_TOKEN", "xoxb-fake")
    for n in ("HEARTH_SLACK_APP_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "0")
    with pytest.raises(HomeError, match="HEARTH_SLACK_APP_TOKEN"):
        chat.build_chat_transport("slack")
    with pytest.raises(HomeError):
        chat.build_chat_transport("irc")
