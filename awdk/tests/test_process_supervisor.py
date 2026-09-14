"""Tests for adk.process_supervisor — native process management."""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from adk.process_supervisor import (
    ProcessSpec,
    ProcessSupervisor,
    resolve_room_launch_target,
)


class TestProcessSupervisor:
    """Test suite for ProcessSupervisor."""

    def test_port_available_check(self):
        """Test port availability check."""
        sup = ProcessSupervisor()

        # Port 0 is always available
        available, err = sup.check_port_available(0)
        assert available or err  # Either available or error message

        # Port 1 is usually in use on Unix
        available, err = sup.check_port_available(1)
        # Don't assert result — platform-dependent

    def test_setup_logging(self, tmp_path):
        """Test per-process log file creation."""
        sup = ProcessSupervisor()

        with patch("adk.process_supervisor._LOG_DIR", tmp_path):
            handler = sup._setup_logging("test-process")
            assert handler is not None
            # Log file may not exist until first write
            assert handler.baseFilename.endswith("test-process.log")

    def test_start_process_success(self):
        """Test successful process startup."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="test-echo",
            argv=["python", "-c", "import time; time.sleep(60)"],
            port=19999,
            health_url="/health",
        )

        with patch.object(
            sup, "check_port_available", return_value=(True, "")
        ):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.pid = 12345
                mock_popen.return_value = mock_proc

                success, msg = sup.start(spec)

        assert success
        assert "12345" in msg
        assert spec.name in sup.processes

    def test_start_process_port_conflict(self):
        """Test startup fails when port is in use."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="conflict",
            argv=["dummy"],
            port=8080,
            health_url="/health",
        )

        with patch.object(
            sup,
            "check_port_available",
            return_value=(False, "Port 8080 already in use"),
        ):
            success, msg = sup.start(spec)

        assert not success
        assert "Port 8080" in msg
        assert "conflict" not in sup.processes

    def test_is_healthy_process_running(self):
        """Test health check for running process."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="healthy",
            argv=["python", "-c", "pass"],
            port=8350,
            health_url="http://127.0.0.1:8350/health",
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        sup.processes["healthy"] = mock_proc

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_response

            result = sup.is_healthy(spec)

        assert result

    def test_is_healthy_process_exited(self):
        """Test health check for exited process."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="dead",
            argv=["dummy"],
            port=8350,
            health_url="/health",
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited
        sup.processes["dead"] = mock_proc

        result = sup.is_healthy(spec)
        assert not result

    def test_restart_with_backoff(self):
        """Test restart with exponential backoff."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="restart-test",
            argv=["python", "-c", "pass"],
            port=9999,
            health_url="/health",
        )

        # Mock the process
        mock_proc = MagicMock()
        sup.processes[spec.name] = mock_proc
        sup.restart_counts[spec.name] = 0
        sup.backoff_delays[spec.name] = 1.0

        with patch.object(sup, "start", return_value=(True, "Started")):
            with patch("time.sleep") as mock_sleep:
                sup.restart(spec, "test reason")

        # Verify exponential backoff
        mock_sleep.assert_called()
        assert sup.restart_counts[spec.name] == 1
        assert sup.backoff_delays[spec.name] == 2.0

    def test_restart_backoff_cap(self):
        """Test restart backoff caps at 60 seconds."""
        sup = ProcessSupervisor()
        spec = ProcessSpec(
            name="test",
            argv=["dummy"],
            port=9999,
            health_url="/health",
        )

        mock_proc = MagicMock()
        sup.processes[spec.name] = mock_proc
        sup.backoff_delays[spec.name] = 60.0

        with patch.object(sup, "start", return_value=(True, "")):
            with patch("time.sleep"):
                sup.restart(spec)

        assert sup.backoff_delays[spec.name] == 60.0  # Capped

    def test_shutdown_gracefully(self):
        """Test graceful shutdown of all processes."""
        sup = ProcessSupervisor()

        mock_proc1 = MagicMock()
        mock_proc1.poll.return_value = None  # Still running
        mock_proc2 = MagicMock()
        mock_proc2.poll.return_value = None  # Still running

        sup.processes = {
            "proc1": mock_proc1,
            "proc2": mock_proc2,
        }

        with patch("time.sleep"):
            sup.shutdown_gracefully(timeout=1)

        # Both should have terminate called
        assert mock_proc1.terminate.called or mock_proc1.kill.called
        assert mock_proc2.terminate.called or mock_proc2.kill.called

    def test_shutdown_force_kill_on_timeout(self):
        """Test force kill if graceful shutdown times out."""
        sup = ProcessSupervisor()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running after terminate
        sup.processes = {"stubborn": mock_proc}

        with patch("time.sleep"):
            sup.shutdown_gracefully(timeout=0)

        # Should eventually call kill
        assert mock_proc.kill.called


class TestResolveRoomLaunchTarget:
    """Test Room service launch target resolution."""

    def test_resolve_from_env(self):
        """Test resolution from env var."""
        with patch.dict("os.environ", {"AITHER_ROOM_LAUNCH_TARGET": "custom.app:app"}):
            result = resolve_room_launch_target()
        assert result == "custom.app:app"

    def test_resolve_from_import(self):
        """Test resolution when aither_room is installed."""
        with patch.dict("os.environ", {}, clear=False):
            with patch("builtins.__import__", return_value=MagicMock()):
                # Mock successful import
                result = resolve_room_launch_target()
                # Should return the standard path if import succeeds
                assert result is None or "aither_room" in (result or "")

    def test_resolve_not_available(self):
        """Test resolution returns None when no env/package/repo target exists and
        the consumer-SKU room binary is also unavailable (no cache/download/bundle)."""
        with patch.dict(
            "os.environ",
            {"AITHER_ROOM_LAUNCH_TARGET": ""},
            clear=False,
        ):
            # No installed package / repo checkout.
            with patch.object(Path, "exists", return_value=False):
                # No room binary available (offline, nothing bundled/cached).
                with patch("adk.room_launcher.get_room_binary", return_value=None):
                    result = resolve_room_launch_target()
        assert result is None
