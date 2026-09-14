"""
Local Backend Picker for ADK Quickstart
========================================

Pure functions for selecting the right inference backend based on
hardware capabilities and user preference.

Public API:
    pick_backend(accel, prefer: str = "auto",
                 docker_available: bool | None = None) -> str
    docker_available() -> bool
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Bonsai detection
# ---------------------------------------------------------------------------

BONSAI_LOCAL_PORT = 8090


def bonsai_available() -> bool:
    """Check if the Bonsai local container is running or the GGUF is present."""
    # Check running container
    if shutil.which("docker"):
        try:
            result = subprocess.run(
                ["docker", "ps", "-q", "-f", "name=^aither-bonsai-local$"],
                capture_output=True, text=True, timeout=5,
            )
            if (result.stdout or "").strip():
                return True
        except Exception:
            pass

    # Check for downloaded GGUF model
    from pathlib import Path
    models_dir = Path.home() / ".aither" / "models"
    if any(models_dir.glob("**/Bonsai*Q1*.gguf")) if models_dir.exists() else False:
        return True
    if any(models_dir.glob("**/bonsai*.gguf")) if models_dir.exists() else False:
        return True

    return False

# ---------------------------------------------------------------------------
# Docker detection
# ---------------------------------------------------------------------------


def docker_available() -> bool:
    """Check if Docker is installed and the daemon is running."""
    if not shutil.which("docker"):
        return False

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Backend picker
# ---------------------------------------------------------------------------


def pick_backend(
    accel: object,
    prefer: str = "auto",
    docker_available_override: Optional[bool] = None,
) -> str:
    """
    Select the best local inference backend for this hardware.

    Args:
        accel: AccelInfo-like object with attributes:
               kind (cuda|vulkan|metal|cpu), vram_gb, name
        prefer: "auto", "llamacpp", "ollama", or "vllm"
        docker_available_override: explicitly pass docker availability
                                   (for testing); None = auto-detect

    Returns:
        "llamacpp" | "ollama" | "vllm"

    Logic (when prefer="auto"):
      1. NVIDIA CUDA + >=16GB VRAM + Docker available → vllm
      2. Ollama installed → ollama
      3. Otherwise → llamacpp (pure-stdlib, no Docker needed)

    Explicit prefer always wins.
    """
    # Explicit preference
    if prefer in ("llamacpp", "ollama", "vllm", "bonsai"):
        return prefer

    # Auto-detection
    docker_ok = (
        docker_available_override
        if docker_available_override is not None
        else docker_available()
    )
    vram_gb = getattr(accel, "vram_gb", 0.0)
    accel_kind = getattr(accel, "kind", "cpu")

    # Bonsai already running or downloaded → use it (works on anything)
    if bonsai_available():
        return "bonsai"

    # Ollama installed → Ollama (the simple, portable default — "use the ollama")
    from adk.ollama_setup import is_installed as ollama_is_installed

    if ollama_is_installed():
        return "ollama"

    # No Ollama: CUDA with sufficient VRAM + Docker → vLLM (best throughput)
    if accel_kind == "cuda" and vram_gb >= 16 and docker_ok:
        return "vllm"

    # CPU-only or low-VRAM with Docker → recommend Bonsai (1-bit, runs anywhere)
    if (accel_kind == "cpu" or vram_gb < 6) and docker_ok:
        return "bonsai"

    # Fallback: llama.cpp (always works, no Docker)
    return "llamacpp"
