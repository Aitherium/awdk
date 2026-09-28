"""Hardware probing for the first-run wizard.

Detects:
  - RAM (total system memory)
  - CPU cores
  - GPU (reuses setup_cli GPU detection)
  - Ollama installation status
  - Disks: every mounted volume with its size, free space and filesystem
    (stdlib only; the storage plane's `awstorage files scan` indexes them)

Provides: recommend_setup() → recommendation dict with rationale.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
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
    # [{"mount": "C:/", "device": ..., "fs": "NTFS", "total_bytes": ..., "free_bytes": ...}]
    disks: list = field(default_factory=list)


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


# Pseudo / virtual filesystems: not disks, and statvfs on some of them hangs or lies.
_PSEUDO_FS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "securityfs",
    "pstore", "debugfs", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs",
    "bpf", "autofs", "binfmt_misc", "overlay", "squashfs", "nsfs", "ramfs", "rpc_pipefs",
    "efivarfs", "selinuxfs", "fuse.gvfsd-fuse", "fuse.portal", "devfs", "nullfs",
})


def _disk_usage(mount: str) -> tuple[int, int] | None:
    try:
        u = shutil.disk_usage(mount)
    except OSError:
        return None
    return int(u.total), int(u.free)


def _windows_disks() -> list[dict]:
    import ctypes  # noqa: PLC0415
    import string  # noqa: PLC0415

    out: list[dict] = []
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)  # 2 removable, 3 fixed, 4 remote, 5 cdrom, 6 ramdisk
        if dtype not in (2, 3, 4, 6):
            continue
        fs_buf = ctypes.create_unicode_buffer(64)
        name_buf = ctypes.create_unicode_buffer(256)
        ok = k32.GetVolumeInformationW(root, name_buf, 256, None, None, None, fs_buf, 64)
        usage = _disk_usage(root)
        if not ok or usage is None:
            continue  # an empty card reader / a disconnected share
        out.append({"mount": f"{letter}:/", "device": name_buf.value or f"{letter}:",
                    "fs": fs_buf.value, "total_bytes": usage[0], "free_bytes": usage[1],
                    "kind": {2: "removable", 3: "fixed", 4: "network", 6: "ramdisk"}[dtype]})
    return out


def _posix_disks() -> list[dict]:
    lines: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        try:  # macOS / BSD: "dev on /mnt (apfs, local, ...)"
            out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5,
                                 encoding="utf-8", errors="replace")
            for ln in out.stdout.splitlines():
                if " on " in ln and " (" in ln:
                    dev, rest = ln.split(" on ", 1)
                    mnt, opts = rest.rsplit(" (", 1)
                    mnt = mnt.replace(" ", r"\040")  # same escaping as /proc/mounts
                    lines.append(f"{dev} {mnt} {opts.split(',')[0].strip(')')} -")
        except Exception:  # noqa: BLE001 -- no mount table == no disks reported
            return []
    disks: list[dict] = []
    seen: set[str] = set()
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], parts[1].replace(r"\040", " "), parts[2]
        if fs in _PSEUDO_FS or mnt.startswith(("/proc", "/sys", "/dev", "/run")):
            continue
        if dev in seen:  # a bind mount of a device already listed
            continue
        usage = _disk_usage(mnt)
        if usage is None or usage[0] == 0:
            continue
        seen.add(dev)
        disks.append({"mount": mnt, "device": dev, "fs": fs, "total_bytes": usage[0],
                      "free_bytes": usage[1], "kind": "network" if fs in (
                          "nfs", "nfs4", "cifs", "smb3", "9p", "drvfs") else "fixed"})
    return disks


def detect_disks() -> list[dict]:
    """Every mounted volume: mount, device, fs, total/free bytes, kind. Never raises."""
    try:
        if platform.system() == "Windows":
            return _windows_disks()
        return _posix_disks()
    except Exception as e:  # noqa: BLE001 -- a probe must never break the wizard
        logger.debug("disk detection failed: %s", e)
        return []


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
        disks=detect_disks(),
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
