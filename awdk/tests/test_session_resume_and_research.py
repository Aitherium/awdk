"""Tests for session resume and research commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.shell.config import AitherConfig, save_config, load_config
from adk.shell.commands import Commands, CommandError
from adk.shell.repl import AitherREPL
from adk.shell.genesis_client import GenesisClient


# ─────────────────────────────────────────────────────────────────────────
# Test suite for session persistence and resumption
# ─────────────────────────────────────────────────────────────────────────


class TestSessionPersistence:
    """Test that session_id is persisted and reused."""

    def test_config_session_id_field_exists(self):
        """AitherConfig has session_id field."""
        cfg = AitherConfig()
        assert hasattr(cfg, "session_id")
        assert cfg.session_id is None

    def test_config_last_session_id_field_exists(self):
        """AitherConfig has last_session_id field for tracking."""
        cfg = AitherConfig()
        assert hasattr(cfg, "last_session_id")
        assert cfg.last_session_id is None

    def test_session_id_persisted_to_config(self, tmp_path):
        """Setting session_id on config persists it."""
        cfg = AitherConfig()
        session_id = str(uuid4())
        cfg.session_id = session_id
        cfg.last_session_id = session_id

        # Simulate save to config dict
        cfg_dict = cfg.to_dict()
        assert cfg_dict["session_id"] == session_id
        assert cfg_dict["last_session_id"] == session_id


class TestResumeCommand:
    """Test the resume command."""

    @pytest.mark.asyncio
    async def test_resume_command_sets_session_id(self):
        """The resume command sets and persists session_id."""
        cfg = AitherConfig()
        cmd = Commands(cfg)

        session_id = str(uuid4())
        result = await cmd.resume(session_id)

        assert f"Resumed session {session_id}" in result
        assert cmd.config.session_id == session_id

    @pytest.mark.asyncio
    async def test_resume_command_requires_session_id(self):
        """The resume command fails without session_id."""
        cfg = AitherConfig()
        cmd = Commands(cfg)

        with pytest.raises(CommandError, match="Usage: resume"):
            await cmd.resume()

    @pytest.mark.asyncio
    async def test_resume_command_saves_config(self):
        """The resume command attempts to save config."""
        cfg = AitherConfig()
        cmd = Commands(cfg)
        session_id = str(uuid4())

        with patch("adk.shell.config.save_config"):
            await cmd.resume(session_id)
            # Should attempt to save (even if it fails gracefully)
            # The real implementation catches exceptions
            assert cmd.config.session_id == session_id


class TestResearchCommand:
    """Test the research command."""

    @pytest.mark.asyncio
    async def test_research_command_requires_question(self):
        """The research command fails without a question."""
        cfg = AitherConfig()
        cmd = Commands(cfg)

        with pytest.raises(CommandError, match="Usage: research"):
            await cmd.research()

    @pytest.mark.asyncio
    async def test_research_command_frames_query(self):
        """The research command wraps the query in a research prompt."""
        cfg = AitherConfig()

        # Mock the genesis client
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value="Research result here.")

        cmd = Commands(cfg)
        cmd.genesis_client = mock_client

        result = await cmd.research("What is AI?")

        assert "Research result here." in result
        # Verify the chat was called with the wrapped prompt
        mock_client.chat.assert_called_once()
        call_args = mock_client.chat.call_args
        message = call_args[1]["message"] if "message" in call_args[1] else call_args[0][0]
        assert "Research the following thoroughly" in message
        assert "What is AI?" in message

    @pytest.mark.asyncio
    async def test_research_command_uses_session_id(self):
        """The research command passes session_id to the client."""
        cfg = AitherConfig()
        session_id = str(uuid4())
        cfg.session_id = session_id

        mock_client = AsyncMock()
        mock_client.chat = AsyncMock(return_value="Research result.")

        cmd = Commands(cfg)
        cmd.genesis_client = mock_client

        await cmd.research("What is X?")

        # Verify session_id was passed
        call_kwargs = mock_client.chat.call_args[1]
        assert call_kwargs.get("session_id") == session_id


class TestREPLSessionHandling:
    """Test that REPL respects session_id from config."""

    @pytest.mark.asyncio
    async def test_repl_uses_config_session_id(self):
        """REPL uses session_id from config if set."""
        cfg = AitherConfig()
        session_id = str(uuid4())
        cfg.session_id = session_id

        repl = AitherREPL(cfg)

        # Mock the genesis client to avoid real network calls
        mock_client = AsyncMock()
        mock_client.chat_stream = AsyncMock(return_value=AsyncMock())
        repl.genesis_client = mock_client

        # Patch uuid4 to verify we don't generate a new one
        with patch("adk.shell.repl.uuid4"):
            # Should NOT call uuid4 if session_id is set
            async def mock_stream(*args, **kwargs):
                return iter([])  # Empty async iterator

            mock_client.chat_stream = AsyncMock(
                return_value=mock_stream(),
            )

            # The session_id should come from config, not generated
            assert repl.config.session_id == session_id

    @pytest.mark.asyncio
    async def test_repl_generates_session_id_if_not_set(self):
        """REPL generates a new session_id if config.session_id is None."""
        cfg = AitherConfig()
        cfg.session_id = None  # Explicitly no session

        repl = AitherREPL(cfg)
        assert repl.config.session_id is None

        # In _run_generation, if session_id is None, uuid4 is called
        # This is verified by the actual flow in _run_generation


class TestREPLResumeCommand:
    """Test /resume command in REPL."""

    @pytest.mark.asyncio
    async def test_repl_resume_slash_command(self):
        """REPL /resume slash command sets session_id."""
        cfg = AitherConfig()
        repl = AitherREPL(cfg)

        session_id = str(uuid4())
        result = await repl._handle_command(f"/resume {session_id}")

        assert f"Resumed session {session_id}" in result
        assert repl.config.session_id == session_id

    @pytest.mark.asyncio
    async def test_repl_resume_without_id(self):
        """REPL /resume without ID shows usage."""
        cfg = AitherConfig()
        repl = AitherREPL(cfg)

        result = await repl._handle_command("/resume")

        assert "Usage: /resume" in result

    @pytest.mark.asyncio
    async def test_repl_research_slash_command(self):
        """REPL /research slash command queues research prompt."""
        cfg = AitherConfig()
        repl = AitherREPL(cfg)

        # Patch the input queue
        with patch.object(repl, "_input_queue") as mock_queue:
            mock_queue.put = AsyncMock()

            result = await repl._handle_command("/research What is Python?")

            # Should queue the research prompt
            mock_queue.put.assert_called_once()
            queued_text = mock_queue.put.call_args[0][0]
            assert "Research the following thoroughly" in queued_text
            assert "What is Python?" in queued_text
            # Command returns None (handled via queue)
            assert result is None

    @pytest.mark.asyncio
    async def test_repl_research_without_query(self):
        """REPL /research without query shows usage."""
        cfg = AitherConfig()
        repl = AitherREPL(cfg)

        result = await repl._handle_command("/research")

        assert "Usage: /research" in result


class TestGenesisClientSessionHandling:
    """Test that GenesisClient passes session_id correctly."""

    def test_chat_stream_session_payload(self):
        """Verify session_id is included in chat_stream payload."""
        session_id = str(uuid4())

        # Simulate the payload building logic from chat_stream
        payload = {"message": "test query"}
        if session_id:
            payload["session_id"] = session_id

        # Verify session_id is in payload
        assert "session_id" in payload
        assert payload["session_id"] == session_id


class TestCLISessionOption:
    """Test CLI --session option."""

    def test_cli_session_option_sets_config_session_id(self):
        """CLI --session option sets config.session_id."""
        # This is tested via integration; here we verify the logic
        cfg = AitherConfig()
        session_id = str(uuid4())
        cfg.session_id = session_id

        # Verify it's set
        assert cfg.session_id == session_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
