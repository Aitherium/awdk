"""Telegram, Discord and Slack as Hearth transports -- direct messages only.

Each transport WRAPS the matching :mod:`adk.channels` adapter: the adapter owns
the SDK client, polling / gateway / socket-mode loop and per-platform chunking
(``LIMITS``); this module adds only what an owner-only channel needs on top.

* ``user_id`` is the platform's stable id -- Telegram's numeric user id,
  Discord's snowflake, Slack's ``U``/``W`` member id. Anything else (a display
  name, the adapters' ``"unknown"`` placeholder) is dropped before the core
  sees it, so an owner can never be bound to a name somebody else can take.
* Only direct messages reach :class:`adk.home.hearth.HearthCore`: a Telegram
  private chat, a Discord DM channel whose one recipient is the sender, a Slack
  IM (``D...``). A group, server channel or @mention is dropped even from the
  owner, because everyone there would read the reply.
* Replies go back to the DM the owner last wrote from; with none yet (a
  follow-up after a restart) the transport opens the DM itself.

Licensing: talking to your OWN agent over your own bot is free (bring your own
channel). :class:`adk.channels.ChannelAdapter` still asks for the ``channels``
entitlement for general bots, so the owner-only, DM-only transports here build
their adapter with ``licensed=True`` -- nowhere else does.

Tokens come from the environment only (see ``adk.home.hearth.CHANNEL_ENV``):
never argv, never logged, never in a receipt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from ..hearth import CHANNEL_ENV, SLACK_APP_ENV, ChannelAdapterTransport, HomeError, _env_secret

logger = logging.getLogger("adk.home.transports.chat")

__all__ = [
    "ChatTransport", "TelegramTransport", "DiscordTransport", "SlackTransport",
    "CHAT_TRANSPORTS", "build_chat_transport",
]


class ChatTransport(ChannelAdapterTransport):
    """Shared DM-only bridge; subclasses say what a DM and a user id look like."""

    platform = ""
    #: A stable platform user id. Anything else is not a user we can bind.
    user_id_re = re.compile(r"\d+")

    def __init__(self, adapter: Any, name: str = ""):
        super().__init__(adapter, name or self.platform, private_only=False)
        self.dropped = 0

    @classmethod
    def from_env(cls) -> "ChatTransport":
        """What ``adk home serve`` calls: :func:`build_chat_transport` for this platform."""
        return build_chat_transport(cls.platform)

    # -- what subclasses decide ---------------------------------------------
    async def is_direct(self, channel_id: str, user_id: str) -> bool:
        raise NotImplementedError

    async def open_dm(self, user_id: str) -> str:
        """The DM channel id for ``user_id`` when the owner has not written yet."""
        return user_id

    # -- the bridge -----------------------------------------------------------
    def _drop(self, why: str) -> None:
        # Never the text, never the sender's name: one line saying why.
        self.dropped += 1
        logger.warning("hearth: ignored a %s message (%s)", self.name, why)

    async def _on_message(self, platform: str, channel_id: str, user_id: str,
                          text: str) -> Optional[str]:
        if self.core is None:
            return None
        uid, cid = str(user_id or "").strip(), str(channel_id or "").strip()
        if not cid or not self.user_id_re.fullmatch(uid):
            self._drop("no stable user id")
            return None
        if not await self.is_direct(cid, uid):
            self._drop("not a direct message")
            return None
        self._routes[uid] = cid
        await self.core.on_message(self.name, uid, text or "")
        return None  # the core replies through send(), so every reply is receipted

    async def send(self, user_id: str, text: str) -> bool:
        uid = str(user_id)
        try:
            route = self._routes.get(uid) or await self.open_dm(uid)
            if not route:
                return False
            self._routes[uid] = route
            await self.adapter.send(route, text)  # the adapter chunks to its limit
        except Exception as exc:  # noqa: BLE001 - a failed send is a False, not a crash
            logger.error("hearth: %s send failed: %s", self.name, type(exc).__name__)
            return False
        return True


class TelegramTransport(ChatTransport):
    """A Telegram private chat's id IS the user's id; groups are negative ids."""

    platform = "telegram"
    REQUIRED_ENV = (CHANNEL_ENV["telegram"],)

    async def is_direct(self, channel_id: str, user_id: str) -> bool:
        return channel_id == user_id


class DiscordTransport(ChatTransport):
    """A DM is a guild-less channel whose single recipient is the sender.

    Asked of the adapter's own client cache (discord.py caches the DM channel of
    every message it delivers). Unknown channel = not a DM: fail closed.
    """

    platform = "discord"
    REQUIRED_ENV = (CHANNEL_ENV["discord"],)

    def _client(self) -> Any:
        return getattr(self.adapter, "_client", None)

    async def is_direct(self, channel_id: str, user_id: str) -> bool:
        client = self._client()
        if client is None or not channel_id.isdigit():
            return False
        channel = client.get_channel(int(channel_id))
        if channel is None or getattr(channel, "guild", None) is not None:
            return False
        recipient = getattr(channel, "recipient", None)  # group DMs have none
        return recipient is not None and str(getattr(recipient, "id", "")) == user_id

    async def open_dm(self, user_id: str) -> str:
        client = self._client()
        if client is None:
            return ""
        user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
        dm = getattr(user, "dm_channel", None) or await user.create_dm()
        return str(dm.id)


class SlackTransport(ChatTransport):
    """A Slack IM's conversation id starts with ``D``; members are ``U``/``W`` ids.

    The bot's own posts come back as ``message`` events in the IM; they are
    dropped so they cannot burn pairing attempts. ``start`` runs the adapter in
    the background because socket mode's ``start_async`` never returns.
    """

    platform = "slack"
    REQUIRED_ENV = (CHANNEL_ENV["slack"], SLACK_APP_ENV)
    user_id_re = re.compile(r"[UW][A-Z0-9]+")

    def __init__(self, adapter: Any, name: str = "", *, start_grace: float = 5.0):
        super().__init__(adapter, name)
        self.start_grace = start_grace
        self._self_id: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self, core: Any) -> None:
        self.core = core
        self.adapter.on_message = self._on_message
        self._task = asyncio.ensure_future(self.adapter.start())
        done, _ = await asyncio.wait({self._task}, timeout=self.start_grace)
        if done:
            self._task.result()  # a failed start (bad token, no slack-bolt) raises here

    async def stop(self) -> None:
        try:
            await self.adapter.stop()
        finally:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    logger.debug("hearth: %s listener cancelled", self.name)
                except Exception as exc:  # noqa: BLE001 - stopping; report, do not raise
                    logger.debug("hearth: %s listener ended with %s", self.name, exc)

    async def _bot_user_id(self) -> str:
        if self._self_id is None:
            try:
                auth = await self.adapter._bolt_app.client.auth_test()
                self._self_id = str(auth.get("user_id") or "")
            except Exception as exc:  # noqa: BLE001 - best effort; retried next message
                logger.warning("hearth: slack auth.test failed: %s", type(exc).__name__)
                return ""
        return self._self_id

    async def is_direct(self, channel_id: str, user_id: str) -> bool:
        if not channel_id.startswith("D"):
            return False
        return user_id != await self._bot_user_id()


CHAT_TRANSPORTS: Dict[str, type] = {
    "telegram": TelegramTransport,
    "discord": DiscordTransport,
    "slack": SlackTransport,
}


def build_chat_transport(channel: str) -> ChatTransport:
    """The Hearth transport for ``channel``, token read from the environment.

    Raises :class:`adk.home.config.HomeError` naming the env var (never a value)
    when a token is unset, and ``adk.licensing.LicenseError`` when neither the
    Agent Home kit nor a tier with ``channels`` is owned.
    """
    from adk import channels

    cls = CHAT_TRANSPORTS.get(channel)
    if cls is None:
        raise HomeError(f"unknown chat channel {channel!r} "
                        f"(known: {', '.join(sorted(CHAT_TRANSPORTS))})")
    names = CHANNEL_ENV[channel]
    token = _env_secret(names)
    if not token:
        raise HomeError(f"{channel}: set {names[0]} (the bot token) in the environment")
    if channel == "telegram":
        adapter: Any = channels.TelegramAdapter(token, licensed=True)
    elif channel == "discord":
        adapter = channels.DiscordAdapter(token, licensed=True)
    else:
        app_token = _env_secret(SLACK_APP_ENV)
        if not app_token:
            raise HomeError(f"slack: set {SLACK_APP_ENV[0]} (the Socket Mode app token)")
        adapter = channels.SlackAdapter(token, app_token=app_token, licensed=True)
    return cls(adapter, channel)
