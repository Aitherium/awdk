"""What memory does THIS machine have for a model? One number, and where it came from.

The appliance selector asks ``nvidia-smi`` and falls back to ``/proc/meminfo``, which is
right for an awnix box and wrong for a customer's laptop: no NVIDIA tool on a Mac or an
AMD card, no ``/proc`` on Windows. This probe has no hard dependency on any of them --
every source is tried, a missing tool is a normal answer, and the result names its source
so the recommendation can say what it sized against.

Order, first hit wins:

1. NVIDIA      ``nvidia-smi`` total VRAM of the largest card.
2. Apple       Apple Silicon unified memory: the GPU addresses system RAM.
3. Other GPU   AMD / Intel dedicated VRAM, from the driver (Linux sysfs, the Windows
               display-class registry key) or from ``vulkaninfo``'s device-local heap of
               a DISCRETE device. Below ``MIN_GPU_MB`` it is an integrated GPU sharing
               system RAM and is not a budget of its own.
4. RAM         CPU inference.

The headroom rule is NOT applied here; ``catalogue.fits`` owns it.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MIB = 1024 * 1024
#: Dedicated VRAM below this is an integrated GPU's carve-out of system RAM (Intel UHD
#: reports 128 MB-1 GB): sizing a model against it would refuse a machine whose CPU and
#: RAM run the model fine.
MIN_GPU_MB = 2048
#: The Windows display-adapter device class.
_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


@dataclass(frozen=True)
class Hardware:
    """The memory budget a model is sized against."""

    kind: str        # nvidia | apple | gpu | ram | given | none
    budget_mb: int   # VRAM for a GPU, unified memory on Apple, RAM on CPU
    detail: str      # human-readable source, e.g. "NVIDIA GeForce RTX 5090"
    ram_mb: int      # system RAM, always reported

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        gb = self.budget_mb / 1024
        if self.kind == "nvidia":
            return f"{self.detail}: {gb:.1f} GB VRAM"
        if self.kind == "apple":
            return f"{self.detail}: {gb:.1f} GB unified memory"
        if self.kind == "gpu":
            return f"{self.detail}: {gb:.1f} GB VRAM (Vulkan)"
        if self.kind == "given":
            return f"sized against {gb:.1f} GB ({self.detail})"
        if self.kind == "ram":
            return f"no usable GPU found; {gb:.1f} GB RAM (CPU inference, slower)"
        return "could not read GPU memory or RAM"


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run(argv: List[str], timeout: float = 15.0) -> Optional[str]:
    """stdout of ``argv``, or None when it is absent, fails, or times out."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _ram_mb(system: str) -> int:
    """Total system RAM in MiB; 0 when it cannot be read."""
    try:
        if system == "Windows":
            import ctypes

            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return int(ms.ullTotalPhys // MIB)
        if system == "Darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            return int(out.strip()) // MIB if out and out.strip().isdigit() else 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // MIB)
    except (OSError, ValueError, AttributeError):
        return 0


def _nvidia() -> Optional[Tuple[str, int]]:
    """(name, MiB) of the largest NVIDIA card, or None without one / without the tool."""
    exe = _which("nvidia-smi")
    if not exe:
        return None
    out = _run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    best: Optional[Tuple[str, int]] = None
    for line in (out or "").splitlines():
        name, _, mem = line.rpartition(",")
        if mem.strip().isdigit() and (best is None or int(mem) > best[1]):
            best = (name.strip() or "NVIDIA GPU", int(mem))
    return best


def _sysfs_vram() -> List[Tuple[str, int]]:
    """Linux: dedicated VRAM the amdgpu / xe drivers publish per card."""
    out = []
    for f in sorted(Path("/sys/class/drm").glob("card*/device/mem_info_vram_total")):
        try:
            out.append((f"GPU {f.parent.parent.name}", int(f.read_text().strip()) // MIB))
        except (OSError, ValueError):
            continue
    return out


def _registry_vram() -> List[Tuple[str, int]]:
    """Windows: ``HardwareInformation.qwMemorySize`` of every display adapter.

    The 64-bit value; the older ``HardwareInformation.MemorySize`` is a DWORD that wraps
    at 4 GB and reports an 8 GB card as 0.
    """
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as cls:
            for i in range(winreg.QueryInfoKey(cls)[0]):
                try:
                    with winreg.OpenKey(cls, winreg.EnumKey(cls, i)) as dev:
                        size = winreg.QueryValueEx(dev, "HardwareInformation.qwMemorySize")[0]
                        name = winreg.QueryValueEx(dev, "DriverDesc")[0]
                except OSError:
                    continue
                if isinstance(size, bytes):
                    size = int.from_bytes(size, "little")
                out.append((str(name), int(size) // MIB))
    except OSError:
        return []
    return out


def _vulkan_vram() -> List[Tuple[str, int]]:
    """Largest device-local heap of each DISCRETE device ``vulkaninfo`` lists."""
    exe = _which("vulkaninfo")
    text = _run([exe], timeout=30.0) if exe else None
    out: List[Tuple[str, int]] = []
    if not text:
        return out
    # One block per physical device: "GPU0:" ... up to the next "GPUn:" header.
    for block in re.split(r"(?m)^GPU\d+:\s*$", text)[1:]:
        if "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU" not in block:
            continue
        name = re.search(r"deviceName\s*=\s*(.+)", block)
        best = 0
        for heap in re.split(r"memoryHeaps\[\d+\]:", block)[1:]:
            size = re.search(r"size\s*=\s*(\d+)", heap)
            head = heap.split("memoryTypes", 1)[0]
            if size and "MEMORY_HEAP_DEVICE_LOCAL_BIT" in head:
                best = max(best, int(size.group(1)) // MIB)
        if best:
            out.append((name.group(1).strip() if name else "Vulkan GPU", best))
    return out


def _other_gpu(system: str) -> Optional[Tuple[str, int]]:
    """(name, MiB) of the largest non-NVIDIA dedicated GPU worth sizing against."""
    found: List[Tuple[str, int]] = []
    if system == "Linux":
        found = _sysfs_vram()
    elif system == "Windows":
        found = _registry_vram()
    if not found:
        found = _vulkan_vram()
    found = [g for g in found if g[1] >= MIN_GPU_MB]
    return max(found, key=lambda g: g[1]) if found else None


def detect(system: str = "", machine: str = "") -> Hardware:
    """Probe this machine. Never raises; an unreadable machine is ``kind="none"``.

    Args:
        system: ``platform.system()`` override (tests): Windows | Linux | Darwin.
        machine: ``platform.machine()`` override (tests).
    """
    system = system or platform.system() or sys.platform
    machine = (machine or platform.machine() or "").lower()
    ram = _ram_mb(system)

    if system == "Darwin":
        if machine in ("arm64", "aarch64") and ram:
            return Hardware("apple", ram, "Apple Silicon", ram)
    else:
        nv = _nvidia()
        if nv:
            return Hardware("nvidia", nv[1], nv[0], ram)
        other = _other_gpu(system)
        if other:
            return Hardware("gpu", other[1], other[0], ram)
    if ram:
        return Hardware("ram", ram, "system RAM", ram)
    return Hardware("none", 0, "unknown", 0)
