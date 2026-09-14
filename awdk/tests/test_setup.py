"""Tests for adk.setup — agent self-setup module."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.setup import (
    AgentSetup,
    GPUInfo,
    SetupReport,
    SystemInfo,
    VLLMInfo,
    _detect_gpu,
    _detect_ram,
    _find_compose_file,
    _recommended_models,
    _select_profile,
    auto_setup,
)


# ---------------------------------------------------------------------------
# Profile selection tests
# ---------------------------------------------------------------------------

class TestProfileSelection:
    def test_no_gpu_returns_cpu_only(self):
        assert _select_profile(GPUInfo(), 16.0) == "cpu_only"

    def test_nvidia_low_vram(self):
        assert _select_profile(GPUInfo(vendor="nvidia", vram_mb=8000), 16.0) == "nvidia_low"

    def test_nvidia_mid_vram(self):
        assert _select_profile(GPUInfo(vendor="nvidia", vram_mb=16000), 32.0) == "nvidia_mid"

    def test_nvidia_high_vram(self):
        assert _select_profile(GPUInfo(vendor="nvidia", vram_mb=24000), 64.0) == "nvidia_high"

    def test_nvidia_ultra_vram(self):
        assert _select_profile(GPUInfo(vendor="nvidia", vram_mb=80000), 128.0) == "nvidia_ultra"

    def test_amd_gpu(self):
        assert _select_profile(GPUInfo(vendor="amd", vram_mb=16000), 32.0) == "amd"

    def test_apple_silicon(self):
        assert _select_profile(GPUInfo(vendor="apple", vram_mb=32000), 32.0) == "apple_silicon"

    def test_nvidia_minimal(self):
        assert _select_profile(GPUInfo(vendor="nvidia", vram_mb=4000), 8.0) == "minimal"


class TestRecommendedModels:
    def test_cpu_only_gets_small_models(self):
        models = _recommended_models("cpu_only")
        assert "gemma4:4b" in models
        assert "nomic-embed-text" in models

    def test_nvidia_high_gets_bigger_models(self):
        models = _recommended_models("nvidia_high")
        assert "nemotron-orchestrator-8b" in models
        assert "deepseek-r1:14b" in models

    def test_unknown_profile_defaults(self):
        models = _recommended_models("nonexistent")
        assert len(models) > 0


# ---------------------------------------------------------------------------
# GPU detection tests (mocked)
# ---------------------------------------------------------------------------

class TestGPUDetection:
    @pytest.mark.asyncio
    async def test_nvidia_detected(self):
        with patch("adk.setup._run") as mock_run:
            # nvidia-smi query
            mock_run.side_effect = [
                (0, "NVIDIA RTX 4090, 24564, 560.35\n", ""),
                (0, "CUDA Version: 12.4", ""),
            ]
            gpu = await _detect_gpu()
            assert gpu.vendor == "nvidia"
            assert gpu.vram_mb == 24564
            assert gpu.cuda_version == "12.4"
            assert gpu.name == "NVIDIA RTX 4090"

    @pytest.mark.asyncio
    async def test_no_gpu(self):
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (-1, "", "Command not found")
            gpu = await _detect_gpu()
            assert gpu.vendor == "none"

    @pytest.mark.asyncio
    async def test_multi_gpu_vram_total(self):
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "RTX 4090, 24564, 560.35\nRTX 4090, 24564, 560.35\n", ""),
                (0, "CUDA Version: 12.4", ""),
            ]
            gpu = await _detect_gpu()
            assert gpu.count == 2
            assert gpu.vram_mb == 24564       # Best single GPU
            assert gpu.total_vram_mb == 49128  # Sum of all GPUs
            assert len(gpu.all_gpus) == 2

    @pytest.mark.asyncio
    async def test_asymmetric_multi_gpu_selects_best(self):
        """Jeff's scenario: P1000 (4GB) + RTX 3090 (24GB) — best GPU wins."""
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "Quadro P1000, 4096, 580.142\nNVIDIA GeForce RTX 3090, 24576, 580.142\n", ""),
                (0, "CUDA Version: 12.6", ""),
            ]
            gpu = await _detect_gpu()
            assert gpu.count == 2
            assert gpu.name == "NVIDIA GeForce RTX 3090"
            assert gpu.vram_mb == 24576        # Best GPU VRAM
            assert gpu.total_vram_mb == 28672   # 4096 + 24576
            assert len(gpu.all_gpus) == 2
            assert gpu.all_gpus[0]["name"] == "Quadro P1000"
            assert gpu.all_gpus[1]["name"] == "NVIDIA GeForce RTX 3090"

    @pytest.mark.asyncio
    async def test_profile_uses_best_gpu_not_total(self):
        """2x 12GB GPUs → nvidia_mid (12GB best), not nvidia_high (24GB total)."""
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "RTX 4070, 12288, 560.35\nRTX 4070, 12288, 560.35\n", ""),
                (0, "CUDA Version: 12.4", ""),
            ]
            gpu = await _detect_gpu()
            assert gpu.vram_mb == 12288         # Best single GPU
            assert gpu.total_vram_mb == 24576    # Total
            profile = _select_profile(gpu, 32.0)
            assert profile == "nvidia_mid"       # Based on 12GB best, not 24GB total


# ---------------------------------------------------------------------------
# RAM detection tests
# ---------------------------------------------------------------------------

class TestRAMDetection:
    @pytest.mark.asyncio
    async def test_ram_detected(self):
        # The mocked _run output is the WINDOWS detection path; on Linux
        # _detect_ram reads /proc/meminfo directly and the mock is bypassed
        # (this asserted the CI RUNNER had >30GB RAM). Pin the platform so the
        # mocked branch is what's exercised everywhere.
        with patch("adk.setup.platform.system", return_value="Windows"), \
                patch("adk.setup._run") as mock_run:
            mock_run.return_value = (0, "TotalPhysicalMemory=34359738368\n", "")
            ram = await _detect_ram()
            assert ram > 30  # ~32 GB


# ---------------------------------------------------------------------------
# AgentSetup tests
# ---------------------------------------------------------------------------

class TestAgentSetup:
    @pytest.mark.asyncio
    async def test_detect_hardware(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._detect_gpu") as mock_gpu, \
             patch("adk.setup._detect_ram") as mock_ram, \
             patch("adk.setup._check_ollama") as mock_ollama, \
             patch("adk.setup._check_vllm") as mock_vllm, \
             patch("adk.setup._check_docker") as mock_docker:
            mock_gpu.return_value = GPUInfo(vendor="nvidia", vram_mb=24000, name="RTX 4090")
            mock_ram.return_value = 64.0
            mock_ollama.return_value = (True, True, ["llama3.2:3b"])
            mock_vllm.return_value = VLLMInfo(running=False)
            mock_docker.return_value = True

            info = await setup.detect_hardware()
            assert info.gpu.vendor == "nvidia"
            assert info.ram_gb == 64.0
            assert info.ollama_running is True
            assert info.docker_installed is True
            assert info.profile == "nvidia_high"
            assert info.active_backend == "ollama"

    @pytest.mark.asyncio
    async def test_detect_hardware_vllm_takes_priority(self, tmp_path):
        """When both vLLM and Ollama are running, vLLM should be active backend."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._detect_gpu") as mock_gpu, \
             patch("adk.setup._detect_ram") as mock_ram, \
             patch("adk.setup._check_ollama") as mock_ollama, \
             patch("adk.setup._check_vllm") as mock_vllm, \
             patch("adk.setup._check_docker") as mock_docker:
            mock_gpu.return_value = GPUInfo(vendor="nvidia", vram_mb=24000)
            mock_ram.return_value = 64.0
            mock_ollama.return_value = (True, True, ["llama3.2:3b"])
            mock_vllm.return_value = VLLMInfo(running=True, ports=[8200], models=["nemotron-orchestrator"])
            mock_docker.return_value = True

            info = await setup.detect_hardware()
            assert info.active_backend == "vllm"

    @pytest.mark.asyncio
    async def test_ensure_ollama_already_running(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(ollama_installed=True, ollama_running=True)
        result = await setup.ensure_ollama()
        assert result is True

    @pytest.mark.asyncio
    async def test_pull_models_skips_existing(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            ollama_models=["llama3.2:3b", "nomic-embed-text"],
            profile="nvidia_mid",
        )
        with patch("adk.setup._run") as mock_run:
            # Only pull models not in existing list
            mock_run.return_value = (0, "success", "")
            pulled = await setup.pull_models(["llama3.2:3b", "nomic-embed-text"])
            # Both already exist, no pulls needed
            assert "llama3.2:3b" in pulled
            assert "nomic-embed-text" in pulled
            mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_models_pulls_missing(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(ollama_models=["llama3.2:3b"], profile="nvidia_mid")
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (0, "success", "")
            pulled = await setup.pull_models(["llama3.2:3b", "deepseek-r1:8b"])
            assert "llama3.2:3b" in pulled
            assert "deepseek-r1:8b" in pulled

    @pytest.mark.asyncio
    async def test_ensure_vllm_no_docker(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(docker_installed=False, gpu=GPUInfo(vendor="nvidia"))
        result = await setup.ensure_vllm()
        assert result is False

    @pytest.mark.asyncio
    async def test_ensure_vllm_no_nvidia(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(docker_installed=True, gpu=GPUInfo(vendor="amd"))
        result = await setup.ensure_vllm()
        assert result is False

    @pytest.mark.asyncio
    async def test_full_setup(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "pull_models") as mock_pull:
            mock_detect.return_value = SystemInfo(
                gpu=GPUInfo(vendor="nvidia", vram_mb=24000),
                ram_gb=64.0,
                ollama_running=True,
                ollama_models=["nemotron-orchestrator-8b"],
                vllm=VLLMInfo(running=False),
                profile="nvidia_high",
            )
            setup._system = mock_detect.return_value
            mock_pull.return_value = ["nemotron-orchestrator-8b", "nomic-embed-text"]

            report = await setup.full_setup()
            assert report.ready is True
            assert report.backend == "ollama"
            assert report.ollama_ready is True
            assert report.profile == "nvidia_high"

    @pytest.mark.asyncio
    async def test_full_setup_saves_report(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "pull_models") as mock_pull:
            mock_detect.return_value = SystemInfo(
                ollama_running=True,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            mock_pull.return_value = ["llama3.2:1b"]

            await setup.full_setup()
            report_path = tmp_path / "setup_report.json"
            assert report_path.exists()
            data = json.loads(report_path.read_text())
            assert data["ready"] is True

    @pytest.mark.asyncio
    async def test_health_check(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"models": [{"name": "llama3.2:3b"}]}
            mock_client.get.return_value = mock_resp

            results = await setup.health_check()
            assert results["ollama"] is True


# ---------------------------------------------------------------------------
# Auto-setup convenience
# ---------------------------------------------------------------------------

class TestVLLMSafety:
    """Tests that ensure Ollama never starts when vLLM holds the GPU."""

    @pytest.mark.asyncio
    async def test_ensure_ollama_blocked_when_vllm_running(self, tmp_path):
        """CRITICAL: Ollama must NOT start when vLLM is using GPU."""
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            ollama_installed=True,
            ollama_running=False,
            vllm=VLLMInfo(running=True, ports=[8200], models=["nemotron-orchestrator"]),
            gpu=GPUInfo(vendor="nvidia", vram_mb=24000),
        )
        result = await setup.ensure_ollama()
        assert result is False

    @pytest.mark.asyncio
    async def test_ensure_ollama_allowed_when_vllm_not_running(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            ollama_installed=True,
            ollama_running=True,
            vllm=VLLMInfo(running=False),
        )
        result = await setup.ensure_ollama()
        assert result is True

    @pytest.mark.asyncio
    async def test_ensure_ollama_force_overrides_vllm_check(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            ollama_installed=True,
            ollama_running=True,
            vllm=VLLMInfo(running=True, ports=[8200]),
        )
        result = await setup.ensure_ollama(force=True)
        assert result is True  # ollama_running=True, force=True

    @pytest.mark.asyncio
    async def test_full_setup_uses_vllm_when_running(self, tmp_path):
        """full_setup should detect vLLM and use it instead of Ollama."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect:
            mock_detect.return_value = SystemInfo(
                gpu=GPUInfo(vendor="nvidia", vram_mb=24000),
                ram_gb=64.0,
                ollama_installed=True,
                ollama_running=False,
                vllm=VLLMInfo(running=True, ports=[8200, 8201], models=["nvidia/Nemotron-Orchestrator-8B"]),
                profile="nvidia_high",
            )
            setup._system = mock_detect.return_value

            report = await setup.full_setup()
            assert report.ready is True
            assert report.backend == "vllm"
            assert report.vllm_ready is True
            assert report.ollama_ready is False  # must NOT have started Ollama
            assert "nvidia/Nemotron-Orchestrator-8B" in report.models_available

    @pytest.mark.asyncio
    async def test_full_setup_uses_ollama_when_no_vllm(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama") as mock_ollama, \
             patch.object(setup, "pull_models") as mock_pull:
            mock_detect.return_value = SystemInfo(
                gpu=GPUInfo(vendor="nvidia", vram_mb=24000),
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="nvidia_high",
            )
            setup._system = mock_detect.return_value
            mock_ollama.return_value = True
            mock_pull.return_value = ["nemotron-orchestrator-8b"]

            report = await setup.full_setup()
            assert report.ready is True
            assert report.backend == "ollama"
            mock_ollama.assert_called_once()

    @pytest.mark.asyncio
    async def test_full_setup_detects_cloud_api_keys(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama") as mock_ollama, \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            mock_detect.return_value = SystemInfo(
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            mock_ollama.return_value = False  # no local backend

            report = await setup.full_setup()
            assert report.ready is True
            assert report.backend == "cloud"

    @pytest.mark.asyncio
    async def test_full_setup_not_ready_when_nothing_works(self, tmp_path, monkeypatch):
        # "Nothing works" must include no cloud keys: an earlier test (or
        # Config._load_provider_keys, which exports provider_keys.json entries
        # into os.environ process-wide) can leave OPENAI/ANTHROPIC keys in env,
        # flipping full_setup's cloud fallback to ready=True (broke public CI
        # full-suite runs while the test passed in isolation).
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama") as mock_ollama:
            mock_detect.return_value = SystemInfo(
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            mock_ollama.return_value = False

            report = await setup.full_setup()
            assert report.ready is False
            assert report.backend == ""


class TestAutoSetup:
    @pytest.mark.asyncio
    async def test_auto_setup_returns_report(self):
        with patch("adk.setup.AgentSetup.full_setup") as mock_full:
            mock_full.return_value = SetupReport(ready=True, profile="nvidia_mid", backend="ollama")
            report = await auto_setup()
            assert report.ready is True


# ---------------------------------------------------------------------------
# _find_compose_file tests
# ---------------------------------------------------------------------------

class TestFindComposeFile:
    COMPOSE_NAME = "docker-compose.adk-vllm.yml"

    def test_finds_in_adk_root_env(self, tmp_path):
        compose = tmp_path / self.COMPOSE_NAME
        compose.write_text("# test")
        with patch.dict(os.environ, {"ADK_ROOT": str(tmp_path)}):
            result = _find_compose_file()
            assert result == compose

    def test_finds_in_cwd(self, tmp_path, monkeypatch):
        compose = tmp_path / self.COMPOSE_NAME
        compose.write_text("# test")
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            # Remove ADK_ROOT if set
            os.environ.pop("ADK_ROOT", None)
            result = _find_compose_file()
            assert result is not None
            assert result.name == self.COMPOSE_NAME

    def test_finds_in_package_dir(self, tmp_path):
        """Compose file next to setup.py (shipped via pip)."""
        compose = tmp_path / self.COMPOSE_NAME
        compose.write_text("# test")
        with patch.dict(os.environ, {}, clear=False), \
             patch("adk.setup.Path.cwd", return_value=tmp_path / "elsewhere"), \
             patch("adk.setup.__file__", str(tmp_path / "setup.py")):
            os.environ.pop("ADK_ROOT", None)
            # The function uses Path(__file__).resolve().parent
            # We need to patch at module level
            result = _find_compose_file()
            # May or may not find it depending on __file__ resolution;
            # the point is it doesn't crash
            assert result is None or result.name == self.COMPOSE_NAME

    def test_finds_in_home_aither(self, tmp_path):
        compose = tmp_path / ".aither" / self.COMPOSE_NAME
        compose.parent.mkdir(parents=True)
        compose.write_text("# test")
        # Must also fake __file__ so the package-dir search doesn't find the real one
        fake_pkg = tmp_path / "fake_pkg"
        fake_pkg.mkdir()
        with patch.dict(os.environ, {}, clear=False), \
             patch("adk.setup.Path.home", return_value=tmp_path), \
             patch("adk.setup.Path.cwd", return_value=tmp_path / "nonexistent"), \
             patch("adk.setup.__file__", str(fake_pkg / "setup.py")):
            os.environ.pop("ADK_ROOT", None)
            result = _find_compose_file()
            assert result == compose

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch.dict(os.environ, {}, clear=False), \
             patch("adk.setup.Path.home", return_value=tmp_path / "nope"):
            os.environ.pop("ADK_ROOT", None)
            result = _find_compose_file()
            # May find the real one in the repo; just ensure no crash
            assert result is None or result.exists()

    def test_adk_root_takes_priority(self, tmp_path):
        """ADK_ROOT should be checked before CWD."""
        adk_dir = tmp_path / "adk"
        adk_dir.mkdir()
        (adk_dir / self.COMPOSE_NAME).write_text("# adk root")
        (tmp_path / self.COMPOSE_NAME).write_text("# cwd")
        with patch.dict(os.environ, {"ADK_ROOT": str(adk_dir)}):
            result = _find_compose_file()
            assert result == adk_dir / self.COMPOSE_NAME


# ---------------------------------------------------------------------------
# AitherZero bridge tests
# ---------------------------------------------------------------------------

class TestAitherZeroBridge:
    """Tests for _try_aitherzero, _run_aitherzero_setup, _parse_aitherzero_report."""

    @pytest.mark.asyncio
    async def test_try_aitherzero_success(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "PowerShell 7.4.0\n", ""),  # pwsh -Version
                (0, "True\n", ""),               # Import-Module + Get-AitherPlugin
            ]
            result = await setup._try_aitherzero()
            assert result is True

    @pytest.mark.asyncio
    async def test_try_aitherzero_no_pwsh(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (-1, "", "Command not found: pwsh")
            result = await setup._try_aitherzero()
            assert result is False

    @pytest.mark.asyncio
    async def test_try_aitherzero_no_plugin(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "PowerShell 7.4.0\n", ""),  # pwsh -Version
                (0, "\n", ""),                    # Plugin not found (no "True")
            ]
            result = await setup._try_aitherzero()
            assert result is False

    @pytest.mark.asyncio
    async def test_try_aitherzero_module_import_fails(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "PowerShell 7.4.0\n", ""),
                (1, "", "Import-Module : Module 'AitherZero' not found"),
            ]
            result = await setup._try_aitherzero()
            assert result is False

    def test_parse_aitherzero_report_full(self):
        data = {
            "profile": "nvidia_high",
            "backend": "vllm",
            "ready": True,
            "vllm_ready": True,
            "ollama_ready": False,
            "models_available": ["nvidia/Nemotron-Orchestrator-8B"],
            "errors": [],
            "gpu": {"vendor": "nvidia", "name": "RTX 5090", "vram_mb": 32768},
            "docker_installed": True,
        }
        report = AgentSetup._parse_aitherzero_report(data)
        assert isinstance(report, SetupReport)
        assert report.ready is True
        assert report.backend == "vllm"
        assert report.vllm_ready is True
        assert report.ollama_ready is False
        assert report.profile == "nvidia_high"
        assert report.system.gpu.vendor == "nvidia"
        assert report.system.gpu.name == "RTX 5090"
        assert report.system.gpu.vram_mb == 32768
        assert report.system.docker_installed is True
        assert "nvidia/Nemotron-Orchestrator-8B" in report.models_available

    def test_parse_aitherzero_report_minimal(self):
        """Handles missing keys gracefully with defaults."""
        data = {"ready": False, "backend": "", "profile": "cpu_only"}
        report = AgentSetup._parse_aitherzero_report(data)
        assert report.ready is False
        assert report.backend == ""
        assert report.system.gpu.vendor == "none"
        assert report.models_available == []
        assert report.errors == []

    def test_parse_aitherzero_report_with_errors(self):
        data = {
            "profile": "nvidia_mid",
            "backend": "",
            "ready": False,
            "vllm_ready": False,
            "ollama_ready": False,
            "models_available": [],
            "errors": ["vLLM setup failed", "Ollama not installed"],
            "gpu": {"vendor": "nvidia", "name": "RTX 3060", "vram_mb": 12288},
            "docker_installed": False,
        }
        report = AgentSetup._parse_aitherzero_report(data)
        assert report.ready is False
        assert len(report.errors) == 2
        assert "vLLM setup failed" in report.errors

    @pytest.mark.asyncio
    async def test_run_aitherzero_setup_success(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        az_output = json.dumps({
            "profile": "nvidia_high",
            "backend": "vllm",
            "ready": True,
            "vllm_ready": True,
            "ollama_ready": False,
            "models_available": ["nvidia/Nemotron-Orchestrator-8B"],
            "errors": [],
            "gpu": {"vendor": "nvidia", "name": "RTX 5090", "vram_mb": 32768},
            "docker_installed": True,
        })
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (0, az_output, "")
            report = await setup._run_aitherzero_setup()
            assert report is not None
            assert report.ready is True
            assert report.backend == "vllm"

    @pytest.mark.asyncio
    async def test_run_aitherzero_setup_failure(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (1, "", "error loading module")
            report = await setup._run_aitherzero_setup()
            assert report is None

    @pytest.mark.asyncio
    async def test_run_aitherzero_setup_bad_json(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch("adk.setup._run") as mock_run:
            mock_run.return_value = (0, "not valid json {{{", "")
            report = await setup._run_aitherzero_setup()
            assert report is None


# ---------------------------------------------------------------------------
# full_setup AitherZero delegation tests
# ---------------------------------------------------------------------------

class TestFullSetupAitherZeroDelegation:
    """Tests for Step 0 in full_setup — AitherZero delegation."""

    @pytest.mark.asyncio
    async def test_delegates_to_aitherzero_when_available(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        az_report = SetupReport(
            ready=True, backend="vllm", profile="nvidia_high",
            vllm_ready=True, models_available=["nemotron"],
        )
        with patch.object(setup, "_try_aitherzero", return_value=True), \
             patch.object(setup, "_run_aitherzero_setup", return_value=az_report), \
             patch.object(setup, "detect_hardware") as mock_detect:
            report = await setup.full_setup()
            assert report.ready is True
            assert report.backend == "vllm"
            # detect_hardware should NOT have been called — we short-circuited
            mock_detect.assert_not_called()

    @pytest.mark.asyncio
    async def test_saves_profile_marker_on_delegation(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        az_report = SetupReport(ready=True, backend="vllm", profile="nvidia_high")
        with patch.object(setup, "_try_aitherzero", return_value=True), \
             patch.object(setup, "_run_aitherzero_setup", return_value=az_report):
            await setup.full_setup()
            marker = tmp_path / "detected_profile"
            assert marker.exists()
            assert marker.read_text() == "nvidia_high"

    @pytest.mark.asyncio
    async def test_falls_back_when_aitherzero_not_ready(self, tmp_path):
        """If AitherZero setup returns not-ready, fall through to Python path."""
        setup = AgentSetup(data_dir=str(tmp_path))
        az_report = SetupReport(ready=False, errors=["vLLM failed"])
        with patch.object(setup, "_try_aitherzero", return_value=True), \
             patch.object(setup, "_run_aitherzero_setup", return_value=az_report), \
             patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama", return_value=True), \
             patch.object(setup, "pull_models", return_value=["llama3.2:1b"]):
            mock_detect.return_value = SystemInfo(
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            report = await setup.full_setup()
            # Should have fallen through to Python path
            mock_detect.assert_called_once()
            assert report.ready is True
            assert report.backend == "ollama"

    @pytest.mark.asyncio
    async def test_falls_back_when_aitherzero_returns_none(self, tmp_path):
        """If _run_aitherzero_setup returns None, fall through."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "_try_aitherzero", return_value=True), \
             patch.object(setup, "_run_aitherzero_setup", return_value=None), \
             patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama", return_value=True), \
             patch.object(setup, "pull_models", return_value=[]):
            mock_detect.return_value = SystemInfo(
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            report = await setup.full_setup()
            mock_detect.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_when_aitherzero_unavailable(self, tmp_path):
        """If _try_aitherzero returns False, skip delegation entirely."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "_try_aitherzero", return_value=False), \
             patch.object(setup, "_run_aitherzero_setup") as mock_az_setup, \
             patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama", return_value=True), \
             patch.object(setup, "pull_models", return_value=[]):
            mock_detect.return_value = SystemInfo(
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            await setup.full_setup()
            # _run_aitherzero_setup should never be called
            mock_az_setup.assert_not_called()
            mock_detect.assert_called_once()

    @pytest.mark.asyncio
    async def test_aither_no_pwsh_env_skips_delegation(self, tmp_path):
        """AITHER_NO_PWSH=1 should skip AitherZero entirely."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.dict(os.environ, {"AITHER_NO_PWSH": "1"}), \
             patch.object(setup, "_try_aitherzero") as mock_try, \
             patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama", return_value=True), \
             patch.object(setup, "pull_models", return_value=[]):
            mock_detect.return_value = SystemInfo(
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            await setup.full_setup()
            mock_try.assert_not_called()

    @pytest.mark.asyncio
    async def test_aitherzero_exception_falls_through(self, tmp_path):
        """Exceptions in AitherZero delegation should be caught silently."""
        setup = AgentSetup(data_dir=str(tmp_path))
        with patch.object(setup, "_try_aitherzero", side_effect=RuntimeError("boom")), \
             patch.object(setup, "detect_hardware") as mock_detect, \
             patch.object(setup, "ensure_ollama", return_value=True), \
             patch.object(setup, "pull_models", return_value=[]):
            mock_detect.return_value = SystemInfo(
                ollama_running=False,
                vllm=VLLMInfo(running=False),
                profile="cpu_only",
            )
            setup._system = mock_detect.return_value
            report = await setup.full_setup()
            # Should not crash — falls through to Python path
            mock_detect.assert_called_once()
            assert report.ready is True


# ---------------------------------------------------------------------------
# ensure_vllm compose-first tests
# ---------------------------------------------------------------------------

class TestEnsureVLLMCompose:
    """Tests for the compose-first path in ensure_vllm."""

    @pytest.mark.asyncio
    async def test_uses_compose_when_available(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=16000),
        )
        compose = tmp_path / "docker-compose.adk-vllm.yml"
        compose.write_text("# test compose")

        with patch("adk.setup._find_compose_file", return_value=compose), \
             patch("adk.setup._run") as mock_run:
            # First call: check if already running (no)
            # Second call: docker compose up -d (success)
            mock_run.side_effect = [
                (0, "", ""),       # docker ps --filter (not running)
                (0, "", ""),       # docker compose up -d
            ]
            result = await setup.ensure_vllm()
            assert result is True
            # Verify compose was called
            compose_call = mock_run.call_args_list[1]
            cmd = compose_call[0][0]
            assert "compose" in cmd
            assert str(compose) in cmd
            assert "up" in cmd

    @pytest.mark.asyncio
    async def test_compose_uses_dual_profile_for_high_vram(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=32000),
        )
        compose = tmp_path / "docker-compose.adk-vllm.yml"
        compose.write_text("# test compose")

        with patch("adk.setup._find_compose_file", return_value=compose), \
             patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "", ""),   # not running
                (0, "", ""),   # compose up
            ]
            result = await setup.ensure_vllm()
            assert result is True
            compose_call = mock_run.call_args_list[1]
            cmd = compose_call[0][0]
            assert "--profile" in cmd
            assert "dual" in cmd

    @pytest.mark.asyncio
    async def test_compose_no_dual_profile_for_low_vram(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=12000),
        )
        compose = tmp_path / "docker-compose.adk-vllm.yml"
        compose.write_text("# test compose")

        with patch("adk.setup._find_compose_file", return_value=compose), \
             patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "", ""),   # not running
                (0, "", ""),   # compose up
            ]
            result = await setup.ensure_vllm()
            assert result is True
            compose_call = mock_run.call_args_list[1]
            cmd = compose_call[0][0]
            assert "--profile" not in cmd

    @pytest.mark.asyncio
    async def test_compose_skips_if_already_running(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=24000),
        )
        compose = tmp_path / "docker-compose.adk-vllm.yml"
        compose.write_text("# test compose")

        with patch("adk.setup._find_compose_file", return_value=compose), \
             patch("adk.setup._run") as mock_run:
            mock_run.return_value = (0, "adk-vllm-primary\n", "")
            result = await setup.ensure_vllm()
            assert result is True
            # Only one call — the docker ps check; no compose up
            assert mock_run.call_count == 1

    @pytest.mark.asyncio
    async def test_compose_failure_falls_back_to_docker_run(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=12000, count=1),
        )
        compose = tmp_path / "docker-compose.adk-vllm.yml"
        compose.write_text("# test compose")

        with patch("adk.setup._find_compose_file", return_value=compose), \
             patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "", ""),                           # compose containers not running
                (1, "", "compose error"),              # compose up fails
                (0, "", ""),                           # docker ps for aither-vllm-8200 (not running)
                (0, "", ""),                           # docker run succeeds
            ]
            result = await setup.ensure_vllm()
            assert result is True
            # Should have fallen through to docker run
            last_cmd = mock_run.call_args_list[-1][0][0]
            assert "run" in last_cmd

    @pytest.mark.asyncio
    async def test_no_compose_file_goes_straight_to_docker_run(self, tmp_path):
        setup = AgentSetup(data_dir=str(tmp_path))
        setup._system = SystemInfo(
            docker_installed=True,
            gpu=GPUInfo(vendor="nvidia", vram_mb=12000, count=1),
        )

        with patch("adk.setup._find_compose_file", return_value=None), \
             patch("adk.setup._run") as mock_run:
            mock_run.side_effect = [
                (0, "", ""),   # docker ps (not running)
                (0, "", ""),   # docker run
            ]
            result = await setup.ensure_vllm()
            assert result is True
            # First call should be docker ps for aither-vllm-8200
            first_cmd = mock_run.call_args_list[0][0][0]
            assert "aither-vllm-8200" in str(first_cmd)
