"""Tests for adk relay up — sovereign relay bundle deployment."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRelayUpCommand:
    """Test the 'adk relay up' command."""

    def test_relay_up_dry_run(self):
        """Test dry-run mode shows configuration without starting containers."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = "test-relay"
        args.rooms = "#general,#dev"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = False
        args.directory_url = ""
        args.token = "test-token"
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch("builtins.print") as mock_print:
            with patch("pathlib.Path.exists", return_value=True):
                result = _relay_up(args)

        assert result == 0
        # Verify dry-run output was printed
        calls_text = "".join(str(call) for call in mock_print.call_args_list)
        assert "DRY RUN" in calls_text
        assert "test-relay" in calls_text
        assert "#general,#dev" in calls_text

    def test_relay_up_env_resolution(self):
        """Test environment variable resolution for relay config."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = ""  # Not set via flag
        args.rooms = ""  # Not set via flag
        args.hub_url = ""  # Not set via flag
        args.no_federation = False
        args.directory_url = ""
        args.token = ""
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch.dict(
            os.environ,
            {
                "AITHER_NODE_SLUG": "env-relay",
                "AITHERNET_ADVERTISED_ROOMS": "#test",
                "AITHERNET_HUB_URL": "wss://env.example.com/ws/chat",
            },
        ):
            with patch("builtins.print") as mock_print:
                with patch("pathlib.Path.exists", return_value=True):
                    result = _relay_up(args)

        assert result == 0
        # Verify env vars were used
        calls_text = "".join(str(call) for call in mock_print.call_args_list)
        assert "env-relay" in calls_text or "AITHER_NODE_SLUG=env-relay" in calls_text

    def test_relay_up_compose_file_not_found(self):
        """Test error when compose file is not found."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = "test-relay"
        args.rooms = "#general"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = False
        args.directory_url = ""
        args.token = ""
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = False

        with patch("pathlib.Path.exists", return_value=False):
            result = _relay_up(args)

        assert result == 1

    def test_relay_up_federation_flag(self):
        """Test federation can be disabled with --no-federation."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = "test-relay"
        args.rooms = "#general"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = True  # Disable federation
        args.directory_url = ""
        args.token = ""
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch("builtins.print") as mock_print:
            with patch("pathlib.Path.exists", return_value=True):
                result = _relay_up(args)

        assert result == 0
        calls_text = "".join(str(call) for call in mock_print.call_args_list)
        assert "AITHERNET_FEDERATION=false" in calls_text

    def test_relay_up_custom_port(self):
        """Test custom port configuration."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = "test-relay"
        args.rooms = "#general"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = False
        args.directory_url = ""
        args.token = ""
        args.port = 9999  # Custom port
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch("builtins.print") as mock_print:
            with patch("pathlib.Path.exists", return_value=True):
                result = _relay_up(args)

        assert result == 0
        calls_text = "".join(str(call) for call in mock_print.call_args_list)
        assert "RELAY_PORT=9999" in calls_text

    def test_relay_up_directory_registration(self):
        """Test directory URL is included when provided."""
        from adk.cli import _relay_up

        args = MagicMock()
        args.slug = "test-relay"
        args.rooms = "#general"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = False
        args.directory_url = "http://localhost:8001/v1/aithernet"
        args.token = ""
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch("builtins.print") as mock_print:
            with patch("pathlib.Path.exists", return_value=True):
                result = _relay_up(args)

        assert result == 0
        calls_text = "".join(str(call) for call in mock_print.call_args_list)
        assert "AITHERNET_DIRECTORY_URL=http://localhost:8001/v1/aithernet" in calls_text


class TestRelayCommandRouter:
    """Test the cmd_relay command router."""

    def test_relay_up_routed_correctly(self):
        """Test that relay_command='up' routes to _relay_up."""
        from adk.cli import cmd_relay

        args = MagicMock()
        args.relay_command = "up"
        args.slug = "test-relay"
        args.rooms = "#general"
        args.hub_url = "wss://test.example.com/ws/chat"
        args.no_federation = False
        args.directory_url = ""
        args.token = ""
        args.port = 8205
        args.compose_file = None
        args.foreground = False
        args.dry_run = True

        with patch("adk.cli._relay_up", return_value=0) as mock_relay_up:
            with patch("pathlib.Path.exists", return_value=True):
                result = cmd_relay(args)

        # Should have called _relay_up
        assert mock_relay_up.called

    def test_relay_join_still_works(self):
        """Test that join subcommand still works after adding up."""
        from adk.cli import cmd_relay

        args = MagicMock()
        args.relay_command = "join"
        args.nick = "test-agent"
        args.url = "http://localhost:8205/v1"
        args.local = True
        args.channel = "#test"
        args.token = "test-token"

        # This would normally run the relay join logic
        # For now just verify it doesn't error on routing
        with patch("adk.cli.load_saved_config", return_value={}):
            with patch("adk.relay_client.RelayClient"):
                with patch("asyncio.run"):
                    # We're not testing the full join logic, just routing
                    # Verify the function returns 0 or 1 (doesn't crash)
                    result = cmd_relay(args)
                    assert result in (0, 1)
