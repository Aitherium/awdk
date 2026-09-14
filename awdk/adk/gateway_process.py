"""Multi-channel gateway process -- run an agent across multiple platforms.

Usage:
    adk gateway --telegram --discord --slack
    adk gateway --webhook --port 9000

Reads tokens from environment variables:
    TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN, SLACK_APP_TOKEN
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

logger = logging.getLogger("adk.gateway_process")


async def run_gateway(
    agent_name: str = "assistant",
    telegram: bool = False,
    discord: bool = False,
    slack: bool = False,
    webhook: bool = False,
    webhook_port: int = 9000,
) -> None:
    """Start a multi-channel gateway serving a single agent."""
    from adk.agent import AitherAgent

    agent = AitherAgent(agent_name)

    async def on_message(platform: str, channel_id: str, user_id: str, text: str) -> str:
        resp = await agent.chat(text, session_id=f"{platform}:{channel_id}:{user_id}")
        return resp.content

    adapters = []

    try:
        from adk.channels import (
            TelegramAdapter,
            DiscordAdapter,
            SlackAdapter,
            WebhookAdapter,
        )
    except ImportError as e:
        print(f"Channel adapters not available: {e}")
        print("Install with: pip install awdk")
        sys.exit(1)

    if telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            print("Set TELEGRAM_BOT_TOKEN environment variable")
            sys.exit(1)
        adapters.append(TelegramAdapter(token=token, on_message=on_message))

    if discord:
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            print("Set DISCORD_BOT_TOKEN environment variable")
            sys.exit(1)
        adapters.append(DiscordAdapter(token=token, on_message=on_message))

    if slack:
        bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not bot_token or not app_token:
            print("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN environment variables")
            sys.exit(1)
        adapters.append(SlackAdapter(
            token=bot_token, app_token=app_token, on_message=on_message,
        ))

    if webhook:
        adapters.append(WebhookAdapter(
            token="", on_message=on_message, port=webhook_port,
        ))

    if not adapters:
        print("No channels enabled. Use --telegram, --discord, --slack, or --webhook")
        sys.exit(1)

    platforms = [a.platform for a in adapters]
    print(f"Starting gateway for agent '{agent_name}' on: {', '.join(platforms)}")

    # Start all adapters
    tasks = []
    for adapter in adapters:
        await adapter.start()
        logger.info("Started %s adapter", adapter.platform)

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    print("Gateway running. Press Ctrl+C to stop.")

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass

    # Stop all adapters
    print("\nShutting down...")
    for adapter in adapters:
        try:
            await adapter.stop()
        except Exception as e:
            logger.warning("Error stopping %s: %s", adapter.platform, e)

    print("Gateway stopped.")


def cmd_gateway(args) -> int:
    """CLI entry point for `adk gateway`."""
    asyncio.run(run_gateway(
        agent_name=getattr(args, "agent", "assistant"),
        telegram=getattr(args, "telegram", False),
        discord=getattr(args, "discord", False),
        slack=getattr(args, "slack", False),
        webhook=getattr(args, "webhook", False),
        webhook_port=getattr(args, "webhook_port", 9000),
    ))
    return 0
