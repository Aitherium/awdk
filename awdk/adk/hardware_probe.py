"""Hardware probing for the first-run wizard.

Detects:
  - RAM (total system memory)
  - CPU cores
  - GPU (reuses setup_cli GPU detection)
  - Ollama installation status

Provides: recommend_setup() → recommendation dict with rationale.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adk.hardware_probe")


@dataclass
class SystemInfo:
    """Detected system hardware."""

    ram_gb: float = 0.0
    cpu_cores: int = 0
    gpu_vendor: str = "none"  # nvidia, amd, apple, none
    gpu_name: str = ""
    gpu_vram_mb: int = 0
    ollama_installed: bool = False
    python_version: str = ""


def _detect_ram() -> float:
    """Detect total system RAM in GB.

    Uses psutil if available, else ctypes (Windows), sysctl (macOS), /proc (Linux).
    """
    try:
        import psutil

        return psutil.virtual_memory().total / (1024**3)
    except ImportError:
        pass

    if platform.system() == "Windows":
        try:
            import ctypes

            mem = ctypes.c_ulonglong()
            ctypes.windll.kernel32.GetPhysicallyInstalledSystemMemory(
                ctypes.byref(mem)
            )
            return mem.value / (1024**2 / 1024)  # Convert MB to GB
        except Exception:
            pass
    elif platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0:
                return int(out.stdout.strip()) / (1024**3)
        except Exception:
            pass
    else:  # Linux
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
        except Exception:
            pass

    return 0.0


def _detect_cpu_cores() -> int:
    """Detect number of CPU cores."""
    try:
        import psutil

        return psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    except ImportError:
        pass

    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def _detect_gpu() -> tuple[str, str, int]:
    """Detect GPU: vendor, name, best_vram_mb.

    Reuses setup_cli's GPU detection logic.
    """
    try:
        from adk.setup_cli import detect_gpu

        gpu_info = detect_gpu()
        return (
            gpu_info.vendor,
            gpu_info.name,
            gpu_info.vram_mb,
        )
    except Exception as e:
        logger.debug("GPU detection failed: %s", e)
        return "none", "", 0


def _detect_ollama() -> bool:
    """Check if Ollama is installed and available."""
    return bool(shutil.which("ollama"))


def detect_system() -> SystemInfo:
    """Probe the system and return detected hardware info."""
    ram = _detect_ram()
    cores = _detect_cpu_cores()
    gpu_vendor, gpu_name, gpu_vram = _detect_gpu()
    ollama = _detect_ollama()

    return SystemInfo(
        ram_gb=ram,
        cpu_cores=cores,
        gpu_vendor=gpu_vendor,
        gpu_name=gpu_name,
        gpu_vram_mb=gpu_vram,
        ollama_installed=ollama,
        python_version=platform.python_version(),
    )


@dataclass
class Recommendation:
    """Wizard recommendation based on hardware."""

    local_model: Optional[str] = None  # Model name to run locally, or None
    backend: str = "cloud"  # cloud, ollama, local-8b, local-3b
    local_model_gb: float = 0.0  # Disk space needed
    local_vram_gb: float = 0.0  # VRAM needed
    rationale: str = ""
    warnings: list[str] = None  # Caution messages

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def recommend_setup(system: SystemInfo) -> Recommendation:
    """Recommend setup configuration based on hardware.

    Logic:
      - NVIDIA GPU >= 8GB VRAM → local 8B model
      - Apple Silicon >= 16GB RAM → local 8-14B via Ollama
      - CPU-only >= 16GB RAM → local 3B model + suggest cloud key
      - Below that → cloud-key only, Ollama optional

    Returns Recommendation with setup steps.
    """
    rec = Recommendation(warnings=[])

    # NVIDIA GPU path
    if system.gpu_vendor == "nvidia":
        gpu_vram_gb = system.gpu_vram_mb / 1024
        if gpu_vram_gb >= 8:
            rec.backend = "local-8b"
            rec.local_model = "nemotron-orchestrator-8b"
            rec.local_vram_gb = 7.0
            rec.local_model_gb = 16.0
            rec.rationale = (
                f"NVIDIA GPU detected ({system.gpu_name}, {gpu_vram_gb:.0f}GB). "
                "Running local 8B model for best performance and cost."
            )
        elif gpu_vram_gb >= 4:
            rec.backend = "local-3b"
            rec.local_model = "qwen2:3b"
            rec.local_vram_gb = 4.0
            rec.local_model_gb = 4.0
            rec.rationale = (
                f"NVIDIA GPU detected ({system.gpu_name}, {gpu_vram_gb:.0f}GB). "
                "Running local 3B model; for reasoning tasks, a cloud API key recommended."
            )
            rec.warnings.append(
                "VRAM limited — consider cloud API key for reasoning tasks"
            )
        else:
            rec.backend = "cloud"
            rec.rationale = (
                f"NVIDIA GPU detected but VRAM limited ({gpu_vram_gb:.0f}GB). "
                "Using cloud-only backend. Provide an API key for Anthropic, OpenAI, or DeepSeek."
            )
            rec.warnings.append(
                "VRAM too small for local models; cloud API key required"
            )
        return rec

    # Apple Silicon path
    if system.gpu_vendor == "apple":
        if system.ram_gb >= 16:
            rec.backend = "ollama"
            rec.local_model = "nemotron-orchestrator-8b"
            rec.local_model_gb = 16.0
            rec.rationale = (
                f"Apple Silicon detected ({system.gpu_name}, {system.ram_gb:.0f}GB RAM). "
                "Ollama will use Metal acceleration for smooth local inference."
            )
            return rec
        elif system.ram_gb >= 8:
            rec.backend = "ollama"
            rec.local_model = "qwen2:3b"
            rec.local_model_gb = 4.0
            rec.rationale = (
                f"Apple Silicon detected ({system.gpu_name}, {system.ram_gb:.0f}GB RAM). "
                "Running small Ollama model; cloud key suggested for reasoning."
            )
            rec.warnings.append("Memory limited; cloud API key recommended")
            return rec

    # CPU-only path
    if system.ram_gb >= 16:
        rec.backend = "local-3b"
        rec.local_model = "qwen2:3b"
        rec.local_model_gb = 4.0
        rec.rationale = (
            f"CPU-only system ({system.cpu_cores} cores, {system.ram_gb:.0f}GB RAM). "
            "Local 3B model will work; recommend cloud API key for reasoning and complex tasks."
        )
        rec.warnings.append("CPU inference is slow; cloud API key strongly recommended")
        return rec

    # Fallback: cloud-only
    rec.backend = "cloud"
    rec.rationale = (
        f"System has {system.ram_gb:.0f}GB RAM, {system.cpu_cores} cores. "
        "Cloud-only backend recommended. Provide an API key."
    )
    rec.warnings.append("Local models won't fit; cloud API key required")
    return rec
