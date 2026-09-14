"""Tests for _self_mint_gateway_key remote/local path selection."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.fleet_enroll import _self_mint_gateway_key


@pytest.mark.asyncio
async def test_local_path_secrets_vault():
    """Test LOCAL path: mints via localhost:8111/api-keys when AITHER_SECRETS_URL is localhost."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"api_key": "avk_test_local_key_12345"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://localhost:8111",
                "AITHER_GENESIS_URL": "http://localhost:8001",
            },
        ):
            result = await _self_mint_gateway_key("bearer_test_token", "test-node-1")

    assert result == "avk_test_local_key_12345"
    # Verify the POST was to the local secrets endpoint
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/api-keys" in call_args[0][0]
    assert "localhost:8111" in call_args[0][0]
    assert call_args[1]["json"]["name"] == "node-test-node-1"


@pytest.mark.asyncio
async def test_local_path_default_localhost():
    """Test LOCAL path: uses localhost:8111 by default when AITHER_SECRETS_URL unset."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"api_key": "avk_default_local_key"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(os.environ, {}, clear=True):
            result = await _self_mint_gateway_key("bearer_test_token", "test-node-2")

    assert result == "avk_default_local_key"
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "localhost:8111" in call_args[0][0]


@pytest.mark.asyncio
async def test_remote_path_genesis_exchange():
    """Test REMOTE path: exchanges via Genesis when AITHER_SECRETS_URL is non-localhost."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "token": "ep_remote_gateway_token_abcdef",
        "renewal_secret": "renewal_abc123",
        "expires_at": "2026-07-12T00:00:00Z",
        "node_id": "test-node-remote",
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://remote-secrets.example.com:8111",
                "AITHER_GENESIS_URL": "https://genesis.example.com",
            },
        ):
            result = await _self_mint_gateway_key(
                "enrollment_token_xyz", "test-node-remote"
            )

    assert result == "ep_remote_gateway_token_abcdef"
    # Verify the POST was to the Genesis exchange endpoint
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert "/v1/workspace/api-keys/enrollment-token/exchange" in call_args[0][0]
    assert "genesis.example.com" in call_args[0][0]
    # Verify it sent enrollment_token in the body, not as Bearer
    assert call_args[1]["json"]["enrollment_token"] == "enrollment_token_xyz"


@pytest.mark.asyncio
async def test_remote_path_explicit_gateway_url():
    """Test REMOTE path: treats as remote when AITHER_GATEWAY_URL is explicitly set."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "ep_gateway_via_explicit_url"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_GATEWAY_URL": "https://gateway.example.com",
                "AITHER_GENESIS_URL": "https://genesis.example.com",
            },
            clear=True,
        ):
            result = await _self_mint_gateway_key("bearer_token", "test-node-gw")

    assert result == "ep_gateway_via_explicit_url"
    # Should call Genesis exchange, not local :8111
    call_args = mock_client.post.call_args
    assert "/v1/workspace/api-keys/enrollment-token/exchange" in call_args[0][0]


@pytest.mark.asyncio
async def test_remote_path_explicit_api_url():
    """Test REMOTE path: treats as remote when AITHER_API_URL is explicitly set."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "ep_via_api_url"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_API_URL": "https://api.example.com",
                "AITHER_GENESIS_URL": "https://genesis.example.com",
            },
            clear=True,
        ):
            result = await _self_mint_gateway_key("bearer_token", "test-node-api")

    assert result == "ep_via_api_url"
    call_args = mock_client.post.call_args
    assert "/v1/workspace/api-keys/enrollment-token/exchange" in call_args[0][0]


@pytest.mark.asyncio
async def test_local_path_http_error_fallback():
    """Test LOCAL path gracefully falls back to empty string on HTTP error."""
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://localhost:8111",
            },
        ):
            result = await _self_mint_gateway_key("bad_token", "test-node-3")

    # Should return empty string, not raise
    assert result == ""


@pytest.mark.asyncio
async def test_remote_path_http_error_fallback():
    """Test REMOTE path gracefully falls back on Genesis exchange error."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Invalid enrollment token"

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://remote-vault.example.com:8111",
                "AITHER_GENESIS_URL": "https://genesis.example.com",
            },
        ):
            result = await _self_mint_gateway_key("bad_enrollment_token", "test-node-4")

    # Should return empty string, not raise
    assert result == ""


@pytest.mark.asyncio
async def test_local_path_missing_api_key_in_response():
    """Test LOCAL path handles missing api_key in response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}  # No api_key

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://localhost:8111",
            },
        ):
            result = await _self_mint_gateway_key("bearer_token", "test-node-5")

    # Should return empty string when response has no api_key
    assert result == ""


@pytest.mark.asyncio
async def test_remote_path_missing_token_in_response():
    """Test REMOTE path handles missing token in response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"renewal_secret": "xyz"}  # No token

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://remote-vault.example.com:8111",
                "AITHER_GENESIS_URL": "https://genesis.example.com",
            },
        ):
            result = await _self_mint_gateway_key("enrollment_token", "test-node-6")

    # Should return empty string when response has no token
    assert result == ""


@pytest.mark.asyncio
async def test_exception_during_request_does_not_raise():
    """Test that any exception during request is caught and returns empty string."""

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.side_effect = RuntimeError("Connection failed")
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://localhost:8111",
            },
        ):
            result = await _self_mint_gateway_key("bearer_token", "test-node-7")

    # Should return empty string, not raise
    assert result == ""


@pytest.mark.asyncio
async def test_localhost_ipv6_treated_as_local():
    """Test that IPv6 localhost (::1) is treated as local path."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"api_key": "avk_ipv6_local"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        with patch.dict(
            os.environ,
            {
                "AITHER_SECRETS_URL": "http://[::1]:8111",
            },
        ):
            result = await _self_mint_gateway_key("bearer_token", "test-node-ipv6")

    assert result == "avk_ipv6_local"
    # Should call local endpoint
    call_args = mock_client.post.call_args
    assert "localhost" in call_args[0][0] or "::1" in call_args[0][0]
