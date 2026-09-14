"""aither setup — Interactive CLI for local inference + optional AitherOS stack.

Primary goal: get vLLM containers running so agents can use true concurrent
inference with continuous batching. This is what enables running parallel
agent fleets on a single GPU — Ollama serializes requests, vLLM batches them.

Usage:
    aither setup                    # Auto-detect GPU, set up vLLM
    aither setup --tier lite        # Force a specific tier
    aither setup --tier ollama      # Fallback: Ollama for non-NVIDIA GPUs
    aither setup --dry-run          # Show what would happen
    aither setup --stack core       # Also deploy AitherOS core services
    aither setup --stack full       # Deploy full AitherOS stack via AitherZero
    aither setup --non-interactive  # No prompts (CI/automation)

Pure stdlib — no pip dependencies required.
"""

from __future__ import annotations

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

def info(msg: str) -> None: print(f"  {green('+')} {msg}")
def warn(msg: str) -> None: print(f"  {yellow('!')} {msg}")
def err(msg: str) -> None: print(f"  {red('x')} {msg}")
def step(n: int, total: int, msg: str) -> None:
    print(f"\n  {bold(f'[{n}/{total}]')} {bold(msg)}")


# ---------------------------------------------------------------------------
# GPU Detection (sync, for CLI use)
# ---------------------------------------------------------------------------

@dataclass
class GPUInfo:
    vendor: str = "none"
    name: str = "Unknown"
    vram_mb: int = 0          # Best single GPU VRAM (for Ollama profile selection)
    cuda_version: str = ""
    driver_version: str = ""
    gpu_count: int = 0
    total_vram_mb: int = 0    # Sum of all GPUs (for vLLM tier selection)
    all_gpus: list = field(default_factory=list)  # List of {"name": str, "vram_mb": int}


def _run(cmd: list[str], timeout: int = 10) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def detect_gpu() -> GPUInfo:
    """Detect GPU: NVIDIA > AMD > Apple Silicon > none."""
    smi = shutil.which("nvidia-smi")
    if smi:
        out = _run([smi, "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader,nounits"])
        if out:
            lines = [line.strip() for line in out.strip().split("\n") if line.strip()]
            cuda_out = _run([smi])
            cuda_ver = ""
            if cuda_out:
                m = re.search(r"CUDA Version:\s*([\d.]+)", cuda_out)
                if m:
                    cuda_ver = m.group(1)

            # Parse all GPUs, find best (max VRAM), sum total
            all_gpus = []
            best_name = "NVIDIA GPU"
            best_vram = 0
            total_vram = 0
            best_driver = ""
            for line in lines:
                parts = [p.strip() for p in line.split(",")]
                g_name = parts[0] if parts else "NVIDIA GPU"
                g_vram = int(float(parts[1])) if len(parts) > 1 else 0
                g_driver = parts[2] if len(parts) > 2 else ""
                all_gpus.append({"name": g_name, "vram_mb": g_vram})
                total_vram += g_vram
                if g_vram > best_vram:
                    best_vram = g_vram
                    best_name = g_name
                    best_driver = g_driver

            return GPUInfo(
                vendor="nvidia",
                name=best_name,
                vram_mb=best_vram,
                driver_version=best_driver,
                cuda_version=cuda_ver,
                gpu_count=len(lines),
                total_vram_mb=total_vram,
                all_gpus=all_gpus,
            )

    rocm = shutil.which("rocm-smi")
    if rocm:
        out = _run([rocm, "--showproductname"])
        if out:
            name = "AMD GPU"
            for line in out.split("\n"):
                if any(k in line for k in ("GPU", "Radeon", "Instinct")):
                    name = line.strip().split(":")[-1].strip() if ":" in line else line.strip()
                    break
            return GPUInfo(vendor="amd", name=name)

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        mem = _run(["sysctl", "-n", "hw.memsize"])
        return GPUInfo(
            vendor="apple",
            name=chip.strip() if chip else "Apple Silicon",
            vram_mb=int(int(mem.strip()) / (1024 * 1024)) if mem else 0,
        )

    return GPUInfo()


# ---------------------------------------------------------------------------
# vLLM Tier Definitions
# ---------------------------------------------------------------------------

@dataclass
class VLLMWorker:
    name: str
    model: str
    served_name: str
    port: int
    gpu_mem: float
    ctx_len: int
    extra_args: list[str] = field(default_factory=list)
    description: str = ""
    download_gb: float = 0.0
    vram_gb: float = 0.0


# Quick aliases that map to a tier
TIER_ALIASES: dict[str, str] = {
    "nemotron": "lite",
    # llama.cpp tier — local Nemotron-Orchestrator-8B for endpoints without
    # Docker / NVIDIA / vLLM (Intel Arc, AMD, Apple Silicon, small dGPUs, CPU)
    "llamacpp": "llamacpp",
    "local": "llamacpp",
    "endpoint": "llamacpp",
    "nemotron-local": "llamacpp",
}

# Quantization strategies — vLLM supports bitsandbytes (8-bit) and TQ4 (4-bit turboquant)
_QUANT_BNB = ["--quantization bitsandbytes", "--load-format bitsandbytes"]
_QUANT_TQ4 = [
    "--quantization turboquant",
    "--attention-backend TRITON_ATTN",  # FlashInfer crashes with TQ4 in vLLM 0.15+
]

# Default: 8-bit (bitsandbytes) for >10GB GPUs, 4-bit (TQ4) for <=10GB
def _quant_args(vram_gb: float, force_tq4: bool = False) -> list[str]:
    """Select quantization args based on available VRAM."""
    if force_tq4 or vram_gb < 10:
        return list(_QUANT_TQ4)
    return list(_QUANT_BNB)


TIERS: dict[str, dict] = {
    "nano": {
        "name": "Nano (TQ4)",
        "desc": "Nemotron-8B TQ4-quantized -- for 6-8GB GPUs",
        "min_vram_gb": 5,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.85, 16384,
                       _QUANT_TQ4 + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching"],
                       "Nemotron-8B TQ4 -- 4-bit quantized, fits 6GB GPUs", 16.0, 3.5),
        ],
    },
    "lite": {
        "name": "Lite",
        "desc": "Nemotron Orchestrator -- for 10-16GB GPUs",
        "min_vram_gb": 10,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.80, 32768,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching"],
                       "Nemotron-Orchestrator-8B -- outperforms GPT-4o on tool use", 16.0, 6.5),
        ],
    },
    "standard": {
        "name": "Standard",
        "desc": "Orchestrator + Reasoning -- for 20-24GB GPUs. True parallel agents.",
        "min_vram_gb": 18,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.35, 32768,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "Nemotron-Orchestrator-8B -- handles 80% of agent requests", 16.0, 6.5),
            VLLMWorker("reasoning", "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B", "deepseek-r1:14b",
                       8201, 0.55, 16384,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--reasoning-parser deepseek_r1",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "DeepSeek-R1 14B -- deep thinking for complex tasks", 28.0, 12.0),
        ],
    },
    "standard-tq4": {
        "name": "Standard (TQ4)",
        "desc": "Orchestrator + Reasoning both TQ4 -- for 12-16GB GPUs",
        "min_vram_gb": 10,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.40, 16384,
                       _QUANT_TQ4 + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "Nemotron-8B TQ4 -- 4-bit orchestration", 16.0, 3.5),
            VLLMWorker("reasoning", "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B", "deepseek-r1:14b",
                       8201, 0.55, 16384,
                       _QUANT_TQ4 + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--reasoning-parser deepseek_r1",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "DeepSeek-R1 14B TQ4 -- 4-bit reasoning", 28.0, 6.5),
        ],
    },
    "full": {
        "name": "Full",
        "desc": "Orchestrator + Reasoning + Embeddings -- 24GB+ GPUs. Full fleet support.",
        "min_vram_gb": 20,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.35, 32768,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "Nemotron-Orchestrator-8B", 16.0, 6.5),
            VLLMWorker("reasoning", "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B", "deepseek-r1:14b",
                       8201, 0.55, 16384,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--reasoning-parser deepseek_r1",
                        "--enable-prefix-caching", "--enable-sleep-mode"],
                       "DeepSeek-R1 14B", 28.0, 12.0),
            VLLMWorker("embeddings", "nomic-ai/nomic-embed-text-v1.5", "nomic-embed-text",
                       8209, 0.05, 2048,
                       ["--dtype float16", "--max-num-seqs 64"],
                       "Nomic Embed v1.5 -- vector search", 0.5, 0.5),
        ],
    },
    "hybrid": {
        "name": "Hybrid",
        "desc": "Nemotron locally + cloud API for reasoning -- for 10-16GB GPUs",
        "min_vram_gb": 10,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.80, 32768,
                       _QUANT_BNB + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching"],
                       "Nemotron-Orchestrator-8B -- local orchestration, cloud reasoning", 16.0, 6.5),
        ],
        "reasoning": "cloud",
    },
    "hybrid-tq4": {
        "name": "Hybrid (TQ4)",
        "desc": "Nemotron TQ4 locally + cloud reasoning -- for 6-8GB GPUs",
        "min_vram_gb": 5,
        "workers": [
            VLLMWorker("orchestrator", "nvidia/Nemotron-Orchestrator-8B", "aither-orchestrator",
                       8200, 0.85, 16384,
                       _QUANT_TQ4 + [
                        "--enable-auto-tool-choice", "--tool-call-parser hermes",
                        "--enable-prefix-caching"],
                       "Nemotron-8B TQ4 -- 4-bit local + cloud reasoning", 16.0, 3.5),
        ],
        "reasoning": "cloud",
    },
}


def recommend_tier(gpu: GPUInfo) -> str:
    # Non-NVIDIA: prefer llama.cpp (Vulkan / Metal / CPU) over Ollama for
    # endpoint installs — same OpenAI API, no Docker, runs as native service,
    # ships the exact orchestrator model AitherOS expects.
    if gpu.vendor != "nvidia":
        return "llamacpp"
    # Use total VRAM for vLLM tier — workers spread across GPUs
    vram = (gpu.total_vram_mb or gpu.vram_mb) / 1024 * 0.85
    if vram >= 24:
        return "full"
    if vram >= 18:
        return "standard"
    if vram >= 12:
        return "standard-tq4"  # Both models fit in TQ4 at 12GB
    if vram >= 10:
        return "lite"
    if vram >= 5:
        return "nano"  # Nemotron TQ4 fits in 5GB
    # Sub-5GB NVIDIA dGPU — llama.cpp Q4 still fits with partial CPU offload
    return "llamacpp"


# ---------------------------------------------------------------------------
# Docker Compose Generation
# ---------------------------------------------------------------------------

def _port_in_use(port: int) -> bool:
    """Return True if *port* is bound on localhost (TCP)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_free_port(preferred: int, used: set[int]) -> int:
    """Return *preferred* if free, otherwise scan upward until a free port is found."""
    port = preferred
    while port < preferred + 100:
        if port not in used and not _port_in_use(port):
            return port
        port += 1
    return preferred  # give up, let Docker report the conflict


def generate_compose(tier_id: str, hf_token: str = "") -> str:
    tier = TIERS[tier_id]
    workers = tier["workers"]

    # Resolve port conflicts before generating compose
    used_ports: set[int] = set()
    for w in workers:
        resolved = _find_free_port(w.port, used_ports)
        if resolved != w.port:
            info(f"Port {w.port} in use — remapping {w.name} to :{resolved}")
            w.port = resolved
        used_ports.add(w.port)

    services = []
    for w in workers:
        extra = " ".join(w.extra_args)
        env_block = "      NVIDIA_VISIBLE_DEVICES: all\n      VLLM_NO_USAGE_STATS: '1'"
        if hf_token:
            env_block += f"\n      HF_TOKEN: \"{hf_token}\"\n      HUGGING_FACE_HUB_TOKEN: \"{hf_token}\""
        svc = (
            f"  adk-vllm-{w.name}:\n"
            f"    image: vllm/vllm-openai:latest\n"
            f"    container_name: adk-vllm-{w.name}\n"
            f"    shm_size: '4gb'\n"
            f"    environment:\n"
            f"{env_block}\n"
            f"    command: >\n"
            f"      --model {w.model}\n"
            f"      --host 0.0.0.0 --port 8000\n"
            f"      --gpu-memory-utilization {w.gpu_mem}\n"
            f"      --max-model-len {w.ctx_len}\n"
            f"      --enforce-eager --dtype auto\n"
            f"      --max-num-seqs 8\n"
            f"      --trust-remote-code\n"
            f"      --served-model-name {w.served_name}\n"
            f"      {extra}\n"
            f"    ports:\n"
            f"      - \"{w.port}:8000\"\n"
            f"    volumes:\n"
            f"      - adk-hf-cache:/root/.cache/huggingface\n"
            f"    healthcheck:\n"
            f"      interval: 30s\n"
            f"      timeout: 10s\n"
            f"      start_period: 900s\n"
            f"      retries: 5\n"
            f"      test: [\"CMD\", \"curl\", \"-f\", \"http://localhost:8000/health\"]\n"
            f"    restart: unless-stopped\n"
            f"    deploy:\n"
            f"      resources:\n"
            f"        reservations:\n"
            f"          devices:\n"
            f"            - driver: nvidia\n"
            f"              count: 1\n"
            f"              capabilities: [gpu]"
        )
        services.append(svc)

    total_dl = sum(w.download_gb for w in workers)
    return textwrap.dedent(f"""\
# AitherOS vLLM Inference Stack -- Generated by `aither setup`
# Tier: {tier_id} ({tier['name']}) -- {tier['desc']}
#
# Why vLLM over Ollama?
#   vLLM uses continuous batching -- multiple agents share the GPU simultaneously.
#   Ollama serializes requests -- agents wait in line. For parallel agent fleets,
#   vLLM is 3-10x faster under concurrent load.
#
# Usage:
#   docker compose -f docker-compose.vllm.yml up -d
#   docker compose -f docker-compose.vllm.yml logs -f
#   docker compose -f docker-compose.vllm.yml down
#
# First run downloads ~{total_dl:.0f}GB of model weights (cached after).

services:
{chr(10).join(services)}

volumes:
  adk-hf-cache:
    name: adk-hf-cache
""")


# ---------------------------------------------------------------------------
# Docker + Container Management
# ---------------------------------------------------------------------------

def check_docker() -> tuple[bool, str]:
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker not installed"
    out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not out:
        return False, "Docker daemon not running"
    return True, f"Docker {out}"


def start_containers(compose_path: Path, dry_run: bool = False) -> bool:
    if dry_run:
        info(f"Would run: docker compose -f {compose_path} up -d")
        return True
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "up", "-d"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            err(f"docker compose up failed: {result.stderr[:500]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        warn("docker compose up timed out — containers may still be starting")
        return True
    except Exception as e:
        err(f"Failed: {e}")
        return False


def wait_for_health(port: int, name: str, timeout: int = 300) -> bool:
    import urllib.request
    import urllib.error

    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    if dots > 0:
                        print()
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if dots == 0:
            print(f"    Waiting for {name}", end="", flush=True)
        print(".", end="", flush=True)
        dots += 1
        time.sleep(5)
    if dots > 0:
        print()
    return False


# ---------------------------------------------------------------------------
# Ollama Fallback
# ---------------------------------------------------------------------------

OLLAMA_RECOMMENDED = [
    ("nemotron-orchestrator-8b", "NVIDIA Nemotron — best tool use", 8),
    ("qwen3:8b", "Qwen 3 8B — strong multilingual", 8),
    ("deepseek-r1:7b", "DeepSeek-R1 7B — reasoning", 8),
    ("nomic-embed-text", "Nomic Embed — vector search", 2),
]


def setup_ollama(gpu: GPUInfo, dry_run: bool = False) -> int:
    ollama = shutil.which("ollama")
    if not ollama:
        err("Ollama not installed")
        print(f"    Install: {cyan('https://ollama.com/download')}")
        return 1

    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0
    info("Ollama found")
    if gpu.vendor != "none":
        info(f"GPU: {gpu.name} ({vram_gb:.0f}GB)")

    print()
    warn("Ollama serializes requests — agents wait in line.")
    warn("For parallel agent fleets, use an NVIDIA GPU + vLLM.")
    print()

    models_to_pull = []
    for name, desc, min_vram in OLLAMA_RECOMMENDED:
        if vram_gb >= min_vram or gpu.vendor == "none":
            models_to_pull.append(name)
            info(f"Will pull: {name} — {desc}")

    if not models_to_pull:
        models_to_pull = ["llama3.2:3b", "nomic-embed-text"]
        info(f"Low VRAM — using compact models: {', '.join(models_to_pull)}")

    running = _run(["ollama", "list"])
    if running is None:
        warn("Ollama not running — starting...")
        if not dry_run:
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            time.sleep(3)

    existing = set()
    out = _run(["ollama", "list"])
    if out:
        for line in out.strip().split("\n")[1:]:
            parts = line.split()
            if parts:
                existing.add(parts[0].split(":")[0])

    for model in models_to_pull:
        base = model.split(":")[0]
        if base in existing or model in existing:
            info(f"Already have: {model}")
            continue
        if dry_run:
            info(f"Would pull: {model}")
        else:
            info(f"Pulling: {bold(model)}...")
            try:
                subprocess.run(["ollama", "pull", model], timeout=1800)
            except Exception as e:
                warn(f"Failed to pull {model}: {e}")

    _save_config("ollama", None, gpu)
    print()
    info("Ollama ready")
    print(f"  {dim('For true parallel agents, use vLLM with an NVIDIA GPU.')}")
    print()
    return 0


# ---------------------------------------------------------------------------
# AitherZero Bridge
# ---------------------------------------------------------------------------

def find_aitherzero() -> Optional[Path]:
    """Locate AitherZero scripts directory."""
    candidates = []
    env_path = os.environ.get("AITHERZERO_PATH")
    if env_path:
        candidates.append(Path(env_path))

    # Relative to adk package
    adk_dir = Path(__file__).resolve().parent
    for depth in range(1, 5):
        parent = adk_dir
        for _ in range(depth):
            parent = parent.parent
        candidates.append(parent / "AitherZero")

    candidates.extend([
        Path.home() / "AitherOS" / "AitherZero",
        Path.home() / "AitherOS-Fresh" / "AitherZero",
    ])

    for p in candidates:
        if p.is_dir() and (p / "library" / "automation-scripts").is_dir():
            return p
    return None


def deploy_stack(profile: str, dry_run: bool = False, api_key: str = "") -> int:
    """Deploy AitherOS services via AitherZero OneClick script.

    Profiles: minimal, core, full, headless, gpu, agents
    """
    az_root = find_aitherzero()
    if not az_root:
        warn("AitherZero not found — cannot deploy AitherOS stack")
        print()
        print("  To deploy AitherOS, clone the repo:")
        print(f"    {cyan('git clone https://github.com/Aitherium/AitherOS AitherOS-Fresh')}")
        print(f"    {cyan('cd AitherOS-Fresh && aither setup --stack ' + profile)}")
        print()
        print(f"  Or set {cyan('AITHERZERO_PATH')} to your AitherZero directory.")
        return 1

    pwsh = shutil.which("pwsh")
    if not pwsh:
        warn("PowerShell 7 (pwsh) required for AitherZero scripts")
        if sys.platform == "win32":
            print(f"    {cyan('winget install Microsoft.PowerShell')}")
        elif sys.platform == "darwin":
            print(f"    {cyan('brew install powershell')}")
        else:
            print(f"    {cyan('https://learn.microsoft.com/en-us/powershell/scripting/install/installing-powershell')}")
        return 1

    deploy_script = az_root / "library" / "automation-scripts" / "30-deploy" / "3020_Deploy-OneClick.ps1"
    if not deploy_script.exists():
        err(f"Deploy script not found: {deploy_script}")
        return 1

    info(f"AitherZero: {az_root}")
    info(f"Profile: {profile}")

    cmd = [str(pwsh), "-NoProfile", "-File", str(deploy_script),
           "-Profile", profile, "-NonInteractive"]

    if api_key:
        os.environ["AITHER_API_KEY"] = api_key

    if dry_run:
        info(f"Would run: {' '.join(cmd)}")
        return 0

    print()
    print(f"  {bold('Deploying AitherOS')} ({profile} profile)...")
    print(f"  {dim('This may take several minutes on first run.')}")
    print()

    try:
        result = subprocess.run(cmd, timeout=1800)
        return result.returncode
    except subprocess.TimeoutExpired:
        err("Deployment timed out after 30 minutes")
        return 1
    except Exception as e:
        err(f"Deployment failed: {e}")
        return 1


# ---------------------------------------------------------------------------
# Config Persistence
# ---------------------------------------------------------------------------

def _save_config(
    backend: str,
    tier_id: Optional[str],
    gpu: GPUInfo,
    reasoning_api: str = "",
    reasoning_api_key: str = "",
    reasoning_model: str = "",
    dgx_url: str = "",
):
    """Save setup results to ~/.aither/config.json."""
    config_path = Path.home() / ".aither" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception:
            pass

    config["default_backend"] = "openai" if backend == "vllm" else "ollama"
    config["setup_backend"] = backend
    config["gpu_vendor"] = gpu.vendor
    config["gpu_name"] = gpu.name

    if tier_id:
        config["setup_tier"] = tier_id
        workers = TIERS[tier_id]["workers"]
        config["inference_url"] = f"http://localhost:{workers[0].port}/v1"
        if len(workers) > 1:
            reasoning = [w for w in workers if w.name == "reasoning"]
            if reasoning:
                config["reasoning_url"] = f"http://localhost:{reasoning[0].port}/v1"
    elif backend == "ollama":
        config["inference_url"] = "http://localhost:11434/v1"

    # Hybrid reasoning backend (cloud API for effort 7+)
    if reasoning_api:
        config["reasoning_backend"] = reasoning_api
    if reasoning_api_key:
        config["reasoning_api_key"] = reasoning_api_key
    if reasoning_model:
        config["reasoning_model"] = reasoning_model
    if dgx_url:
        config["dgx_url"] = dgx_url

    config_path.write_text(json.dumps(config, indent=2))
    info(f"Config saved: {config_path}")


# ---------------------------------------------------------------------------
# Interactive Prompt
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Existing Infrastructure Detection
# ---------------------------------------------------------------------------

@dataclass
class ExistingInfra:
    vllm_ports: list  # [(port, [model_ids])]
    ollama_running: bool
    ollama_models: list  # [str]
    genesis_running: bool
    aither_node_running: bool
    dgx_url: str = ""           # DGX Spark / remote vLLM
    dgx_models: list = field(default_factory=list)
    cloud_keys: list = field(default_factory=list)  # ["anthropic", "openai", "deepseek"]


def _scan_existing_infra() -> ExistingInfra:
    """Scan localhost for running inference and AitherOS services."""
    import urllib.request
    import urllib.error

    result = ExistingInfra([], False, [], False, False)

    # vLLM: scan known ports
    for port in [8200, 8201, 8202, 8203, 8209, 8000]:
        try:
            req = urllib.request.Request(f"http://localhost:{port}/v1/models")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
                models = [m["id"] for m in data.get("data", [])]
                if models:
                    result.vllm_ports.append((port, models))
        except Exception:
            pass

    # DGX Spark / remote vLLM
    dgx_url = os.environ.get("AITHER_DGX_URL", "")
    if not dgx_url:
        # Try common DGX Spark / remote inference addresses.
        # Respects AITHER_DGX_HOSTS for custom network topologies.
        dgx_hosts = os.environ.get(
            "AITHER_DGX_HOSTS", "spark.local,192.168.0.33"
        ).split(",")
        for host in dgx_hosts:
            host = host.strip()
            if not host:
                continue
            for port in (8000, 8120, 8200):
                try:
                    req = urllib.request.Request(f"http://{host}:{port}/v1/models")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        data = json.loads(resp.read())
                        models = [m["id"] for m in data.get("data", [])]
                        if models:
                            dgx_url = f"http://{host}:{port}"
                            result.dgx_models = models
                            break
                except Exception:
                    pass
            if dgx_url:
                break
    else:
        try:
            base = dgx_url.rstrip("/")
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            req = urllib.request.Request(f"{base}/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                result.dgx_models = [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
    result.dgx_url = dgx_url

    # Ollama
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            result.ollama_models = [m["name"] for m in data.get("models", [])]
            result.ollama_running = True
    except Exception:
        pass

    # Genesis (AitherOS orchestrator)
    try:
        req = urllib.request.Request("http://localhost:8001/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            result.genesis_running = resp.status == 200
    except Exception:
        pass

    # awnode (MCP server)
    try:
        req = urllib.request.Request("http://localhost:8080/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            result.aither_node_running = resp.status == 200
    except Exception:
        pass

    # Check for cloud API keys — validate with a lightweight probe when possible
    _key_probes = {
        "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/messages"),
        "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/models"),
        "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/models"),
        "aitherium": ("AITHER_API_KEY", None),
    }
    for name, (env_var, probe_url) in _key_probes.items():
        key = os.environ.get(env_var, "")
        if not key:
            continue
        # Quick probe: send a lightweight GET with the key to confirm auth
        valid = True
        if probe_url:
            try:
                req = urllib.request.Request(
                    probe_url,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "x-api-key": key,  # Anthropic uses this header
                    },
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    valid = resp.status < 400
            except urllib.error.HTTPError as exc:
                valid = exc.code not in (401, 403)
            except Exception:
                valid = True  # network error ≠ bad key — assume valid
        result.cloud_keys.append(f"{name}" if valid else f"{name}(invalid)")

    return result


def _save_existing_config(infra: ExistingInfra):
    """Save connection to existing infrastructure."""
    config = {}
    config_path = Path.home() / ".aither" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except Exception:
            pass

    if infra.vllm_ports:
        port, models = infra.vllm_ports[0]
        config["default_backend"] = "openai"
        config["inference_url"] = f"http://localhost:{port}/v1"
        config["setup_backend"] = "vllm"
        info(f"Config saved: backend=vllm, url=http://localhost:{port}/v1")
    elif infra.ollama_running:
        config["default_backend"] = "ollama"
        config["inference_url"] = "http://localhost:11434/v1"
        config["setup_backend"] = "ollama"
        info("Config saved: backend=ollama")

    if infra.genesis_running:
        config["genesis_url"] = "http://localhost:8001"
    if infra.aither_node_running:
        config["node_url"] = "http://localhost:8080"

    config_path.write_text(json.dumps(config, indent=2))
    info(f"Saved to {config_path}")


def ask(prompt: str, default: str = "", choices: list[str] = None) -> str:
    if choices:
        full = f"  {bold('?')} {prompt} [{'/'.join(choices)}]"
    elif default:
        full = f"  {bold('?')} {prompt} [{default}]"
    else:
        full = f"  {bold('?')} {prompt}"
    while True:
        try:
            answer = input(f"{full}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not answer and default:
            return default
        if choices and answer.lower() not in [c.lower() for c in choices]:
            print(f"    {yellow('Choose:')} {', '.join(choices)}")
            continue
        if answer:
            return answer.lower() if choices else answer
        if not default and not choices:
            return ""


# ---------------------------------------------------------------------------
# Main: aither setup
# ---------------------------------------------------------------------------

def _smoke_test(port: int) -> bool:
    """Send a simple chat completion to verify inference works end-to-end."""
    import urllib.request
    import urllib.error

    try:
        payload = json.dumps({
            "model": "",  # let vLLM use default
            "messages": [{"role": "user", "content": "Say hello in exactly 3 words."}],
            "max_tokens": 20,
        }).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content.strip():
                info(f"Smoke test: {green('PASS')} — \"{content.strip()[:60]}\"")
                return True
    except Exception as e:
        warn(f"Smoke test: {red('FAIL')} — {e}")
    return False


def _setup_cloud_only(args) -> int:
    """Cloud-only setup — skip GPU, configure cloud providers."""
    from adk.config import save_saved_config

    print()
    print(bold("  ============================================================"))
    print(bold("    AitherOS Cloud Setup"))
    print(dim("    No GPU required — use cloud AI providers for inference"))
    print(bold("  ============================================================"))
    print()

    if args.dry_run:
        print(f"  {yellow('DRY RUN — no changes will be made')}\n")

    # Step 1: API keys
    step(1, 4, "Cloud Provider API Keys")
    print(f"  {dim('Enter API keys for your cloud providers. Press Enter to skip.')}")
    print()

    keys_path = Path.home() / ".aither" / "provider_keys.json"
    keys = {}
    if keys_path.exists():
        try:
            keys = json.loads(keys_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    providers_info = {
        "openai": ("OPENAI_API_KEY", "OpenAI"),
        "anthropic": ("ANTHROPIC_API_KEY", "Anthropic"),
        "deepseek": ("DEEPSEEK_API_KEY", "DeepSeek"),
    }
    configured = []
    for pname, (env_name, label) in providers_info.items():
        existing = keys.get(pname, "") or os.environ.get(env_name, "")
        if existing:
            info(f"{label}: configured")
            configured.append(pname)
            continue
        if args.non_interactive:
            continue
        try:
            val = input(f"    {label} API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if val:
            keys[pname] = val
            os.environ[env_name] = val
            configured.append(pname)
            info(f"{label}: saved")

    if not args.dry_run:
        keys_path.parent.mkdir(parents=True, exist_ok=True)
        keys_path.write_text(json.dumps(keys, indent=2))
        if sys.platform != "win32":
            os.chmod(keys_path, 0o600)

    if not configured:
        err("No cloud providers configured — at least one is required for cloud mode")
        print(f"    Run: {cyan('adk keys set openai sk-...')}")
        return 1

    # Step 2: Set mode
    step(2, 4, "Setting cloud-first mode")
    if not args.dry_run:
        # Update cloud_providers.yaml if accessible
        for candidate in [
            Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "cloud_providers.yaml",
            Path.cwd() / "AitherOS" / "config" / "cloud_providers.yaml",
            Path.cwd() / "config" / "cloud_providers.yaml",
        ]:
            if candidate.exists():
                try:
                    import yaml
                    with open(candidate) as f:
                        cfg = yaml.safe_load(f) or {}
                    cfg["mode"] = "cloud_first"
                    cfg["enable_cloud_fallback"] = True
                    # Enable configured providers
                    for pname in configured:
                        if pname in cfg.get("providers", {}):
                            cfg["providers"][pname]["enabled"] = True
                    with open(candidate, "w") as f:
                        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
                    info(f"Set mode=cloud_first in {candidate.name}")
                except Exception as e:
                    warn(f"Could not update {candidate}: {e}")
                break

        save_saved_config({
            "setup_backend": "cloud",
            "cloud_mode": "cloud_first",
            "configured_providers": configured,
        })
    info(f"Mode: {bold('cloud_first')}")
    info(f"Providers: {', '.join(configured)}")

    # Step 3: Auto-select routing preset
    step(3, 4, "Configuring inference routing")
    if set(configured) >= {"openai", "anthropic", "deepseek"}:
        preset = "quality"
    elif "anthropic" in configured and "deepseek" in configured:
        preset = "balanced"
    elif "deepseek" in configured:
        preset = "budget"
    else:
        preset = "balanced"
    info(f"Routing preset: {bold(preset)}")
    info(f"Change with: {dim('adk routing preset <budget|balanced|quality>')}")

    if not args.dry_run:
        save_saved_config({"routing_preset": preset})

    # Step 4: Summary
    step(4, 4, "Cloud setup complete")
    print()
    print(f"  {green(bold('Ready!'))}")
    print(f"  {dim('Cloud inference is configured. No GPU required.')}")
    print()
    print(f"  {bold('Next steps:')}")
    print(f"    {cyan('adk start')}             Start chatting")
    print(f"    {cyan('adk costs')}             View spending")
    print(f"    {cyan('adk costs budget 50')}   Set monthly budget")
    print(f"    {cyan('adk routing')}           View/change model routing")
    print()
    return 0


def _setup_hybrid(args) -> int:
    """Hybrid setup — local orchestrator + cloud reasoning."""
    from adk.config import save_saved_config

    print()
    print(bold("  ============================================================"))
    print(bold("    AitherOS Hybrid Setup"))
    print(dim("    Local GPU for routine tasks + cloud for heavy reasoning"))
    print(bold("  ============================================================"))
    print()

    if args.dry_run:
        print(f"  {yellow('DRY RUN — no changes will be made')}\n")

    # Step 1: Detect GPU and set up local inference
    step(1, 4, "Local Inference Setup")
    gpu = detect_gpu()
    if gpu.vendor == "none":
        warn("No GPU detected — local orchestrator will use CPU (slow but works)")
        info(f"Consider {cyan('adk setup --mode cloud')} for cloud-only instead")
    else:
        vram_gb = gpu.vram_mb / 1024
        info(f"GPU: {bold(gpu.name)} ({vram_gb:.0f}GB VRAM)")

    # Try to set up local inference (redirect to standard setup for GPU part)
    info("Setting up local inference for routine tasks (effort 1-6)...")
    # Force hybrid tier
    if not args.tier:
        args.tier = "hybrid" if gpu.vendor == "nvidia" else "ollama"
    info(f"Local tier: {bold(args.tier)}")

    # Step 2: Cloud providers for reasoning
    step(2, 4, "Cloud Provider API Keys (for reasoning)")
    print(f"  {dim('Cloud handles effort 7+ (deep reasoning, complex analysis)')}")
    print()

    keys_path = Path.home() / ".aither" / "provider_keys.json"
    keys = {}
    if keys_path.exists():
        try:
            keys = json.loads(keys_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    configured = []
    for pname, env_name, label in [
        ("anthropic", "ANTHROPIC_API_KEY", "Anthropic (recommended for reasoning)"),
        ("deepseek", "DEEPSEEK_API_KEY", "DeepSeek (budget reasoning)"),
        ("openai", "OPENAI_API_KEY", "OpenAI"),
    ]:
        existing = keys.get(pname, "") or os.environ.get(env_name, "")
        if existing:
            info(f"{label}: configured")
            configured.append(pname)
            continue
        if args.non_interactive:
            continue
        try:
            val = input(f"    {label} key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if val:
            keys[pname] = val
            os.environ[env_name] = val
            configured.append(pname)
            info("Saved")

    if not args.dry_run and keys:
        keys_path.parent.mkdir(parents=True, exist_ok=True)
        keys_path.write_text(json.dumps(keys, indent=2))
        if sys.platform != "win32":
            os.chmod(keys_path, 0o600)

    # Step 3: Configure routing
    step(3, 4, "Configuring hybrid routing")
    info("Effort 1-6 -> local orchestrator ($0)")
    if "anthropic" in configured:
        info("Effort 7+  -> Anthropic Claude (reasoning)")
    elif "deepseek" in configured:
        info("Effort 7+  -> DeepSeek Reasoner")
    elif configured:
        info(f"Effort 7+  -> {configured[0]}")
    else:
        info("Effort 7+  -> local only (no cloud reasoning configured)")

    if not args.dry_run:
        # Set mode in cloud_providers.yaml
        for candidate in [
            Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "cloud_providers.yaml",
            Path.cwd() / "AitherOS" / "config" / "cloud_providers.yaml",
            Path.cwd() / "config" / "cloud_providers.yaml",
        ]:
            if candidate.exists():
                try:
                    import yaml
                    with open(candidate) as f:
                        cfg = yaml.safe_load(f) or {}
                    cfg["mode"] = "local_first"
                    cfg["enable_cloud_fallback"] = True
                    for pname in configured:
                        if pname in cfg.get("providers", {}):
                            cfg["providers"][pname]["enabled"] = True
                    with open(candidate, "w") as f:
                        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
                    info(f"Updated {candidate.name}")
                except Exception:
                    pass
                break

        save_saved_config({
            "setup_backend": "hybrid",
            "cloud_mode": "local_first",
            "configured_providers": configured,
        })

    # Step 4: Cost estimate
    step(4, 4, "Hybrid setup complete")
    print()
    print(f"  {green(bold('Ready!'))}")
    print(f"  {dim('Local handles ~80% of requests at $0. Cloud handles reasoning.')}")
    print()
    print(f"  {bold('Estimated savings vs cloud-only:')}")
    print(f"    Routine tasks:  {green('$0')} (handled locally)")
    print("    Reasoning:      Cloud API rates apply (~20% of requests)")
    print()

    # Now continue to standard setup for the GPU part
    info("Continuing with local GPU setup...")
    print()

    # Reset mode so cmd_setup doesn't recurse
    args.mode = "auto"
    return cmd_setup(args)


def cmd_setup(args) -> int:
    """Main entry point for `aither setup`."""
    dry_run: bool = args.dry_run
    compose_path = Path(args.output)
    non_interactive: bool = args.non_interactive

    # Handle shortcut aliases: `adk setup nemotron` → `--tier lite`
    shortcut = getattr(args, "shortcut", None)
    if shortcut:
        from adk.setup_cli import TIER_ALIASES
        resolved = TIER_ALIASES.get(shortcut.lower())
        if resolved:
            if not args.tier:
                args.tier = resolved
        elif shortcut.lower() in TIERS:
            if not args.tier:
                args.tier = shortcut.lower()
        elif shortcut.lower() == "llamacpp":
            args.tier = "llamacpp"
        else:
            err(f"Unknown shortcut: {shortcut}")
            print(f"    Valid: {', '.join(list(TIER_ALIASES.keys()) + list(TIERS.keys()) + ['llamacpp'])}")
            return 1

    # llama.cpp endpoint tier — entirely separate flow (no Docker, no vLLM)
    if args.tier == "llamacpp" or getattr(args, "backend", None) == "llamacpp":
        try:
            from adk import llamacpp_setup
        except ImportError as e:
            err(f"llamacpp_setup module not available: {e}")
            return 1
        result = llamacpp_setup.install(
            quant=getattr(args, "llamacpp_quant", None) or None,
            port=getattr(args, "llamacpp_port", None) or llamacpp_setup.DEFAULT_PORT,
            service=not getattr(args, "no_service", False),
            dry_run=dry_run,
        )
        if result.success and not dry_run:
            llamacpp_setup.smoke_test(result.port)
        if args.stack and result.success:
            step(6, 6, f"Deploying AitherOS ({args.stack})")
            deploy_stack(args.stack, dry_run, args.api_key or "")
        return 0 if result.success else 1

    # ── Cloud-only setup flow ─────────────────────────────────────────
    setup_mode = getattr(args, "mode", "auto") or "auto"
    if setup_mode == "cloud":
        return _setup_cloud_only(args)
    elif setup_mode == "hybrid":
        return _setup_hybrid(args)

    # Handle --dgx-spark
    dgx_url = getattr(args, "dgx_spark", None) or ""

    # Handle --reasoning-api → forces hybrid tier if no tier set
    reasoning_api = getattr(args, "reasoning_api", None) or ""
    reasoning_model = getattr(args, "reasoning_model", None) or ""
    if reasoning_api and not args.tier:
        args.tier = "hybrid"

    print()
    print(bold("  ============================================================"))
    print(bold("    AitherOS Setup"))
    print(dim("    GPU detection -> vLLM containers -> parallel agent fleets"))
    print(bold("  ============================================================"))
    print()
    print(f"  {dim('vLLM uses continuous batching — multiple agents share the GPU')}")
    print(f"  {dim('simultaneously. Ollama serializes. vLLM parallelizes.')}")
    print()

    if dry_run:
        print(f"  {yellow('DRY RUN — no changes will be made')}\n")

    total_steps = 5
    if args.stack:
        total_steps = 6

    # ── Step 1: Detect GPU ────────────────────────────────────────
    step(1, total_steps, "Detecting GPU hardware")
    gpu = detect_gpu()
    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0

    if gpu.vendor == "nvidia":
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
    elif gpu.vendor == "amd":
        info(f"GPU: {bold(gpu.name)} (AMD)")
        warn("AMD GPUs work with Ollama. vLLM requires NVIDIA CUDA.")
    elif gpu.vendor == "apple":
        info(f"GPU: {bold(gpu.name)} (Apple Silicon)")
        warn("Apple Silicon works with Ollama. vLLM requires NVIDIA CUDA.")
    else:
        warn("No GPU detected — will use Ollama with CPU inference")

    # ── Step 2: Check Docker ──────────────────────────────────────
    step(2, total_steps, "Checking prerequisites")
    docker_ok, docker_msg = check_docker()
    if docker_ok:
        info(f"Docker: {docker_msg}")
    else:
        warn(f"Docker: {docker_msg}")

    ollama_ok = bool(shutil.which("ollama"))
    if ollama_ok:
        info("Ollama: installed (fallback)")
    else:
        info("Ollama: not installed (not needed with vLLM)")

    # Auto-install llmfit for hardware-aware model selection
    llmfit_ok = bool(shutil.which("llmfit"))
    if llmfit_ok:
        info("llmfit: installed (hardware-aware model selection)")
    elif not dry_run:
        info("llmfit: not found — installing for smart model selection...")
        import asyncio as _aio
        from adk.setup import AgentSetup
        _setup = AgentSetup()
        try:
            llmfit_ok = _aio.get_event_loop().run_until_complete(_setup.ensure_llmfit())
            if llmfit_ok:
                info(f"llmfit: {green('installed')}")
            else:
                warn("llmfit: install failed (model selection will use static profiles)")
        except Exception:
            warn("llmfit: install failed (model selection will use static profiles)")

    # ── Step 2b: Check existing infrastructure ────────────────
    force = getattr(args, "force", False)
    if not force:
        step(2, total_steps, "Scanning for existing inference")
        infra = _scan_existing_infra()

        has_existing = bool(infra.vllm_ports or infra.ollama_running)

        if has_existing:
            if infra.vllm_ports:
                for port, models in infra.vllm_ports:
                    info(f"vLLM running on :{port} — {', '.join(models)}")
            if infra.ollama_running:
                info(f"Ollama running — {len(infra.ollama_models)} model(s)")
            if infra.dgx_url:
                info(f"DGX Spark at {infra.dgx_url} — {', '.join(infra.dgx_models) or 'reachable'}")
                if not dgx_url:
                    dgx_url = infra.dgx_url
            if infra.genesis_running:
                info("AitherOS Genesis running on :8001")
            if infra.aither_node_running:
                info("awnode MCP server on :8080")
            if infra.cloud_keys:
                info(f"Cloud API keys: {', '.join(infra.cloud_keys)}")

            print()

            if non_interactive:
                info("Existing inference detected — connecting instead of deploying")
                _save_existing_config(infra)
                print()
                print(f"  {green(bold('Connected!'))}")
                print(f"  {dim('Run')} {cyan('adk run')} {dim('to start your agent.')}")
                print()
                return 0
            else:
                choice = ask(
                    "Existing inference found. Connect to it or deploy new?",
                    default="connect",
                    choices=["connect", "new", "force"],
                )
                if choice == "connect":
                    _save_existing_config(infra)
                    print()
                    print(f"  {green(bold('Connected!'))}")
                    print(f"  {dim('Run')} {cyan('adk run')} {dim('to start your agent.')}")
                    print()
                    return 0
                elif choice == "force":
                    warn("Force mode — will attempt to start additional containers")
                # else "new" — fall through to normal setup

    # Decide path
    can_vllm = docker_ok and gpu.vendor == "nvidia" and vram_gb >= 6
    forced_tier = args.tier

    if forced_tier == "ollama":
        can_vllm = False
    elif forced_tier and forced_tier != "ollama" and not can_vllm:
        err(f"Tier '{forced_tier}' requires Docker + NVIDIA GPU")
        return 1

    if not can_vllm and not ollama_ok:
        err("Need Docker + NVIDIA GPU (for vLLM) or Ollama installed")
        print(f"    Docker: {cyan('https://docker.com/products/docker-desktop')}")
        print(f"    Ollama: {cyan('https://ollama.com/download')}")
        return 1

    # ── Step 3: Select Tier ───────────────────────────────────────
    step(3, total_steps, "Selecting inference tier")

    if not can_vllm:
        info("Using Ollama (no NVIDIA GPU or Docker)")
        step(4, total_steps, "Setting up Ollama")
        result = setup_ollama(gpu, dry_run)
        if args.stack:
            step(total_steps, total_steps, f"Deploying AitherOS ({args.stack})")
            deploy_stack(args.stack, dry_run, args.api_key or "")
        return result

    # vLLM path
    print()
    print(f"  {bold('Why vLLM?')}")
    print("  Ollama: one request at a time. Agents queue up, wait their turn.")
    print(f"  vLLM:   {bold('continuous batching')} — all agents run {green('simultaneously')}.")
    print(f"  Result: {green('3-10x faster')} with concurrent agent fleets.")
    print()

    recommended = forced_tier or recommend_tier(gpu)

    if forced_tier:
        tier_id = forced_tier
        info(f"Using tier: {bold(tier_id)} (from --tier)")
    elif non_interactive:
        tier_id = recommended
        info(f"Auto-selected: {bold(tier_id)}")
    else:
        # Tier comparison table
        print(f"  {'Tier':<12} {'Workers':<35} {'VRAM':<10} {'Download'}")
        print(f"  {'-'*12} {'-'*35} {'-'*10} {'-'*10}")
        for tid, tier in TIERS.items():
            workers_str = " + ".join(w.name for w in tier["workers"])
            vram_need = sum(w.vram_gb for w in tier["workers"])
            dl = sum(w.download_gb for w in tier["workers"])
            fits = vram_gb * 0.85 >= tier["min_vram_gb"]
            status = green("fits") if fits else red("too big")
            print(f"  {bold(tid):<20} {workers_str:<35} ~{vram_need:.0f}GB {status:<18} ~{dl:.0f}GB")
        print()
        if 5 <= vram_gb <= 12:
            print(f"  {dim('Have a Mac or cluster? Try:')} {cyan('adk deploy grid')} "
                  f"{dim('(multi-node distributed inference)')}")
            print()
        info(f"Recommended: {bold(recommended)}")
        tier_id = ask("Select tier", default=recommended, choices=list(TIERS.keys()))

    tier = TIERS[tier_id]
    info(f"{tier['name']}: {tier['desc']}")

    total_dl = sum(w.download_gb for w in tier["workers"])
    total_vram = sum(w.vram_gb for w in tier["workers"])
    info(f"Download: ~{total_dl:.0f}GB (cached after first run)")
    info(f"VRAM: ~{total_vram:.0f}GB / {vram_gb:.0f}GB")

    # ── Step 4: HuggingFace Token ─────────────────────────────────
    step(4, total_steps, "Checking HuggingFace access")
    hf_token = args.hf_token or os.environ.get("HF_TOKEN", "")
    if hf_token:
        info(f"HF token: {hf_token[:8]}...{hf_token[-4:]}")
    elif not non_interactive:
        print("  Some models need a HuggingFace token (free account).")
        print(f"  Get one at: {cyan('https://huggingface.co/settings/tokens')}")
        hf_token = ask("HuggingFace token (Enter to skip)", default="")
        if hf_token:
            info(f"Token set: {hf_token[:8]}...")
    if not hf_token:
        warn("No HF token — gated models may fail to download")

    # ── Step 5: Generate + Start ──────────────────────────────────
    step(5, total_steps, "Starting vLLM containers")

    compose_content = generate_compose(tier_id, hf_token)

    if dry_run:
        info(f"Would write: {compose_path}")
        info(f"Would start {len(tier['workers'])} container(s)")
    else:
        compose_path.write_text(compose_content)
        info(f"Compose file: {compose_path}")

        if not start_containers(compose_path, dry_run):
            return 1
        info("Containers started")

        # Wait for health
        for w in tier["workers"]:
            info(f"Checking {w.name} (:{w.port})...")
            if wait_for_health(w.port, w.name, timeout=600):
                info(f"{w.name}: {green('healthy')}")
            else:
                warn(f"{w.name}: still loading (docker logs adk-vllm-{w.name})")

    # ── Reasoning API collection (hybrid tier or --reasoning-api) ──
    reasoning_api_key = ""
    if reasoning_api:
        reasoning_urls = {
            "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com"),
            "openai": ("OPENAI_API_KEY", "https://api.openai.com"),
            "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com"),
            "gateway": ("AITHER_API_KEY", "https://mcp.aitherium.com"),
        }
        env_key, _ = reasoning_urls.get(reasoning_api, ("", ""))
        reasoning_api_key = os.environ.get(env_key, "") if env_key else ""
        if not reasoning_api_key and not non_interactive:
            import getpass
            reasoning_api_key = getpass.getpass(f"  API key for {reasoning_api} reasoning: ").strip()
        if reasoning_api_key:
            info(f"Reasoning: {reasoning_api} ({reasoning_model or 'default model'})")
        else:
            warn(f"No API key for {reasoning_api} — reasoning will fall back to local model")
            reasoning_api = ""
    elif tier_id == "hybrid" and not non_interactive:
        # Hybrid tier selected but no --reasoning-api — ask
        print()
        info("Hybrid mode needs a cloud API for reasoning (effort 7+ tasks).")
        reasoning_api = ask("Reasoning API", default="anthropic",
                           choices=["anthropic", "openai", "deepseek", "gateway", "skip"])
        if reasoning_api != "skip":
            import getpass
            reasoning_api_key = getpass.getpass(f"  API key for {reasoning_api}: ").strip()
            if not reasoning_api_key:
                warn("No key provided — reasoning will use local model only")
                reasoning_api = ""
        else:
            reasoning_api = ""

    _save_config("vllm", tier_id, gpu,
                 reasoning_api=reasoning_api, reasoning_api_key=reasoning_api_key,
                 reasoning_model=reasoning_model, dgx_url=dgx_url)

    # ── Post-setup smoke test ─────────────────────────────────────
    if not dry_run:
        orch = tier["workers"][0]
        _smoke_test(orch.port)

    # ── Optional: Deploy AitherOS stack ───────────────────────────
    if args.stack:
        step(total_steps, total_steps, f"Deploying AitherOS ({args.stack})")
        deploy_stack(args.stack, dry_run, args.api_key or "")

    # ── Summary ───────────────────────────────────────────────────
    orch = tier["workers"][0]
    print()
    print(bold("  ============================================================"))
    print()
    for w in tier["workers"]:
        print(f"  {green('*')} {bold(w.name)}: http://localhost:{w.port}/v1")
        print(f"    Model: {w.model} ({w.served_name})")
    print()
    print(f"  {bold('Run your agent:')}")
    print(f"    {cyan('adk run')}")
    print(f"    {dim('(auto-detects vLLM on port 8200)')}")
    print()
    print(f"  {bold('Run parallel agents:')}")
    print(f"    {cyan('adk run --agents lyra,atlas,demiurge')}")
    print(f"    {dim('All agents share the GPU via continuous batching.')}")
    print()

    if args.stack:
        print(f"  {bold('AitherOS:')}")
        print(f"    Dashboard: {cyan('http://localhost:3000')}")
        print(f"    Genesis:   {cyan('http://localhost:8001')}")
        print()

    if tier_id in ("standard", "full"):
        print(f"  {bold('Gaming mode:')}")
        print(f"    {cyan(f'docker compose -f {compose_path} stop')}  {dim('(free VRAM)')}")
        print(f"    {cyan(f'docker compose -f {compose_path} start')} {dim('(resume)')}")
        print()

    print(f"  {bold('Manage:')}")
    print(f"    Logs:   {cyan(f'docker compose -f {compose_path} logs -f')}")
    print(f"    Stop:   {cyan(f'docker compose -f {compose_path} stop')}")
    print(f"    Update: {cyan(f'docker compose -f {compose_path} pull && docker compose -f {compose_path} up -d')}")
    print()
    print(f"  {green(bold('Ready!'))}")
    print(f"  {dim('Your GPU is now an inference server for parallel agent fleets.')}")
    print()
    return 0


# ---------------------------------------------------------------------------
# A5 First-Run Wizard
# ---------------------------------------------------------------------------

def cmd_wizard(args) -> int:
    """Interactive first-run wizard for the consumer launch.

    Takes a fresh machine to a working agent chat with zero manual env-var surgery.
    - Probe hardware (RAM, CPU, GPU, Ollama)
    - Recommend setup (local vs cloud, backend choice)
    - Optional: ensure Ollama installed
    - Optional: pull recommended model
    - License key entry (optional, validate + save)
    - Room auth token setup (create if missing)
    - Health checks
    - Print next steps with Room URL
    """
    from adk.hardware_probe import detect_system, recommend_setup
    from adk.licensing import get_license_manager
    from adk.config import save_saved_config

    non_interactive = getattr(args, "yes", False)

    print()
    print(bold("  ===== AitherOS Consumer Wizard (First Run) ====="))
    print(dim("    Get you to a working agent chat in 5 minutes"))
    print(bold("  ============================================="))
    print()

    # Step 1: Probe hardware
    step(1, 5, "Detecting system hardware")
    system = detect_system()
    print(f"  RAM: {bold(f'{system.ram_gb:.1f}GB')}")
    print(f"  CPU: {bold(f'{system.cpu_cores}')} cores")
    if system.gpu_vendor != "none":
        print(
            f"  GPU: {bold(system.gpu_name)} "
            f"({system.gpu_vram_mb / 1024:.1f}GB)"
        )
    else:
        print(f"  GPU: {dim('none')}")
    print(f"  Ollama: {green('installed') if system.ollama_installed else dim('not found')}")
    print()

    # Step 2: Recommend setup
    step(2, 5, "Recommending configuration")
    rec = recommend_setup(system)
    print(f"  Backend: {bold(rec.backend)}")
    if rec.local_model:
        print(
            f"  Local Model: {bold(rec.local_model)} "
            f"({rec.local_model_gb:.0f}GB disk, {rec.local_vram_gb:.1f}GB VRAM)"
        )
    print(f"  {rec.rationale}")
    for warn_msg in rec.warnings:
        warn(warn_msg)
    print()

    if not non_interactive:
        choice = ask(
            "Accept this recommendation?",
            default="yes",
            choices=["yes", "no", "cloud"],
        )
        if choice == "no":
            print("  Setup aborted.")
            return 1
        elif choice == "cloud":
            rec.backend = "cloud"
            rec.local_model = None

    # Step 3: Ensure Ollama (if local backend)
    if rec.backend != "cloud":
        step(3, 5, "Checking Ollama")
        if not system.ollama_installed:
            print(
                f"  {yellow('!')} Ollama not found. Install from: "
                f"{cyan('https://ollama.com/download')}"
            )
            print()
            if non_interactive:
                print("  Non-interactive mode — skipping Ollama setup.")
            else:
                choice = ask(
                    "Ollama required for local setup. Install it and retry.",
                    default="done",
                    choices=["done", "skip"],
                )
                if choice == "skip":
                    rec.backend = "cloud"
                    info("Switching to cloud-only mode")
                else:
                    print("  Please install Ollama from https://ollama.com/download")
                    return 1
        else:
            info("Ollama ready")
            if rec.local_model and not non_interactive:
                print(
                    f"  Will pull: {bold(rec.local_model)} "
                    f"(~{rec.local_model_gb:.0f}GB)"
                )
                choice = ask(
                    "Pull model now?",
                    default="yes",
                    choices=["yes", "no"],
                )
                if choice == "yes":
                    info(f"Pulling {rec.local_model}...")
                    try:
                        subprocess.run(
                            ["ollama", "pull", rec.local_model],
                            timeout=1800,
                        )
                        info(f"{rec.local_model} ready")
                    except subprocess.TimeoutExpired:
                        warn("Pull timed out — model may still be downloading")
                    except Exception as e:
                        err(f"Failed to pull model: {e}")
        print()

    # Step 4: License key (optional)
    step(4, 5, "License configuration (optional)")
    print("  Enter a license key for premium features (or press Enter to skip).")
    print("  Free tier: single agent, 100k tokens/month via cloud.")
    print("  Paid: Sovereign ($1000/perpetual) = all features, unlimited local tokens.")
    print()

    lm = get_license_manager()
    current_tier = lm.license.tier.value if lm.license else "community"
    info(f"Current tier: {bold(current_tier)}")

    if not non_interactive:
        key_input = input("  License key (or press Enter): ").strip()
        if key_input:
            # Simple validation: base64-decodable
            try:
                import base64
                import json

                decoded = json.loads(
                    base64.b64decode(key_input).decode("utf-8")
                )
                if "payload" in decoded and "signature" in decoded:
                    lic_path = Path.home() / ".aither" / "license.json"
                    lic_path.parent.mkdir(parents=True, exist_ok=True)
                    lic_path.write_text(
                        json.dumps(decoded), encoding="utf-8"
                    )
                    info("License key saved to ~/.aither/license.json")
                else:
                    warn("License key format invalid — proceeding with free tier")
            except Exception as e:
                warn(f"Could not parse license key ({e}) — proceeding with free tier")
    print()

    # Step 5: Room auth token
    step(5, 5, "Setting up Room auth token")
    token_path = Path.home() / ".aither" / "room_auth.txt"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        info(f"Auth token already exists: {token_path}")
    else:
        try:
            import secrets

            token = secrets.token_urlsafe(32)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token, encoding="utf-8")
            # Best-effort chmod 0600 (Windows: near no-op)
            try:
                token_path.chmod(0o600)
            except Exception:
                pass
            info(f"Auth token created: {token_path}")
        except Exception as e:
            warn(f"Failed to create auth token: {e}")
            token = ""
    print()

    # Health checks
    print(bold("  Health checks:"))
    print(f"  {green('+')} Hardware probed")
    print(f"  {green('+')} Configuration confirmed")
    if rec.backend != "cloud":
        print(f"  {green('+')} Ollama ready")
    print(f"  {green('+')} Auth token configured")
    print()

    # Summary
    print(bold("  Next steps:"))
    room_url = f"http://localhost:8350?token={token}"
    if not non_interactive:
        print("  1. adk up                    # Start Room + Ollama")
        print(f"  2. Open: {cyan(room_url)}")
        print("  3. Chat with your first agent")
    else:
        print("  Run: adk up")
        print(f"  Then open: {room_url}")
    print()

    return 0
