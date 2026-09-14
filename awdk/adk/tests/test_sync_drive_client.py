"""Tests for DriveClient HTTP communication."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.sync.drive_client import DriveClient


@pytest.fixture
def mock_drive_client():
    """Create a DriveClient for testing."""
    client = DriveClient(
        base_url="https://communication-core:8205",
        tenant_id="tnt_test",
        workspace_id="ws_test",
        token="bearer_token",
    )
    return client


@pytest.mark.asyncio
async def test_list_changes_success():
    """Test successful list_changes response."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "cursor": 42,
            "changes": [
                {
                    "path": "memory/file1.jsonl",
                    "hash": "abc123",
                    "size": 1024,
                    "version": 1,
                    "deleted": False,
                },
                {
                    "path": "graph/entities.db",
                    "hash": "def456",
                    "size": 2048,
                    "version": 2,
                    "deleted": False,
                },
            ]
        }

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
            token="bearer_token",
        )

        cursor, changes = await client.list_changes(since=0)

        assert cursor == 42
        assert len(changes) == 2
        assert "memory/file1.jsonl" in changes
        assert changes["memory/file1.jsonl"].hash == "abc123"
        assert changes["memory/file1.jsonl"].size == 1024
        await client.close()


@pytest.mark.asyncio
async def test_list_changes_empty():
    """Test list_changes with no changes."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "cursor": 10,
            "changes": []
        }

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
        )

        cursor, changes = await client.list_changes(since=10)

        assert cursor == 10
        assert len(changes) == 0
        await client.close()


@pytest.mark.asyncio
async def test_list_changes_http_error():
    """Test list_changes with HTTP error."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
        )

        with pytest.raises(ValueError, match="401"):
            await client.list_changes(since=0)
        await client.close()


@pytest.mark.asyncio
async def test_upload_success():
    """Test successful file upload."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"version": 2}

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
        )

        result = await client.upload("memory/file.jsonl", b"content", version=1)

        assert result["version"] == 2
        await client.close()


@pytest.mark.asyncio
async def test_download_success():
    """Test successful file download."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"file content"

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
        )

        content = await client.download("memory/file.jsonl")

        assert content == b"file content"
        await client.close()


@pytest.mark.asyncio
async def test_delete_success():
    """Test successful file deletion."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = DriveClient(
            base_url="https://communication-core:8205",
            tenant_id="tnt_test",
            workspace_id="ws_test",
        )

        await client.delete("memory/file.jsonl", version=1)
        # Should not raise
        await client.close()
