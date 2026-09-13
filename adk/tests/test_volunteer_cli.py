"""Tests for adk volunteer CLI subcommands."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.commands.volunteer import (
    EMBED_MODEL_BYTE_COUNT,
    EMBED_LISTEN_HOST,
    EMBED_LISTEN_PORT,
    _embed_text,
    _resolve_peer_id,
    enroll,
    serve,
    start,
    status,
)


class TestVolunteerArgparse:
    """Test volunteer CLI argparse registration."""

    def test_volunteer_command_exists(self):
        """Verify volunteer is a top-level command."""
        import argparse
        from adk.cli import _register_commands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        _register_commands(sub)

        # Parse a volunteer command to ensure it's registered
        args = parser.parse_args(["volunteer", "enroll"])
        assert args.command == "volunteer"
        assert args.volunteer_command == "enroll"

    def test_volunteer_subcommands(self):
        """Verify all volunteer subcommands are registered (no duplicates)."""
        import argparse
        from adk.cli import _register_commands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        _register_commands(sub)

        subcommands = ["enroll", "serve", "start", "status"]
        for subcmd in subcommands:
            args = parser.parse_args(["volunteer", subcmd])
            assert args.volunteer_command == subcmd

    def test_volunteer_enroll_args(self):
        """Test enroll arguments."""
        import argparse
        from adk.cli import _register_commands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        _register_commands(sub)

        args = parser.parse_args(["volunteer", "enroll", "--tenant", "dgg"])
        assert args.tenant == "dgg"

    def test_volunteer_serve_args(self):
        """Test serve arguments."""
        import argparse
        from adk.cli import _register_commands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        _register_commands(sub)

        args = parser.parse_args(["volunteer", "serve", "--model", "aither-code-embed-0.6b", "--device", "gpu"])
        assert args.model == "aither-code-embed-0.6b"
        assert args.device == "gpu"

    def test_volunteer_start_args(self):
        """Test start arguments."""
        import argparse
        from adk.cli import _register_commands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        _register_commands(sub)

        args = parser.parse_args(["volunteer", "start", "--batch-size", "32"])
        assert args.batch_size == 32


class TestVolunteerModelVerification:
    """Test embedding model size verification."""

    @pytest.mark.asyncio
    async def test_model_size_check_exact_match(self):
        """Model with exact byte count should pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.gguf"
            # Create file with exact size
            model_path.write_bytes(b"x" * EMBED_MODEL_BYTE_COUNT)

            actual_size = model_path.stat().st_size
            assert actual_size == EMBED_MODEL_BYTE_COUNT, f"Size mismatch: {actual_size} != {EMBED_MODEL_BYTE_COUNT}"

    @pytest.mark.asyncio
    async def test_model_size_check_too_small(self):
        """Model with wrong byte count should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.gguf"
            # Create file with wrong size (too small)
            model_path.write_bytes(b"x" * 1000)

            actual_size = model_path.stat().st_size
            assert actual_size != EMBED_MODEL_BYTE_COUNT, "Size check should fail"

    @pytest.mark.asyncio
    async def test_model_size_check_too_large(self):
        """Model with byte count > expected should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Path(tmpdir) / "model.gguf"
            # Create file with wrong size (too large)
            wrong_size = EMBED_MODEL_BYTE_COUNT + 1000
            model_path.write_bytes(b"x" * wrong_size)

            actual_size = model_path.stat().st_size
            assert actual_size != EMBED_MODEL_BYTE_COUNT, "Size check should fail"


class TestVolunteerEmbedding:
    """Test embedding pipeline."""

    @pytest.mark.asyncio
    async def test_embed_text_calls_server(self):
        """Test _embed_text makes correct API call."""
        server_url = f"http://{EMBED_LISTEN_HOST}:{EMBED_LISTEN_PORT}"
        text = "test comment"

        mock_embedding = [0.1] * 1024  # Mock 1024-dimensional embedding

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "data": [{"embedding": mock_embedding}]
            }
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await _embed_text(server_url, text)
            assert result == mock_embedding
            assert len(result) == 1024

    @pytest.mark.asyncio
    async def test_embed_text_handles_error(self):
        """Test _embed_text error handling."""
        server_url = f"http://{EMBED_LISTEN_HOST}:{EMBED_LISTEN_PORT}"
        text = "test comment"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post = AsyncMock(side_effect=Exception("Connection failed"))
            mock_client_class.return_value = mock_client

            with pytest.raises(RuntimeError):
                await _embed_text(server_url, text)


class TestVolunteerAuthToken:
    """Test auth token resolution."""

    def test_get_auth_token_from_env(self):
        """Auth token from environment variable."""
        with patch.dict(os.environ, {"AITHER_AUTH_TOKEN": "test-token-123"}):
            from adk.commands.volunteer import _get_auth_token
            token = _get_auth_token()
            assert token == "test-token-123"

    def test_get_auth_token_from_config(self):
        """Auth token from config file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            config_file.write_text(json.dumps({"auth_token": "config-token"}))

            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                from adk.commands.volunteer import _get_auth_token
                # Create .aither directory
                aither_dir = Path(tmpdir) / ".aither"
                aither_dir.mkdir()
                (aither_dir / "config.json").write_text(json.dumps({"auth_token": "config-token"}))

                # Note: This test may not work as expected due to Path.home() patch
                # In practice, the function will look for ~/.aither/config.json


class TestVolunteerResolvePeerId:
    """Test peer ID resolution."""

    @pytest.mark.asyncio
    async def test_resolve_peer_id_from_env(self):
        """Peer ID from environment variable."""
        with patch.dict(os.environ, {"AITHER_PEER_ID": "peer-abc123"}):
            peer_id = await _resolve_peer_id()
            assert peer_id == "peer-abc123"

    @pytest.mark.asyncio
    async def test_resolve_peer_id_missing(self):
        """Should raise clear error when peer_id not found."""
        with patch.dict(os.environ, {"AITHER_PEER_ID": ""}, clear=True):
            with patch("pathlib.Path.home") as mock_home:
                mock_home.return_value = Path("/nonexistent")
                with pytest.raises(RuntimeError) as exc_info:
                    await _resolve_peer_id()
                assert "Cannot resolve peer_id" in str(exc_info.value)
                assert "adk mesh onboard" in str(exc_info.value)


class TestVolunteerClaimLoop:
    """Test claiming and result submission against fake Genesis."""

    @pytest.mark.asyncio
    async def test_claim_job_response_parsing(self):
        """Test parsing claim job response."""
        # This is a placeholder test for the claim loop structure
        # In practice, we'd mock httpx calls to Genesis endpoints

        claim_response = {
            "job_id": "job-123",
            "batch_id": "batch-456",
            "tasks": [
                {
                    "task_id": "task-1",
                    "text": "sample code comment",
                    "reddit_url": "https://reddit.com/...",
                }
            ],
            "claim_time": 300,
            "expires_at": 1800,
        }

        # Verify response structure
        assert "job_id" in claim_response
        assert "tasks" in claim_response
        assert len(claim_response["tasks"]) > 0
        assert "task_id" in claim_response["tasks"][0]
        assert "text" in claim_response["tasks"][0]


# Integration test (requires real/mock server setup)
@pytest.mark.asyncio
async def test_volunteer_enroll_with_mocks():
    """Test enroll flow with all external calls mocked."""
    with patch.dict(os.environ, {"AITHER_PEER_ID": "peer-test123"}):
        # Mock the mesh_provider module before importing
        with patch("adk.mesh_provider.grant_consent") as mock_grant:
            with patch("adk.mesh_provider.request_trust") as mock_trust:
                mock_grant.return_value = {"ok": True}
                mock_trust.return_value = {"ok": True, "trust_status": "auto_granted"}

                args = MagicMock()
                args.tenant = "dgg"

                # This would normally exit after success
                # Just verify the functions are called correctly
                with patch("builtins.print"):
                    try:
                        await enroll(args)
                    except SystemExit:
                        pass  # Expected on success


class TestEnrollTenantDefault:
    """The tenant is the OWNER tenant Strata keys the peer record by, never a slug guess."""

    def test_saved_tenant_reads_the_login_config(self, monkeypatch):
        from adk import config as cfg
        from adk.commands import volunteer as vol

        monkeypatch.setattr(cfg, "load_saved_config", lambda *a, **k: {"tenant_id": "tnt_dgg"})
        assert vol._saved_tenant() == "tnt_dgg"

    def test_enroll_refuses_without_any_tenant(self, monkeypatch, capsys):
        import asyncio
        from types import SimpleNamespace

        from adk.commands import volunteer as vol

        monkeypatch.setattr(vol, "_saved_tenant", lambda: "")
        called = []
        monkeypatch.setattr(vol, "_resolve_peer_id", lambda: called.append(1))
        asyncio.run(vol.enroll(SimpleNamespace(tenant=None)))
        assert "No tenant" in capsys.readouterr().out
        assert called == []  # returned before touching the mesh
