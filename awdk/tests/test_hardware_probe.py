"""Tests for adk.hardware_probe — system hardware detection and recommendations."""

from unittest.mock import MagicMock, patch

import pytest

from adk.hardware_probe import (
    SystemInfo,
    detect_system,
    recommend_setup,
    _detect_cpu_cores,
    _detect_gpu,
    _detect_ollama,
    _detect_ram,
)


class TestDetectRAM:
    """Test RAM detection."""

    def test_detect_ram_with_psutil(self):
        """Test RAM detection using psutil."""
        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value.total = 16 * (1024**3)

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            with patch("builtins.__import__", side_effect=lambda *a, **k: mock_psutil if "psutil" in str(a) else __import__(*a, **k)):
                result = _detect_ram()
                # Should return a number (platform-dependent)
                assert result >= 0

    def test_detect_ram_fallback(self):
        """Test RAM detection fallback."""
        # Should not crash even if all methods fail
        with patch("builtins.__import__", side_effect=ImportError):
            result = _detect_ram()
        # Fallback may return 0 or try other methods
        assert result >= 0


class TestDetectCPUCores:
    """Test CPU core detection."""

    def test_detect_cpu_cores(self):
        """Test CPU core detection."""
        # os.cpu_count must be patched FIRST: setting up patch("os.cpu_count")
        # itself imports its target, so with __import__ already broken the mock
        # setup raises (seen on the 3.10 runner; 3.12 mock resolves from
        # sys.modules and hid it). The __import__ patch (blocking psutil) goes
        # innermost.
        with patch("os.cpu_count", return_value=8):
            with patch("builtins.__import__", side_effect=ImportError):
                result = _detect_cpu_cores()
        assert result == 8

    def test_detect_cpu_cores_fallback(self):
        """Test CPU core fallback to 1."""
        with patch("os.cpu_count", return_value=None):
            with patch("builtins.__import__", side_effect=ImportError):
                result = _detect_cpu_cores()
        assert result >= 1


class TestDetectGPU:
    """Test GPU detection."""

    def test_detect_gpu_no_gpu(self):
        """Test when no GPU is detected."""
        with patch("adk.setup_cli.detect_gpu") as mock_gpu:
            mock_gpu.return_value.vendor = "none"
            mock_gpu.return_value.name = ""
            mock_gpu.return_value.vram_mb = 0

            vendor, name, vram = _detect_gpu()

        assert vendor == "none"
        assert vram == 0

    def test_detect_gpu_nvidia(self):
        """Test NVIDIA GPU detection."""
        with patch("adk.setup_cli.detect_gpu") as mock_gpu:
            mock_gpu.return_value.vendor = "nvidia"
            mock_gpu.return_value.name = "RTX 4090"
            mock_gpu.return_value.vram_mb = 24576

            vendor, name, vram = _detect_gpu()

        assert vendor == "nvidia"
        assert "RTX" in name
        assert vram == 24576


class TestDetectOllama:
    """Test Ollama detection."""

    def test_ollama_installed(self):
        """Test when Ollama is installed."""
        with patch("shutil.which", return_value="/usr/bin/ollama"):
            result = _detect_ollama()
        assert result is True

    def test_ollama_not_installed(self):
        """Test when Ollama is not installed."""
        with patch("shutil.which", return_value=None):
            result = _detect_ollama()
        assert result is False


class TestDetectSystem:
    """Test full system detection."""

    def test_detect_system_basic(self):
        """Test basic system detection."""
        with patch("adk.hardware_probe._detect_ram", return_value=16.0):
            with patch("adk.hardware_probe._detect_cpu_cores", return_value=8):
                with patch("adk.hardware_probe._detect_gpu", return_value=("none", "", 0)):
                    with patch("adk.hardware_probe._detect_ollama", return_value=False):
                        result = detect_system()

        assert result.ram_gb == 16.0
        assert result.cpu_cores == 8
        assert result.gpu_vendor == "none"
        assert result.ollama_installed is False


class TestRecommendSetup:
    """Test setup recommendations."""

    def test_recommend_nvidia_8gb(self):
        """Recommend local 8B model for NVIDIA GPU with 8GB VRAM."""
        system = SystemInfo(
            ram_gb=16.0,
            cpu_cores=8,
            gpu_vendor="nvidia",
            gpu_name="RTX 4060",
            gpu_vram_mb=8192,
        )
        rec = recommend_setup(system)

        assert rec.backend == "local-8b"
        assert "nemotron" in rec.local_model.lower()
        assert rec.local_vram_gb > 0
        assert "NVIDIA" in rec.rationale

    def test_recommend_nvidia_4gb(self):
        """Recommend local 3B model for NVIDIA GPU with 4GB VRAM."""
        system = SystemInfo(
            ram_gb=8.0,
            cpu_cores=4,
            gpu_vendor="nvidia",
            gpu_name="GTX 1050",
            gpu_vram_mb=4096,
        )
        rec = recommend_setup(system)

        assert rec.backend == "local-3b"
        assert "qwen" in rec.local_model.lower()
        assert len(rec.warnings) > 0

    def test_recommend_nvidia_tiny(self):
        """Cloud-only for NVIDIA GPU with <4GB VRAM."""
        system = SystemInfo(
            ram_gb=8.0,
            cpu_cores=4,
            gpu_vendor="nvidia",
            gpu_name="GTX 750",
            gpu_vram_mb=2048,
        )
        rec = recommend_setup(system)

        assert rec.backend == "cloud"
        assert rec.local_model is None
        assert len(rec.warnings) > 0

    def test_recommend_apple_16gb(self):
        """Recommend Ollama for Apple Silicon with 16GB RAM."""
        system = SystemInfo(
            ram_gb=16.0,
            cpu_cores=8,
            gpu_vendor="apple",
            gpu_name="Apple M3 Pro",
            gpu_vram_mb=0,
        )
        rec = recommend_setup(system)

        assert rec.backend == "ollama"
        assert "nemotron" in rec.local_model.lower()

    def test_recommend_apple_8gb(self):
        """Recommend small Ollama model for Apple Silicon with 8GB RAM."""
        system = SystemInfo(
            ram_gb=8.0,
            cpu_cores=4,
            gpu_vendor="apple",
            gpu_name="Apple M1",
            gpu_vram_mb=0,
        )
        rec = recommend_setup(system)

        assert rec.backend == "ollama"
        assert "qwen" in rec.local_model.lower()
        assert len(rec.warnings) > 0

    def test_recommend_cpu_only_16gb(self):
        """Recommend local 3B for CPU-only with 16GB RAM."""
        system = SystemInfo(
            ram_gb=16.0,
            cpu_cores=8,
            gpu_vendor="none",
        )
        rec = recommend_setup(system)

        assert rec.backend == "local-3b"
        assert "qwen" in rec.local_model.lower()
        assert len(rec.warnings) > 0

    def test_recommend_cpu_only_low_memory(self):
        """Cloud-only for CPU-only with <16GB RAM."""
        system = SystemInfo(
            ram_gb=4.0,
            cpu_cores=2,
            gpu_vendor="none",
        )
        rec = recommend_setup(system)

        assert rec.backend == "cloud"
        assert rec.local_model is None
        assert len(rec.warnings) > 0
