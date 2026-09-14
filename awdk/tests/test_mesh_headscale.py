"""Unit tests for adk.mesh headscale transport implementation.

Tests the headscale command construction and tunnel bring-up logic without
actually executing the tailscale binary.
"""

import pytest
from unittest.mock import patch, MagicMock

from adk.mesh import _tailscale_up, _tailscale


class TestHeadscaleTransport:
    """Unit tests for Headscale (Tailscale) transport in mesh.py."""

    def test_tailscale_binary_found_unix(self):
        """Verify _tailscale() finds tailscale binary on Unix systems."""
        with patch("shutil.which") as mock_which:
            # First call (tailscale) succeeds
            mock_which.return_value = "/usr/bin/tailscale"
            result = _tailscale()
            assert result == "/usr/bin/tailscale"
            mock_which.assert_called_once_with("tailscale")

    def test_tailscale_binary_found_windows(self):
        """Verify _tailscale() finds tailscale.exe on Windows."""
        with patch("shutil.which") as mock_which:
            # Unix binary not found, Windows binary found
            mock_which.side_effect = [None, r"C:\Program Files\Tailscale\tailscale.exe"]
            result = _tailscale()
            assert result == r"C:\Program Files\Tailscale\tailscale.exe"
            assert mock_which.call_count == 2

    def test_tailscale_binary_not_found(self):
        """Verify _tailscale() returns None when tailscale is not installed."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            result = _tailscale()
            assert result is None

    def test_tailscale_up_command_construction(self):
        """Verify _tailscale_up() constructs the correct tailscale command."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Connected.\n",
                stderr=""
            )

            headscale_url = "https://headscale.aitherium.com"
            auth_key = "test-auth-key-abc123"
            hostname = "aither-gpu-worker-1"

            _tailscale_up(headscale_url, auth_key, hostname)

            # Verify subprocess.run was called with correct command
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            cmd = call_args[0][0]

            # Check command structure
            assert cmd[0] == "/usr/bin/tailscale"
            assert cmd[1] == "up"
            assert "--login-server" in cmd
            assert "--authkey" in cmd
            assert "--hostname" in cmd
            assert headscale_url in cmd
            assert auth_key in cmd
            assert hostname in cmd

    def test_tailscale_up_flags_order(self):
        """Verify _tailscale_up() passes flags in correct format."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr=""
            )

            _tailscale_up("https://hs.example.com", "key123", "hostname")

            cmd = mock_run.call_args[0][0]
            # Verify the exact sequence of flags and values
            assert "--login-server" in cmd
            login_idx = cmd.index("--login-server")
            assert cmd[login_idx + 1] == "https://hs.example.com"

            assert "--authkey" in cmd
            key_idx = cmd.index("--authkey")
            assert cmd[key_idx + 1] == "key123"

            assert "--hostname" in cmd
            host_idx = cmd.index("--hostname")
            assert cmd[host_idx + 1] == "hostname"

    def test_tailscale_up_not_found_raises_error(self):
        """Verify _tailscale_up() raises RuntimeError when tailscale is missing."""
        with patch("adk.mesh._tailscale") as mock_find_ts:
            mock_find_ts.return_value = None

            with pytest.raises(RuntimeError) as exc_info:
                _tailscale_up("https://hs.example.com", "key123", "hostname")
            assert "Tailscale" in str(exc_info.value)

    def test_tailscale_up_command_failure(self):
        """Verify _tailscale_up() logs warnings on command failure."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Error: authentication failed"
            )

            result = _tailscale_up("https://hs.example.com", "invalid-key", "hostname")

            # Should return output even on failure for diagnostics
            assert "authentication failed" in result

    def test_tailscale_up_timeout(self):
        """Verify _tailscale_up() raises RuntimeError on timeout."""
        import subprocess
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.side_effect = subprocess.TimeoutExpired("tailscale up", 30)

            with pytest.raises(RuntimeError) as exc_info:
                _tailscale_up("https://hs.example.com", "key123", "hostname")
            assert "timed out" in str(exc_info.value)

    def test_tailscale_up_subprocess_error(self):
        """Verify _tailscale_up() raises RuntimeError on subprocess error."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.side_effect = OSError("Cannot execute tailscale")

            with pytest.raises(RuntimeError) as exc_info:
                _tailscale_up("https://hs.example.com", "key123", "hostname")
            assert "failed" in str(exc_info.value)

    def test_tailscale_up_returns_combined_output(self):
        """Verify _tailscale_up() returns combined stdout + stderr."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Setting up interface...\n",
                stderr="Some diagnostic info\n"
            )

            result = _tailscale_up("https://hs.example.com", "key123", "hostname")

            # Result should combine both stdout and stderr
            assert "Setting up interface" in result
            assert "Some diagnostic info" in result

    def test_tailscale_up_strips_output(self):
        """Verify _tailscale_up() strips whitespace from output."""
        with patch("adk.mesh._tailscale") as mock_find_ts, \
             patch("subprocess.run") as mock_run:
            mock_find_ts.return_value = "/usr/bin/tailscale"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="  Output with spaces  \n",
                stderr="  More output  \n"
            )

            result = _tailscale_up("https://hs.example.com", "key123", "hostname")

            # Should be stripped of leading/trailing whitespace
            assert not result.startswith(" ")
            assert not result.endswith(" ")
