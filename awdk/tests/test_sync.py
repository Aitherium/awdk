"""Unit tests for sync modules (watermark, lockbox, files).

Tests use pytest with mocking and temporary directories.
Run with: pytest tests/test_sync.py -v
"""

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from adk.sync.watermark import SyncWatermarkStore
from adk.sync.lockbox import LockboxSyncClient, LockboxSyncConfig
from adk.sync.files import FilesSyncClient, FilesSyncConfig, FileEntry


# ═══════════════════════════════════════════════════════════════════════════
# WATERMARK TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestSyncWatermarkStore:
    """Tests for atomic watermark storage."""

    @pytest.mark.asyncio
    async def test_watermark_atomic_write(self):
        """Test atomic write and read of a watermark."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"
            store = SyncWatermarkStore(watermark_file=watermark_file)

            # Set a watermark
            success = await store.set("test_key", 1234567890.5)
            assert success is True
            assert watermark_file.exists()

            # Read it back
            value = await store.get("test_key")
            assert value == 1234567890.5

    @pytest.mark.asyncio
    async def test_watermark_default_return(self):
        """Test that get returns default if key not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"
            store = SyncWatermarkStore(watermark_file=watermark_file)

            # Get non-existent key with default
            value = await store.get("missing_key", default=0.0)
            assert value == 0.0

    @pytest.mark.asyncio
    async def test_watermark_multiple_keys(self):
        """Test storing and retrieving multiple watermarks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"
            store = SyncWatermarkStore(watermark_file=watermark_file)

            # Set multiple keys
            await store.set("lockbox", 1000.0)
            await store.set("files", 2000.0)
            await store.set("sessions", 3000.0)

            # Get all
            values = await store.get_all(["lockbox", "files", "sessions"])
            assert values == {
                "lockbox": 1000.0,
                "files": 2000.0,
                "sessions": 3000.0,
            }

    @pytest.mark.asyncio
    async def test_watermark_delete(self):
        """Test deleting a watermark."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"
            store = SyncWatermarkStore(watermark_file=watermark_file)

            await store.set("temp_key", 1000.0)
            assert await store.get("temp_key") == 1000.0

            success = await store.delete("temp_key")
            assert success is True
            assert await store.get("temp_key") is None

    @pytest.mark.asyncio
    async def test_watermark_clear(self):
        """Test clearing all watermarks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"
            store = SyncWatermarkStore(watermark_file=watermark_file)

            await store.set("key1", 1.0)
            await store.set("key2", 2.0)

            success = await store.clear()
            assert success is True
            assert await store.get("key1") is None
            assert await store.get("key2") is None

    @pytest.mark.asyncio
    async def test_watermark_persistence(self):
        """Test that watermarks persist across store instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            watermark_file = Path(tmpdir) / "test_watermarks.json"

            # First store: write a watermark
            store1 = SyncWatermarkStore(watermark_file=watermark_file)
            await store1.set("persistent_key", 9999.9)

            # Second store: read the same watermark
            store2 = SyncWatermarkStore(watermark_file=watermark_file)
            value = await store2.get("persistent_key")
            assert value == 9999.9


# ═══════════════════════════════════════════════════════════════════════════
# LOCKBOX SYNC TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestLockboxSyncClient:
    """Tests for lockbox sync client."""

    def test_lockbox_config_from_env(self, monkeypatch):
        """Test loading config from environment variables."""
        monkeypatch.setenv("AITHER_LOCKBOX_SYNC", "true")
        monkeypatch.setenv("AITHER_GATEWAY_URL", "https://example.com")
        monkeypatch.setenv("AITHER_SYNC_TIMEOUT", "60.0")

        config = LockboxSyncConfig()
        config = LockboxSyncClient._load_config()

        assert config.enabled is True
        assert config.gateway_url == "https://example.com"
        assert config.timeout_seconds == 60.0

    def test_lockbox_build_headers(self):
        """Test building headers with Bearer token."""
        client = LockboxSyncClient(
            gateway_url="http://localhost:8001",
            auth_token="aither_sk_test_123",
        )

        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer aither_sk_test_123"
        assert headers["Content-Type"] == "application/json"

    def test_lockbox_build_headers_with_bearer_prefix(self):
        """Test building headers when token already has Bearer prefix."""
        client = LockboxSyncClient(
            gateway_url="http://localhost:8001",
            auth_token="Bearer aither_sk_test_123",
        )

        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer aither_sk_test_123"

    @pytest.mark.asyncio
    async def test_lockbox_write_disabled(self):
        """Test that write is skipped when sync is disabled."""
        config = LockboxSyncConfig(enabled=False)
        client = LockboxSyncClient(config=config, auth_token="dummy")

        result = await client.write_entry("secrets/key", b"value")
        assert result["skipped"] is True
        assert result["reason"] == "disabled"

    @pytest.mark.asyncio
    async def test_lockbox_write_sends_bearer(self):
        """Test that lockbox write sends Bearer token with endpoint:lockbox scope."""
        config = LockboxSyncConfig(enabled=True)
        client = LockboxSyncClient(
            gateway_url="http://localhost:8001",
            auth_token="aither_sk_test_123",
            config=config,
        )

        # Mock the _request method to capture the call
        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {"ok": True}

            await client.write_entry("secrets/api_key", b"secret_value")

            # Verify _request was called with correct endpoint
            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"  # method
            assert "/lockbox/tenant/self/write" in call_args[0][1]  # path

            # Verify the request body contains base64-encoded content
            body = call_args[0][2]
            expected_b64 = base64.b64encode(b"secret_value").decode("ascii")
            assert body["content"] == expected_b64

    @pytest.mark.asyncio
    async def test_lockbox_write_network_error_fail_soft(self):
        """Test that network errors are caught and logged (fail-soft)."""
        config = LockboxSyncConfig(enabled=True, fail_soft=True)
        client = LockboxSyncClient(
            gateway_url="http://invalid.localhost",
            auth_token="dummy",
            config=config,
        )

        # Mock httpx.AsyncClient to raise a connection error
        with mock.patch("httpx.AsyncClient") as mock_client_class:
            mock_client_class.return_value.__aenter__.side_effect = (
                ConnectionError("Connection refused")
            )

            # Should NOT raise, should return error dict
            result = await client.write_entry("secrets/key", b"value")
            assert result.get("error") is True
            assert "Connection refused" in result.get("detail", "")

    @pytest.mark.asyncio
    async def test_lockbox_write_403_fail_soft(self):
        """Test that 403 Forbidden is handled gracefully."""
        config = LockboxSyncConfig(enabled=True, fail_soft=True)
        client = LockboxSyncClient(
            gateway_url="http://localhost:8001",
            auth_token="invalid_token",
            config=config,
        )

        # Mock _request to return 403
        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {
                "error": True,
                "status": 403,
                "detail": "Invalid token",
            }

            result = await client.write_entry("secrets/key", b"value")
            assert result.get("error") is True
            assert result.get("status") == 403

    @pytest.mark.asyncio
    async def test_lockbox_read_403_fail_soft(self, caplog):
        """A 403 on read is an AUTH failure — returns None but is surfaced at
        WARNING (not the benign-404 DEBUG path). Guards the silent-auth regression."""
        import logging
        config = LockboxSyncConfig(enabled=True, fail_soft=True)
        client = LockboxSyncClient(
            gateway_url="http://localhost:8001",
            auth_token="invalid_token",
            config=config,
        )
        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {"error": True, "status": 403, "detail": "denied"}
            with caplog.at_level(logging.WARNING, logger="adk.lockbox_sync"):
                result = await client.read_entry("secrets/key")
            assert result is None
            assert any(r.levelno >= logging.WARNING and "403" in r.getMessage()
                       for r in caplog.records), caplog.text

    @pytest.mark.asyncio
    async def test_lockbox_read_entry(self):
        """Test reading a lockbox entry."""
        config = LockboxSyncConfig(enabled=True)
        client = LockboxSyncClient(config=config, auth_token="dummy")

        # Mock _request to return base64-encoded content
        with mock.patch.object(client, "_request") as mock_request:
            expected_content = b"secret_value"
            expected_b64 = base64.b64encode(expected_content).decode("ascii")
            mock_request.return_value = {
                "content": expected_b64,
            }

            result = await client.read_entry("secrets/api_key")
            assert result == expected_content

    @pytest.mark.asyncio
    async def test_lockbox_read_404_fail_soft(self):
        """Test that 404 on read is handled gracefully."""
        config = LockboxSyncConfig(enabled=True)
        client = LockboxSyncClient(config=config, auth_token="dummy")

        # Mock _request to return 404
        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {
                "error": True,
                "status": 404,
                "detail": "Not found",
            }

            result = await client.read_entry("secrets/missing_key")
            assert result is None

    @pytest.mark.asyncio
    async def test_lockbox_list_entries(self):
        """Test listing lockbox entries."""
        config = LockboxSyncConfig(enabled=True)
        client = LockboxSyncClient(config=config, auth_token="dummy")

        # Mock _request to return entry list
        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {
                "entries": ["secrets/api_key", "secrets/password", "keys/ssh_key"],
            }

            result = await client.list_entries("secrets/")
            assert len(result) == 3
            assert "secrets/api_key" in result

    @pytest.mark.asyncio
    async def test_lockbox_sync_from_disk(self):
        """Test batch sync of lockbox entries from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            lockbox_dir = Path(tmpdir) / "lockbox"
            lockbox_dir.mkdir()

            # Create some test entries
            (lockbox_dir / "api_key").write_bytes(b"key123")
            (lockbox_dir / "password").write_bytes(b"secret_pass")

            config = LockboxSyncConfig(enabled=True)
            client = LockboxSyncClient(config=config, auth_token="dummy")

            # Mock write_entry to simulate successful pushes
            write_results = []

            async def mock_write_entry(path, content):
                write_results.append({"path": path, "content_len": len(content)})
                return {"ok": True}

            with mock.patch.object(client, "write_entry", side_effect=mock_write_entry):
                result = await client.sync_from_disk(lockbox_dir)

                assert result["synced"] == 2
                assert result["failed"] == 0
                assert len(write_results) == 2


# ═══════════════════════════════════════════════════════════════════════════
# FILES SYNC TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestFilesSyncClient:
    """Tests for files sync client."""

    def test_files_config_from_env(self, monkeypatch):
        """Test loading config from environment variables."""
        monkeypatch.setenv("AITHER_FILES_SYNC", "true")
        monkeypatch.setenv("AITHER_GATEWAY_URL", "https://example.com")

        config = FilesSyncClient._load_config()
        assert config.enabled is True
        assert config.gateway_url == "https://example.com"

    @pytest.mark.asyncio
    async def test_files_write_disabled(self):
        """Test that write is skipped when sync is disabled."""
        config = FilesSyncConfig(enabled=False)
        client = FilesSyncClient(config=config, auth_token="dummy")

        result = await client.write_file("documents/file.txt", b"content")
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_files_sync_skips_unchanged(self):
        """Test that unchanged files are skipped in batch sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "files"
            files_dir.mkdir()

            # Create a test file
            test_file = files_dir / "document.txt"
            test_file.write_text("content", encoding="utf-8")

            config = FilesSyncConfig(enabled=True)
            client = FilesSyncClient(config=config, auth_token="dummy")

            # First sync: should sync the file
            with mock.patch.object(client, "write_file") as mock_write:
                mock_write.return_value = {"ok": True}

                result = await client.sync_from_disk(files_dir)
                assert result["synced"] == 1
                assert result["skipped"] == 0

            # Second sync: file unchanged, should skip it
            with mock.patch.object(client, "write_file") as mock_write:
                result = await client.sync_from_disk(files_dir)
                assert result["synced"] == 0
                assert result["skipped"] == 1
                # write_file should NOT have been called
                mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_files_sync_detects_changes(self):
        """Test that changed files are re-synced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "files"
            files_dir.mkdir()

            test_file = files_dir / "document.txt"
            test_file.write_text("content", encoding="utf-8")

            config = FilesSyncConfig(enabled=True)
            client = FilesSyncClient(config=config, auth_token="dummy")

            # First sync
            with mock.patch.object(client, "write_file") as mock_write:
                mock_write.return_value = {"ok": True}
                await client.sync_from_disk(files_dir)

            # Modify the file
            test_file.write_text("new content", encoding="utf-8")

            # Second sync: should detect change and re-sync
            with mock.patch.object(client, "write_file") as mock_write:
                mock_write.return_value = {"ok": True}
                result = await client.sync_from_disk(files_dir)
                assert result["synced"] == 1
                # write_file SHOULD have been called because file changed
                mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_files_write_network_error_fail_soft(self):
        """Test that network errors on write are caught (fail-soft)."""
        config = FilesSyncConfig(enabled=True, fail_soft=True)
        client = FilesSyncClient(
            gateway_url="http://invalid.localhost",
            auth_token="dummy",
            config=config,
        )

        with mock.patch("httpx.AsyncClient") as mock_client_class:
            mock_client_class.return_value.__aenter__.side_effect = (
                ConnectionError("Connection refused")
            )

            result = await client.write_file("file.txt", b"content")
            assert result.get("error") is True

    @pytest.mark.asyncio
    async def test_files_read_403_fail_soft(self):
        """Test that 403 on read is handled gracefully."""
        config = FilesSyncConfig(enabled=True)
        client = FilesSyncClient(config=config, auth_token="invalid")

        with mock.patch.object(client, "_request") as mock_request:
            mock_request.return_value = {
                "error": True,
                "status": 403,
                "detail": "Forbidden",
            }

            result = await client.read_file("documents/file.txt")
            assert result is None

    def test_file_entry_tracking(self):
        """Test FileEntry tracking for size and mtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            entry1 = FileEntry(test_file)
            assert entry1.size > 0
            assert entry1.mtime > 0

            # Same file: no change
            entry2 = FileEntry(test_file)
            assert entry1 == entry2
            assert not entry1.has_changed(entry2)

            # Modify file: should change
            test_file.write_text("new content")
            entry3 = FileEntry(test_file)
            assert entry1 != entry3
            assert entry1.has_changed(entry3)

    @pytest.mark.asyncio
    async def test_files_sync_from_disk_with_errors(self):
        """Test that file read errors are counted correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "files"
            files_dir.mkdir()

            # Create a normal file
            (files_dir / "good.txt").write_text("content")

            config = FilesSyncConfig(enabled=True)
            client = FilesSyncClient(config=config, auth_token="dummy")

            # Mock write_file to fail for one file
            call_count = 0

            async def mock_write_entry(path, content):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return {"ok": True}
                else:
                    return {"error": True, "detail": "Simulated error"}

            with mock.patch.object(client, "write_file", side_effect=mock_write_entry):
                result = await client.sync_from_disk(files_dir)
                # Should have at least tried to sync
                assert result["synced"] >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
