"""Tests for ADKSyncEngine — three-way reconciliation of adk data."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adk.sync.adk_sync_engine import ADKSyncEngine, BaseManifestDB


@pytest.fixture
def temp_aither_dir():
    """Create a temporary ~/.aither structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        aither_root = Path(tmpdir)
        (aither_root / "memory").mkdir(parents=True)
        (aither_root / "graph").mkdir(parents=True)
        (aither_root / "sync").mkdir(parents=True)
        yield aither_root


@pytest.fixture
def mock_drive_client():
    """Create a mock DriveClient."""
    client = AsyncMock()
    client.list_changes = AsyncMock()
    client.download = AsyncMock()
    client.upload = AsyncMock()
    client.delete = AsyncMock()
    client.close = AsyncMock()
    return client


def test_base_manifest_db_persistence(temp_aither_dir):
    """Test persisting and loading base manifest."""
    db_path = temp_aither_dir / "sync" / "base.json"
    db = BaseManifestDB(db_path=db_path)

    # Initially empty
    assert db.get_base() == {}

    # Store a manifest
    manifest = {
        "file1.txt": {
            "hash": "abc123",
            "size": 100,
            "mtime": 1.0,
            "version": 1,
            "deleted": False,
        }
    }
    db.set_base(manifest)

    # Load it back
    loaded = db.get_base()
    assert loaded == manifest


def test_base_manifest_db_missing_file(temp_aither_dir):
    """Test loading manifest when file doesn't exist."""
    db = BaseManifestDB(db_path=temp_aither_dir / "sync" / "missing.json")
    assert db.get_base() == {}


def test_adk_sync_engine_initialization(temp_aither_dir, mock_drive_client):
    """Test ADKSyncEngine initialization."""
    engine = ADKSyncEngine(
        drive_client=mock_drive_client,
        manifest_db=BaseManifestDB(db_path=temp_aither_dir / "sync" / "base.json"),
        endpoint_name="test-device",
    )

    assert engine.endpoint_name == "test-device"
    assert engine.include_dirs == ["memory", "graph"]


def test_scan_local_empty(temp_aither_dir, mock_drive_client):
    """Test scanning local when no files exist."""
    with patch("adk.sync.adk_sync_engine._ensure_aitheros_path"):
        with patch("adk.sync.adk_sync_engine._get_filestate") as mock_fs_getter:
            # Mock FileState
            mock_fs_class = MagicMock()
            mock_fs_class.side_effect = lambda **kwargs: MagicMock(**kwargs)
            mock_fs_getter.return_value = mock_fs_class

            engine = ADKSyncEngine(
                drive_client=mock_drive_client,
                manifest_db=BaseManifestDB(
                    db_path=temp_aither_dir / "sync" / "base.json"
                ),
            )
            # Patch the aither_root to use our temp dir
            engine.aither_root = temp_aither_dir

            manifest = engine.scan_local()
            assert manifest == {}


def test_scan_local_with_files(temp_aither_dir, mock_drive_client):
    """Test scanning local with files present."""
    # Create test files
    (temp_aither_dir / "memory" / "test.jsonl").write_text("line1\n")
    (temp_aither_dir / "graph" / "entities.db").write_bytes(b"binary data")

    with patch("adk.sync.adk_sync_engine._ensure_aitheros_path"):
        with patch("adk.sync.adk_sync_engine._get_filestate") as mock_fs_getter:
            # Create a real FileState-like class
            class MockFileState:
                def __init__(self, hash, size, mtime, version, deleted):
                    self.hash = hash
                    self.size = size
                    self.mtime = mtime
                    self.version = version
                    self.deleted = deleted

            mock_fs_getter.return_value = MockFileState

            engine = ADKSyncEngine(
                drive_client=mock_drive_client,
                manifest_db=BaseManifestDB(
                    db_path=temp_aither_dir / "sync" / "base.json"
                ),
            )
            engine.aither_root = temp_aither_dir

            manifest = engine.scan_local()

            assert "memory/test.jsonl" in manifest
            assert "graph/entities.db" in manifest
            # File size is 7 with newline
            assert manifest["memory/test.jsonl"].size == 7
            assert manifest["graph/entities.db"].size == 11


@pytest.mark.asyncio
async def test_reconcile_once_success(temp_aither_dir, mock_drive_client):
    """Test reconcile_once with successful actions."""
    # Set up remote changes
    mock_drive_client.list_changes.return_value = (
        1,
        {},  # No changes initially
    )

    with patch("adk.sync.adk_sync_engine._ensure_aitheros_path"):
        with patch("adk.sync.adk_sync_engine._get_reconcile") as mock_reconcile_getter:
            with patch("adk.sync.adk_sync_engine._get_filestate") as mock_fs_getter:
                with patch("adk.sync.adk_sync_engine._get_syncaction") as mock_sa_getter:
                    with patch(
                        "adk.sync.adk_sync_engine._get_actionkind"
                    ) as mock_ak_getter:
                        # Mock reconcile to return NOOP actions
                        class MockActionKind:
                            UPLOAD = "upload"
                            DOWNLOAD = "download"
                            DELETE_LOCAL = "delete_local"
                            DELETE_REMOTE = "delete_remote"
                            CONFLICT = "conflict"
                            NOOP = "noop"

                        class MockFileState:
                            def __init__(self, **kwargs):
                                self.__dict__.update(kwargs)

                        class MockSyncAction:
                            def __init__(self, path, kind, **kwargs):
                                self.path = path
                                self.kind = kind
                                for k, v in kwargs.items():
                                    setattr(self, k, v)

                        def mock_reconcile(local, remote, base, **kwargs):
                            return []  # No actions

                        mock_reconcile_getter.return_value = mock_reconcile
                        mock_fs_getter.return_value = MockFileState
                        mock_sa_getter.return_value = MockSyncAction
                        mock_ak_getter.return_value = MockActionKind

                        engine = ADKSyncEngine(
                            drive_client=mock_drive_client,
                            manifest_db=BaseManifestDB(
                                db_path=temp_aither_dir / "sync" / "base.json"
                            ),
                        )
                        engine.aither_root = temp_aither_dir

                        actions = await engine.reconcile_once()

                        assert actions == []
                        # Verify base was updated
                        base = engine.manifest_db.get_base()
                        assert isinstance(base, dict)


@pytest.mark.asyncio
async def test_reconcile_once_network_error(temp_aither_dir, mock_drive_client):
    """Test reconcile_once with network error."""
    mock_drive_client.list_changes.side_effect = Exception("Network error")

    with patch("adk.sync.adk_sync_engine._ensure_aitheros_path"):
        engine = ADKSyncEngine(
            drive_client=mock_drive_client,
            manifest_db=BaseManifestDB(
                db_path=temp_aither_dir / "sync" / "base.json"
            ),
        )
        engine.aither_root = temp_aither_dir

        with pytest.raises(Exception, match="Network error"):
            await engine.reconcile_once()


@pytest.mark.asyncio
async def test_apply_action_upload(temp_aither_dir, mock_drive_client):
    """Test applying an UPLOAD action."""
    # Create test file
    test_file = temp_aither_dir / "memory" / "test.jsonl"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("test content")

    with patch("adk.sync.adk_sync_engine._get_actionkind") as mock_ak_getter:

        class MockActionKind:
            UPLOAD = "upload"
            NOOP = "noop"

        class MockSyncAction:
            def __init__(self, path, kind, **kwargs):
                self.path = path
                self.kind = kind
                self.base_version = kwargs.get("base_version", 0)
                self.conflict_copy = kwargs.get("conflict_copy")

        mock_ak_getter.return_value = MockActionKind

        engine = ADKSyncEngine(
            drive_client=mock_drive_client,
            manifest_db=BaseManifestDB(
                db_path=temp_aither_dir / "sync" / "base.json"
            ),
        )
        engine.aither_root = temp_aither_dir

        action = MockSyncAction("memory/test.jsonl", "upload", base_version=0)
        await engine._apply_action(action)

        mock_drive_client.upload.assert_called_once()
        args, kwargs = mock_drive_client.upload.call_args
        assert args[0] == "memory/test.jsonl"
        assert args[1] == b"test content"


@pytest.mark.asyncio
async def test_apply_action_download(temp_aither_dir, mock_drive_client):
    """Test applying a DOWNLOAD action."""
    mock_drive_client.download.return_value = b"remote content"

    with patch("adk.sync.adk_sync_engine._get_actionkind") as mock_ak_getter:

        class MockActionKind:
            UPLOAD = "upload"
            DOWNLOAD = "download"
            DELETE_LOCAL = "delete_local"
            DELETE_REMOTE = "delete_remote"
            CONFLICT = "conflict"
            NOOP = "noop"

        class MockSyncAction:
            def __init__(self, path, kind, **kwargs):
                self.path = path
                self.kind = kind
                self.base_version = kwargs.get("base_version", 0)
                self.conflict_copy = kwargs.get("conflict_copy")

        mock_ak_getter.return_value = MockActionKind

        engine = ADKSyncEngine(
            drive_client=mock_drive_client,
            manifest_db=BaseManifestDB(
                db_path=temp_aither_dir / "sync" / "base.json"
            ),
        )
        engine.aither_root = temp_aither_dir

        action = MockSyncAction("memory/new.jsonl", "download", base_version=1)
        await engine._apply_action(action)

        mock_drive_client.download.assert_called_once_with("memory/new.jsonl")
        # Check file was written
        assert (temp_aither_dir / "memory" / "new.jsonl").read_bytes() == b"remote content"


@pytest.mark.asyncio
async def test_apply_action_conflict(temp_aither_dir, mock_drive_client):
    """Test applying a CONFLICT action — verifies local divergent content is preserved."""
    # Create test file with original local content
    test_file = temp_aither_dir / "memory" / "report.txt"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    original_local_content = b"local divergent version"
    test_file.write_bytes(original_local_content)

    # Mock the download to return cloud version
    cloud_version = b"cloud canonical version"
    mock_drive_client.download.return_value = cloud_version

    with patch("adk.sync.adk_sync_engine._get_actionkind") as mock_ak_getter:

        class MockActionKind:
            UPLOAD = "upload"
            DOWNLOAD = "download"
            DELETE_LOCAL = "delete_local"
            DELETE_REMOTE = "delete_remote"
            CONFLICT = "conflict"
            NOOP = "noop"

        class MockSyncAction:
            def __init__(self, path, kind, **kwargs):
                self.path = path
                self.kind = kind
                self.base_version = kwargs.get("base_version", 0)
                self.conflict_copy = kwargs.get("conflict_copy", None)

        mock_ak_getter.return_value = MockActionKind

        engine = ADKSyncEngine(
            drive_client=mock_drive_client,
            manifest_db=BaseManifestDB(
                db_path=temp_aither_dir / "sync" / "base.json"
            ),
        )
        engine.aither_root = temp_aither_dir

        action = MockSyncAction(
            "memory/report.txt",
            "conflict",
            base_version=1,
            conflict_copy="memory/report (conflict adk-device 20260703T1430).txt",
        )
        await engine._apply_action(action)

        # Verify cloud version is in local_path
        assert test_file.read_bytes() == cloud_version

        # Verify local divergent version is in conflict copy
        conflict_file = temp_aither_dir / "memory" / "report (conflict adk-device 20260703T1430).txt"
        assert conflict_file.exists()
        assert conflict_file.read_bytes() == original_local_content

        # Ensure they are NOT the same
        assert test_file.read_bytes() != conflict_file.read_bytes()

        mock_drive_client.download.assert_called_once_with("memory/report.txt")
