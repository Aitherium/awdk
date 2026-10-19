#!/usr/bin/env python3
"""AitherADK Installer — cross-platform, auto-detecting setup for the Agent Development Kit.

Detects OS, GPU hardware, VRAM, and CUDA version to select the best hardware profile.
Installs awdk and (with --with-local-orchestrator) llama.cpp + the orchestrator GGUF.

Usage:
    python install.py
    python install.py --dry-run
    python install.py --profile nvidia_high
    python install.py --skip-models

Pure stdlib — no pip dependencies required to run this script.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI color output
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Check whether the terminal supports ANSI color codes."""
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    if sys.platform == "win32":
        # Windows 10+ supports ANSI if we enable virtual terminal processing,
        # but the safest check is whether we are in a real terminal.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # Enable ANSI on Windows 10+
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
        except Exception:
            return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    """Wrap text in ANSI escape if color is supported."""
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _c("1", text)


def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def dim(text: str) -> str:
    return _c("2", text)


def blue(text: str) -> str:
    return _c("34", text)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"  {green('>>>')} {msg}")


def warn(msg: str) -> None:
    print(f"  {yellow('!!!')} {msg}")


def error(msg: str) -> None:
    print(f"  {red('ERR')} {msg}")


def step(n: int, total: int, msg: str) -> None:
    label = dim(f"[{n}/{total}]")
    print(f"\n  {label} {bold(msg)}")


def detail(msg: str) -> None:
    print(f"        {dim(msg)}")


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

BANNER = r"""
     _    _ _   _               _    ____  _  __
    / \  (_) |_| |__   ___ _ __/ \  |  _ \| |/ /
   / _ \ | | __| '_ \ / _ \ '__/ _ \ | | | | ' /
  / ___ \| | |_| | | |  __/ | / ___ \| |_| | . \
 /_/   \_\_|\__|_| |_|\___|_|/_/   \_\____/|_|\_\
"""


def print_banner() -> None:
    print(cyan(BANNER))
    print(bold("  Agent Development Kit — Installer"))
    print(dim("  Build AI agents that work with any LLM backend\n"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GPUInfo:
    """Detected GPU information."""
    vendor: str = "none"  # "nvidia", "amd", "apple", "none"
    name: str = ""
    vram_mb: int = 0          # Best single GPU VRAM (for profile selection)
    cuda_version: str = ""
    driver_version: str = ""
    gpu_count: int = 0
    total_vram_mb: int = 0    # Sum of all GPUs
    all_gpus: list = field(default_factory=list)  # List of {"name": str, "vram_mb": int}


@dataclass
class SystemInfo:
    """Collected system information."""
    os_name: str = ""           # "Windows", "Linux", "macOS"
    os_version: str = ""
    arch: str = ""              # "x86_64", "arm64"
    python_version: str = ""
    python_path: str = ""
    gpu: GPUInfo = field(default_factory=GPUInfo)
    ram_gb: float = 0.0


@dataclass
class HardwareProfile:
    """A hardware profile loaded from profiles/ YAML."""
    name: str = "cpu_only"
    description: str = ""
    models: dict[str, str] = field(default_factory=dict)
    recommended_models: list[str] = field(default_factory=list)
    limits: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Minimal YAML parser (stdlib only, no pyyaml dependency)
# ---------------------------------------------------------------------------

def _parse_simple_yaml(text: str) -> dict:
    """Parse a simple YAML file into a nested dict.

    Handles:
      - Scalar keys at any indent level
      - Nested mappings (multi-level)
      - Sequences (- item)
      - Comments and blank lines
      - Block scalars with > or |
      - Inline comments after values
      - Quoted strings
      - Empty sequences ([])

    This is deliberately minimal — it only needs to read the profile YAMLs
    shipped with AitherADK, not arbitrary YAML.
    """
    lines = text.splitlines()

    def _clean_value(val: str) -> str:
        """Strip inline comments and surrounding quotes from a scalar value."""
        # Strip inline comments (must have 2+ spaces before #)
        if "  #" in val:
            val = val[: val.index("  #")].strip()
        # Remove surrounding quotes
        if len(val) >= 2:
            if (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'"):
                val = val[1:-1]
        return val

    def _parse_block(start: int, base_indent: int) -> tuple[dict, int]:
        """Parse a YAML block at a given indent level.

        Returns:
            (parsed_dict, next_line_index)
        """
        result: dict = {}
        i = start

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Skip blanks and comments
            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            indent = len(line) - len(line.lstrip())

            # If we've dedented back to or past our parent, we are done
            if indent < base_indent:
                break

            # Skip lines deeper than our level (already consumed by recursion)
            if indent > base_indent:
                i += 1
                continue

            # Must be a key: value line at our indent
            if ":" not in stripped:
                i += 1
                continue

            key, _, raw_val = stripped.partition(":")
            key = key.strip()
            raw_val = raw_val.strip()

            # Block scalar (> or |)
            if raw_val in (">", "|"):
                parts: list[str] = []
                i += 1
                while i < len(lines):
                    sub = lines[i]
                    sub_stripped = sub.strip()
                    sub_indent = len(sub) - len(sub.lstrip())
                    if sub_stripped and sub_indent <= base_indent:
                        break
                    if sub_stripped:
                        parts.append(sub_stripped)
                    i += 1
                result[key] = " ".join(parts)
                continue

            # Inline empty sequence []
            if raw_val == "[]":
                result[key] = []
                i += 1
                continue

            # Inline value present
            if raw_val:
                result[key] = _clean_value(raw_val)
                i += 1
                continue

            # No inline value — look ahead to determine child type
            i += 1
            # Find the first non-blank, non-comment child line
            child_indent = -1
            peek = i
            while peek < len(lines):
                ps = lines[peek].strip()
                if ps and not ps.startswith("#"):
                    child_indent = len(lines[peek]) - len(lines[peek].lstrip())
                    break
                peek += 1

            # No children found
            if child_indent <= base_indent:
                result[key] = ""
                continue

            # Determine if children form a sequence or a mapping
            first_child = lines[peek].strip()

            if first_child.startswith("- "):
                # Sequence
                items: list = []
                while i < len(lines):
                    sub = lines[i]
                    sub_stripped = sub.strip()
                    sub_indent = len(sub) - len(sub.lstrip())
                    if not sub_stripped or sub_stripped.startswith("#"):
                        i += 1
                        continue
                    if sub_indent < child_indent:
                        break
                    if sub_stripped.startswith("- "):
                        val = sub_stripped[2:].strip()
                        items.append(_clean_value(val))
                    i += 1
                result[key] = items
            else:
                # Nested mapping — recurse
                child_dict, i = _parse_block(i, child_indent)
                result[key] = child_dict

        return result, i

    parsed, _ = _parse_block(0, 0)
    return parsed


def load_profile(name: str) -> HardwareProfile:
    """Load a hardware profile from the profiles/ directory.

    Handles two profile formats:
      - Simple format: ``recommended_models`` list at top level
      - Rich format: ``local.models`` nested under ``local:``

    Both are normalized into the same HardwareProfile dataclass.
    """
    profiles_dir = Path(__file__).parent / "profiles"
    path = profiles_dir / f"{name}.yaml"
    if not path.exists():
        error(f"Profile not found: {path}")
        sys.exit(1)

    data = _parse_simple_yaml(path.read_text(encoding="utf-8"))
    profile = HardwareProfile()
    profile.name = data.get("name", name)
    profile.description = data.get("description", "")

    if isinstance(data.get("models"), dict):
        profile.models = data["models"]

    # Recommended models: try top-level list first, then local.models
    if isinstance(data.get("recommended_models"), list):
        profile.recommended_models = data["recommended_models"]
    elif isinstance(data.get("local"), dict):
        local_section = data["local"]
        if isinstance(local_section.get("models"), list):
            profile.recommended_models = local_section["models"]

    # Limits: try top-level, then resource_limits
    limits_src = data.get("limits") or data.get("resource_limits")
    if isinstance(limits_src, dict):
        profile.limits = {}
        for k, v in limits_src.items():
            if v:
                try:
                    profile.limits[k] = int(float(v))
                except (ValueError, TypeError):
                    pass

    return profile


def list_profiles() -> list[str]:
    """Return names of all available hardware profiles."""
    profiles_dir = Path(__file__).parent / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(p.stem for p in profiles_dir.glob("*.yaml"))


# ---------------------------------------------------------------------------
# Detection: OS
# ---------------------------------------------------------------------------

def detect_os() -> tuple[str, str, str]:
    """Detect the operating system.

    Returns:
        (os_name, os_version, arch)
    """
    system = platform.system()
    arch = platform.machine().lower()

    if system == "Darwin":
        os_name = "macOS"
        ver = platform.mac_ver()[0] or platform.release()
    elif system == "Windows":
        os_name = "Windows"
        ver = platform.version()
    elif system == "Linux":
        os_name = "Linux"
        ver = platform.release()
    else:
        os_name = system
        ver = platform.release()

    # Normalize arch names
    if arch in ("x86_64", "amd64"):
        arch = "x86_64"
    elif arch in ("arm64", "aarch64"):
        arch = "arm64"

    return os_name, ver, arch


# ---------------------------------------------------------------------------
# Detection: GPU
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], timeout: int = 10) -> Optional[str]:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def detect_nvidia_gpu() -> Optional[GPUInfo]:
    """Detect NVIDIA GPU via nvidia-smi."""
    # Try to find nvidia-smi
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        # Common Windows locations
        if sys.platform == "win32":
            for candidate in [
                r"C:\Windows\System32\nvidia-smi.exe",
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ]:
                if os.path.isfile(candidate):
                    nvidia_smi = candidate
                    break
        if not nvidia_smi:
            return None

    # Query GPU name and memory
    csv_output = _run_cmd([
        nvidia_smi,
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    if not csv_output:
        return None

    lines = [l.strip() for l in csv_output.splitlines() if l.strip()]
    if not lines:
        return None

    # Parse all GPUs, find best (max VRAM), sum total
    all_gpus = []
    best_name = "NVIDIA GPU"
    best_vram = 0
    total_vram = 0
    best_driver = ""
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        g_name = parts[0]
        try:
            g_vram = int(float(parts[1]))
        except (ValueError, IndexError):
            g_vram = 0
        g_driver = parts[2]
        all_gpus.append({"name": g_name, "vram_mb": g_vram})
        total_vram += g_vram
        if g_vram > best_vram:
            best_vram = g_vram
            best_name = g_name
            best_driver = g_driver

    if not all_gpus:
        return None

    # Detect CUDA version
    cuda_ver = ""
    cuda_output = _run_cmd([nvidia_smi])
    if cuda_output:
        match = re.search(r"CUDA Version:\s*([\d.]+)", cuda_output)
        if match:
            cuda_ver = match.group(1)

    return GPUInfo(
        vendor="nvidia",
        name=best_name,
        vram_mb=best_vram,
        cuda_version=cuda_ver,
        driver_version=best_driver,
        gpu_count=len(all_gpus),
        total_vram_mb=total_vram,
        all_gpus=all_gpus,
    )


def detect_amd_gpu() -> Optional[GPUInfo]:
    """Detect AMD GPU via rocm-smi."""
    rocm_smi = shutil.which("rocm-smi")
    if not rocm_smi:
        return None

    output = _run_cmd([rocm_smi, "--showproductname"])
    if not output:
        return None

    gpu_name = ""
    for line in output.splitlines():
        if "GPU" in line and ":" in line:
            gpu_name = line.split(":", 1)[1].strip()
            break

    # Try to get VRAM
    vram_mb = 0
    mem_output = _run_cmd([rocm_smi, "--showmeminfo", "vram"])
    if mem_output:
        for line in mem_output.splitlines():
            if "Total" in line:
                match = re.search(r"(\d+)", line)
                if match:
                    # rocm-smi reports in bytes or MB depending on version
                    val = int(match.group(1))
                    vram_mb = val if val < 1_000_000 else val // (1024 * 1024)

    return GPUInfo(
        vendor="amd",
        name=gpu_name or "AMD GPU (ROCm)",
        vram_mb=vram_mb,
    )


def detect_apple_silicon() -> Optional[GPUInfo]:
    """Detect Apple Silicon via sysctl."""
    if platform.system() != "Darwin":
        return None

    # Check for Apple Silicon
    brand = _run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    if not brand or "Apple" not in brand:
        return None

    chip_name = brand.strip()

    # Get total unified memory (Apple Silicon shares RAM with GPU)
    memsize_str = _run_cmd(["sysctl", "-n", "hw.memsize"])
    vram_mb = 0
    if memsize_str:
        try:
            vram_mb = int(memsize_str) // (1024 * 1024)
        except ValueError:
            pass

    return GPUInfo(
        vendor="apple",
        name=chip_name,
        vram_mb=vram_mb,
    )


def detect_gpu() -> GPUInfo:
    """Detect GPU hardware. Tries NVIDIA, then AMD, then Apple Silicon.

    Returns:
        GPUInfo with vendor="none" if no GPU is detected.
    """
    # NVIDIA first (most common for ML)
    gpu = detect_nvidia_gpu()
    if gpu:
        return gpu

    # AMD ROCm
    gpu = detect_amd_gpu()
    if gpu:
        return gpu

    # Apple Silicon
    gpu = detect_apple_silicon()
    if gpu:
        return gpu

    return GPUInfo(vendor="none")


# ---------------------------------------------------------------------------
# Detection: Python
# ---------------------------------------------------------------------------

def check_python() -> tuple[bool, str, str]:
    """Check if the current Python meets the >=3.10 requirement.

    Returns:
        (meets_requirement, version_string, python_path)
    """
    ver = sys.version_info
    version_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    meets = ver >= (3, 10)
    return meets, version_str, sys.executable


# ---------------------------------------------------------------------------
# Detection: System RAM
# ---------------------------------------------------------------------------

def detect_ram_gb() -> float:
    """Detect total system RAM in GB."""
    system = platform.system()

    if system == "Darwin":
        out = _run_cmd(["sysctl", "-n", "hw.memsize"])
        if out:
            try:
                return int(out) / (1024 ** 3)
            except ValueError:
                pass

    elif system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)
        except (OSError, ValueError):
            pass

    elif system == "Windows":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return stat.ullTotalPhys / (1024 ** 3)
        except Exception:
            pass

    return 0.0


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

def select_profile(gpu: GPUInfo, ram_gb: float = 0.0) -> str:
    """Select the best hardware profile based on detected GPU and RAM.

    Profile selection ladder (NVIDIA):
      - 80+ GB VRAM, 128+ GB RAM  -> server
      - 48+ GB VRAM, 64+ GB RAM   -> workstation
      - 24+ GB VRAM               -> standard
      - 8-23 GB VRAM              -> minimal
      - <8 GB VRAM                -> nvidia_low
      - No GPU                    -> cpu_only

    Returns:
        Profile name string.
    """
    if gpu.vendor == "apple":
        return "apple_silicon"

    if gpu.vendor == "amd":
        return "amd"

    if gpu.vendor == "nvidia":
        vram_gb = gpu.vram_mb / 1024

        if vram_gb >= 80 and ram_gb >= 128:
            return "server"
        elif vram_gb >= 48 and ram_gb >= 64:
            return "workstation"
        elif vram_gb >= 24:
            return "standard"
        elif vram_gb >= 8:
            return "minimal"
        else:
            return "nvidia_low"

    return "cpu_only"


# ---------------------------------------------------------------------------
# llama.cpp
# ---------------------------------------------------------------------------

def check_llamacpp() -> tuple[bool, str]:
    """Check whether llama.cpp's ``llama-server`` is on PATH.

    Returns:
        (is_installed, version_or_message)
    """
    if not shutil.which("llama-server"):
        return False, "not found in PATH (installed by --with-local-orchestrator)"
    version_output = _run_cmd(["llama-server", "--version"])
    return True, version_output or "installed (version unknown)"


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def install_adk(dry_run: bool = False) -> bool:
    """Install awdk via pip."""
    # Check if already installed
    try:
        output = _run_cmd([sys.executable, "-m", "pip", "show", "awdk"])
        if output and "Name: awdk" in output:
            info("awdk is already installed")
            return True
    except Exception:
        pass

    # Install from the current directory if pyproject.toml exists, otherwise from PyPI
    install_dir = Path(__file__).parent
    has_pyproject = (install_dir / "pyproject.toml").exists()

    if has_pyproject:
        cmd = [sys.executable, "-m", "pip", "install", "-e", str(install_dir)]
        source = f"local ({install_dir})"
    else:
        cmd = [sys.executable, "-m", "pip", "install", "awdk"]
        source = "PyPI"

    if dry_run:
        info(f"Would install awdk from {source}")
        detail(" ".join(cmd))
        return True

    info(f"Installing awdk from {source} ...")
    try:
        result = subprocess.run(cmd, timeout=120)
        if result.returncode == 0:
            info(green("awdk installed successfully"))
            return True
        else:
            error("pip install failed")
            return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        error(f"Installation failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def run_health_check(port: int, dry_run: bool = False) -> bool:
    """Run a quick health check: the local llama-server lists its models."""
    url = f"http://127.0.0.1:{port}/v1/models"
    if dry_run:
        info(f"Would run health check ({url})")
        return True
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 -- nothing serving yet is reported, not raised
        warn(f"No llama-server answering at {url}")
        return False
    models = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    info(f"llama.cpp serves {len(models)} model(s): {', '.join(models[:5])}")
    return bool(models)


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def print_system_summary(sys_info: SystemInfo) -> None:
    """Print a formatted summary of the detected system."""
    print(f"\n  {bold('System')}")
    print(f"    OS:      {sys_info.os_name} {sys_info.os_version} ({sys_info.arch})")
    if sys_info.ram_gb > 0:
        print(f"    RAM:     {sys_info.ram_gb:.1f} GB")
    print(f"    Python:  {sys_info.python_version} ({sys_info.python_path})")

    gpu = sys_info.gpu
    if gpu.vendor == "none":
        print(f"    GPU:     {dim('None detected')}")
    else:
        if gpu.gpu_count > 1 and gpu.all_gpus:
            print(f"    GPUs:    {gpu.gpu_count} detected")
            for i, g in enumerate(gpu.all_gpus):
                g_vram = g['vram_mb'] / 1024
                print(f"      GPU {i}: {g['name']} ({g_vram:.1f} GB)")
            total_gb = gpu.total_vram_mb / 1024 if gpu.total_vram_mb else gpu.vram_mb / 1024
            print(f"    Best:    {gpu.name} ({gpu.vram_mb / 1024:.1f} GB VRAM)")
            print(f"    Total:   {total_gb:.1f} GB VRAM")
        else:
            vram_str = f"{gpu.vram_mb / 1024:.1f} GB VRAM" if gpu.vram_mb else "VRAM unknown"
            print(f"    GPU:     {gpu.name} ({vram_str})")
        if gpu.cuda_version:
            print(f"    CUDA:    {gpu.cuda_version}")
        if gpu.driver_version:
            print(f"    Driver:  {gpu.driver_version}")


def print_profile_summary(profile: HardwareProfile) -> None:
    """Print the selected hardware profile."""
    print(f"\n  {bold('Hardware Profile')}: {cyan(profile.name)}")
    if profile.description:
        print(f"    {profile.description}")
    if profile.models:
        # Support both key schemes: default/small/large and chat/reasoning/coding
        if "default" in profile.models:
            default_val = profile.models.get("default", "?")
            print(f"    Default model:  {bold(default_val)}")
            print(f"    Small model:    {profile.models.get('small', '?')}")
            print(f"    Large model:    {profile.models.get('large', '?')}")
        elif "chat" in profile.models:
            chat_val = profile.models.get("chat", "null")
            print(f"    Chat model:      {bold(chat_val)}")
            reasoning = profile.models.get("reasoning", "null")
            if reasoning and reasoning != "null":
                print(f"    Reasoning model: {reasoning}")
            coding = profile.models.get("coding", "null")
            if coding and coding != "null":
                print(f"    Coding model:    {coding}")
            embedding = profile.models.get("embedding", "?")
            print(f"    Embedding model: {embedding}")
    if profile.recommended_models:
        # Filter out null values that may appear in the rich format
        display_models = [m for m in profile.recommended_models if m and m != "null"]
        if display_models:
            print(f"    Models to pull:  {', '.join(display_models)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TOTAL_STEPS = 7


def main() -> int:
    """Run the AitherADK installer.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    parser = argparse.ArgumentParser(
        description="AitherADK Installer — auto-detecting cross-platform setup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Available profiles (auto-detected based on GPU and RAM):
              cpu_only       No GPU — cloud LLM fallback
              nvidia_low     NVIDIA <8 GB VRAM — small quantized models
              minimal        Single GPU, 8-12 GB VRAM — chat model only
              standard       Single GPU, 24 GB VRAM — chat + reasoning
              workstation    Dual GPU, 48+ GB VRAM — full model stack
              server         Multi-GPU, 80+ GB VRAM — all services
              amd            AMD GPU with ROCm
              apple_silicon  Apple M1/M2/M3/M4
        """),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Override hardware profile (e.g., nvidia_high, cpu_only)",
    )
    parser.add_argument(
        "--skip-models",
        action="store_true",
        help="Skip local model setup",
    )
    parser.add_argument(
        "--with-local-orchestrator",
        action="store_true",
        help="Also install Nemotron-Orchestrator-8B locally via llama.cpp "
             "(native OpenAI endpoint at http://127.0.0.1:8209/v1, no Docker)",
    )
    parser.add_argument(
        "--orchestrator-quant",
        type=str,
        default=None,
        help="GGUF quant for local orchestrator (Q3_K_M, Q4_K_M, Q5_K_M, Q6_K, Q8_0). "
             "Default: Q8_0 (~8.7 GB) when it fits, else the largest that does; "
             "pass Q4_K_M (~5.0 GB) for a small device",
    )
    parser.add_argument(
        "--orchestrator-port",
        type=int,
        default=8209,
        help="Port for local orchestrator server (default: 8209)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="No prompts — auto-accept defaults",
    )
    args = parser.parse_args()

    dry_run: bool = args.dry_run
    profile_override: Optional[str] = args.profile
    skip_models: bool = args.skip_models

    # --- Banner ---
    print_banner()

    if dry_run:
        print(yellow("  DRY RUN — no changes will be made\n"))

    # --- Step 1: Detect OS ---
    step(1, TOTAL_STEPS, "Detecting operating system")
    os_name, os_ver, arch = detect_os()
    info(f"{os_name} {os_ver} ({arch})")

    # --- Step 2: Detect GPU ---
    step(2, TOTAL_STEPS, "Detecting GPU hardware")
    gpu = detect_gpu()
    if gpu.vendor == "none":
        warn("No supported GPU detected — will use CPU-only profile")
    elif gpu.vendor == "nvidia":
        vram_gb = gpu.vram_mb / 1024
        info(f"NVIDIA: {gpu.name} ({vram_gb:.1f} GB VRAM)")
        if gpu.cuda_version:
            info(f"CUDA: {gpu.cuda_version}")
    elif gpu.vendor == "amd":
        info(f"AMD: {gpu.name}")
        if gpu.vram_mb:
            info(f"VRAM: {gpu.vram_mb / 1024:.1f} GB")
    elif gpu.vendor == "apple":
        info(f"Apple Silicon: {gpu.name}")
        if gpu.vram_mb:
            info(f"Unified memory: {gpu.vram_mb / 1024:.0f} GB")

    # Detect RAM (used for profile selection and summary)
    ram_gb = detect_ram_gb()
    if ram_gb > 0:
        info(f"RAM: {ram_gb:.1f} GB")

    # --- Step 3: Select profile ---
    step(3, TOTAL_STEPS, "Selecting hardware profile")
    if profile_override:
        available = list_profiles()
        if profile_override not in available:
            error(f"Unknown profile: {profile_override}")
            error(f"Available profiles: {', '.join(available)}")
            return 1
        profile_name = profile_override
        info(f"Using override: {bold(profile_name)}")
    else:
        profile_name = select_profile(gpu, ram_gb=ram_gb)
        info(f"Auto-selected: {bold(profile_name)}")

    profile = load_profile(profile_name)

    # --- Step 4: Check Python ---
    step(4, TOTAL_STEPS, "Checking Python version")
    py_ok, py_ver, py_path = check_python()
    if py_ok:
        info(f"Python {py_ver} ({py_path})")
    else:
        error(f"Python {py_ver} is too old — AitherADK requires Python 3.10+")
        error("Install Python 3.10+ from https://python.org")
        return 1

    # --- Step 5: Install awdk ---
    step(5, TOTAL_STEPS, "Installing awdk")
    if not install_adk(dry_run=dry_run):
        error("Failed to install awdk")
        return 1

    # --- Step 6: Check llama.cpp ---
    step(6, TOTAL_STEPS, "Checking llama.cpp")
    llamacpp_ok, llamacpp_info = check_llamacpp()
    if llamacpp_ok:
        info(f"llama.cpp: {llamacpp_info}")
    else:
        info(f"llama.cpp: {llamacpp_info}")
    if skip_models:
        info("Skipping local model setup (--skip-models)")

    # --- Step 7: Health check ---
    step(7, TOTAL_STEPS, "Running health check")
    if llamacpp_ok and not skip_models:
        run_health_check(getattr(args, "orchestrator_port", 8209), dry_run=dry_run)
    else:
        info("Skipping health check (no local llama-server yet)")

    # --- Summary ---
    sys_info = SystemInfo(
        os_name=os_name,
        os_version=os_ver,
        arch=arch,
        python_version=py_ver,
        python_path=py_path,
        gpu=gpu,
        ram_gb=ram_gb,
    )
    print_system_summary(sys_info)
    print_profile_summary(profile)

    # --- Optional: install local orchestrator (Nemotron-8B via llama.cpp) ---
    install_orch = bool(getattr(args, "with_local_orchestrator", False))
    if not install_orch and not getattr(args, "non_interactive", False):
        try:
            resp = input(
                "\n  Install local Nemotron-Orchestrator-8B "
                "(native, no Docker, ~5GB)? [y/N] "
            ).strip().lower()
            install_orch = resp == "y"
        except (EOFError, KeyboardInterrupt):
            install_orch = False

    if install_orch:
        print(f"\n  {bold('Installing local orchestrator (Nemotron-8B / llama.cpp)...')}")
        try:
            from adk import llamacpp_setup  # type: ignore
        except ImportError:
            warn("adk.llamacpp_setup not importable yet — run after restart: "
                 "aither setup llamacpp")
        else:
            try:
                result = llamacpp_setup.install(
                    quant=getattr(args, "orchestrator_quant", None),
                    port=getattr(args, "orchestrator_port", 8209),
                    service=True,
                    dry_run=dry_run,
                )
                if getattr(result, "success", False):
                    print(f"  {green('[OK]')} Local orchestrator: "
                          f"http://127.0.0.1:{getattr(args, 'orchestrator_port', 8209)}/v1")
                else:
                    warn("Local orchestrator install reported failure — "
                         "retry: aither setup llamacpp")
            except Exception as e:
                warn(f"Local orchestrator install failed: {e}")
                warn("Retry with: aither setup llamacpp")

    # --- Done ---
    print(f"\n  {green(bold('Ready!'))}")
    print(f"  Run: {cyan('aither-serve')}")
    print(f"       {dim('or: python -m adk.server')}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
