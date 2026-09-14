"""Channel adapters for connecting agents to messaging platforms.

Lightweight framework providing a unified interface for Telegram, Discord,
Slack, and webhook-based messaging.  Each adapter lazy-imports its SDK so
the module loads without any optional dependency installed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

logger = logging.getLogger("adk.channels")

MessageHandler = Callable[[str, str, str, str], Awaitable[str]]
# (platform, channel_id, user_id, text) -> response

# Per-platform message size limits (characters).
LIMITS = {"telegram": 4096, "discord": 2000, "slack": 4000, "webhook": 0}

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks from LLM responses."""
    return _THINK_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class ChannelAdapter(ABC):
    """Base class for messaging platform adapters."""

    def __init__(self, token: str, on_message: MessageHandler | None = None) -> None:
        # GATED: deploying agents to messaging platforms (Discord/Telegram/
        # Slack/Webhook) is a paid-tier capability. Raises LicenseError unless
        # entitled (or enforcement disabled / tier INTERNAL).
        try:
            from adk.licensing import get_license_manager
            get_license_manager().require("channels", friendly="Channel adapters")
        except ImportError:
            pass

        self.token = token
        self.on_message = on_message
        self._running = False

    @property
    @abstractmethod
    def platform(self) -> str: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, channel_id: str, message: str) -> None: ...

    # -- helpers ----------------------------------------------------------

    def _chunk_message(self, text: str, limit: int) -> list[str]:
        """Split *text* into chunks respecting *limit*.

        Tries to break on newlines first, falls back to hard split.
        """
        if limit <= 0 or len(text) <= limit:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            idx = text.rfind("\n", 0, limit)
            if idx == -1:
                idx = limit
            chunks.append(text[:idx])
            text = text[idx:].lstrip("\n")
        return chunks

    async def _dispatch(self, channel_id: str, user_id: str, text: str) -> str | None:
        """Invoke the registered callback and return the (cleaned) reply."""
        if self.on_message is None:
            return None
        try:
            reply = await self.on_message(self.platform, channel_id, user_id, text)
            return _strip_think(reply) if reply else None
        except Exception:
            logger.exception("on_message callback failed (%s/%s)", self.platform, channel_id)
            return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class TelegramAdapter(ChannelAdapter):
    """Adapter for Telegram using *python-telegram-bot* (>=20)."""

    def __init__(self, token: str, on_message: MessageHandler | None = None) -> None:
        super().__init__(token, on_message)
        self._app: Any = None

    @property
    def platform(self) -> str:
        return "telegram"

    async def start(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import (
                Application,
                CommandHandler,
                MessageHandler as TGMsgHandler,
                filters,
            )
        except ImportError as exc:
            raise RuntimeError(
                "python-telegram-bot is required: pip install python-telegram-bot"
            ) from exc

        builder = Application.builder().token(self.token)
        self._app = builder.build()

        async def _start_cmd(update: Update, ctx: Any) -> None:
            if update.effective_chat:
                await update.effective_chat.send_message("Ready.")

        async def _handle(update: Update, ctx: Any) -> None:
            msg = update.effective_message
            if msg is None or msg.text is None:
                return
            chat_id = str(msg.chat_id)
            user_id = str(msg.from_user.id) if msg.from_user else "unknown"
            reply = await self._dispatch(chat_id, user_id, msg.text)
            if reply:
                for chunk in self._chunk_message(reply, LIMITS["telegram"]):
                    await msg.reply_text(chunk)

        self._app.add_handler(CommandHandler("start", _start_cmd))
        self._app.add_handler(TGMsgHandler(filters.TEXT & ~filters.COMMAND, _handle))

        self._running = True
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()  # type: ignore[union-attr]
        logger.info("Telegram adapter started")

    async def stop(self) -> None:
        if self._app and self._running:
            self._running = False
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram adapter stopped")

    async def send(self, channel_id: str, message: str) -> None:
        if self._app is None:
            raise RuntimeError("Adapter not started")
        for chunk in self._chunk_message(message, LIMITS["telegram"]):
            await self._app.bot.send_message(chat_id=int(channel_id), text=chunk)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

class DiscordAdapter(ChannelAdapter):
    """Adapter for Discord using *discord.py* (>=2)."""

    def __init__(self, token: str, on_message: MessageHandler | None = None) -> None:
        super().__init__(token, on_message)
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def platform(self) -> str:
        return "discord"

    async def start(self) -> None:
        try:
            import discord
        except ImportError as exc:
            raise RuntimeError(
                "discord.py is required: pip install discord.py"
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            logger.info("Discord adapter ready as %s", client.user)

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return
            # Respond to DMs or mentions
            is_dm = message.guild is None
            is_mention = client.user in message.mentions if client.user else False
            if not (is_dm or is_mention):
                return

            text = message.content
            # Strip the bot mention prefix if present
            if client.user and is_mention:
                text = re.sub(rf"<@!?{client.user.id}>\s*", "", text).strip()

            channel_id = str(message.channel.id)
            user_id = str(message.author.id)
            reply = await self._dispatch(channel_id, user_id, text)
            if reply:
                for chunk in self._chunk_message(reply, LIMITS["discord"]):
                    await message.channel.send(chunk)

        self._running = True
        self._task = asyncio.create_task(client.start(self.token))
        logger.info("Discord adapter starting")

    async def stop(self) -> None:
        if self._client and self._running:
            self._running = False
            await self._client.close()
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("Discord adapter stopped")

    async def send(self, channel_id: str, message: str) -> None:
        if self._client is None:
            raise RuntimeError("Adapter not started")
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(channel_id))
        for chunk in self._chunk_message(message, LIMITS["discord"]):
            await channel.send(chunk)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class SlackAdapter(ChannelAdapter):
    """Adapter for Slack using *slack-bolt* with Socket Mode."""

    def __init__(
        self,
        token: str,
        on_message: MessageHandler | None = None,
        *,
        app_token: str = "",
    ) -> None:
        super().__init__(token, on_message)
        self.app_token = app_token
        self._bolt_app: Any = None
        self._handler: Any = None

    @property
    def platform(self) -> str:
        return "slack"

    async def start(self) -> None:
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError as exc:
            raise RuntimeError(
                "slack-bolt is required: pip install slack-bolt"
            ) from exc

        if not self.app_token:
            raise ValueError("app_token is required for Slack Socket Mode")

        self._bolt_app = AsyncApp(token=self.token)

        async def _handle_event(event: dict[str, Any], say: Any) -> None:
            text = event.get("text", "")
            channel_id = event.get("channel", "")
            user_id = event.get("user", "unknown")
            # Strip bot mention from text
            text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
            reply = await self._dispatch(channel_id, user_id, text)
            if reply:
                for chunk in self._chunk_message(reply, LIMITS["slack"]):
                    await say(chunk)

        self._bolt_app.event("app_mention")(_handle_event)
        self._bolt_app.event("message")(_handle_event)

        self._handler = AsyncSocketModeHandler(self._bolt_app, self.app_token)
        self._running = True
        await self._handler.start_async()
        logger.info("Slack adapter started (socket mode)")

    async def stop(self) -> None:
        if self._handler and self._running:
            self._running = False
            await self._handler.close_async()
            logger.info("Slack adapter stopped")

    async def send(self, channel_id: str, message: str) -> None:
        if self._bolt_app is None:
            raise RuntimeError("Adapter not started")
        for chunk in self._chunk_message(message, LIMITS["slack"]):
            await self._bolt_app.client.chat_postMessage(channel=channel_id, text=chunk)


# ---------------------------------------------------------------------------
# Webhook (FastAPI)
# ---------------------------------------------------------------------------

class WebhookAdapter(ChannelAdapter):
    """Adapter that exposes a FastAPI POST endpoint for generic webhooks.

    Accepts JSON: ``{"text": "...", "user_id": "...", "channel_id": "..."}``
    Returns JSON: ``{"response": "..."}``
    """

    def __init__(
        self,
        token: str,
        on_message: MessageHandler | None = None,
        *,
        host: str = "0.0.0.0",
        port: int = 8090,
        path: str = "/webhook",
    ) -> None:
        super().__init__(token, on_message)
        self.host = host
        self.port = port
        self.path = path
        self._server: Any = None

    @property
    def platform(self) -> str:
        return "webhook"

    async def start(self) -> None:
        try:
            from fastapi import FastAPI, Header, HTTPException
            from pydantic import BaseModel
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "fastapi + uvicorn are required: pip install fastapi uvicorn"
            ) from exc

        app = FastAPI(title="ADK Webhook Channel")

        class Payload(BaseModel):
            text: str
            user_id: str = "webhook"
            channel_id: str = "default"

        class Response(BaseModel):
            response: str

        adapter = self

        @app.post(self.path, response_model=Response)
        async def handle_webhook(
            payload: Payload,
            authorization: str | None = Header(default=None),
        ) -> Response:
            # Simple bearer check
            if adapter.token:
                expected = f"Bearer {adapter.token}"
                if authorization != expected:
                    raise HTTPException(status_code=401, detail="Unauthorized")
            reply = await adapter._dispatch(
                payload.channel_id, payload.user_id, payload.text
            )
            return Response(response=reply or "")

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info")
        self._server = uvicorn.Server(config)
        self._running = True
        asyncio.create_task(self._server.serve())
        logger.info("Webhook adapter listening on %s:%d%s", self.host, self.port, self.path)

    async def stop(self) -> None:
        if self._server and self._running:
            self._running = False
            self._server.should_exit = True
            logger.info("Webhook adapter stopped")

    async def send(self, channel_id: str, message: str) -> None:
        # Webhook adapter is pull-based; outbound push is a no-op.
        logger.debug("Webhook send is a no-op (channel=%s)", channel_id)
