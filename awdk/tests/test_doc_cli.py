"""Tests for the 'adk doc' command (encrypted document management).

All network calls are mocked. No real files or network access.
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from adk.doc import (
    cmd_doc,
    _cmd_doc_upload,
    _cmd_doc_list,
    _cmd_doc_get,
    _cmd_doc_delete,
    _get_client_with_auth,
)


class MockArgs:
    """Simple args mock for testing."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestGetClientWithAuth:
    """Test _get_client_with_auth helper."""

    @pytest.mark.asyncio
    async def test_no_auth_provided(self):
        """Client can be created without auth headers."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("adk.doc.load_saved_config", return_value={}):
                client, headers = await _get_client_with_auth("http://localhost:8001")
                assert headers == {}
                await client.aclose()

    @pytest.mark.asyncio
    async def test_api_key_from_env(self):
        """API key is read from AITHER_API_KEY env var."""
        with patch.dict("os.environ", {"AITHER_API_KEY": "sk-test-123"}):
            with patch("adk.doc.load_saved_config", return_value={}):
                client, headers = await _get_client_with_auth("http://localhost:8001")
                assert headers.get("Authorization") == "Bearer sk-test-123"
                await client.aclose()

    @pytest.mark.asyncio
    async def test_api_key_from_arg(self):
        """API key from argument takes precedence."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("adk.doc.load_saved_config", return_value={"api_key": "saved-key"}):
                client, headers = await _get_client_with_auth(
                    "http://localhost:8001",
                    api_key="arg-key"
                )
                assert headers.get("Authorization") == "Bearer arg-key"
                await client.aclose()


class TestDocUpload:
    """Test 'adk doc upload' command."""

    @pytest.mark.asyncio
    async def test_upload_success(self):
        """Successful document upload."""
        # Create args
        file_path = Path("/tmp/test.txt")
        args = MockArgs(
            path=str(file_path),
            type=None,
            gateway=None,
            api_key="sk-test-123",
        )

        # Mock file reading
        test_content = b"Hello, Document!"

        # Mock httpx client
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "doc_id": "uuid-1234-5678",
            "filename": "test.txt",
            "size": len(test_content),
            "sha256": "abc123...",
            "encrypted": True,
            "created_at": "2026-06-11T12:00:00Z",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.is_file", return_value=True):
                    with patch("builtins.open", mock_open(read_data=test_content)):
                        with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {"Authorization": "Bearer sk-test-123"})):
                            result = await _cmd_doc_upload(args)

        assert result == 0
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_file_not_found(self):
        """Upload fails if file doesn't exist."""
        args = MockArgs(
            path="/nonexistent/file.txt",
            type=None,
            gateway=None,
            api_key="sk-test-123",
        )

        with patch("pathlib.Path.exists", return_value=False):
            result = await _cmd_doc_upload(args)

        assert result == 1

    @pytest.mark.asyncio
    async def test_upload_empty_file(self):
        """Upload fails if file is empty."""
        args = MockArgs(
            path="/tmp/empty.txt",
            type=None,
            gateway=None,
            api_key="sk-test-123",
        )

        # Scope TLS off: this test globally mocks Path.is_file=True, which would
        # otherwise make the TLS CA-bundle probe return a bogus (mock-existing)
        # bundle path that httpx can't load. Upload logic is what's under test.
        with patch.dict("os.environ", {"AITHER_TLS_VERIFY": "false"}):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.is_file", return_value=True):
                    with patch("builtins.open", mock_open(read_data=b"")):
                        result = await _cmd_doc_upload(args)

        assert result == 1

    @pytest.mark.asyncio
    async def test_upload_auth_error(self):
        """Upload fails with 401 when not authenticated."""
        args = MockArgs(
            path="/tmp/test.txt",
            type=None,
            gateway=None,
            api_key=None,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.is_file", return_value=True):
                with patch("builtins.open", mock_open(read_data=b"test")):
                    with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                        result = await _cmd_doc_upload(args)

        assert result == 1


class TestDocList:
    """Test 'adk doc list' command."""

    @pytest.mark.asyncio
    async def test_list_success(self):
        """List documents successfully."""
        args = MockArgs(
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "documents": [
                {
                    "doc_id": "uuid-1",
                    "filename": "report.pdf",
                    "size": 12345,
                    "doc_type": "application/pdf",
                    "created_at": "2026-06-11T10:00:00Z",
                    "encrypted": True,
                },
                {
                    "doc_id": "uuid-2",
                    "filename": "notes.txt",
                    "size": 234,
                    "doc_type": "text/plain",
                    "created_at": "2026-06-11T11:00:00Z",
                    "encrypted": True,
                },
            ],
            "total": 2,
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {"Authorization": "Bearer sk-test-123"})):
                result = await _cmd_doc_list(args)

        assert result == 0
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """List documents when none exist."""
        args = MockArgs(
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "documents": [],
            "total": 0,
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                result = await _cmd_doc_list(args)

        assert result == 0

    @pytest.mark.asyncio
    async def test_list_auth_error(self):
        """List fails with 401."""
        args = MockArgs(
            gateway=None,
            api_key=None,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                result = await _cmd_doc_list(args)

        assert result == 1


class TestDocGet:
    """Test 'adk doc get' command."""

    @pytest.mark.asyncio
    async def test_get_success(self):
        """Download document successfully."""
        args = MockArgs(
            doc_id="uuid-1234",
            output="/tmp/output.bin",
            o=None,
            gateway=None,
            api_key="sk-test-123",
        )

        document_bytes = b"Encrypted document content"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = document_bytes

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {"Authorization": "Bearer sk-test-123"})):
                with patch("builtins.open", mock_open()) as m_open:
                    result = await _cmd_doc_get(args)

        assert result == 0
        m_open.assert_called_once_with(Path("/tmp/output.bin"), "wb")
        m_open().write.assert_called_once_with(document_bytes)

    @pytest.mark.asyncio
    async def test_get_no_output_path(self):
        """Get fails if no output path provided."""
        args = MockArgs(
            doc_id="uuid-1234",
            output=None,
            o=None,
            gateway=None,
            api_key="sk-test-123",
        )

        result = await _cmd_doc_get(args)
        assert result == 1

    @pytest.mark.asyncio
    async def test_get_not_found(self):
        """Get fails with 404 if document doesn't exist."""
        args = MockArgs(
            doc_id="uuid-nonexistent",
            output="/tmp/output.bin",
            o=None,
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {
            "error": "document_not_found",
            "detail": "Document not found",
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                result = await _cmd_doc_get(args)

        assert result == 1

    @pytest.mark.asyncio
    async def test_get_access_denied(self):
        """Get fails with 403 if not the owner."""
        args = MockArgs(
            doc_id="uuid-other-user",
            output="/tmp/output.bin",
            o=None,
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.json.return_value = {
            "error": "access_denied",
            "detail": "Not the document owner",
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                result = await _cmd_doc_get(args)

        assert result == 1


class TestDocDelete:
    """Test 'adk doc delete' command."""

    @pytest.mark.asyncio
    async def test_delete_success_with_confirm(self):
        """Delete document with user confirmation."""
        args = MockArgs(
            doc_id="uuid-1234",
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                with patch("builtins.input", return_value="yes"):
                    result = await _cmd_doc_delete(args)

        assert result == 0
        mock_client.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_cancelled(self):
        """Delete is cancelled if user doesn't confirm."""
        args = MockArgs(
            doc_id="uuid-1234",
            gateway=None,
            api_key="sk-test-123",
        )

        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                with patch("builtins.input", return_value="no"):
                    result = await _cmd_doc_delete(args)

        assert result == 0
        mock_client.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        """Delete fails with 404 if document doesn't exist."""
        args = MockArgs(
            doc_id="uuid-nonexistent",
            gateway=None,
            api_key="sk-test-123",
        )

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {
            "error": "document_not_found",
            "detail": "Document not found",
        }

        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("os.environ", {"AITHER_CORE_URL": "http://localhost:8001"}):
            with patch("adk.doc._get_client_with_auth", return_value=(mock_client, {})):
                with patch("builtins.input", return_value="yes"):
                    result = await _cmd_doc_delete(args)

        assert result == 1


class TestCmdDoc:
    """Test the main cmd_doc dispatcher."""

    def test_dispatch_upload(self):
        """Dispatcher routes to upload."""
        args = MockArgs(
            doc_command="upload",
            path="/tmp/test.txt",
            type=None,
            gateway=None,
            api_key="sk-test",
        )

        with patch("asyncio.run", return_value=0):
            result = cmd_doc(args)

        assert result == 0

    def test_dispatch_list(self):
        """Dispatcher routes to list."""
        args = MockArgs(
            doc_command="list",
            gateway=None,
            api_key="sk-test",
        )

        with patch("asyncio.run", return_value=0):
            result = cmd_doc(args)

        assert result == 0

    def test_dispatch_get(self):
        """Dispatcher routes to get."""
        args = MockArgs(
            doc_command="get",
            doc_id="uuid-1234",
            output="/tmp/out.bin",
            o=None,
            gateway=None,
            api_key="sk-test",
        )

        with patch("asyncio.run", return_value=0):
            result = cmd_doc(args)

        assert result == 0

    def test_dispatch_delete(self):
        """Dispatcher routes to delete."""
        args = MockArgs(
            doc_command="delete",
            doc_id="uuid-1234",
            gateway=None,
            api_key="sk-test",
        )

        with patch("asyncio.run", return_value=0):
            result = cmd_doc(args)

        assert result == 0

    def test_dispatch_unknown(self):
        """Dispatcher shows usage for unknown command."""
        args = MockArgs(doc_command=None)

        result = cmd_doc(args)
        assert result == 1
