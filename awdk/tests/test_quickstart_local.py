"""Tests for quickstart-local command — local inference backend setup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.cli import cmd_quickstart_local
from adk.local_backends import pick_backend, docker_available


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------


@dataclass
class MockAccelInfo:
    """Mock AccelInfo for testing pick_backend."""

    kind: str = "cpu"
    name: str = "Test GPU"
    vram_gb: float = 0.0
    ram_gb: float = 8.0
    cuda_version: str = ""
    os_family: str = "linux"
    arch: str = "x64"
    notes: list = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for CLI commands."""
    defaults = {
        "backend": "auto",
        "model": None,
        "port": 8200,
        "dry_run": False,
        "api_key": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Tests for pick_backend
# ---------------------------------------------------------------------------


class TestPickBackend:
    """Test the backend picker logic."""

    def test_prefer_explicit_llamacpp(self):
        """Explicit prefer=llamacpp always wins."""
        accel = MockAccelInfo(kind="cuda", vram_gb=32.0)
        result = pick_backend(accel, prefer="llamacpp")
        assert result == "llamacpp"

    def test_prefer_explicit_ollama(self):
        """Explicit prefer=ollama always wins."""
        accel = MockAccelInfo(kind="cpu")
        result = pick_backend(accel, prefer="ollama")
        assert result == "ollama"

    def test_prefer_explicit_vllm(self):
        """Explicit prefer=vllm always wins."""
        accel = MockAccelInfo(kind="cpu")
        result = pick_backend(accel, prefer="vllm")
        assert result == "vllm"

    def test_cuda_16gb_docker_chooses_vllm(self):
        """CUDA with 16+GB VRAM + Docker available (and no Ollama) → vllm."""
        accel = MockAccelInfo(kind="cuda", vram_gb=16.0)
        with patch("adk.local_backends.docker_available", return_value=True):
            with patch("adk.ollama_setup.is_installed", return_value=False):
                result = pick_backend(accel, prefer="auto")
        assert result == "vllm"

    def test_cuda_16gb_no_docker_chooses_ollama_or_llamacpp(self):
        """CUDA with 16+GB but no Docker → falls through to ollama/llamacpp."""
        accel = MockAccelInfo(kind="cuda", vram_gb=16.0)
        with patch("adk.local_backends.docker_available", return_value=False):
            with patch("adk.ollama_setup.is_installed", return_value=False):
                result = pick_backend(accel, prefer="auto")
        assert result == "llamacpp"

    def test_cuda_small_vram_skips_vllm(self):
        """CUDA with <16GB VRAM → skips vllm even if Docker available."""
        accel = MockAccelInfo(kind="cuda", vram_gb=8.0)
        with patch("adk.local_backends.docker_available", return_value=True):
            with patch("adk.ollama_setup.is_installed", return_value=False):
                result = pick_backend(accel, prefer="auto")
        assert result == "llamacpp"

    def test_ollama_installed_fallback(self):
        """Ollama installed → ollama (when not CUDA+Docker)."""
        accel = MockAccelInfo(kind="cpu")
        with patch("adk.local_backends.docker_available", return_value=False):
            with patch("adk.ollama_setup.is_installed", return_value=True):
                result = pick_backend(accel, prefer="auto")
        assert result == "ollama"

    def test_cpu_no_ollama_llamacpp_fallback(self):
        """CPU with no Ollama → llamacpp (always-works fallback)."""
        accel = MockAccelInfo(kind="cpu")
        with patch("adk.local_backends.docker_available", return_value=False):
            with patch("adk.ollama_setup.is_installed", return_value=False):
                result = pick_backend(accel, prefer="auto")
        assert result == "llamacpp"

    def test_docker_available_override(self):
        """docker_available_override parameter works."""
        accel = MockAccelInfo(kind="cuda", vram_gb=20.0)
        # Explicitly set docker_available_override=True (and no Ollama present)
        with patch("adk.ollama_setup.is_installed", return_value=False):
            result = pick_backend(accel, prefer="auto", docker_available_override=True)
        assert result == "vllm"

        # Explicitly set docker_available_override=False
        with patch("adk.ollama_setup.is_installed", return_value=False):
            result = pick_backend(
                accel, prefer="auto", docker_available_override=False
            )
        assert result == "llamacpp"


# ---------------------------------------------------------------------------
# Tests for cmd_quickstart_local (with dry-run)
# ---------------------------------------------------------------------------


class TestCmdQuickstartLocal:
    """Test the quickstart-local command (with mocks)."""

    def test_dry_run_llamacpp(self, capsys, tmp_path):
        """Test dry-run flow for llamacpp backend."""
        args = _make_args(
            backend="llamacpp",
            dry_run=True,
            port=8200,
        )

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu", ram_gb=16.0)

            with patch("adk.llamacpp_setup.install") as mock_install:
                result = MagicMock()
                result.success = True
                result.port = 8200
                result.quant = "Q4_K_M"
                result.accel = mock_detect.return_value
                mock_install.return_value = result

                with patch("adk.llamacpp_setup.smoke_test", return_value=True):
                    with patch(
                        "adk.cli.save_saved_config"
                    ) as mock_save:
                        exit_code = cmd_quickstart_local(args)

        assert exit_code == 0
        mock_install.assert_called_once()
        mock_save.assert_called_once()
        saved_config = mock_save.call_args[0][0]
        assert saved_config["setup_backend"] == "llamacpp"
        assert "inference_url" in saved_config

    def test_dry_run_ollama(self, tmp_path):
        """Test dry-run flow for ollama backend."""
        args = _make_args(
            backend="ollama",
            model="qwen2.5:3b",
            dry_run=True,
        )

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")

            with patch("adk.ollama_setup.ensure_running", return_value=True):
                with patch("adk.ollama_setup.pull") as mock_pull:
                    mock_pull.return_value = True

                    with patch(
                        "adk.ollama_setup.register_config"
                    ) as mock_ollama_save:
                        with patch(
                            "adk.ollama_setup.smoke_test", return_value=True
                        ):
                            with patch(
                                "adk.cli.save_saved_config"
                            ) as mock_save:
                                exit_code = cmd_quickstart_local(args)

        assert exit_code == 0
        mock_save.assert_called_once()

    def test_ollama_default_model_when_flag_omitted(self):
        """--model omitted (None) must resolve to DEFAULT_OLLAMA_MODEL, not None."""
        from adk.ollama_setup import DEFAULT_OLLAMA_MODEL

        args = _make_args(backend="ollama", model=None, dry_run=True)

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")
            with patch("adk.ollama_setup.ensure_running", return_value=True):
                with patch("adk.ollama_setup.register_config") as mock_reg:
                    with patch("adk.cli.save_saved_config"):
                        exit_code = cmd_quickstart_local(args)

        assert exit_code == 0
        mock_reg.assert_called_once()
        assert mock_reg.call_args[0][0] == DEFAULT_OLLAMA_MODEL

    def test_backend_auto_detection(self, tmp_path):
        """Test auto backend detection."""
        args = _make_args(
            backend="auto",
            dry_run=True,
        )

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")

            with patch("adk.local_backends.pick_backend") as mock_pick:
                mock_pick.return_value = "llamacpp"

                with patch("adk.llamacpp_setup.install") as mock_install:
                    result = MagicMock()
                    result.success = True
                    result.port = 8200
                    result.quant = "Q3_K_M"
                    result.accel = mock_detect.return_value
                    mock_install.return_value = result

                    with patch("adk.llamacpp_setup.smoke_test", return_value=True):
                        with patch(
                            "adk.cli.save_saved_config"
                        ):
                            exit_code = cmd_quickstart_local(args)

        assert exit_code == 0
        mock_pick.assert_called_once()

    def test_ollama_pull_failure(self):
        """Test error handling when ollama pull fails."""
        args = _make_args(
            backend="ollama",
            model="qwen2.5:3b",
            dry_run=False,
        )

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")

            with patch("adk.ollama_setup.ensure_running", return_value=True):
                with patch("adk.ollama_setup.pull", return_value=False):
                    exit_code = cmd_quickstart_local(args)

        assert exit_code == 1

    def test_llamacpp_install_failure(self):
        """Test error handling when llamacpp install fails."""
        args = _make_args(
            backend="llamacpp",
            dry_run=False,
        )

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")

            with patch("adk.llamacpp_setup.install") as mock_install:
                result = MagicMock()
                result.success = False
                result.error = "Download failed"
                mock_install.return_value = result

                exit_code = cmd_quickstart_local(args)

        assert exit_code == 1

    def test_llamacpp_smoke_failure_returns_1(self):
        """A failed smoke test must FAIL the command, not falsely report success."""
        args = _make_args(backend="llamacpp", dry_run=False, port=8200)

        with patch("adk.llamacpp_setup.detect_accel") as mock_detect:
            mock_detect.return_value = MockAccelInfo(kind="cpu")

            with patch("adk.llamacpp_setup.install") as mock_install:
                result = MagicMock()
                result.success = True
                result.port = 8200
                result.quant = "Q4_K_M"
                mock_install.return_value = result

                with patch("adk.llamacpp_setup.status") as mock_status:
                    mock_status.return_value = MagicMock(running=True)
                    with patch("adk.llamacpp_setup.smoke_test", return_value=False):
                        with patch("adk.cli.save_saved_config"):
                            exit_code = cmd_quickstart_local(args)

        assert exit_code == 1


# ---------------------------------------------------------------------------
# Tests for docker_available helper
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    """Test docker availability detection."""

    def test_docker_available_true(self):
        """docker_available returns True when docker is present and running."""
        with patch("adk.local_backends.shutil.which", return_value="/usr/bin/docker"):
            with patch("adk.local_backends.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = docker_available()
        assert result is True

    def test_docker_not_found(self):
        """docker_available returns False when docker binary not found."""
        with patch("adk.local_backends.shutil.which", return_value=None):
            result = docker_available()
        assert result is False

    def test_docker_daemon_not_running(self):
        """docker_available returns False when daemon not running."""
        with patch("adk.local_backends.shutil.which", return_value="/usr/bin/docker"):
            with patch("adk.local_backends.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                result = docker_available()
        assert result is False

    def test_docker_info_timeout(self):
        """docker_available handles timeout gracefully."""
        with patch("adk.local_backends.shutil.which", return_value="/usr/bin/docker"):
            with patch("adk.local_backends.subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired("docker", 5)
                result = docker_available()
        assert result is False


# Standalone import for timeout test
import subprocess
