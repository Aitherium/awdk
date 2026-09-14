#!/usr/bin/env python3
"""AitherOS Inference Setup Wizard — interactive, hardware-aware local AI setup.

Detects your GPU, recommends the best backend (vLLM containers or Ollama),
lets you pick models that fit your hardware, and gets everything running.

Usage:
    python setup-vllm.py              # Interactive wizard
    python setup-vllm.py --dry-run    # Show what would happen
    python setup-vllm.py --tier full  # Skip prompts, go full stack
    python setup-vllm.py --tier ollama # Use Ollama (any GPU/CPU)
    python setup-vllm.py --generate   # Just generate compose file, don't start

Tiers:
    nano     — Qwen3-8B via vLLM (~5.5GB VRAM). For 8-12GB GPUs.
    lite     — Nemotron Orchestrator via vLLM (~6.5GB VRAM). For 12-16GB GPUs.
    standard — Orchestrator + DeepSeek R1 (~18GB VRAM). For 20-24GB GPUs.
    full     — Orchestrator + R1 + Embeddings (~19GB VRAM). For 24GB+ GPUs.
    ollama   — Any GPU (NVIDIA/AMD/Apple Silicon) or CPU. Interactive model picker.

Models (vLLM):
    Orchestrator: nvidia/Nemotron-Orchestrator-8B (outperforms GPT-4o on tool use)
    Reasoning:    deepseek-ai/DeepSeek-R1-Distill-Qwen-14B (deep thinking)
    Embeddings:   nomic-ai/nomic-embed-text-v1.5 (fast vector search)

Models (Ollama):
    Chat:      llama3.2:3b, nemotron-orchestrator-8b, qwen3:8b, gemma3:12b
    Reasoning: deepseek-r1:7b/14b/32b, qwen3:14b
    Coding:    qwen2.5-coder:7b/14b
    Vision:    llama3.2-vision:11b

Pure stdlib — no pip dependencies required.
When you're not gaming, your agents can be earning money.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
        except Exception:
            return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

_COLOR = _supports_color()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def bold(t: str) -> str: return _c("1", t)
def green(t: str) -> str: return _c("92", t)
def yellow(t: str) -> str: return _c("93", t)
def red(t: str) -> str: return _c("91", t)
def cyan(t: str) -> str: return _c("96", t)
def dim(t: str) -> str: return _c("2", t)
def blue(t: str) -> str: return _c("94", t)
def magenta(t: str) -> str: return _c("95", t)

def info(msg: str) -> None: print(f"  {green('+')} {msg}")
def warn(msg: str) -> None: print(f"  {yellow('!')} {msg}")
def error(msg: str) -> None: print(f"  {red('x')} {msg}")
def step(n: int, total: int, msg: str) -> None:
    print(f"\n  {bold(f'[{n}/{total}]')} {bold(msg)}")

# ---------------------------------------------------------------------------
# GPU Detection
# ---------------------------------------------------------------------------

@dataclass
class GPUInfo:
    vendor: str = "none"
    name: str = "Unknown"
    vram_mb: int = 0          # Best single GPU VRAM (for Ollama profile selection)
    cuda_version: str = ""
    driver_version: str = ""
    compute_capability: str = ""
    gpu_count: int = 0
    total_vram_mb: int = 0    # Sum of all GPUs (for vLLM tier selection)
    all_gpus: list = field(default_factory=list)  # List of {"name": str, "vram_mb": int}


def _run_cmd(cmd: list[str], timeout: int = 10) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def detect_nvidia_gpu() -> Optional[GPUInfo]:
    """Detect NVIDIA GPU via nvidia-smi."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None

    # Get GPU name and VRAM
    out = _run_cmd([smi, "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader,nounits"])
    if not out:
        return None

    lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
    if not lines:
        return None

    # Parse all GPUs, find best (max VRAM), sum total
    all_gpus = []
    best_name = "Unknown NVIDIA GPU"
    best_vram = 0
    total_vram = 0
    best_driver = ""
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        g_name = parts[0] if len(parts) > 0 else "Unknown NVIDIA GPU"
        g_vram = int(float(parts[1])) if len(parts) > 1 else 0
        g_driver = parts[2] if len(parts) > 2 else ""
        all_gpus.append({"name": g_name, "vram_mb": g_vram})
        total_vram += g_vram
        if g_vram > best_vram:
            best_vram = g_vram
            best_name = g_name
            best_driver = g_driver

    # CUDA version
    cuda_out = _run_cmd([smi])
    cuda_ver = ""
    if cuda_out:
        m = re.search(r"CUDA Version:\s*([\d.]+)", cuda_out)
        if m:
            cuda_ver = m.group(1)

    # Compute capability (from best GPU — first line with max VRAM)
    cc = ""
    cc_out = _run_cmd([smi, "--query-gpu=compute_cap", "--format=csv,noheader"])
    if cc_out:
        cc_lines = [l.strip() for l in cc_out.strip().split("\n") if l.strip()]
        # Find CC for best GPU by index
        best_idx = next((i for i, g in enumerate(all_gpus) if g["vram_mb"] == best_vram), 0)
        if best_idx < len(cc_lines):
            cc = cc_lines[best_idx]

    return GPUInfo(
        vendor="nvidia",
        name=best_name,
        vram_mb=best_vram,
        cuda_version=cuda_ver,
        driver_version=best_driver,
        compute_capability=cc,
        gpu_count=len(all_gpus),
        total_vram_mb=total_vram,
        all_gpus=all_gpus,
    )


def detect_amd_gpu() -> Optional[GPUInfo]:
    """Detect AMD GPU via rocm-smi."""
    rocm = shutil.which("rocm-smi")
    if not rocm:
        return None
    out = _run_cmd([rocm, "--showproductname"])
    if not out:
        return None
    # Parse product name
    name = "AMD GPU"
    for line in out.split("\n"):
        if "GPU" in line or "Radeon" in line or "Instinct" in line:
            name = line.strip().split(":")[-1].strip() if ":" in line else line.strip()
            break
    # Try to get VRAM
    vram_mb = 0
    mem_out = _run_cmd([rocm, "--showmeminfo", "vram"])
    if mem_out:
        for line in mem_out.split("\n"):
            if "Total" in line:
                m = re.search(r"(\d+)", line)
                if m:
                    vram_mb = int(m.group(1)) // (1024 * 1024)  # bytes to MB
    return GPUInfo(vendor="amd", name=name, vram_mb=vram_mb)


def detect_apple_silicon() -> Optional[GPUInfo]:
    """Detect Apple Silicon GPU."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    # Get chip name
    out = _run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    name = out.strip() if out else "Apple Silicon"
    # Unified memory = GPU memory
    mem_out = _run_cmd(["sysctl", "-n", "hw.memsize"])
    vram_mb = int(int(mem_out.strip()) / (1024 * 1024)) if mem_out else 0
    return GPUInfo(vendor="apple", name=name, vram_mb=vram_mb)


def detect_gpu() -> GPUInfo:
    """Auto-detect GPU hardware."""
    nvidia = detect_nvidia_gpu()
    if nvidia:
        return nvidia
    amd = detect_amd_gpu()
    if amd:
        return amd
    apple = detect_apple_silicon()
    if apple:
        return apple
    return GPUInfo()


# ---------------------------------------------------------------------------
# Tier Configuration
# ---------------------------------------------------------------------------

@dataclass
class VLLMWorker:
    name: str
    model: str
    served_name: str
    port: int
    gpu_memory_utilization: float
    max_model_len: int
    extra_args: list[str] = field(default_factory=list)
    description: str = ""
    download_size_gb: float = 0.0
    vram_estimate_gb: float = 0.0


@dataclass
class OllamaModel:
    name: str
    description: str
    size_gb: float
    min_vram_gb: float
    role: str  # "chat", "reasoning", "embedding", "coding", "vision"


# Ollama models organized by VRAM tier
OLLAMA_MODELS = {
    "chat": [
        OllamaModel("llama3.2:3b", "Meta Llama 3.2 3B — fast, efficient chat", 2.0, 4, "chat"),
        OllamaModel("nemotron-orchestrator-8b", "NVIDIA Nemotron Orchestrator 8B — best tool use", 4.9, 8, "chat"),
        OllamaModel("qwen3:8b", "Qwen 3 8B — strong multilingual + reasoning", 4.9, 8, "chat"),
        OllamaModel("gemma3:12b", "Google Gemma 3 12B — high quality", 8.1, 12, "chat"),
        OllamaModel("gemma4:27b", "Google Gemma 4 27B — multimodal, 256K context, Apache 2.0", 17.0, 24, "chat"),
    ],
    "reasoning": [
        OllamaModel("deepseek-r1:7b", "DeepSeek-R1 7B — compact reasoning", 4.7, 8, "reasoning"),
        OllamaModel("deepseek-r1:14b", "DeepSeek-R1 14B — strong reasoning", 9.0, 12, "reasoning"),
        OllamaModel("deepseek-r1:32b", "DeepSeek-R1 32B — top-tier reasoning", 20.0, 24, "reasoning"),
        OllamaModel("qwen3:14b", "Qwen 3 14B — reasoning + tool use", 9.0, 12, "reasoning"),
    ],
    "embedding": [
        OllamaModel("nomic-embed-text", "Nomic Embed v1.5 — fast 768-dim embeddings", 0.3, 2, "embedding"),
    ],
    "coding": [
        OllamaModel("qwen2.5-coder:7b", "Qwen 2.5 Coder 7B — code completion", 4.7, 8, "coding"),
        OllamaModel("qwen2.5-coder:14b", "Qwen 2.5 Coder 14B — advanced coding", 9.0, 12, "coding"),
    ],
    "vision": [
        OllamaModel("llama3.2-vision:11b", "Llama 3.2 Vision 11B — image understanding", 7.9, 12, "vision"),
    ],
}

# PicoLM — ultra-lightweight inference for edge/CPU scenarios
PICOLM_MODELS = [
    {"name": "picolm-125m", "description": "PicoLM 125M — ultra-fast CPU inference, tiny footprint", "size_mb": 250},
    {"name": "picolm-350m", "description": "PicoLM 350M — lightweight CPU inference", "size_mb": 700},
]


TIERS = {
    "nano": {
        "name": "Nano",
        "description": "Small model via vLLM — for 8-12GB GPUs",
        "min_vram_gb": 6,
        "workers": [
            VLLMWorker(
                name="orchestrator",
                model="Qwen/Qwen3-8B",
                served_name="aither-orchestrator",
                port=8120,
                gpu_memory_utilization=0.80,
                max_model_len=16384,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--enable-prefix-caching",
                ],
                description="Qwen3-8B — capable chat + tool calling for smaller GPUs",
                download_size_gb=8.0,
                vram_estimate_gb=5.5,
            ),
        ],
    },
    "lite": {
        "name": "Lite",
        "description": "Nemotron Orchestrator — for 12-16GB GPUs",
        "min_vram_gb": 10,
        "workers": [
            VLLMWorker(
                name="orchestrator",
                model="nvidia/Nemotron-Orchestrator-8B",
                served_name="aither-orchestrator",
                port=8120,
                gpu_memory_utilization=0.80,
                max_model_len=32768,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--enable-prefix-caching",
                ],
                description="Nemotron-Orchestrator-8B — outperforms GPT-4o on tool orchestration",
                download_size_gb=16.0,
                vram_estimate_gb=6.5,
            ),
        ],
    },
    "standard": {
        "name": "Standard",
        "description": "Orchestrator + Reasoning — for 20-24GB GPUs",
        "min_vram_gb": 18,
        "workers": [
            VLLMWorker(
                name="orchestrator",
                model="nvidia/Nemotron-Orchestrator-8B",
                served_name="aither-orchestrator",
                port=8120,
                gpu_memory_utilization=0.35,
                max_model_len=32768,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--enable-prefix-caching", "--enable-sleep-mode",
                ],
                description="Nemotron-Orchestrator-8B — handles 80% of requests",
                download_size_gb=16.0,
                vram_estimate_gb=6.5,
            ),
            VLLMWorker(
                name="reasoning",
                model="deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
                served_name="deepseek-r1:14b",
                port=8176,
                gpu_memory_utilization=0.55,
                max_model_len=16384,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--reasoning-parser deepseek_r1",
                    "--enable-prefix-caching", "--enable-sleep-mode",
                ],
                description="DeepSeek-R1 14B — deep thinking for complex tasks",
                download_size_gb=28.0,
                vram_estimate_gb=12.0,
            ),
        ],
    },
    "full": {
        "name": "Full",
        "description": "Orchestrator + Reasoning + Embeddings — 24GB+ GPUs",
        "min_vram_gb": 20,
        "workers": [
            VLLMWorker(
                name="orchestrator",
                model="nvidia/Nemotron-Orchestrator-8B",
                served_name="aither-orchestrator",
                port=8120,
                gpu_memory_utilization=0.35,
                max_model_len=32768,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--enable-prefix-caching", "--enable-sleep-mode",
                ],
                description="Nemotron-Orchestrator-8B",
                download_size_gb=16.0,
                vram_estimate_gb=6.5,
            ),
            VLLMWorker(
                name="reasoning",
                model="deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
                served_name="deepseek-r1:14b",
                port=8176,
                gpu_memory_utilization=0.55,
                max_model_len=16384,
                extra_args=[
                    "--quantization bitsandbytes", "--load-format bitsandbytes",
                    "--enable-auto-tool-choice", "--tool-call-parser hermes",
                    "--reasoning-parser deepseek_r1",
                    "--enable-prefix-caching", "--enable-sleep-mode",
                ],
                description="DeepSeek-R1 14B",
                download_size_gb=28.0,
                vram_estimate_gb=12.0,
            ),
            VLLMWorker(
                name="embeddings",
                model="nomic-ai/nomic-embed-text-v1.5",
                served_name="nomic-embed-text",
                port=8209,
                gpu_memory_utilization=0.05,
                max_model_len=2048,
                extra_args=["--dtype float16", "--max-num-seqs 64"],
                description="Nomic Embed v1.5 — vector search",
                download_size_gb=0.5,
                vram_estimate_gb=0.5,
            ),
        ],
    },
    "ollama": {
        "name": "Ollama",
        "description": "Ollama backend — works on any GPU (AMD, NVIDIA, Apple Silicon)",
        "min_vram_gb": 4,
        "workers": [],  # Populated dynamically based on model selection
    },
}


# ---------------------------------------------------------------------------
# VRAM Calculator
# ---------------------------------------------------------------------------

def recommend_tier(gpu: GPUInfo) -> str:
    """Recommend tier based on available VRAM.

    Uses total_vram_mb for vLLM tier — workers spread across GPUs.
    """
    # Use total VRAM for vLLM tier selection
    vram_gb = (gpu.total_vram_mb or gpu.vram_mb) / 1024
    usable = vram_gb * 0.85

    if gpu.vendor != "nvidia":
        return "ollama"  # Non-NVIDIA GPUs use Ollama
    if usable >= 24:
        return "full"
    elif usable >= 18:
        return "standard"
    elif usable >= 10:
        return "lite"
    elif usable >= 6:
        return "nano"
    elif usable >= 4:
        return "ollama"
    else:
        return ""


def print_tier_comparison(gpu: GPUInfo):
    """Show tier comparison table."""
    vram_gb = gpu.vram_mb / 1024

    print(f"\n  {bold('Available Tiers:')}\n")
    print(f"  {'Tier':<12} {'Backend':<8} {'Workers':<30} {'VRAM':<10} {'Download':<10} {'Status'}")
    print(f"  {'-'*12} {'-'*8} {'-'*30} {'-'*10} {'-'*10} {'-'*10}")

    for tid, tier in TIERS.items():
        if tid == "ollama":
            workers_str = "model selection wizard"
            vram_str = "varies"
            dl_str = "varies"
            fits = True  # Ollama always works
            backend = "Ollama"
        else:
            workers_str = " + ".join(w.name for w in tier["workers"])
            vram_need = sum(w.vram_estimate_gb for w in tier["workers"])
            download = sum(w.download_size_gb for w in tier["workers"])
            fits = vram_gb * 0.85 >= tier["min_vram_gb"]
            vram_str = f"~{vram_need:.0f}GB"
            dl_str = f"~{download:.0f}GB"
            backend = "vLLM"

        status = green("fits") if fits else red("needs more VRAM")
        print(f"  {bold(tid):<20} {backend:<8} {workers_str:<30} {vram_str:<10} {dl_str:<10} {status}")

    print()
    for tid, tier in TIERS.items():
        print(f"  {bold(tid)}: {tier['description']}")
        for w in tier["workers"]:
            print(f"    {dim('*')} {w.description}")
        if tid == "ollama":
            print(f"    {dim('*')} Interactive model picker based on your VRAM")
    print()


# ---------------------------------------------------------------------------
# Ollama Model Picker
# ---------------------------------------------------------------------------

def pick_ollama_models(gpu: GPUInfo) -> list[str]:
    """Interactive Ollama model selection based on VRAM."""
    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0

    print(f"\n  {bold('Select models to install with Ollama:')}")
    print(f"  {dim(f'Your GPU: {gpu.name} ({vram_gb:.0f}GB VRAM)' if gpu.vendor != 'none' else 'No GPU — CPU mode')}")
    print()

    selected = []

    for role, models in OLLAMA_MODELS.items():
        # Filter to models that fit
        fitting = [m for m in models if vram_gb >= m.min_vram_gb or gpu.vendor == "none"]
        if not fitting:
            continue

        print(f"  {bold(role.upper())}:")
        for i, m in enumerate(fitting):
            fits_tag = green("fits") if vram_gb >= m.min_vram_gb else yellow("CPU only")
            print(f"    {i+1}. {m.name:<30} {m.description:<50} {dim(f'{m.size_gb:.1f}GB')} {fits_tag}")

        if role == "embedding":
            # Always recommend embeddings
            selected.append(fitting[0].name)
            info(f"Auto-selected: {fitting[0].name}")
            continue

        # For chat, pick the best fitting model by default
        if role == "chat":
            # Pick the largest that fits in VRAM
            best = [m for m in reversed(fitting) if vram_gb >= m.min_vram_gb]
            default = best[0].name if best else fitting[0].name
        else:
            default = ""

        if role in ("chat", "reasoning"):
            choice = ask(
                f"Pick {role} model (number, name, or 'skip')",
                default=default if default else "skip",
            )
            if choice.lower() == "skip":
                continue
            # Resolve by number or name
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(fitting):
                    selected.append(fitting[idx].name)
                    continue
            except ValueError:
                pass
            # Try by name substring
            matched = [m for m in fitting if choice in m.name]
            if matched:
                selected.append(matched[0].name)
            elif default:
                selected.append(default)
        else:
            # Optional: coding, vision
            choice = ask(f"Install {role} model?", default="no", choices=["yes", "no"])
            if choice == "yes":
                best = [m for m in reversed(fitting) if vram_gb >= m.min_vram_gb]
                if best:
                    selected.append(best[0].name)
                elif fitting:
                    selected.append(fitting[0].name)

    return selected


def install_ollama_models(models: list[str], dry_run: bool = False) -> bool:
    """Pull selected Ollama models."""
    ollama = shutil.which("ollama")
    if not ollama:
        error("Ollama not installed")
        print(f"  Install: {cyan('https://ollama.com/download')}")
        if sys.platform == "win32":
            print(f"  Or: {cyan('winget install Ollama.Ollama')}")
        elif sys.platform == "darwin":
            print(f"  Or: {cyan('brew install ollama')}")
        else:
            print(f"  Or: {cyan('curl -fsSL https://ollama.com/install.sh | sh')}")
        return False

    # Check Ollama is running
    out = _run_cmd(["ollama", "list"])
    if out is None:
        warn("Ollama not running — starting it...")
        if not dry_run:
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            time.sleep(3)

    # Get existing models
    existing = set()
    out = _run_cmd(["ollama", "list"])
    if out:
        for line in out.strip().split("\n")[1:]:  # Skip header
            parts = line.split()
            if parts:
                existing.add(parts[0].split(":")[0])  # Strip tag

    for model in models:
        base_name = model.split(":")[0]
        if base_name in existing or model in existing:
            info(f"Already have: {model}")
            continue

        if dry_run:
            info(f"Would pull: {model}")
        else:
            info(f"Pulling: {bold(model)} (this may take a while)...")
            try:
                result = subprocess.run(
                    ["ollama", "pull", model],
                    timeout=1800,  # 30 min max per model
                )
                if result.returncode == 0:
                    info(f"Pulled: {green(model)}")
                else:
                    warn(f"Failed to pull {model}")
            except subprocess.TimeoutExpired:
                warn(f"Timeout pulling {model}")
            except Exception as e:
                warn(f"Error pulling {model}: {e}")

    return True


# ---------------------------------------------------------------------------
# Docker Compose Generator
# ---------------------------------------------------------------------------

def generate_compose(tier_id: str, hf_token: str = "") -> str:
    """Generate docker-compose.vllm.yml for the selected tier."""
    tier = TIERS[tier_id]
    workers = tier["workers"]

    services = []
    volumes = ["  aither-hf-cache:", "  aither-vllm-cache:"]

    for w in workers:
        extra = " ".join(w.extra_args)
        env_lines = [
            f"      NVIDIA_VISIBLE_DEVICES: all",
        ]
        if hf_token:
            env_lines.append(f"      HF_TOKEN: \"{hf_token}\"")
            env_lines.append(f"      HUGGING_FACE_HUB_TOKEN: \"{hf_token}\"")

        svc = textwrap.dedent(f"""\
  aither-vllm-{w.name}:
    image: vllm/vllm-openai:latest
    container_name: aither-vllm-{w.name}
    hostname: aither-vllm-{w.name}
    shm_size: '4gb'
    environment:
{chr(10).join(env_lines)}
    command: >
      --model {w.model}
      --host 0.0.0.0 --port {w.port}
      --gpu-memory-utilization {w.gpu_memory_utilization}
      --max-model-len {w.max_model_len}
      --enforce-eager --dtype auto
      --max-num-seqs 4
      --trust-remote-code
      --served-model-name {w.served_name}
      {extra}
    ports:
      - "{w.port}:{w.port}"
    volumes:
      - aither-hf-cache:/root/.cache/huggingface
      - aither-vllm-cache:/root/.cache/vllm
    healthcheck:
      interval: 30s
      timeout: 10s
      start_period: 1800s
      retries: 5
      test: ["CMD", "curl", "-f", "http://localhost:{w.port}/health"]
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
        limits:
          memory: 4G""")
        services.append(svc)

    services_block = "\n\n".join(services)
    volumes_block = "\n".join(volumes)

    compose = textwrap.dedent(f"""\
# AitherOS vLLM Inference Stack — Generated by setup-vllm.py
# Tier: {tier_id} ({tier['name']})
# {tier['description']}
#
# Usage:
#   docker compose -f docker-compose.vllm.yml up -d
#   docker compose -f docker-compose.vllm.yml logs -f
#   docker compose -f docker-compose.vllm.yml down
#
# First run downloads model weights (~{sum(w.download_size_gb for w in workers):.0f}GB).
# Subsequent starts use cached weights and boot in ~60s.
#
# Test:
#   curl http://localhost:{workers[0].port}/v1/chat/completions \\
#     -d '{{"model": "{workers[0].served_name}", "messages": [{{"role": "user", "content": "hello"}}]}}'

services:
{services_block}

volumes:
{volumes_block}
""")
    return compose


# ---------------------------------------------------------------------------
# Container Management
# ---------------------------------------------------------------------------

def check_docker() -> tuple[bool, str]:
    """Check if Docker is available and has GPU support."""
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker not found — install from https://docker.com"

    # Check Docker is running
    out = _run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not out:
        return False, "Docker daemon not running"

    # Check GPU runtime
    gpu_out = _run_cmd(["docker", "run", "--rm", "--gpus", "all",
                        "nvidia/cuda:12.4.0-base-ubuntu22.04", "nvidia-smi"],
                       timeout=30)
    if not gpu_out:
        # Try without the test container
        return True, f"Docker {out} (GPU runtime not verified — may need nvidia-container-toolkit)"

    return True, f"Docker {out} with NVIDIA GPU runtime"


def start_containers(compose_path: Path, dry_run: bool = False) -> bool:
    """Start vLLM containers."""
    if dry_run:
        info(f"Would run: docker compose -f {compose_path} up -d")
        return True

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            error(f"docker compose up failed: {result.stderr[:500]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        warn("docker compose up timed out — containers may still be starting")
        return True
    except Exception as e:
        error(f"Failed to start containers: {e}")
        return False


def wait_for_health(port: int, name: str, timeout: int = 300) -> bool:
    """Wait for a vLLM container to become healthy."""
    import urllib.request
    import urllib.error

    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    # Clear the dots line
                    if dots > 0:
                        print()
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

        # Print progress dots
        if dots == 0:
            print(f"    Waiting for {name}", end="", flush=True)
        print(".", end="", flush=True)
        dots += 1
        time.sleep(5)

    if dots > 0:
        print()
    return False


def verify_inference(port: int, model_name: str) -> tuple[bool, str]:
    """Send a test chat completion to verify inference works."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": model_name,
        "messages": [{"role": "user", "content": "Say 'hello' and nothing else."}],
        "max_tokens": 20,
        "temperature": 0.1,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return bool(content), content.strip()[:100]
    except Exception as e:
        return False, str(e)[:100]


# ---------------------------------------------------------------------------
# Interactive Wizard
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "", choices: list[str] = None) -> str:
    """Ask the user a question with optional default and validation."""
    if choices:
        choice_str = "/".join(choices)
        full_prompt = f"  {bold('?')} {prompt} [{choice_str}]"
    elif default:
        full_prompt = f"  {bold('?')} {prompt} [{default}]"
    else:
        full_prompt = f"  {bold('?')} {prompt}"

    while True:
        try:
            answer = input(f"{full_prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)

        if not answer and default:
            return default
        if choices and answer.lower() not in [c.lower() for c in choices]:
            print(f"    {yellow('Choose one of:')} {', '.join(choices)}")
            continue
        if answer:
            return answer.lower() if choices else answer
        if not default and not choices:
            return ""


def print_banner():
    print()
    print(bold("  ============================================================"))
    print(bold("    AitherOS Inference Setup Wizard"))
    print(dim("    vLLM containers / Ollama / any GPU or CPU"))
    print(bold("  ============================================================"))
    print()
    print(f"  {dim('When you are not gaming, your agents can be earning money.')}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TOTAL_STEPS = 7


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AitherOS vLLM Setup Wizard — interactive inference container setup",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--tier", type=str, choices=["nano", "lite", "standard", "full", "ollama"],
                        help="Skip tier selection, use this tier directly")
    parser.add_argument("--generate", action="store_true",
                        help="Generate compose file only, don't start containers")
    parser.add_argument("--hf-token", type=str, default="",
                        help="HuggingFace token for gated models")
    parser.add_argument("--output", type=str, default="docker-compose.vllm.yml",
                        help="Output compose file path (default: docker-compose.vllm.yml)")
    args = parser.parse_args()

    dry_run: bool = args.dry_run
    output_path = Path(args.output)

    print_banner()

    if dry_run:
        print(f"  {yellow('DRY RUN — no changes will be made')}\n")

    # ── Step 1: Detect GPU ────────────────────────────────────────────
    step(1, TOTAL_STEPS, "Detecting GPU hardware")
    gpu = detect_gpu()

    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0

    if gpu.vendor == "none":
        warn("No GPU detected — will use Ollama with CPU inference")
        warn("This works but is slower. Consider a cloud API for better performance.")
    elif gpu.vendor == "nvidia":
        if gpu.gpu_count > 1 and gpu.all_gpus:
            info(f"GPUs: {bold(str(gpu.gpu_count))} detected")
            for i, g in enumerate(gpu.all_gpus):
                g_vram = g['vram_mb'] / 1024
                info(f"  GPU {i}: {g['name']} ({g_vram:.0f}GB)")
            total_gb = gpu.total_vram_mb / 1024 if gpu.total_vram_mb else vram_gb
            info(f"Best GPU: {bold(gpu.name)} ({vram_gb:.0f}GB)")
            info(f"Total VRAM: {bold(f'{total_gb:.0f}GB')}")
        else:
            info(f"GPU: {bold(gpu.name)}")
            info(f"VRAM: {bold(f'{vram_gb:.0f}GB')}")
        if gpu.cuda_version:
            info(f"CUDA: {gpu.cuda_version}")
        if gpu.driver_version:
            info(f"Driver: {gpu.driver_version}")
        if gpu.compute_capability:
            info(f"Compute: sm_{gpu.compute_capability.replace('.', '')}")
    elif gpu.vendor == "amd":
        info(f"GPU: {bold(gpu.name)} (AMD)")
        if vram_gb:
            info(f"VRAM: {bold(f'{vram_gb:.0f}GB')}")
        warn("AMD GPUs work with Ollama. vLLM requires NVIDIA CUDA.")
    elif gpu.vendor == "apple":
        info(f"GPU: {bold(gpu.name)} (Apple Silicon)")
        if vram_gb:
            info(f"Unified memory: {bold(f'{vram_gb:.0f}GB')}")
        warn("Apple Silicon works great with Ollama. vLLM requires NVIDIA CUDA.")

    # ── Step 2: Check Docker ──────────────────────────────────────────
    step(2, TOTAL_STEPS, "Checking Docker + Ollama")

    docker_ok = False
    docker_info = ""
    ollama_ok = False

    # Check Docker
    docker_ok, docker_info = check_docker()
    if docker_ok:
        info(f"Docker: {docker_info}")
    else:
        warn(f"Docker: {docker_info}")
        if gpu.vendor == "nvidia" and vram_gb >= 6:
            print()
            print(f"  Docker is needed for vLLM. Install from:")
            print(f"    {cyan('https://docker.com/products/docker-desktop')}")
            if sys.platform == "win32":
                print(f"    Windows: Docker Desktop + WSL2 + NVIDIA driver >= 525.60")
            elif sys.platform == "linux":
                print(f"    Linux: sudo apt install nvidia-container-toolkit && sudo systemctl restart docker")
            print()

    # Check Ollama
    ollama_path = shutil.which("ollama")
    if ollama_path:
        out = _run_cmd(["ollama", "list"])
        if out is not None:
            model_count = max(0, len(out.strip().split("\n")) - 1)
            ollama_ok = True
            info(f"Ollama: installed ({model_count} models)")
        else:
            info(f"Ollama: installed (not running)")
            ollama_ok = True
    else:
        warn("Ollama: not installed")
        print(f"    Install: {cyan('https://ollama.com/download')}")

    # Check PicoLM
    picolm_path = shutil.which("picolm")
    if picolm_path:
        info(f"PicoLM: installed")

    if not docker_ok and not ollama_ok:
        error("Need either Docker (for vLLM) or Ollama. Install one and retry.")
        return 1

    # ── Step 3: Select Tier ───────────────────────────────────────────
    step(3, TOTAL_STEPS, "Selecting inference tier")

    recommended = recommend_tier(gpu)
    if not recommended:
        recommended = "ollama"  # Fallback

    # Force ollama if no Docker or non-NVIDIA
    if not docker_ok and gpu.vendor == "nvidia":
        warn("Docker not available — falling back to Ollama")
        recommended = "ollama"
    elif gpu.vendor != "nvidia" and gpu.vendor != "none":
        info(f"Non-NVIDIA GPU — using Ollama (vLLM needs CUDA)")
        recommended = "ollama"

    if args.tier:
        tier_id = args.tier
        if tier_id != "ollama":
            tier_vram = sum(w.vram_estimate_gb for w in TIERS[tier_id]["workers"])
            if vram_gb * 0.85 < tier_vram:
                warn(f"Tier '{tier_id}' needs ~{tier_vram:.0f}GB VRAM but you have {vram_gb:.0f}GB")
                warn("This may cause out-of-memory errors. Proceed with caution.")
        info(f"Using tier: {bold(tier_id)} (from --tier flag)")
    else:
        # Show available tiers
        available = {}
        for tid, tier in TIERS.items():
            if tid == "ollama":
                available[tid] = tier
            elif docker_ok and gpu.vendor == "nvidia" and vram_gb * 0.85 >= tier["min_vram_gb"]:
                available[tid] = tier

        if len(available) <= 1:
            # Only ollama fits
            tier_id = "ollama"
            info(f"Using: {bold('ollama')} (best option for your hardware)")
        else:
            print_tier_comparison(gpu)
            info(f"Recommended for your GPU: {bold(recommended)}")
            tier_id = ask("Select tier", default=recommended, choices=list(TIERS.keys()))

    tier = TIERS[tier_id]
    info(f"Tier: {bold(tier['name'])} — {tier['description']}")

    # ── Ollama path ───────────────────────────────────────────────────
    if tier_id == "ollama":
        step(4, TOTAL_STEPS, "Selecting models")
        selected_models = pick_ollama_models(gpu)
        if not selected_models:
            warn("No models selected")
            return 0

        info(f"Selected: {', '.join(selected_models)}")

        step(5, TOTAL_STEPS, "Installing models")
        install_ollama_models(selected_models, dry_run=dry_run)

        step(6, TOTAL_STEPS, "Checking PicoLM")
        picolm_path = shutil.which("picolm")
        if picolm_path:
            info("PicoLM available for ultra-lightweight inference")
        else:
            print(f"    {dim('Optional: PicoLM for tiny models on CPU')}")
            print(f"    {dim('Install: pip install picolm')}")

        step(7, TOTAL_STEPS, "Verification")
        if not dry_run:
            ollama_running = _run_cmd(["ollama", "list"])
            if ollama_running:
                info(f"Ollama: {green('running')}")
            else:
                warn("Ollama not running — start with: ollama serve")

        # Ollama summary
        chat_model = next((m for m in selected_models
                          if any(m.startswith(p) for p in ["llama", "qwen", "gemma"])), None)
        print()
        print(bold("  ============================================================"))
        print()
        print(f"  {bold('Models installed:')}")
        for m in selected_models:
            print(f"    {green('*')} {m}")
        print()
        print(f"  {bold('Use with AitherADK:')}")
        print(f"    {cyan('export AITHER_LLM_BACKEND=ollama')}")
        if chat_model:
            print(f"    {cyan(f'export AITHER_MODEL={chat_model}')}")
        print(f"    {cyan('aither-serve --port 8080')}")
        print()
        print(f"  {bold('Quick test:')}")
        if chat_model:
            # Same PEP 701 backslash rule as adk/deploy.py; keep it 3.10-parseable.
            _try = f'ollama run {chat_model} "hello"'
            print(f"    {cyan(_try)}")
        print()
        print(f"  {green(bold('Ready!'))}")
        print()
        return 0

    # ── vLLM path continues below ────────────────────────────────────

    total_download = sum(w.download_size_gb for w in tier["workers"])
    total_vram = sum(w.vram_estimate_gb for w in tier["workers"])
    info(f"Total download: ~{total_download:.0f}GB (first run only, cached after)")
    info(f"VRAM usage: ~{total_vram:.0f}GB / {vram_gb:.0f}GB available")

    # ── Step 4: HuggingFace Token ─────────────────────────────────────
    step(4, TOTAL_STEPS, "Checking HuggingFace access")

    hf_token = args.hf_token or os.getenv("HF_TOKEN", "")
    if not hf_token:
        print()
        print(f"  Some models require a HuggingFace token (free account).")
        print(f"  Get one at: {cyan('https://huggingface.co/settings/tokens')}")
        print(f"  Nemotron-Orchestrator-8B requires accepting the license at:")
        print(f"    {cyan('https://huggingface.co/nvidia/Nemotron-Orchestrator-8B')}")
        print()
        hf_token = ask("HuggingFace token (or press Enter to skip)", default="")

    if hf_token:
        info(f"Token: {hf_token[:8]}...{hf_token[-4:]}")
    else:
        warn("No HF token — some gated models may fail to download")
        warn("Set HF_TOKEN env var or pass --hf-token next time")

    # ── Step 5: Generate Compose ──────────────────────────────────────
    step(5, TOTAL_STEPS, "Generating docker-compose.vllm.yml")

    compose_content = generate_compose(tier_id, hf_token)

    if dry_run:
        info(f"Would write to: {output_path}")
        print()
        for line in compose_content.split("\n")[:30]:
            print(f"    {dim(line)}")
        print(f"    {dim('...')}")
    else:
        output_path.write_text(compose_content)
        info(f"Written to: {bold(str(output_path))}")

    if args.generate:
        print(f"\n  {green(bold('Done!'))} Compose file generated.")
        print(f"  Start with: {cyan(f'docker compose -f {output_path} up -d')}")
        return 0

    # ── Step 6: Start Containers ──────────────────────────────────────
    step(6, TOTAL_STEPS, "Starting vLLM containers")

    if not dry_run:
        print()
        print(f"  {bold('This will:')}")
        print(f"    1. Pull the vllm/vllm-openai Docker image (~8GB)")
        print(f"    2. Start {len(tier['workers'])} container(s)")
        print(f"    3. Download model weights (~{total_download:.0f}GB on first run)")
        print(f"    4. Initialize inference engines (~60s after download)")
        print()

        proceed = ask("Start containers now?", default="yes", choices=["yes", "no"])
        if proceed != "yes":
            print(f"\n  Start later with: {cyan(f'docker compose -f {output_path} up -d')}")
            return 0

    if not start_containers(output_path, dry_run=dry_run):
        return 1

    info("Containers started")

    # ── Step 7: Verify ────────────────────────────────────────────────
    step(7, TOTAL_STEPS, "Verifying inference")

    if dry_run:
        info("Would verify each worker's health endpoint")
        info("Would send a test chat completion")
    else:
        all_ok = True
        for w in tier["workers"]:
            print(f"\n  {bold(w.name)} ({w.model})")
            info(f"Port: {w.port}")

            # Wait for health
            healthy = wait_for_health(w.port, w.name, timeout=600)
            if healthy:
                info(f"Health: {green('OK')}")
            else:
                warn(f"Health: {yellow('not ready yet')} — model may still be downloading")
                warn(f"Check: docker logs aither-vllm-{w.name} --tail 20")
                all_ok = False
                continue

            # Test inference (skip for embeddings)
            if w.name != "embeddings":
                ok, response = verify_inference(w.port, w.served_name)
                if ok:
                    info(f"Inference: {green('OK')} — \"{response}\"")
                else:
                    warn(f"Inference: {yellow('failed')} — {response}")
                    all_ok = False

    # ── Summary ───────────────────────────────────────────────────────
    print()
    print(bold("  ============================================================"))
    print()

    workers = tier["workers"]
    for w in workers:
        print(f"  {green('*')} {bold(w.name)}: http://localhost:{w.port}")
        print(f"    Model: {w.model}")
        print(f"    Name:  {w.served_name}")
        print()

    # Print usage examples
    orch = workers[0]
    print(f"  {bold('Quick test:')}")
    print(f"    {cyan(f'curl http://localhost:{orch.port}/v1/models')}")
    print()
    print(f"  {bold('Chat:')}")
    print(f"""    {cyan(f"curl http://localhost:{orch.port}/v1/chat/completions")} \\""")
    print(f"""      {cyan(f'-H "Content-Type: application/json"')} \\""")
    # Nesting a triple-quoted f-string inside another with the SAME delimiter
    # is PEP 701 (3.12+). awdk declares requires-python ">=3.10", where this
    # was a SyntaxError -- so the module failed to import for anyone who
    # pip-installed it on the oldest Python we promise to support.
    _payload = json.dumps({
        "model": orch.served_name,
        "messages": [{"role": "user", "content": "hello"}],
    })
    print(f"      {cyan('-d ' + repr(_payload))}")
    print()

    # ADK integration
    print(f"  {bold('Use with AitherADK:')}")
    print(f"    {cyan('export AITHER_LLM_BACKEND=openai')}")
    print(f"    {cyan(f'export OPENAI_BASE_URL=http://localhost:{orch.port}/v1')}")
    print(f"    {cyan(f'export AITHER_MODEL={orch.served_name}')}")
    print(f"    {cyan('aither-serve --port 8080')}")
    print()

    if len(workers) > 1:
        reasoning = [w for w in workers if w.name == "reasoning"]
        if reasoning:
            r = reasoning[0]
            print(f"  {bold('Reasoning model (effort >= 7):')}")
            print(f"    {cyan(f'export AITHER_REASONING_MODEL={r.served_name}')}")
            print(f"    {cyan(f'export AITHER_REASONING_URL=http://localhost:{r.port}/v1')}")
            print()

    # Gateway integration
    print(f"  {bold('Connect to AitherOS:')}")
    print(f"    {cyan('pip install awdk')}")
    # PEP 701 again: a backslash in a replacement field is 3.12+.
    _snip = 'python -c "from adk.federation import FederationClient; ...'
    print(f"    {cyan(_snip)}")
    print(f"    See: {cyan('examples/federation_demo.py')}")
    print()

    # Gaming mode hint
    if tier_id in ("standard", "full"):
        print(f"  {bold('Gaming mode:')}")
        print(f"  {dim('Free VRAM for games by sleeping inference containers:')}")
        print(f"    {cyan(f'docker compose -f {output_path} stop')}")
        print(f"  {dim('Resume when done:')}")
        print(f"    {cyan(f'docker compose -f {output_path} start')}")
        print()

    # Manage
    print(f"  {bold('Manage:')}")
    print(f"    {dim('Logs:')}   {cyan(f'docker compose -f {output_path} logs -f')}")
    print(f"    {dim('Stop:')}   {cyan(f'docker compose -f {output_path} stop')}")
    print(f"    {dim('Remove:')} {cyan(f'docker compose -f {output_path} down')}")
    print(f"    {dim('Update:')} {cyan(f'docker compose -f {output_path} pull && docker compose -f {output_path} up -d')}")
    print()

    print(f"  {green(bold('Ready!'))}")
    print(f"  {dim('Your GPU is now an inference server.')}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
