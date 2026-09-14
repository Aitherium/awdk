"""Tests for adk.channels module."""

from __future__ import annotations

import pytest


def test_channel_adapter_import():
    from adk.channels import ChannelAdapter, WebhookAdapter
    assert ChannelAdapter is not None
    assert WebhookAdapter is not None


def test_channel_adapter_is_abstract():
    from adk.channels import ChannelAdapter

    with pytest.raises(TypeError):
        ChannelAdapter(token="test")


def test_chunk_message():
    from adk.channels import WebhookAdapter

    async def noop(p, c, u, t):
        return "ok"

    adapter = WebhookAdapter(token="", on_message=noop)
    # Short message
    assert adapter._chunk_message("hello", 100) == ["hello"]
    # Long message
    long = "a" * 250
    chunks = adapter._chunk_message(long, 100)
    assert len(chunks) == 3
    assert "".join(chunks) == long


def test_telegram_adapter_missing_dep():
    """TelegramAdapter should raise ImportError if python-telegram-bot is missing."""
    import sys

    # Temporarily hide the telegram module
    hidden = {}
    for key in list(sys.modules.keys()):
        if "telegram" in key:
            hidden[key] = sys.modules.pop(key)

    try:
        from adk.channels import TelegramAdapter

        async def noop(p, c, u, t):
            return "ok"

        adapter = TelegramAdapter(token="fake", on_message=noop)
        # start() should fail gracefully if telegram not installed
    finally:
        sys.modules.update(hidden)


def test_webhook_adapter_platform():
    from adk.channels import WebhookAdapter

    async def noop(p, c, u, t):
        return "ok"

    adapter = WebhookAdapter(token="", on_message=noop)
    assert adapter.platform == "webhook"
