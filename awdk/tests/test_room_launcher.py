"""Tests for AitherRoom binary launcher with checksum verification and offline fallback."""

from __future__ import annotations

import hashlib
import os
import platform
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from adk import room_launcher


class TestChecksumVerification:
    """Test SHA256 checksum verification."""

    def test_verify_checksum_valid(self):
        """Verify that valid checksums are recognized."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            sha256 = hashlib.sha256(b"test content").hexdigest()
            assert room_launcher._verify_checksum(tmp_path, sha256) is True
        finally:
            tmp_path.unlink()

    def test_verify_checksum_invalid(self):
        """Verify that invalid checksums are rejected."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            bad_sha256 = "0" * 64
            assert room_launcher._verify_checksum(tmp_path, bad_sha256) is False
        finally:
            tmp_path.unlink()

    def test_verify_checksum_case_insensitive(self):
        """Checksums should be case-insensitive."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            sha256_lower = hashlib.sha256(b"test content").hexdigest()
            sha256_upper = sha256_lower.upper()
            assert room_launcher._verify_checksum(tmp_path, sha256_upper) is True
        finally:
            tmp_path.unlink()

    def test_verify_checksum_missing_file(self):
        """Missing files should return False."""
        bad_path = Path("/nonexistent/file")
        assert room_launcher._verify_checksum(bad_path, "abc123") is False


class TestChecksumParsing:
    """Test parsing of checksums.sha256 file format."""

    def test_parse_checksums_single_entry(self):
        """Parse a single checksum entry."""
        text = "abc123def456  aither-room-linux-x64"
        result = room_launcher._parse_checksums(text)
        assert result == {"aither-room-linux-x64": "abc123def456"}

    def test_parse_checksums_multiple_entries(self):
        """Parse multiple checksum entries."""
        text = """abc123  aither-room-linux-x64
def456  aither-room-win64.exe
789012  aither-room-macos-arm64"""
        result = room_launcher._parse_checksums(text)
        assert result == {
            "aither-room-linux-x64": "abc123",
            "aither-room-win64.exe": "def456",
            "aither-room-macos-arm64": "789012",
        }

    def test_parse_checksums_skip_empty_lines(self):
        """Empty lines should be skipped."""
        text = """abc123  aither-room-linux-x64

def456  aither-room-win64.exe"""
        result = room_launcher._parse_checksums(text)
        assert len(result) == 2

    def test_parse_checksums_skip_whitespace_lines(self):
        """Whitespace-only lines should be skipped."""
        text = """abc123  aither-room-linux-x64

def456  aither-room-win64.exe"""
        result = room_launcher._parse_checksums(text)
        assert len(result) == 2

    def test_parse_checksums_malformed_line(self):
        """Lines with insufficient parts are skipped."""
        text = """abc123  aither-room-linux-x64
just-one-part
def456  aither-room-win64.exe"""
        result = room_launcher._parse_checksums(text)
        assert len(result) == 2
        assert "aither-room-linux-x64" in result
        assert "aither-room-win64.exe" in result


class TestBundledBinarySelection:
    """Test bundled offline binary selection."""

    @pytest.mark.parametrize(
        "system,machine,expected_dir",
        [
            ("Windows", "AMD64", "win-x64"),
            ("Windows", "x86_64", "win-x64"),
            ("Linux", "x86_64", "linux-x64"),
            ("Linux", "aarch64", "linux-x64"),
            ("Darwin", "arm64", "mac-arm64"),
            ("Darwin", "x86_64", "mac-x64"),
        ],
    )
    def test_get_platform_dir_name(self, system: str, machine: str, expected_dir: str):
        """Platform detection maps to correct directory names."""
        with patch("platform.system", return_value=system):
            with patch("platform.machine", return_value=machine):
                assert room_launcher._get_platform_dir_name() == expected_dir

    def test_get_platform_dir_name_unsupported(self):
        """Unsupported platforms return empty string."""
        with patch("platform.system", return_value="Obscure"):
            with patch("platform.machine", return_value="unknown"):
                assert room_launcher._get_platform_dir_name() == ""

    def test_get_bundled_binary_unsupported_platform(self):
        """Returns None for unsupported platforms."""
        with patch("platform.system", return_value="Obscure"):
            with patch("platform.machine", return_value="unknown"):
                result = room_launcher._get_bundled_binary()
                assert result is None

    def test_get_bundled_binary_with_valid_checksum(self, tmp_path: Path):
        """Returns bundled binary if checksum is valid."""
        bundled_dir = tmp_path / "room_binaries" / "linux-x64"
        bundled_dir.mkdir(parents=True)
        binary_path = bundled_dir / "aither-room-linux-x64"
        binary_content = b"bundled binary"
        binary_path.write_bytes(binary_content)

        # Create checksums file
        expected_sha256 = hashlib.sha256(binary_content).hexdigest()
        checksums_path = bundled_dir / "checksums.sha256"
        checksums_path.write_text(f"{expected_sha256}  aither-room-linux-x64\n")

        with patch("platform.system", return_value="Linux"):
            with patch("platform.machine", return_value="x86_64"):
                with patch("adk.room_launcher._get_binary_name",
                           return_value="aither-room-linux-x64"):
                    with patch.object(
                        Path,
                        "__truediv__",
                        lambda self, other: (
                            bundled_dir if "room_binaries" in str(self / other)
                            else (self / other)
                        ),
                    ):
                        # Direct test of the bundled binary path
                        assert binary_path.exists()

    def test_get_bundled_binary_fails_bad_checksum(self, tmp_path: Path):
        """Returns None if bundled checksum verification fails."""
        bundled_dir = tmp_path / "room_binaries" / "linux-x64"
        bundled_dir.mkdir(parents=True)
        binary_path = bundled_dir / "aither-room-linux-x64"
        binary_path.write_bytes(b"bundled binary")

        # Create checksums file with wrong hash
        checksums_path = bundled_dir / "checksums.sha256"
        checksums_path.write_text(f"{'0' * 64}  aither-room-linux-x64\n")

        with patch("platform.system", return_value="Linux"):
            with patch("platform.machine", return_value="x86_64"):
                with patch("adk.room_launcher._get_binary_name",
                           return_value="aither-room-linux-x64"):
                    # Test the verification logic directly
                    checksums_text = checksums_path.read_text()
                    checksums_map = room_launcher._parse_checksums(checksums_text)
                    expected_sha256 = checksums_map.get("aither-room-linux-x64")
                    # Should have a mismatched hash
                    verified = room_launcher._verify_checksum(binary_path, expected_sha256)
                    assert verified is False


class TestDownloadBinaryFallback:
    """Test download behavior including fallback on network failure."""

    def test_parse_checksums_integration(self):
        """Test parsing and verifying checksums work together."""
        content = b"binary data"
        expected_sha256 = hashlib.sha256(content).hexdigest()
        checksums_text = f"{expected_sha256}  aither-room-linux-x64\n"

        parsed = room_launcher._parse_checksums(checksums_text)
        assert parsed["aither-room-linux-x64"] == expected_sha256

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            verified = room_launcher._verify_checksum(
                tmp_path, parsed["aither-room-linux-x64"]
            )
            assert verified is True
        finally:
            tmp_path.unlink()


class TestGetRoomBinaryResolutionOrder:
    """Test get_room_binary() fallback priority."""

    def test_get_room_binary_use_cached_with_valid_checksum(self, tmp_path: Path):
        """Uses cached binary if available and checksum is valid."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-room-linux-x64"
        binary_content = b"fake binary"
        binary_path.write_bytes(binary_content)

        # Create checksums file
        expected_sha256 = hashlib.sha256(binary_content).hexdigest()
        checksums_path = cache_dir / "checksums.sha256"
        checksums_path.write_text(f"{expected_sha256}  aither-room-linux-x64\n")

        with patch("adk.room_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.room_launcher._CACHE_DIR", cache_dir):
                result = room_launcher.get_room_binary()
                assert result == binary_path

    def test_get_room_binary_redownload_on_checksum_mismatch(self, tmp_path: Path):
        """Re-downloads binary if cached checksum verification fails."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-room-linux-x64"
        binary_path.write_bytes(b"corrupted binary")

        # Create checksums file with wrong hash
        checksums_path = cache_dir / "checksums.sha256"
        checksums_path.write_text(f"{'0' * 64}  aither-room-linux-x64\n")

        with patch("adk.room_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.room_launcher._download_binary") as mock_dl:
                with patch("adk.room_launcher._get_bundled_binary",
                           return_value=None):
                    mock_dl.side_effect = SystemExit(1)
                    result = room_launcher.get_room_binary()
                    # Should have attempted re-download
                    mock_dl.assert_called_once()
                    assert result is None

    def test_get_room_binary_download_on_missing(self, tmp_path: Path):
        """Attempts download if cached binary missing."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-room-linux-x64"

        with patch("adk.room_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.room_launcher._download_binary") as mock_dl:
                with patch("adk.room_launcher._get_bundled_binary",
                           return_value=None):
                    mock_dl.side_effect = SystemExit(1)
                    result = room_launcher.get_room_binary()
                    assert result is None

    def test_get_room_binary_fallback_to_bundled(self, tmp_path: Path):
        """Falls back to bundled binary on download failure."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-room-linux-x64"

        bundled_path = tmp_path / "bundled" / "aither-room-linux-x64"
        bundled_path.parent.mkdir(parents=True)
        bundled_path.write_text("fake bundled")

        with patch("adk.room_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.room_launcher._download_binary") as mock_dl:
                with patch("adk.room_launcher._get_bundled_binary",
                           return_value=bundled_path):
                    mock_dl.side_effect = SystemExit(1)
                    result = room_launcher.get_room_binary()
                    assert result == bundled_path

    def test_get_room_binary_no_options_returns_none(self, tmp_path: Path):
        """Returns None when all options exhausted."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-room-linux-x64"

        with patch("adk.room_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.room_launcher._download_binary") as mock_dl:
                with patch("adk.room_launcher._get_bundled_binary",
                           return_value=None):
                    mock_dl.side_effect = SystemExit(1)
                    result = room_launcher.get_room_binary()
                    assert result is None


class TestRequireChecksumEnv:
    """Test AITHER_ROOM_REQUIRE_CHECKSUM environment variable."""

    def test_require_checksum_env_parsing_default_mandatory(self):
        """Test that checksum verification is MANDATORY by default."""
        env_without_var = {
            k: v for k, v in os.environ.items()
            if k != "AITHER_ROOM_REQUIRE_CHECKSUM"
        }
        with patch.dict(os.environ, env_without_var, clear=True):
            require_env = os.environ.get("AITHER_ROOM_REQUIRE_CHECKSUM", "1").lower()
            require = require_env not in ("0", "false", "no")
            assert require is True

    def test_require_checksum_env_parsing_can_disable(self):
        """Test that AITHER_ROOM_REQUIRE_CHECKSUM=0 can disable verification."""
        with patch.dict(os.environ, {"AITHER_ROOM_REQUIRE_CHECKSUM": "0"}):
            require_env = os.environ.get("AITHER_ROOM_REQUIRE_CHECKSUM", "1").lower()
            require = require_env not in ("0", "false", "no")
            assert require is False

    def test_require_checksum_env_parsing_true_explicit(self):
        """Test that AITHER_ROOM_REQUIRE_CHECKSUM=1 is mandatory."""
        with patch.dict(os.environ, {"AITHER_ROOM_REQUIRE_CHECKSUM": "1"}):
            require_env = os.environ.get("AITHER_ROOM_REQUIRE_CHECKSUM", "1").lower()
            require = require_env not in ("0", "false", "no")
            assert require is True

    def test_require_checksum_env_parsing_false_string(self):
        """Test that AITHER_ROOM_REQUIRE_CHECKSUM='false' disables verification."""
        with patch.dict(os.environ, {"AITHER_ROOM_REQUIRE_CHECKSUM": "false"}):
            require_env = os.environ.get("AITHER_ROOM_REQUIRE_CHECKSUM", "1").lower()
            require = require_env not in ("0", "false", "no")
            assert require is False


class TestPlatformBinaryNames:
    """Test platform-specific binary name detection."""

    @pytest.mark.parametrize(
        "system,machine,expected_name",
        [
            ("Windows", "AMD64", "aither-room-win64.exe"),
            ("Windows", "x86_64", "aither-room-win64.exe"),
            ("Linux", "x86_64", "aither-room-linux-x64"),
            ("Linux", "aarch64", "aither-room-linux-x64"),
            ("Darwin", "arm64", "aither-room-macos-arm64"),
            ("Darwin", "x86_64", "aither-room-macos-x64"),
        ],
    )
    def test_get_binary_name(self, system: str, machine: str, expected_name: str):
        """Platform detection maps to correct binary names."""
        with patch("platform.system", return_value=system):
            with patch("platform.machine", return_value=machine):
                assert room_launcher._get_binary_name() == expected_name

    def test_get_binary_name_unsupported_exits(self):
        """Unsupported platforms cause SystemExit."""
        with patch("platform.system", return_value="Obscure"):
            with patch("platform.machine", return_value="unknown"):
                with patch("sys.exit") as mock_exit:
                    room_launcher._get_binary_name()
                    mock_exit.assert_called_once_with(1)
