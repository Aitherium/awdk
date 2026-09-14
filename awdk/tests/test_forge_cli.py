"""Tests for adk forge CLI command — dispatch to Genesis /forge/dispatch."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from adk.cli import cmd_forge, _stream_forge_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_forge_args(**kwargs) -> argparse.Namespace:
    """Build argparse.Namespace for forge command."""
    defaults = {
        "task": "Write a Python function",
        "agent": "demiurge",
        "effort": 5,
        "watch": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Tests: cmd_forge (dispatch)
# ---------------------------------------------------------------------------


class TestCmdForgeDispatch:
    """Tests for the forge dispatch logic (POST /forge/dispatch)."""

    def test_forge_missing_task(self, capsys):
        """forge without task returns error."""
        args = _make_forge_args(task="")
        with patch("adk.cli._get_genesis_url") as mock_genesis:
            mock_genesis.return_value = "http://localhost:8001"
            result = cmd_forge(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "task is required" in captured.err

    def test_forge_dispatch_success(self, capsys):
        """forge POSTs to /forge/dispatch and gets session_id."""
        args = _make_forge_args(task="Hello", watch=False)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"session_id": "sess-123"}

        with patch("adk.cli._get_genesis_url") as mock_genesis:
            with patch("adk.cli.tls_verify") as mock_tls:
                with patch("httpx.Client") as mock_client_class:
                    mock_genesis.return_value = "http://localhost:8001"
                    mock_tls.return_value = True
                    mock_client = MagicMock()
                    mock_client.__enter__.return_value = mock_client
                    mock_client.__exit__.return_value = False
                    mock_client.post.return_value = mock_response
                    mock_client_class.return_value = mock_client

                    result = cmd_forge(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "Session ID: sess-123" in captured.out

    def test_forge_dispatch_error_response(self, capsys):
        """forge handles Genesis error response."""
        args = _make_forge_args(task="Hello", watch=False)

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"detail": "Invalid task format"}

        with patch("adk.cli._get_genesis_url") as mock_genesis:
            with patch("adk.cli.tls_verify") as mock_tls:
                with patch("httpx.Client") as mock_client_class:
                    mock_genesis.return_value = "http://localhost:8001"
                    mock_tls.return_value = True
                    mock_client = MagicMock()
                    mock_client.__enter__.return_value = mock_client
                    mock_client.__exit__.return_value = False
                    mock_client.post.return_value = mock_response
                    mock_client_class.return_value = mock_client

                    result = cmd_forge(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "Invalid task format" in captured.err

    def test_forge_genesis_offline(self, capsys):
        """forge handles Genesis offline gracefully."""
        args = _make_forge_args(task="Hello", watch=False)

        with patch("adk.cli._get_genesis_url") as mock_genesis:
            with patch("adk.cli.tls_verify") as mock_tls:
                with patch("httpx.Client") as mock_client_class:
                    mock_genesis.return_value = "http://localhost:8001"
                    mock_tls.return_value = True
                    mock_client = MagicMock()
                    mock_client.__enter__.return_value = mock_client
                    mock_client.__exit__.return_value = False
                    # Simulate connection error
                    import httpx
                    mock_client.post.side_effect = httpx.ConnectError(
                        "Connection refused"
                    )
                    mock_client_class.return_value = mock_client

                    result = cmd_forge(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "offline or unreachable" in captured.err

    def test_forge_payload_structure(self):
        """forge sends correct payload structure to Genesis."""
        args = _make_forge_args(
            task="Review this code",
            agent="hydra",
            effort=7,
            watch=False,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"session_id": "sess-456"}

        with patch("adk.cli._get_genesis_url") as mock_genesis:
            with patch("adk.cli.tls_verify") as mock_tls:
                with patch("httpx.Client") as mock_client_class:
                    mock_genesis.return_value = "http://localhost:8001"
                    mock_tls.return_value = True
                    mock_client = MagicMock()
                    mock_client.__enter__.return_value = mock_client
                    mock_client.__exit__.return_value = False
                    mock_client.post.return_value = mock_response
                    mock_client_class.return_value = mock_client

                    cmd_forge(args)

                    # Verify the POST call
                    call_args = mock_client.post.call_args
                    assert call_args[0][0] == "http://localhost:8001/forge/dispatch"
                    payload = call_args[1]["json"]
                    assert payload["task"] == "Review this code"
                    assert payload["agent"] == "hydra"
                    assert payload["effort"] == 7


# ---------------------------------------------------------------------------
# Tests: _stream_forge_session (SSE streaming)
# ---------------------------------------------------------------------------


class TestStreamForgeSession:
    """Tests for forge session streaming (GET /forge/sessions/{id}/stream)."""

    def test_stream_success(self, capsys):
        """stream processes SSE data and renders phases."""
        session_id = "sess-789"

        sse_data = [
            "data: " + json.dumps({
                "phase": "planning",
                "message": "Analyzing task...",
            }),
            "data: " + json.dumps({
                "phase": "execution",
                "message": "Running agents...",
            }),
            "data: " + json.dumps({
                "phase": "completed",
                "message": "Done",
                "status": "completed",
                "result": "Task completed successfully",
                "pr_url": "https://github.com/pr/123",
            }),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_lines.return_value = sse_data

        with patch("adk.cli.tls_verify") as mock_tls:
            with patch("httpx.Client") as mock_client_class:
                mock_tls.return_value = True
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.__exit__.return_value = False
                mock_client.stream.return_value.__enter__.return_value = (
                    mock_response
                )
                mock_client.stream.return_value.__exit__.return_value = False
                mock_client_class.return_value = mock_client

                result = _stream_forge_session(
                    "http://localhost:8001",
                    session_id,
                )

        assert result == 0
        captured = capsys.readouterr()
        assert "[planning]" in captured.out
        assert "Analyzing task" in captured.out
        assert "[execution]" in captured.out
        assert "Task completed successfully" in captured.out
        assert "https://github.com/pr/123" in captured.out

    def test_stream_error_status(self, capsys):
        """stream handles failed task status."""
        session_id = "sess-fail"

        sse_data = [
            "data: " + json.dumps({
                "phase": "execution",
                "message": "Running...",
            }),
            "data: " + json.dumps({
                "status": "failed",
                "error": "Task timed out",
            }),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_lines.return_value = sse_data

        with patch("adk.cli.tls_verify") as mock_tls:
            with patch("httpx.Client") as mock_client_class:
                mock_tls.return_value = True
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.__exit__.return_value = False
                mock_client.stream.return_value.__enter__.return_value = (
                    mock_response
                )
                mock_client.stream.return_value.__exit__.return_value = False
                mock_client_class.return_value = mock_client

                result = _stream_forge_session(
                    "http://localhost:8001",
                    session_id,
                )

        assert result == 1
        captured = capsys.readouterr()
        assert "Task timed out" in captured.err

    def test_stream_bad_response(self, capsys):
        """stream handles bad response status."""
        session_id = "sess-bad"

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("adk.cli.tls_verify") as mock_tls:
            with patch("httpx.Client") as mock_client_class:
                mock_tls.return_value = True
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.__exit__.return_value = False
                mock_client.stream.return_value.__enter__.return_value = (
                    mock_response
                )
                mock_client.stream.return_value.__exit__.return_value = False
                mock_client_class.return_value = mock_client

                result = _stream_forge_session(
                    "http://localhost:8001",
                    session_id,
                )

        assert result == 1
        captured = capsys.readouterr()
        assert "500" in captured.err

    def test_stream_skip_empty_lines(self, capsys):
        """stream skips empty SSE lines."""
        session_id = "sess-empty"

        sse_data = [
            "",  # Empty line
            "data: " + json.dumps({
                "phase": "planning",
                "message": "Starting...",
            }),
            "",  # Another empty line
            "data: " + json.dumps({
                "status": "completed",
                "result": "Done",
            }),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_lines.return_value = sse_data

        with patch("adk.cli.tls_verify") as mock_tls:
            with patch("httpx.Client") as mock_client_class:
                mock_tls.return_value = True
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.__exit__.return_value = False
                mock_client.stream.return_value.__enter__.return_value = (
                    mock_response
                )
                mock_client.stream.return_value.__exit__.return_value = False
                mock_client_class.return_value = mock_client

                result = _stream_forge_session(
                    "http://localhost:8001",
                    session_id,
                )

        assert result == 0
        captured = capsys.readouterr()
        # Should only see the actual data lines, not empty ones
        assert "[planning]" in captured.out
        assert "Done" in captured.out

    def test_stream_timeout(self, capsys):
        """stream handles timeout gracefully."""
        session_id = "sess-timeout"

        with patch("adk.cli.tls_verify") as mock_tls:
            with patch("httpx.Client") as mock_client_class:
                import httpx
                mock_tls.return_value = True
                mock_client = MagicMock()
                mock_client.__enter__.return_value = mock_client
                mock_client.__exit__.return_value = False
                mock_client.stream.side_effect = httpx.ReadTimeout("Timeout")
                mock_client_class.return_value = mock_client

                result = _stream_forge_session(
                    "http://localhost:8001",
                    session_id,
                )

        assert result == 1
        captured = capsys.readouterr()
        assert "timeout" in captured.err.lower()


# ---------------------------------------------------------------------------
# Integration-style test
# ---------------------------------------------------------------------------


class TestForgeIntegration:
    """End-to-end test simulating dispatch -> stream flow."""

    def test_forge_with_watch(self, capsys):
        """forge with --watch dispatches then streams."""
        args = _make_forge_args(
            task="Do something",
            watch=True,
        )

        # Dispatch response
        dispatch_response = MagicMock()
        dispatch_response.status_code = 200
        dispatch_response.json.return_value = {"session_id": "sess-e2e"}

        # Stream response
        sse_lines = [
            "data: " + json.dumps({
                "phase": "start",
                "message": "Task started",
            }),
            "data: " + json.dumps({
                "status": "completed",
                "result": "Success",
            }),
        ]
        stream_response = MagicMock()
        stream_response.status_code = 200
        stream_response.iter_lines.return_value = sse_lines

        with patch("adk.cli._get_genesis_url") as mock_genesis:
            with patch("adk.cli.tls_verify") as mock_tls:
                with patch("httpx.Client") as mock_client_class:
                    mock_genesis.return_value = "http://localhost:8001"
                    mock_tls.return_value = True

                    mock_client = MagicMock()
                    mock_client.__enter__.return_value = mock_client
                    mock_client.__exit__.return_value = False

                    # First call is POST /forge/dispatch, second is
                    # GET stream
                    mock_client.post.return_value = dispatch_response
                    mock_client.stream.return_value.__enter__.return_value = (
                        stream_response
                    )
                    mock_client.stream.return_value.__exit__.return_value = (
                        False
                    )

                    mock_client_class.return_value = mock_client

                    result = cmd_forge(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "Session ID: sess-e2e" in captured.out
        assert "[start]" in captured.out
        assert "Success" in captured.out
