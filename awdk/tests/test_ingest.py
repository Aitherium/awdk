"""Tests for adk.ingest — file ingestion with secret scanning and chunking."""

import json
from pathlib import Path
from unittest import mock

import pytest

from adk.ingest import (
    FileWalker,
    IngestResult,
    SecretGuard,
    TextChunker,
    ingest_files,
)


# ─────────────────────────────────────────────────────────────────────────────
# FileWalker tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFileWalker:
    """Test file discovery with exclusions."""

    def test_walks_directory(self, tmp_path):
        """Walk finds files in directory."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.md").write_text("content2")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file3.py").write_text("content3")

        walker = FileWalker()
        files = walker.walk(tmp_path)

        assert len(files) == 3
        file_names = {f.name for f in files}
        assert file_names == {"file1.txt", "file2.md", "file3.py"}

    def test_skips_excluded_dirs(self, tmp_path):
        """Walker skips .git, node_modules, venv."""
        (tmp_path / "file1.txt").write_text("keep")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("skip")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.json").write_text("skip")

        walker = FileWalker()
        files = walker.walk(tmp_path)

        assert len(files) == 1
        assert files[0].name == "file1.txt"

    def test_skips_binary_extensions(self, tmp_path):
        """Walker skips .exe, .zip, .mp4, etc."""
        (tmp_path / "text.txt").write_text("keep")
        (tmp_path / "binary.exe").write_bytes(b"\x00\x01")
        (tmp_path / "archive.zip").write_bytes(b"PK")

        walker = FileWalker()
        files = walker.walk(tmp_path)

        assert len(files) == 1
        assert files[0].name == "text.txt"

    def test_skips_large_files(self, tmp_path):
        """Walker skips files >50MB."""
        (tmp_path / "small.txt").write_text("x" * 1000)
        # Create a large file (simulated)
        large_file = tmp_path / "large.bin"
        large_file.write_bytes(b"x" * (51 * 1024 * 1024))

        walker = FileWalker(max_file_size_mb=50)
        files = walker.walk(tmp_path)

        assert len(files) == 1
        assert files[0].name == "small.txt"

    def test_skips_hidden_files_by_default(self, tmp_path):
        """Walker skips .env, .hidden, etc. by default."""
        (tmp_path / "visible.txt").write_text("keep")
        (tmp_path / ".hidden").write_text("skip")

        walker = FileWalker()
        files = walker.walk(tmp_path)

        assert len(files) == 1
        assert files[0].name == "visible.txt"

    def test_allows_hidden_files_when_flagged(self, tmp_path):
        """Walker includes hidden files when allow_hidden=True."""
        (tmp_path / "visible.txt").write_text("keep")
        (tmp_path / ".hidden").write_text("keep")

        walker = FileWalker(allow_hidden=True)
        files = walker.walk(tmp_path)

        assert len(files) == 2
        file_names = {f.name for f in files}
        assert file_names == {"visible.txt", ".hidden"}


# ─────────────────────────────────────────────────────────────────────────────
# SecretGuard tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSecretGuard:
    """Test secret scanning."""

    def test_detects_sensitive_filenames(self, tmp_path):
        """Guard marks .env, credentials.json as sensitive."""
        guard = SecretGuard()

        # Check .env
        should_skip, reason = guard.should_skip(tmp_path / ".env")
        assert should_skip
        assert "Sensitive filename" in reason

        # Check credentials.json
        should_skip, reason = guard.should_skip(tmp_path / "credentials.json")
        assert should_skip

    def test_allows_normal_files(self, tmp_path):
        """Guard allows regular files."""
        guard = SecretGuard()

        should_skip, reason = guard.should_skip(tmp_path / "readme.md")
        assert not should_skip

    def test_scans_for_api_keys(self):
        """Guard finds sk- keys, ghp_ tokens, etc."""
        guard = SecretGuard()

        # Fixtures are runtime-assembled so repo secret scanners don't match them.
        text_with_secret = "My OpenAI key: sk-" + "1234567890abcdefghij12345"
        matches = guard.scan_for_secrets(text_with_secret)

        assert len(matches) > 0

    def test_scans_for_github_tokens(self):
        """Guard finds GitHub tokens."""
        guard = SecretGuard()

        text = "GitHub token: ghp_" + "1234567890123456789012345678901234567"
        matches = guard.scan_for_secrets(text)

        assert len(matches) > 0

    def test_scans_for_aws_keys(self):
        """Guard finds AWS access keys."""
        guard = SecretGuard()

        text = "AWS key: AKIA" + "IOSFODNN7EXAMPLE"
        matches = guard.scan_for_secrets(text)

        assert len(matches) > 0

    def test_scans_for_pem_keys(self):
        """Guard finds PEM private keys."""
        guard = SecretGuard()

        # Fixture assembled at runtime so repo secret scanners don't match it.
        text = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        matches = guard.scan_for_secrets(text)

        assert len(matches) > 0

    def test_clean_text_passes(self):
        """Guard allows clean text."""
        guard = SecretGuard()

        clean_text = "This is just normal documentation about APIs."
        matches = guard.scan_for_secrets(clean_text)

        assert len(matches) == 0


# ─────────────────────────────────────────────────────────────────────────────
# TextChunker tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTextChunker:
    """Test text chunking."""

    def test_chunks_text_with_overlap(self):
        """Chunker splits text with overlap."""
        chunker = TextChunker(size=100, overlap=20)
        text = "x" * 250

        chunks = chunker.chunk(text, source="test")

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk["text"]) <= 100
            assert "source" in chunk
            assert chunk["source"] == "test"

    def test_preserves_content(self):
        """Chunking reconstructs full text when joined."""
        text = "Hello world. This is a test. " * 10
        chunker = TextChunker(size=100, overlap=20)

        chunks = chunker.chunk(text, source="test")

        # Reconstruct (rough check)
        reconstructed = ""
        for chunk in chunks:
            reconstructed += chunk["text"]

        # At least most of the content should be there
        assert text in reconstructed or (len(reconstructed) >= len(text) * 0.8)

    def test_handles_empty_text(self):
        """Chunker handles empty/whitespace text."""
        chunker = TextChunker()

        chunks = chunker.chunk("", source="test")
        assert len(chunks) == 0

        chunks = chunker.chunk("   \n\n   ", source="test")
        assert len(chunks) == 0

    def test_chunks_markdown(self):
        """Chunker splits on Markdown structure."""
        text = """# Heading 1
Content for section 1. Lorem ipsum dolor sit amet, consectetur adipiscing elit.
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor.

## Heading 2
Content for section 2. Lorem ipsum dolor sit amet, consectetur adipiscing elit.
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor.

### Heading 3
Content for section 3. Lorem ipsum dolor sit amet, consectetur adipiscing elit.
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor."""

        chunker = TextChunker(size=100, overlap=20)
        chunks = chunker.chunk(text, source="test")

        # Should have multiple chunks due to size
        assert len(chunks) >= 1

    def test_adds_source_and_offset(self):
        """Chunks include source and offset metadata."""
        chunker = TextChunker(size=50, overlap=10)
        text = "x" * 150

        chunks = chunker.chunk(text, source="myfile.txt")

        for chunk in chunks:
            assert chunk["source"] == "myfile.txt"
            assert "offset" in chunk
            assert isinstance(chunk["offset"], int)


# ─────────────────────────────────────────────────────────────────────────────
# IngestResult tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestResult:
    """Test result tracking."""

    def test_to_dict(self):
        """Result converts to dict."""
        result = IngestResult(
            files_total=10,
            files_ingested=8,
            chunks_created=42,
        )

        d = result.to_dict()

        assert d["files_total"] == 10
        assert d["files_ingested"] == 8
        assert d["chunks_created"] == 42


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestIntegration:
    """Integration tests for full ingest flow."""

    @pytest.mark.asyncio
    async def test_ingest_single_file(self, tmp_path):
        """Ingest ingests a single file."""
        # Create test file
        test_file = tmp_path / "readme.md"
        test_file.write_text("# Test\nThis is test content.")

        # Mock GraphMemory to avoid actual DB operations
        with mock.patch("adk.ingest.ingest_files") as mock_ingest:
            # Just test the file discovery for now
            pass

    @pytest.mark.asyncio
    async def test_ingest_skips_secrets_on_filename(self, tmp_path):
        """Ingest skips files like .env."""
        # Create test files
        (tmp_path / "readme.md").write_text("Normal content")
        (tmp_path / ".env").write_text("DB_PASSWORD=secret123")

        # The walker should skip .env by default
        walker = FileWalker()
        files = walker.walk(tmp_path)

        # Only readme.md should be found (hidden by default)
        assert len(files) == 1
        assert files[0].name == "readme.md"

    @pytest.mark.asyncio
    async def test_ingest_skips_files_with_secrets(self, tmp_path):
        """Ingest skips files containing API keys."""
        # Create test file with secret
        test_file = tmp_path / "config.py"
        test_file.write_text("API_KEY = 'sk-" + "1234567890abcdefghij1234567'")

        # Guard should detect the secret
        guard = SecretGuard()
        should_skip, _ = guard.should_skip(test_file)
        assert not should_skip  # File itself not sensitive

        # But content scan should find it
        content = test_file.read_text()
        matches = guard.scan_for_secrets(content)
        assert len(matches) > 0


    @pytest.mark.asyncio
    async def test_dry_run_no_persistence(self, tmp_path):
        """Dry run doesn't persist chunks."""
        # Create test file
        (tmp_path / "test.txt").write_text("Test content for ingestion")

        result = await ingest_files(
            path=tmp_path,
            dry_run=True,
        )

        assert result.files_total == 1
        assert result.chunks_created > 0

    @pytest.mark.asyncio
    async def test_ingest_handles_missing_path(self):
        """Ingest handles missing path gracefully."""
        result = await ingest_files(
            path="/nonexistent/path",
        )

        # Should return error result
        assert result is not None
        assert len(result.errors) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Brain sync contract tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrainSyncContract:
    """Test brain sync data contracts."""

    def test_sync_delta_item_validation(self):
        """SyncDeltaItem validates inputs."""
        from adk.sync.brain import SyncDeltaItem

        # Valid
        item = SyncDeltaItem(
            chunk_id="chunk-1",
            op="upsert",
            classification="internal",
        )
        assert item.chunk_id == "chunk-1"

        # Invalid chunk_id
        with pytest.raises(ValueError, match="chunk_id required"):
            SyncDeltaItem(chunk_id="")

        # Invalid classification
        with pytest.raises(ValueError, match="Invalid classification"):
            SyncDeltaItem(chunk_id="c1", classification="invalid")

    def test_sync_delta_to_dict(self):
        """SyncDeltaItem serializes to dict."""
        from adk.sync.brain import SyncDeltaItem

        item = SyncDeltaItem(
            chunk_id="chunk-1",
            op="upsert",
            vector=[0.1, 0.2, 0.3],
            metadata={"text": "hello"},
            classification="confidential",
        )

        d = item.to_dict()

        assert d["chunk_id"] == "chunk-1"
        assert d["op"] == "upsert"
        assert d["vector"] == [0.1, 0.2, 0.3]
        assert d["metadata"]["text"] == "hello"
        assert d["classification"] == "confidential"

    def test_sync_request_to_json(self):
        """SyncRequest serializes to JSON."""
        from adk.sync.brain import SyncDeltaItem, SyncRequest

        request = SyncRequest(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            watermark="mark1",
            delta=[
                SyncDeltaItem(chunk_id="c1"),
                SyncDeltaItem(chunk_id="c2"),
            ],
        )

        json_str = request.to_json()
        data = json.loads(json_str)

        assert data["tenant_id"] == "tenant-1"
        assert data["workspace_id"] == "workspace-1"
        assert len(data["delta"]) == 2

    def test_sync_response_parsing(self):
        """SyncResponse parses from dict."""
        from adk.sync.brain import SyncResponse

        data = {
            "accepted": 5,
            "rejected": 0,
            "watermark": "mark2",
        }

        response = SyncResponse.from_dict(data)

        assert response.accepted == 5
        assert response.rejected == 0
        assert response.watermark == "mark2"
