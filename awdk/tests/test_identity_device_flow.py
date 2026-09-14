"""Tests for GitHub device flow authentication."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from adk.identity import AuthError, github_device_flow


class TestGithubDeviceFlow:
    """Test suite for github_device_flow async function."""

    @pytest.mark.asyncio
    async def test_start_complete_flow_success(self):
        """Device flow start → poll → complete returns correct fields."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        mock_poll_response = {
            "status": "complete",
            "access_token": "aither_token_xyz",
            "token_type": "bearer",
            "user_id": "user-123",
            "tenant_id": "tenant-456",
            "username": "testuser",
        }

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_poll_response
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await github_device_flow(
                base="http://localhost:8115",
                poll_timeout=10.0,
                poll_interval=0.1,
            )

        assert result["user_id"] == "user-123"
        assert result["workspace_id"] == "tenant-456"
        assert result["bearer_token"] == "aither_token_xyz"
        assert result["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_start_response_incomplete(self):
        """Device start with missing handle raises AuthError."""
        mock_start_response = {
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            # Missing handle
        }

        async def mock_post(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = mock_start_response
            return resp

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="missing handle"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_start_http_error(self):
        """Device start HTTP error raises AuthError."""
        async def mock_post(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 502
            resp.text = "Bad Gateway"
            return resp

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="HTTP 502"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_poll_authorization_denied(self):
        """Poll receives error status (authorization denied)."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = {
                    "status": "error",
                    "error": "access_denied",
                }
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="access_denied"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                    poll_interval=0.1,
                )

    @pytest.mark.asyncio
    async def test_poll_timeout(self):
        """Poll times out after poll_timeout seconds."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                # Always return pending
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = {
                    "status": "pending",
                    "interval": 5,
                }
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="timeout"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=0.2,
                    poll_interval=0.05,
                )

    @pytest.mark.asyncio
    async def test_poll_missing_token_in_complete(self):
        """Poll complete but missing access_token raises AuthError."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = {
                    "status": "complete",
                    # Missing access_token
                    "user_id": "user-123",
                    "tenant_id": "tenant-456",
                }
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="missing access_token"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                    poll_interval=0.1,
                )

    @pytest.mark.asyncio
    async def test_bearer_token_never_in_logs_or_output(self, capsys):
        """Bearer token is never printed or logged."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        mock_poll_response = {
            "status": "complete",
            "access_token": "aither_token_secret_xyz_12345",
            "token_type": "bearer",
            "user_id": "user-123",
            "tenant_id": "tenant-456",
            "username": "testuser",
        }

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_poll_response
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await github_device_flow(
                base="http://localhost:8115",
                poll_timeout=10.0,
                poll_interval=0.1,
            )

        # Verify token was returned
        assert result["bearer_token"] == "aither_token_secret_xyz_12345"

        # Verify token was never in stderr/stdout
        captured = capsys.readouterr()
        assert "aither_token_secret_xyz_12345" not in captured.out
        assert "aither_token_secret_xyz_12345" not in captured.err
        # Should only print user_code and verification_uri
        assert "TEST-CODE" in captured.err
        assert "https://github.com/login/device" in captured.err

    @pytest.mark.asyncio
    async def test_default_base_url_from_env(self, monkeypatch):
        """Base URL defaults to env AITHER_IDENTITY_URL."""
        mock_start_response = {
            "handle": "test-handle",
            "user_code": "CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        captured_urls = []

        async def mock_post(url, *args, **kwargs):
            captured_urls.append(url)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            if "device/start" in url:
                resp.json.return_value = mock_start_response
            elif "device/poll" in url:
                resp.json.return_value = {
                    "status": "complete",
                    "access_token": "token",
                    "user_id": "user-123",
                    "tenant_id": "tenant-456",
                }
            return resp

        monkeypatch.setenv("AITHER_IDENTITY_URL", "https://custom.example.com")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            await github_device_flow(poll_timeout=5.0, poll_interval=0.1)

        # Verify custom URL was used
        assert any("https://custom.example.com" in url for url in captured_urls)

    @pytest.mark.asyncio
    async def test_uses_tls_verification(self):
        """TLS verification is used via tls_verify()."""
        mock_start_response = {
            "handle": "test-handle",
            "user_code": "CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        async def mock_post(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            if "device/start" in url:
                resp.json.return_value = mock_start_response
            elif "device/poll" in url:
                resp.json.return_value = {
                    "status": "complete",
                    "access_token": "token",
                    "user_id": "user-123",
                    "tenant_id": "tenant-456",
                }
            return resp

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with patch("adk._tls.tls_verify") as mock_tls:
                mock_tls.return_value = True
                await github_device_flow(
                    base="https://example.com",
                    poll_timeout=5.0,
                    poll_interval=0.1,
                )

            # Verify tls_verify was called
            mock_tls.assert_called()

    @pytest.mark.asyncio
    async def test_invalid_json_response_raises_autherror(self):
        """Invalid JSON in response raises AuthError."""

        async def mock_post(url, *args, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.side_effect = ValueError("Invalid JSON")
            return resp

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="invalid JSON"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_httpx_error_raises_autherror(self):
        """httpx.HTTPError raises AuthError."""

        async def mock_post(url, *args, **kwargs):
            raise httpx.ConnectError("Connection failed")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            with pytest.raises(AuthError, match="Connection failed"):
                await github_device_flow(
                    base="http://localhost:8115",
                    poll_timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_poll_pending_continues(self):
        """Poll with pending status continues polling."""
        mock_start_response = {
            "handle": "test-handle-123",
            "user_code": "TEST-CODE",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

        poll_call_count = [0]

        async def mock_post(url, *args, **kwargs):
            if "device/start" in url:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.json.return_value = mock_start_response
                return resp
            elif "device/poll" in url:
                poll_call_count[0] += 1
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                if poll_call_count[0] < 3:
                    # Return pending first 2 times
                    resp.json.return_value = {
                        "status": "pending",
                        "interval": 5,
                    }
                else:
                    # Then complete
                    resp.json.return_value = {
                        "status": "complete",
                        "access_token": "token",
                        "user_id": "user-123",
                        "tenant_id": "tenant-456",
                    }
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await github_device_flow(
                base="http://localhost:8115",
                poll_timeout=10.0,
                poll_interval=0.05,
            )

        # Verify we polled multiple times
        assert poll_call_count[0] >= 3
        # Verify we got the final complete result
        assert result["user_id"] == "user-123"
