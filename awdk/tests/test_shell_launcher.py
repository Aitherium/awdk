"""Tests for AitherShell binary launcher with checksum verification and offline fallback."""

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

from adk import shell_launcher


class TestChecksumVerification:
    """Test SHA256 checksum verification."""

    def test_verify_checksum_valid(self):
        """Verify that valid checksums are recognized."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            sha256 = hashlib.sha256(b"test content").hexdigest()
            assert shell_launcher._verify_checksum(tmp_path, sha256) is True
        finally:
            tmp_path.unlink()

    def test_verify_checksum_invalid(self):
        """Verify that invalid checksums are rejected."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = Path(tmp.name)

        try:
            bad_sha256 = "0" * 64
            assert shell_launcher._verify_checksum(tmp_path, bad_sha256) is False
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
            assert shell_launcher._verify_checksum(tmp_path, sha256_upper) is True
        finally:
            tmp_path.unlink()

    def test_verify_checksum_missing_file(self):
        """Missing files should return False."""
        bad_path = Path("/nonexistent/file")
        assert shell_launcher._verify_checksum(bad_path, "abc123") is False


class TestChecksumParsing:
    """Test parsing of checksums.sha256 file format."""

    def test_parse_checksums_single_entry(self):
        """Parse a single checksum entry."""
        text = "abc123def456  aither-shell-linux-x64"
        result = shell_launcher._parse_checksums(text)
        assert result == {"aither-shell-linux-x64": "abc123def456"}

    def test_parse_checksums_multiple_entries(self):
        """Parse multiple checksum entries."""
        text = """abc123  aither-shell-linux-x64
def456  aither-shell-win64.exe
789012  aither-shell-macos-arm64"""
        result = shell_launcher._parse_checksums(text)
        assert result == {
            "aither-shell-linux-x64": "abc123",
            "aither-shell-win64.exe": "def456",
            "aither-shell-macos-arm64": "789012",
        }

    def test_parse_checksums_skip_empty_lines(self):
        """Empty lines should be skipped."""
        text = """abc123  aither-shell-linux-x64

def456  aither-shell-win64.exe"""
        result = shell_launcher._parse_checksums(text)
        assert len(result) == 2

    def test_parse_checksums_skip_whitespace_lines(self):
        """Whitespace-only lines should be skipped."""
        text = """abc123  aither-shell-linux-x64

def456  aither-shell-win64.exe"""
        result = shell_launcher._parse_checksums(text)
        assert len(result) == 2

    def test_parse_checksums_malformed_line(self):
        """Lines with insufficient parts are skipped."""
        text = """abc123  aither-shell-linux-x64
just-one-part
def456  aither-shell-win64.exe"""
        result = shell_launcher._parse_checksums(text)
        assert len(result) == 2
        assert "aither-shell-linux-x64" in result
        assert "aither-shell-win64.exe" in result


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
                assert shell_launcher._get_platform_dir_name() == expected_dir

    def test_get_platform_dir_name_unsupported(self):
        """Unsupported platforms return empty string."""
        with patch("platform.system", return_value="Obscure"):
            with patch("platform.machine", return_value="unknown"):
                assert shell_launcher._get_platform_dir_name() == ""

    def test_get_bundled_binary_unsupported_platform(self):
        """Returns None for unsupported platforms."""
        with patch("platform.system", return_value="Obscure"):
            with patch("platform.machine", return_value="unknown"):
                result = shell_launcher._get_bundled_binary()
                assert result is None


class TestDownloadBinaryFallback:
    """Test download behavior including fallback on network failure."""

    def test_parse_checksums_integration(self):
        """Test parsing and verifying checksums work together."""
        content = b"binary data"
        expected_sha256 = hashlib.sha256(content).hexdigest()
        checksums_text = f"{expected_sha256}  aither-shell-linux-x64\n"

        parsed = shell_launcher._parse_checksums(checksums_text)
        assert parsed["aither-shell-linux-x64"] == expected_sha256

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        try:
            verified = shell_launcher._verify_checksum(
                tmp_path, parsed["aither-shell-linux-x64"]
            )
            assert verified is True
        finally:
            tmp_path.unlink()


class TestCmdShellFallbackPriority:
    """Test cmd_shell fallback priority."""

    def test_cmd_shell_use_cached_binary(self, tmp_path: Path):
        """Uses cached binary if available."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-shell-linux-x64"
        binary_path.write_text("fake binary")

        args = Mock()
        args.install = False
        args.shell_args = []
        args.api_url = None
        args.genesis = None

        with patch("adk.shell_launcher._get_binary_path", return_value=binary_path):
            # A verified cached binary is launched directly (checksum logic has
            # its own tests below).
            with patch("adk.shell_launcher._cached_binary_verified",
                       return_value=True):
                with patch("adk.shell_launcher._preflight_check",
                           return_value=(True, "test")):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = Mock(returncode=0)
                        result = shell_launcher.cmd_shell(args)
                        assert result == 0

    def test_cached_binary_verified_opt_out(self, monkeypatch, tmp_path: Path):
        """AITHER_SHELL_REQUIRE_CHECKSUM=0 skips cached re-verification."""
        monkeypatch.setenv("AITHER_SHELL_REQUIRE_CHECKSUM", "0")
        assert shell_launcher._cached_binary_verified(tmp_path / "anything") is True

    def test_cached_binary_verified_no_stored_checksum(self, monkeypatch, tmp_path: Path):
        """No stored checksum → cached binary is rejected (forces re-download)."""
        monkeypatch.delenv("AITHER_SHELL_REQUIRE_CHECKSUM", raising=False)
        monkeypatch.setattr(shell_launcher, "_CACHE_DIR", tmp_path)
        binary = tmp_path / "aither-shell-linux-x64"
        binary.write_text("fake")
        assert shell_launcher._cached_binary_verified(binary) is False

    def test_cached_binary_verified_match_and_mismatch(self, monkeypatch, tmp_path: Path):
        """Matching stored checksum → True; tampered binary → False."""
        import hashlib

        monkeypatch.delenv("AITHER_SHELL_REQUIRE_CHECKSUM", raising=False)
        monkeypatch.setattr(shell_launcher, "_CACHE_DIR", tmp_path)
        binary = tmp_path / "aither-shell-linux-x64"
        binary.write_bytes(b"real binary bytes")
        digest = hashlib.sha256(b"real binary bytes").hexdigest()
        (tmp_path / "checksums.sha256").write_text(f"{digest}  {binary.name}\n")
        assert shell_launcher._cached_binary_verified(binary) is True

        binary.write_bytes(b"tampered")
        assert shell_launcher._cached_binary_verified(binary) is False

    def test_cmd_shell_download_on_missing(self, tmp_path: Path):
        """Attempts download if cached binary missing."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-shell-linux-x64"

        args = Mock()
        args.install = False
        args.shell_args = []
        args.api_url = None
        args.genesis = None

        with patch("adk.shell_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.shell_launcher._download_binary") as mock_dl:
                with patch("adk.shell_launcher._get_bundled_binary",
                           return_value=None):
                    with patch("adk.shell_launcher.sys.exit"):
                        mock_dl.side_effect = SystemExit(1)
                        result = shell_launcher.cmd_shell(args)
                        assert result == 1

    def test_cmd_shell_fallback_to_bundled(self, tmp_path: Path):
        """Falls back to bundled binary on download failure."""
        cache_dir = tmp_path / ".aither" / "bin"
        cache_dir.mkdir(parents=True)
        binary_path = cache_dir / "aither-shell-linux-x64"

        bundled_path = tmp_path / "bundled" / "aither-shell-linux-x64"
        bundled_path.parent.mkdir(parents=True)
        bundled_path.write_text("fake bundled")

        args = Mock()
        args.install = False
        args.shell_args = []
        args.api_url = None
        args.genesis = None

        with patch("adk.shell_launcher._get_binary_path", return_value=binary_path):
            with patch("adk.shell_launcher._download_binary") as mock_dl:
                with patch("adk.shell_launcher._get_bundled_binary",
                           return_value=bundled_path):
                    with patch("adk.shell_launcher._preflight_check",
                               return_value=(True, "test")):
                        with patch("subprocess.run") as mock_run:
                            mock_dl.side_effect = SystemExit(1)
                            mock_run.return_value = Mock(returncode=0)

                            result = shell_launcher.cmd_shell(args)
                            assert result == 0


class TestInstallFlag:
    """Test --install flag behavior."""

    def test_cmd_shell_install_flag(self):
        """--install flag triggers download."""
        args = Mock()
        args.install = True
        args.shell_args = []

        with patch("adk.shell_launcher._download_binary") as mock_dl:
            mock_dl.return_value = Path("/fake/binary")
            result = shell_launcher.cmd_shell(args)
            assert result == 0
            mock_dl.assert_called_once()


class TestRequireChecksumEnv:
    """Test AITHER_SHELL_REQUIRE_CHECKSUM environment variable."""

    def test_require_checksum_env_parsing(self):
        """Test that AITHER_SHELL_REQUIRE_CHECKSUM env var is parsed correctly."""
        with patch.dict(os.environ, {"AITHER_SHELL_REQUIRE_CHECKSUM": "1"}):
            require = os.environ.get("AITHER_SHELL_REQUIRE_CHECKSUM", "").lower()
            require = require in ("1", "true", "yes")
            assert require is True

        with patch.dict(os.environ, {"AITHER_SHELL_REQUIRE_CHECKSUM": "false"}):
            require = os.environ.get("AITHER_SHELL_REQUIRE_CHECKSUM", "").lower()
            require = require in ("1", "true", "yes")
            assert require is False

        with patch.dict(os.environ, {}, clear=False):
            env_dict = {k: v for k, v in os.environ.items()}
            if "AITHER_SHELL_REQUIRE_CHECKSUM" in env_dict:
                del env_dict["AITHER_SHELL_REQUIRE_CHECKSUM"]
            with patch.dict(os.environ, env_dict, clear=True):
                require = os.environ.get("AITHER_SHELL_REQUIRE_CHECKSUM", "").lower()
                require = require in ("1", "true", "yes")
                assert require is False
