"""Tests for secrets sync client."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.sync.secrets import SecretsSync, _extract_secret_names


class TestSecretsSync:
    def test_init_from_env(self):
        env = {
            "AITHER_API_KEY": "test-key",
            "AITHER_SECRETS_URL": "http://secrets:8111",
        }
        with patch.dict(os.environ, env, clear=False):
            client = SecretsSync()
            assert client.api_key == "test-key"
            assert client.secrets_url == "http://secrets:8111"

    def test_headers(self):
        client = SecretsSync(api_key="key123")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer key123"
        # X-Tenant-ID should NOT be included (scoping via token)
        assert "X-Tenant-ID" not in headers

    @pytest.mark.asyncio
    async def test_pull_from_secrets_service(self):
        """Test pull from AitherSecrets via batch endpoint."""
        import httpx
        with patch("httpx.AsyncClient") as mock_client_cls:
            # Mock list response (simple list of names)
            list_response = MagicMock()
            list_response.status_code = 200
            list_response.json.return_value = ["API_KEY"]

            # Mock batch response
            batch_response = MagicMock()
            batch_response.status_code = 200
            batch_response.json.return_value = {"secrets": {"API_KEY": "secret123"}}

            mock_client = AsyncMock()
            # Return list response first, batch response second
            mock_client.get = AsyncMock(return_value=list_response)
            mock_client.post = AsyncMock(return_value=batch_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("adk.builtin_tools._load_secrets") as mock_load, \
                 patch("adk.builtin_tools._save_secrets") as mock_save:
                mock_load.return_value = {}

                client = SecretsSync(
                    api_key="key",
                    secrets_url="http://secrets:8111",
                    gateway_url="",  # Disable gateway fallback
                )
                synced = await client.pull()

                # Verify GET was called to list secrets
                mock_client.get.assert_called_once()
                assert "/secrets" in mock_client.get.call_args[0][0]

                # Verify POST was called for batch fetch
                mock_client.post.assert_called_once()
                assert "/secrets/batch" in mock_client.post.call_args[0][0]

                # Verify result
                assert "API_KEY" in synced
                assert synced["API_KEY"] == "secret123"
                mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_pull_no_sources(self):
        """Test pull with no credentials returns empty dict."""
        client = SecretsSync(api_key="", secrets_url="", gateway_url="")
        synced = await client.pull()
        assert synced == {}

    @pytest.mark.asyncio
    async def test_push_to_vault(self):
        """Test pushing a secret to the vault."""
        import httpx
        with patch("httpx.AsyncClient") as mock_client_cls:
            push_response = MagicMock()
            push_response.status_code = 201

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=push_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            client = SecretsSync(
                api_key="key",
                secrets_url="http://secrets:8111",
            )
            success = await client.push("MY_KEY", "my_value")

            # Verify POST to /secrets with correct format
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/secrets" in call_args[0][0]
            assert call_args[1]["json"]["name"] == "MY_KEY"
            assert call_args[1]["json"]["value"] == "my_value"
            assert call_args[1]["json"]["secret_type"] == "api_key"

            assert success is True


class TestExtractSecretNames:
    """A GET /secrets response can arrive in several shapes; none may SILENTLY
    drop names (the fail-closed-looks-like-success trap the review caught)."""

    def test_list_of_metadata_dicts(self):
        # The real AitherSecrets shape: [{"name": ..., "secret_type": ...}]
        assert _extract_secret_names(
            [{"name": "A", "secret_type": "api_key"}, {"name": "B"}]
        ) == ["A", "B"]

    def test_list_of_plain_names(self):
        assert _extract_secret_names(["A", "B"]) == ["A", "B"]

    def test_secrets_envelope_dict(self):
        # Regression: {"secrets": {...}} used to yield [] (silent zero pull).
        assert _extract_secret_names({"secrets": {"A": "x", "B": "y"}}) == ["A", "B"]

    def test_secrets_envelope_list(self):
        assert _extract_secret_names({"secrets": [{"name": "A"}]}) == ["A"]

    def test_flat_dict(self):
        assert _extract_secret_names({"A": "x", "B": "y"}) == ["A", "B"]

    def test_unknown_shape_is_empty_not_crash(self):
        assert _extract_secret_names(None) == []
        assert _extract_secret_names(42) == []
