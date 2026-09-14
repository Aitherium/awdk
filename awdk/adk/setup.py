"""Programmatic agent self-setup — detect hardware, install backends, pull models.

Agents can call these functions to bootstrap their own environment without
human intervention. Wraps the logic from install.py in an async API.

Usage:
    from adk.setup import AgentSetup

    setup = AgentSetup()
    info = await setup.detect_hardware()
    await setup.ensure_ollama()
    await setup.pull_models(["llama3.2:3b", "nomic-embed-text"])
    await setup.ensure_vllm(gpu_count=1)
    report = await setup.full_setup()  # does everything
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("adk.setup")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GPUInfo:
    vendor: str = "none"  # nvidia, amd, apple, none
    name: str = ""
    vram_mb: int = 0          # Best single GPU VRAM (for profile selection)
    cuda_version: str = ""
    driver_version: str = ""
    count: int = 0
    total_vram_mb: int = 0    # Sum of all GPUs (for vLLM tier selection)
    all_gpus: list = field(default_factory=list)  # List of {"name": str, "vram_mb": int}


@dataclass
class VLLMInfo:
    running: bool = False
    ports: list[int] = field(default_factory=list)
    models: list[str] = field(default_factory=list)


@dataclass
class SystemInfo:
    os_name: str = ""
    os_version: str = ""
    arch: str = ""
    python_version: str = ""
    ram_gb: float = 0.0
    gpu: GPUInfo = field(default_factory=GPUInfo)
    ollama_installed: bool = False
    ollama_running: bool = False
    ollama_models: list[str] = field(default_factory=list)
    vllm: VLLMInfo = field(default_factory=VLLMInfo)
    docker_installed: bool = False
    profile: str = "cpu_only"
    # Which inference backend is active (detected at setup time)
    active_backend: str = ""  # "vllm", "ollama", "openai", ""


@dataclass
class SetupReport:
    """Result of a full_setup() call."""
    system: SystemInfo = field(default_factory=SystemInfo)
    profile: str = "cpu_only"
    ollama_ready: bool = False
    vllm_ready: bool = False
    backend: str = ""  # "vllm", "ollama", "openai"
    models_pulled: list[str] = field(default_factory=list)
    models_available: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ready: bool = False


# ---------------------------------------------------------------------------
# Hardware detection (async wrappers around subprocess)
# ---------------------------------------------------------------------------

async def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except asyncio.TimeoutError:
        return -2, "", f"Command timed out: {' '.join(cmd)}"
    except Exception as e:
        return -3, "", str(e)


async def _detect_gpu() -> GPUInfo:
    """Detect GPU hardware."""
    gpu = GPUInfo()

    # NVIDIA
    rc, out, _ = await _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader,nounits"])
    if rc == 0 and out.strip():
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        gpu.vendor = "nvidia"
        gpu.count = len(lines)

        # Parse all GPUs, find best (max VRAM), sum total
        all_gpus = []
        best_vram = 0
        total_vram = 0
        for line in lines:
            p = [x.strip() for x in line.split(",")]
            g_name = p[0] if p else "NVIDIA GPU"
            g_vram = 0
            g_driver = ""
            if len(p) >= 2:
                try:
                    g_vram = int(float(p[1]))
                except ValueError:
                    pass
            if len(p) >= 3:
                g_driver = p[2]
            all_gpus.append({"name": g_name, "vram_mb": g_vram})
            total_vram += g_vram
            if g_vram > best_vram:
                best_vram = g_vram
                gpu.name = g_name
                gpu.driver_version = g_driver

        gpu.vram_mb = best_vram
        gpu.total_vram_mb = total_vram
        gpu.all_gpus = all_gpus

        # CUDA version
        rc2, out2, _ = await _run(["nvidia-smi"])
        if rc2 == 0:
            m = re.search(r"CUDA Version:\s+([\d.]+)", out2)
            if m:
                gpu.cuda_version = m.group(1)
        return gpu

    # AMD
    rc, out, _ = await _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    if rc == 0 and out.strip():
        gpu.vendor = "amd"
        gpu.count = 1
        for line in out.splitlines():
            if "total" in line.lower():
                nums = re.findall(r"(\d+)", line)
                if nums:
                    gpu.vram_mb = int(nums[-1]) // (1024 * 1024) if int(nums[-1]) > 1_000_000 else int(nums[-1])
        # The product NAME matters as much as the capacity: the model resolver
        # matches a known device by name first and only falls back to a
        # vendor+capacity heuristic. Leaving it empty cost an RX 7900 XTX its
        # exact device record (bandwidth, overlays) while still reporting a
        # plausible tier -- a quieter wrong answer than no answer.
        rc_n, out_n, _ = await _run(["rocm-smi", "--showproductname", "--csv"])
        if rc_n == 0 and out_n.strip():
            for line in out_n.splitlines():
                if "card" not in line.lower():
                    continue
                parts = [c.strip() for c in line.split(",")]
                cand = next(
                    (c for c in reversed(parts)
                     if c and not c.lower().startswith("card") and c.lower() != "n/a"),
                    "",
                )
                if cand:
                    gpu.name = cand
                    break
        return gpu

    # Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        gpu.vendor = "apple"
        gpu.name = "Apple Silicon (unified memory)"
        gpu.count = 1
        # Unified memory = system RAM
        rc, out, _ = await _run(["sysctl", "-n", "hw.memsize"])
        if rc == 0:
            try:
                gpu.vram_mb = int(out.strip()) // (1024 * 1024)
            except ValueError:
                pass
        return gpu

    return gpu


async def _detect_ram() -> float:
    """Detect system RAM in GB."""
    system = platform.system()
    if system == "Windows":
        # PowerShell (works on all modern Windows, wmic is deprecated/removed)
        rc, out, _ = await _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
        ])
        if rc == 0 and out.strip():
            try:
                return int(out.strip()) / (1024 ** 3)
            except ValueError:
                pass
        # Fallback: wmic (older Windows)
        rc, out, _ = await _run(["wmic", "computersystem", "get", "TotalPhysicalMemory", "/value"])
        if rc == 0:
            m = re.search(r"TotalPhysicalMemory=(\d+)", out)
            if m:
                return int(m.group(1)) / (1024 ** 3)
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 * 1024)
        except FileNotFoundError:
            pass
    elif system == "Darwin":
        rc, out, _ = await _run(["sysctl", "-n", "hw.memsize"])
        if rc == 0:
            try:
                return int(out.strip()) / (1024 ** 3)
            except ValueError:
                pass
    return 0.0


async def _check_ollama() -> tuple[bool, bool, list[str]]:
    """Check Ollama: (installed, running, models)."""
    installed = shutil.which("ollama") is not None
    if not installed:
        return False, False, []

    # Check if running
    rc, out, _ = await _run(["ollama", "list"])
    if rc != 0:
        return True, False, []

    models = []
    for line in out.strip().splitlines()[1:]:  # skip header
        parts = line.split()
        if parts:
            models.append(parts[0])
    return True, True, models


async def _check_vllm() -> VLLMInfo:
    """Check if vLLM is running on common ports (8200-8203 or custom)."""
    import httpx

    info = VLLMInfo()
    ports_to_check = [8200, 8201, 8202, 8203, 8000]  # AitherOS standard + vLLM default

    for port in ports_to_check:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://localhost:{port}/v1/models")
                if resp.status_code == 200:
                    info.running = True
                    info.ports.append(port)
                    data = resp.json()
                    for m in data.get("data", []):
                        model_id = m.get("id", "")
                        if model_id and model_id not in info.models:
                            info.models.append(model_id)
        except Exception:
            pass

    # Also check for vLLM Docker containers
    rc, out, _ = await _run(["docker", "ps", "--filter", "ancestor=vllm/vllm-openai", "--format", "{{.Names}} {{.Ports}}"])
    if rc == 0 and out.strip():
        info.running = True
        for line in out.strip().splitlines():
            # Extract published ports from Docker port mapping
            import re as _re
            for m in _re.finditer(r":(\d+)->", line):
                p = int(m.group(1))
                if p not in info.ports:
                    info.ports.append(p)

    return info


async def _check_docker() -> bool:
    """Check if Docker is available."""
    rc, _, _ = await _run(["docker", "info"])
    return rc == 0


def _select_profile(gpu: GPUInfo, ram_gb: float) -> str:
    """Select hardware profile based on detected hardware."""
    if gpu.vendor == "none":
        return "cpu_only"
    if gpu.vendor == "apple":
        return "apple_silicon"
    # AMD tiers. This used to `return "amd"` unconditionally, ABOVE the capacity
    # ladder, so a 24GB RX 7900 XTX and a 6GB RX 580 resolved to one profile --
    # whose static model list is the same two entries `cpu_only` gets. The ODS
    # resolver runs first and is VRAM-aware, so the wrong answer only surfaced
    # when ODS was unavailable: a silent downgrade on the fallback path, which is
    # where nobody looks.
    if gpu.vendor == "amd":
        if gpu.vram_mb >= 20000:
            return "amd_high"
        if gpu.vram_mb >= 12000:
            return "amd"
        return "amd_low"
    # NVIDIA tiers
    if gpu.vram_mb >= 48000:
        return "nvidia_ultra"
    if gpu.vram_mb >= 24000:
        return "nvidia_high"
    if gpu.vram_mb >= 12000:
        return "nvidia_mid"
    if gpu.vram_mb >= 6000:
        return "nvidia_low"
    return "minimal"


def _recommended_models(profile: str) -> list[str]:
    """Return recommended Ollama models for a profile.

    Static fallback — prefer ``_recommended_models_llmfit()`` when the llmfit
    sidecar is reachable.
    """
    models = {
        "cpu_only": ["gemma4:4b", "nomic-embed-text"],
        "minimal": ["gemma4:4b", "nomic-embed-text"],
        "nvidia_low": ["gemma4:4b", "nomic-embed-text"],
        "nvidia_mid": ["nemotron-orchestrator-8b", "deepseek-r1:8b", "nomic-embed-text"],
        "nvidia_high": ["nemotron-orchestrator-8b", "deepseek-r1:14b", "nomic-embed-text", "gemma4:27b"],
        "nvidia_ultra": ["nemotron-orchestrator-8b", "deepseek-r1:32b", "nomic-embed-text", "gemma4:27b"],
        "apple_silicon": ["gemma4:4b", "deepseek-r1:8b", "nomic-embed-text"],
        "amd_low": ["gemma4:4b", "nomic-embed-text"],
        "amd": ["nemotron-orchestrator-8b", "deepseek-r1:8b", "nomic-embed-text"],
        "amd_high": [
            "nemotron-orchestrator-8b", "deepseek-r1:14b",
            "nomic-embed-text", "gemma4:27b",
        ],
        "standard": ["nemotron-orchestrator-8b", "deepseek-r1:14b", "nomic-embed-text", "gemma4:27b"],
        "workstation": ["nemotron-orchestrator-8b", "deepseek-r1:32b", "nomic-embed-text", "gemma4:27b"],
        "server": ["nemotron-orchestrator-8b", "deepseek-r1:32b", "nomic-embed-text", "gemma4:27b"],
    }
    return models.get(profile, models["cpu_only"])


async def _recommended_models_llmfit() -> list[str] | None:
    """Query llmfit for hardware-scored Ollama model recommendations (OPTIONAL).

    NOTE: llmfit is now optional. Primary model selection uses OdsResolver
    (deterministic, offline). This function provides optional refinement
    via llmfit if available.

    Returns a curated list of model names suitable for ``ollama pull``, or
    ``None`` only if model selection genuinely failed (missing/corrupt catalog),
    in which case the caller falls back to the static list. A merely-absent
    llmfit binary is NOT a failure — ODS answers offline.

    The recommendations cover all ADK tiers:
    - fast (chat)
    - balanced (general)
    - reasoning
    - coding
    - embedding (always nomic-embed-text for Ollama compatibility)
    """
    try:
        from adk.llmfit import get_llmfit
    except ImportError:
        return None

    fit = get_llmfit()
    # NOTE: deliberately NOT gated on fit.is_available(). That probes the external
    # llmfit REST/CLI only, and recommend_config() now resolves from the vendored
    # ODS catalog offline with llmfit as optional refinement. Keeping the gate here
    # meant that on any box without the llmfit binary — i.e. the normal case — this
    # returned None and the caller silently fell back to the static model list,
    # so the offline resolver was never reached and the feature was inert.
    config = await fit.recommend_config()
    if "error" in config:
        return None

    # Collect unique model names from each tier
    seen: set[str] = set()
    ordered: list[str] = []
    for tier in ("balanced", "fast", "reasoning", "coding"):
        rec = config.get(tier)
        if not rec or not rec.get("model"):
            continue
        model_name = rec["model"]
        # llmfit may return HuggingFace-style names — skip those for Ollama
        if "/" in model_name:
            continue
        if model_name not in seen:
            seen.add(model_name)
            ordered.append(model_name)

    # Always include an embedding model (llmfit doesn't track these well yet)
    if "nomic-embed-text" not in seen:
        ordered.append("nomic-embed-text")

    logger.info("llmfit recommended models: %s", ordered)
    return ordered if ordered else None


# ---------------------------------------------------------------------------
# AgentSetup — programmatic setup API
# ---------------------------------------------------------------------------

def _find_compose_file() -> Path | None:
    """Find the ADK vLLM compose file.

    Search order:
    1. $ADK_ROOT/docker-compose.adk-vllm.yml
    2. Package directory (shipped with pip install)
    3. CWD
    4. ~/.aither/docker-compose.adk-vllm.yml
    """
    name = "docker-compose.adk-vllm.yml"

    # 1. ADK_ROOT env var
    adk_root = os.environ.get("ADK_ROOT")
    if adk_root:
        p = Path(adk_root) / name
        if p.exists():
            return p

    # 2. Package directory (next to this file, or parent)
    pkg_dir = Path(__file__).resolve().parent
    for d in [pkg_dir, pkg_dir.parent]:
        p = d / name
        if p.exists():
            return p

    # 3. CWD
    p = Path.cwd() / name
    if p.exists():
        return p

    # 4. ~/.aither/
    p = Path.home() / ".aither" / name
    if p.exists():
        return p

    return None


class AgentSetup:
    """Programmatic environment setup for ADK agents.

    Agents can use this to self-setup their inference backends:

        setup = AgentSetup()
        report = await setup.full_setup()
        if report.ready:
            agent = AitherAgent("atlas")
            response = await agent.chat("Hello!")
    """

    def __init__(self, data_dir: str = ""):
        self.data_dir = Path(data_dir or os.environ.get("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._system: SystemInfo | None = None

    async def _try_aitherzero(self) -> bool:
        """Check if pwsh + AitherZero module + adk plugin are available."""
        rc, _, _ = await _run(["pwsh", "-Version"])
        if rc != 0:
            return False
        rc, out, _ = await _run([
            "pwsh", "-NoProfile", "-Command",
            "Import-Module AitherZero -ErrorAction Stop; "
            "(Get-AitherPlugin -Name 'adk') -ne $null"
        ])
        return rc == 0 and "True" in out

    async def _run_aitherzero_setup(self) -> SetupReport | None:
        """Delegate setup to AitherZero's Invoke-ADKSetup, parse JSON into SetupReport."""
        cmd = [
            "pwsh", "-NoProfile", "-Command",
            "Import-Module AitherZero; "
            "Invoke-ADKSetup -Backend auto -NonInteractive | ConvertTo-Json -Depth 5"
        ]
        rc, out, err = await _run(cmd, timeout=600.0)
        if rc != 0:
            logger.warning("AitherZero setup failed (rc=%d): %s", rc, err)
            return None
        try:
            data = json.loads(out)
            return self._parse_aitherzero_report(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to parse AitherZero output: %s", exc)
            return None

    @staticmethod
    def _parse_aitherzero_report(data: dict) -> SetupReport:
        """Convert AitherZero Invoke-ADKSetup JSON output to SetupReport."""
        gpu_data = data.get("gpu", {})
        gpu = GPUInfo(
            vendor=gpu_data.get("vendor", "none"),
            name=gpu_data.get("name", ""),
            vram_mb=gpu_data.get("vram_mb", 0),
        )
        system = SystemInfo(
            os_name=platform.system(),
            os_version=platform.version(),
            arch=platform.machine(),
            python_version=platform.python_version(),
            gpu=gpu,
            docker_installed=data.get("docker_installed", False),
            profile=data.get("profile", "cpu_only"),
            active_backend=data.get("backend", ""),
        )
        return SetupReport(
            system=system,
            profile=data.get("profile", "cpu_only"),
            ollama_ready=data.get("ollama_ready", False),
            vllm_ready=data.get("vllm_ready", False),
            backend=data.get("backend", ""),
            models_available=data.get("models_available", []),
            errors=data.get("errors", []),
            ready=data.get("ready", False),
        )

    async def detect_hardware(self) -> SystemInfo:
        """Detect system hardware: GPU, RAM, OS, installed backends.

        Detects both Ollama and vLLM to avoid conflicts. If vLLM is already
        running and using GPU, Ollama model loading would cause VRAM crashes.

        Also queries the llmfit sidecar for cross-validated hardware data
        (it reads CUDA/Metal/ROCm directly and often has better VRAM numbers).
        """
        gpu, ram, ollama_check, vllm_info, docker = await asyncio.gather(
            _detect_gpu(),
            _detect_ram(),
            _check_ollama(),
            _check_vllm(),
            _check_docker(),
        )
        ollama_installed, ollama_running, ollama_models = ollama_check

        # ── Cross-validate GPU data with llmfit if available ──────────────
        llmfit_hw: dict | None = None
        try:
            from adk.llmfit import get_llmfit
            fit = get_llmfit()
            if await fit.is_available():
                result = await fit.system_info()
                # Validate it's a real dict (not a MagicMock or None)
                if isinstance(result, dict) and result.get("has_gpu") is not None:
                    llmfit_hw = result
        except Exception as exc:
            logger.debug("llmfit cross-validation skipped: %s", exc)

        if llmfit_hw:
            # llmfit reads GPU directly — its VRAM/name may be more accurate
            vram_gb = llmfit_hw.get("gpu_vram_gb", 0)
            llmfit_vram_mb = int((vram_gb or 0) * 1024)
            if llmfit_vram_mb > 0 and (gpu.vram_mb == 0 or abs(llmfit_vram_mb - gpu.vram_mb) > 512):
                logger.info(
                    "llmfit reports GPU VRAM=%dMB (native detection=%dMB) — using llmfit value",
                    llmfit_vram_mb, gpu.vram_mb,
                )
                gpu.vram_mb = llmfit_vram_mb
            if llmfit_hw.get("gpu_name") and not gpu.name:
                gpu.name = llmfit_hw["gpu_name"]
            if llmfit_hw.get("has_gpu") and gpu.vendor == "none":
                backend = llmfit_hw.get("backend", "")
                if "cuda" in backend.lower():
                    gpu.vendor = "nvidia"
                elif "metal" in backend.lower():
                    gpu.vendor = "apple"
                elif "rocm" in backend.lower():
                    gpu.vendor = "amd"
                gpu.count = max(gpu.count, llmfit_hw.get("gpu_count", 0))

        # Determine active backend — vLLM takes priority (it's holding GPU memory)
        active_backend = ""
        if vllm_info.running:
            active_backend = "vllm"
        elif ollama_running:
            active_backend = "ollama"

        info = SystemInfo(
            os_name=platform.system(),
            os_version=platform.version(),
            arch=platform.machine(),
            python_version=platform.python_version(),
            ram_gb=round(ram, 1),
            gpu=gpu,
            ollama_installed=ollama_installed,
            ollama_running=ollama_running,
            ollama_models=ollama_models,
            vllm=vllm_info,
            docker_installed=docker,
            profile=_select_profile(gpu, ram),
            active_backend=active_backend,
        )
        self._system = info

        backend_msg = f"active_backend={active_backend}" if active_backend else "no backend running"
        if vllm_info.running:
            backend_msg += f", vLLM on ports {vllm_info.ports} with {vllm_info.models}"
        llmfit_msg = " (llmfit-validated)" if llmfit_hw else ""
        logger.info(f"Detected{llmfit_msg}: {info.os_name} {info.arch}, GPU={gpu.vendor} "
                     f"({gpu.name}, {gpu.vram_mb}MB), RAM={info.ram_gb}GB, "
                     f"profile={info.profile}, {backend_msg}")
        return info

    async def ensure_llmfit(self, auto_install: bool = True) -> bool:
        """Ensure llmfit CLI is installed for hardware-aware model selection.

        Auto-installs via the official installer on Linux/macOS, or via
        scoop/cargo on Windows. Returns True if llmfit is available.
        """
        if shutil.which("llmfit"):
            logger.info("llmfit already installed")
            return True

        if not auto_install:
            logger.info("llmfit not installed (auto_install=False)")
            return False

        system = platform.system()
        logger.info("Installing llmfit for hardware-aware model selection...")

        if system in ("Linux", "Darwin"):
            # Official install script (same pattern as Ollama)
            rc, _, err = await _run(
                ["bash", "-c",
                 "curl -fsSL https://raw.githubusercontent.com/AlexsJones/llmfit/main/install.sh | bash"],
                timeout=120.0,
            )
            if rc == 0 and shutil.which("llmfit"):
                logger.info("llmfit installed via install script")
                return True
            # Fallback: cargo install
            if shutil.which("cargo"):
                logger.info("Install script failed, trying cargo install...")
                rc, _, err = await _run(
                    ["cargo", "install", "llmfit"],
                    timeout=300.0,
                )
                if rc == 0:
                    logger.info("llmfit installed via cargo")
                    return True
            if system == "Darwin" and shutil.which("brew"):
                logger.info("Trying brew install...")
                rc, _, err = await _run(["brew", "install", "llmfit"], timeout=120.0)
                if rc == 0:
                    logger.info("llmfit installed via brew")
                    return True
            logger.warning("Failed to auto-install llmfit: %s", err[:200] if err else "unknown")
            return False

        elif system == "Windows":
            # Try scoop first (most common for Rust CLI tools on Windows)
            if shutil.which("scoop"):
                rc, _, err = await _run(
                    ["scoop", "install", "llmfit"],
                    timeout=120.0,
                )
                if rc == 0:
                    logger.info("llmfit installed via scoop")
                    return True
            # Try cargo
            if shutil.which("cargo"):
                rc, _, err = await _run(
                    ["cargo", "install", "llmfit"],
                    timeout=300.0,
                )
                if rc == 0:
                    logger.info("llmfit installed via cargo")
                    return True
            logger.debug(
                "llmfit not available on Windows (optional). "
                "For hardware-aware model selection, install via: "
                "scoop install llmfit, cargo install llmfit, "
                "or download from https://github.com/AlexsJones/llmfit/releases"
            )
            return False

        logger.warning("Unsupported platform for llmfit auto-install: %s", system)
        return False

    async def ensure_ollama(self, auto_install: bool = True, force: bool = False) -> bool:
        """Ensure Ollama is installed and running.

        SAFETY: Will NOT start Ollama if vLLM is already running on GPU.
        Starting Ollama would load models into VRAM and crash vLLM.
        Use force=True to override (only if you know what you're doing).

        On Linux, auto-installs via curl if not present.
        On macOS, suggests brew install.
        On Windows, suggests manual download.
        Returns True if Ollama is ready.
        """
        if not self._system:
            await self.detect_hardware()
        assert self._system is not None

        # CRITICAL: Don't start Ollama if vLLM is already using GPU
        if self._system.vllm.running and not self._system.ollama_running and not force:
            logger.warning(
                "SKIPPING Ollama — vLLM is already running on ports %s. "
                "Starting Ollama would load models into VRAM and crash vLLM. "
                "Use the vLLM OpenAI-compatible API instead: "
                "AITHER_LLM_BACKEND=openai OPENAI_BASE_URL=http://localhost:%d/v1",
                self._system.vllm.ports,
                self._system.vllm.ports[0] if self._system.vllm.ports else 8200,
            )
            return False

        if self._system.ollama_running:
            logger.info("Ollama already running")
            return True

        if not self._system.ollama_installed and auto_install:
            system = platform.system()
            if system == "Linux":
                logger.info("Installing Ollama via curl...")
                rc, _, err = await _run(
                    ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                    timeout=120.0,
                )
                if rc != 0:
                    logger.error(f"Ollama install failed: {err}")
                    return False
                logger.info("Ollama installed")
            elif system == "Darwin":
                if shutil.which("brew"):
                    logger.info("Installing Ollama via brew...")
                    rc, _, err = await _run(["brew", "install", "ollama"], timeout=120.0)
                    if rc != 0:
                        logger.error(f"brew install ollama failed: {err}")
                        return False
                else:
                    logger.warning("Install Ollama: brew install ollama (or https://ollama.com)")
                    return False
            else:
                logger.warning("Install Ollama from https://ollama.com/download")
                return False

        # Try to start Ollama serve in background
        if not self._system.ollama_running:
            logger.info("Starting Ollama serve...")
            try:
                proc = subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                await asyncio.sleep(3)  # give it time to start
                rc, _, _ = await _run(["ollama", "list"])
                if rc == 0:
                    logger.info("Ollama serve started")
                    return True
                logger.warning("Ollama serve started but not responding yet")
                return False
            except Exception as e:
                logger.error(f"Failed to start Ollama: {e}")
                return False

        return True

    async def pull_models(self, models: list[str] | None = None) -> list[str]:
        """Pull Ollama models. Returns list of successfully pulled models.

        If no models specified, queries llmfit for hardware-scored recommendations
        first, then falls back to static profile-based recommendations.
        """
        if not self._system:
            await self.detect_hardware()
        assert self._system is not None

        if models is None:
            # Try llmfit first — hardware-scored recommendations
            try:
                llmfit_models = await _recommended_models_llmfit()
                if llmfit_models:
                    models = llmfit_models
                    logger.info("Using llmfit-recommended models: %s", models)
            except Exception as exc:
                logger.debug("llmfit model recommendation failed: %s", exc)

            # Fall back to static profile
            if models is None:
                models = _recommended_models(self._system.profile)

        # Skip already-installed models
        existing = set(self._system.ollama_models)
        to_pull = [m for m in models if m not in existing]

        if not to_pull:
            logger.info(f"All {len(models)} models already installed")
            return models

        pulled = []
        for model in to_pull:
            logger.info(f"Pulling {model}...")
            rc, out, err = await _run(["ollama", "pull", model], timeout=600.0)
            if rc == 0:
                pulled.append(model)
                logger.info(f"Pulled {model}")
            else:
                logger.error(f"Failed to pull {model}: {err}")

        return [m for m in models if m in existing] + pulled

    async def ensure_vllm(
        self,
        gpu_count: int = 1,
        port: int = 8200,
        model: str = "",
    ) -> bool:
        """Start a vLLM inference server via Docker.

        Requires Docker and NVIDIA GPU. Returns True if vLLM is running.
        """
        if not self._system:
            await self.detect_hardware()
        assert self._system is not None

        if not self._system.docker_installed:
            logger.warning("Docker not installed — cannot start vLLM container")
            return False

        if self._system.gpu.vendor != "nvidia":
            logger.warning("vLLM Docker requires NVIDIA GPU")
            return False

        if not model:
            # Select vLLM model based on VRAM — use HuggingFace model IDs
            if self._system.gpu.vram_mb >= 48000:
                model = "nvidia/Nemotron-4-340B-Instruct"  # Ultra: 48GB+ VRAM
            elif self._system.gpu.vram_mb >= 24000:
                model = "nvidia/Nemotron-Orchestrator-8B"   # High: 24GB VRAM
            elif self._system.gpu.vram_mb >= 12000:
                model = "nvidia/Nemotron-Orchestrator-8B"   # Mid: 12GB VRAM (fits in 12GB)
            elif self._system.gpu.vram_mb >= 6000:
                model = "meta-llama/Llama-3.2-3B-Instruct"  # Low: 6GB VRAM
            else:
                model = "meta-llama/Llama-3.2-1B-Instruct"  # Minimal

        # Prefer compose file over raw docker run
        compose_path = _find_compose_file()
        if compose_path:
            # Check if ADK compose containers are already running
            rc, out, _ = await _run([
                "docker", "ps", "--filter", "name=adk-vllm-primary",
                "--format", "{{.Names}}"
            ])
            if rc == 0 and "adk-vllm-primary" in out:
                logger.info("ADK vLLM containers already running (via compose)")
                return True

            vram = self._system.gpu.vram_mb
            profile_flag = ["--profile", "dual"] if vram >= 24000 else []
            cmd = (
                ["docker", "compose", "-f", str(compose_path)]
                + profile_flag
                + ["up", "-d"]
            )
            logger.info("Starting vLLM via compose: %s", " ".join(cmd))
            rc, _, err = await _run(cmd, timeout=300.0)
            if rc == 0:
                logger.info("ADK vLLM compose started successfully")
                return True
            logger.warning("Compose startup failed, falling back to docker run: %s", err)

        container_name = f"aither-vllm-{port}"

        # Check if already running
        rc, out, _ = await _run(["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"])
        if rc == 0 and container_name in out:
            logger.info(f"vLLM container {container_name} already running")
            return True

        # Tune context length and memory for available VRAM
        vram = self._system.gpu.vram_mb
        max_model_len = "16384" if vram >= 24000 else "8192" if vram >= 12000 else "4096"
        gpu_util = "0.92" if vram >= 24000 else "0.90" if vram >= 12000 else "0.85"

        logger.info(f"Starting vLLM container: {model} on port {port} (VRAM: {vram}MB)...")
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "--gpus", f'"device={",".join(str(i) for i in range(gpu_count))}"',
            "-p", f"{port}:8000",
            "--shm-size", "4g",
            "vllm/vllm-openai:latest",
            "--model", model,
            "--gpu-memory-utilization", gpu_util,
            "--max-model-len", max_model_len,
        ]
        if gpu_count > 1:
            cmd.extend(["--tensor-parallel-size", str(gpu_count)])
        # Use fp8 quantization on smaller GPUs for better VRAM efficiency
        if vram < 24000 and "Nemotron" in model:
            cmd.extend(["--quantization", "fp8"])

        rc, _, err = await _run(cmd, timeout=300.0)
        if rc != 0:
            logger.error(f"Failed to start vLLM: {err}")
            return False

        logger.info(f"vLLM container started: {container_name}")
        return True

    async def health_check(self) -> dict:
        """Run a quick health check of the inference stack."""
        import httpx

        results: dict = {"ollama": False, "vllm": False, "models": []}

        # Ollama
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://localhost:11434/api/tags")
                if resp.status_code == 200:
                    results["ollama"] = True
                    results["models"] = [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            pass

        # vLLM (check common ports)
        for port in [8200, 8201, 8202, 8203]:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"http://localhost:{port}/v1/models")
                    if resp.status_code == 200:
                        results["vllm"] = True
                        break
            except Exception:
                pass

        return results

    async def full_setup(
        self,
        models: list[str] | None = None,
        install_ollama: bool = True,
        vllm_port: int = 8200,
    ) -> SetupReport:
        """Full automated setup: detect hardware, configure GPU-optimized inference.

        PRIORITY ORDER:
        1. vLLM already running → use it
        2. NVIDIA GPU + Docker → start vLLM containers (PRIMARY path)
        3. Ollama already running → use it (AMD, Apple Silicon, or no Docker)
        4. Start Ollama as fallback (non-NVIDIA or Docker unavailable)
        5. Cloud API keys

        vLLM is the primary backend because it uses GPU memory efficiently with
        paged attention, continuous batching, and tensor parallelism. Ollama is
        the fallback for hardware/platforms where vLLM containers can't run.

        Usage:
            setup = AgentSetup()
            report = await setup.full_setup()
            print(f"Ready: {report.ready}, Backend: {report.backend}")
        """
        report = SetupReport()

        # Step 0: Try AitherZero delegation (pwsh + ADK plugin)
        if os.getenv("AITHER_NO_PWSH") != "1":
            try:
                if await self._try_aitherzero():
                    logger.info("AitherZero + ADK plugin detected — delegating setup")
                    az_report = await self._run_aitherzero_setup()
                    if az_report and az_report.ready:
                        # Save profile marker for Config.from_env()
                        try:
                            (self.data_dir / "detected_profile").write_text(az_report.profile)
                        except Exception:
                            pass
                        return az_report
                    elif az_report:
                        logger.warning(
                            "AitherZero setup completed but not ready: %s",
                            az_report.errors,
                        )
            except Exception as exc:
                logger.debug("AitherZero delegation failed: %s", exc)

        # Step 1: Detect hardware + running backends
        try:
            info = await self.detect_hardware()
            report.system = info
            report.profile = info.profile
        except Exception as e:
            report.errors.append(f"Hardware detection failed: {e}")
            return report

        # Step 1b: Ensure llmfit is installed for smart model selection
        try:
            await self.ensure_llmfit(auto_install=True)
        except Exception as exc:
            logger.debug("llmfit auto-install failed (non-fatal): %s", exc)

        # Step 2: Choose backend — vLLM first, Ollama fallback
        if info.vllm.running:
            # vLLM is already serving — use it
            report.vllm_ready = True
            report.backend = "vllm"
            report.models_available = info.vllm.models
            vllm_port_str = str(info.vllm.ports[0]) if info.vllm.ports else "8200"
            logger.info(
                "vLLM already running on port(s) %s with models %s",
                info.vllm.ports, info.vllm.models,
            )

        elif info.gpu.vendor == "nvidia" and info.docker_installed:
            # NVIDIA GPU + Docker → spin up vLLM containers
            logger.info(
                "NVIDIA GPU detected (%s, %dMB VRAM) with Docker — starting vLLM",
                info.gpu.name, info.gpu.vram_mb,
            )
            try:
                vllm_ok = await self.ensure_vllm(
                    gpu_count=info.gpu.count,
                    port=vllm_port,
                )
                report.vllm_ready = vllm_ok
                if vllm_ok:
                    report.backend = "vllm"
                    # Re-check what models are available after startup
                    await asyncio.sleep(5)  # give vLLM a moment to load model
                    vllm_info = await _check_vllm()
                    report.models_available = vllm_info.models
            except Exception as e:
                report.errors.append(f"vLLM setup failed: {e}")

            # If vLLM failed, fall through to Ollama
            if not report.vllm_ready:
                logger.warning("vLLM setup failed — falling back to Ollama")

        # Ollama fallback — AMD, Apple Silicon, no Docker, or vLLM failed
        if not report.backend:
            if info.ollama_running:
                report.ollama_ready = True
                report.backend = "ollama"
                report.models_available = info.ollama_models

                try:
                    pulled = await self.pull_models(models)
                    report.models_pulled = pulled
                    report.models_available = list(set(report.models_available + pulled))
                except Exception as e:
                    report.errors.append(f"Model pull failed: {e}")

            else:
                try:
                    ollama_ok = await self.ensure_ollama(auto_install=install_ollama)
                    report.ollama_ready = ollama_ok
                    if ollama_ok:
                        report.backend = "ollama"
                except Exception as e:
                    report.errors.append(f"Ollama setup failed: {e}")

                if report.ollama_ready:
                    try:
                        pulled = await self.pull_models(models)
                        report.models_pulled = pulled
                        report.models_available = pulled
                    except Exception as e:
                        report.errors.append(f"Model pull failed: {e}")

        # Cloud API keys as last resort
        if not report.backend:
            if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
                report.backend = "cloud"
                report.ready = True
                logger.info("No local backend — using cloud API keys")

        report.ready = bool(report.backend)
        logger.info(
            f"Setup complete: ready={report.ready}, backend={report.backend}, "
            f"profile={report.profile}, models={report.models_available}, "
            f"errors={report.errors}"
        )

        # Save report
        report_path = self.data_dir / "setup_report.json"
        try:
            report_path.write_text(json.dumps(asdict(report), indent=2))
        except Exception:
            pass

        # Save detected profile so Config.from_env() can apply it on next startup
        profile_marker = self.data_dir / "detected_profile"
        try:
            profile_marker.write_text(report.profile)
        except Exception:
            pass

        return report


# ---------------------------------------------------------------------------
# Convenience function for agents
# ---------------------------------------------------------------------------

async def auto_setup(**kwargs) -> SetupReport:
    """One-liner for agents to setup their environment.

    Usage:
        from adk.setup import auto_setup
        report = await auto_setup()
    """
    setup = AgentSetup()
    return await setup.full_setup(**kwargs)
