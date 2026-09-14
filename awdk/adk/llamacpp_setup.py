"""
AitherADK Local Orchestrator Setup (llama.cpp + Nemotron-Orchestrator-8B)
==========================================================================

Cross-platform provisioner that:
  1. Detects GPU/accelerator (CUDA / Vulkan / Metal / CPU)
  2. Downloads matching llama.cpp prebuilt binary from GitHub releases
  3. Downloads quantized Nemotron-Orchestrator-8B GGUF from HuggingFace
  4. Installs llama-server as a background service (systemd / launchd / Task Scheduler)
  5. Registers OpenAI-compatible endpoint in ~/.aither/config.json

Designed for endpoint laptops/workstations that:
  - Have <16GB VRAM (vLLM doesn't fit)
  - Don't have Docker (or shouldn't run Docker for an inference daemon)
  - Need an always-on local orchestrator that survives reboots
  - Use Intel Arc / AMD / Apple Silicon (no CUDA, vLLM won't work)

Pure stdlib — no pip dependencies. Importable from CLI, MCP tools, and
PowerShell wrappers via subprocess.

Public API:
    detect_accel() -> AccelInfo
    pick_quant(vram_gb, ram_gb) -> str  ("Q3_K_M" / "Q4_K_M" / "Q5_K_M" / "Q6_K" / "Q8_0")
    install(quant=None, port=8200, model_repo=DEFAULT_MODEL_REPO,
            service=True, dry_run=False) -> InstallResult
    status(port=8200) -> StatusResult
    uninstall(port=8200, purge=False) -> bool

CLI entrypoint:
    python -m adk.llamacpp_setup [--quant Q4_K_M] [--port 8200] [--no-service]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# NOT bartowski. That repo never existed -- HF answers 401 (not 404) for a
# nonexistent repo, which made this read as an auth failure. Measured
# 2026-08-22: the API confirms no such repo, while MaziyarPanahi's (92k
# downloads, ungated) and Mungert's are real.
DEFAULT_MODEL_REPO = "MaziyarPanahi/Nemotron-Orchestrator-8B-GGUF"

#: Tried in order after whatever repo the caller named. One dead repo must
#: not end the install when a sibling conversion exists.
DEFAULT_MODEL_REPO_FALLBACKS = [
    "Mungert/Nemotron-Orchestrator-8B-GGUF",
]

#: When the requested quant is not in a repo, walk DOWN this ladder and take
#: the first match. Substitution is announced, never silent.
QUANT_FALLBACK_LADDER = [
    "Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q4_K_M", "Q4_K_S", "Q3_K_M",
]
DEFAULT_MODEL_DISPLAY = "nemotron-orchestrator-8b"
DEFAULT_SERVED_NAME = "aither-orchestrator"
DEFAULT_PORT = 8200
DEFAULT_CTX = 8192
# NOT /releases/latest. GitHub's `latest` EXCLUDES prereleases, and every
# llama.cpp build release (bNNNNN) is marked prerelease -- so `latest` returned
# `v0.2.0`, a non-build tag with a single asset, and the matcher never saw a
# build list. Measured 2026-08-22: that one line broke install on EVERY
# platform, and reported it as 'no matching build for linux/cpu/x64', which
# sent the investigation at the matcher instead of the URL.
LLAMACPP_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5"
HF_RESOLVE_BASE = "https://huggingface.co/{repo}/resolve/main/{filename}"

# Quant catalog with approximate sizes (Nemotron-Orchestrator-8B specific)
QUANTS = {
    "Q3_K_M": {"size_gb": 4.0, "ram_gb": 5.0, "quality": "OK for orchestrator-only"},
    "Q4_K_M": {"size_gb": 4.9, "ram_gb": 6.0, "quality": "Recommended — sweet spot"},
    "Q5_K_M": {"size_gb": 5.7, "ram_gb": 7.0, "quality": "Higher fidelity"},
    "Q6_K":   {"size_gb": 6.6, "ram_gb": 8.0, "quality": "Near-lossless"},
    "Q8_0":   {"size_gb": 8.5, "ram_gb": 10.0, "quality": "Effectively lossless"},
}

AITHER_HOME = Path.home() / ".aither"
LLAMACPP_DIR = AITHER_HOME / "llamacpp"
MODELS_DIR = AITHER_HOME / "models"
LOG_DIR = AITHER_HOME / "logs"
CONFIG_PATH = AITHER_HOME / "config.json"


# ---------------------------------------------------------------------------
# Accelerator detection
# ---------------------------------------------------------------------------

@dataclass
class AccelInfo:
    kind: str = "cpu"   # cuda | vulkan | metal | cpu
    name: str = "Unknown"
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    cuda_version: str = ""
    os_family: str = ""  # windows | linux | macos
    arch: str = ""       # x64 | arm64
    notes: list = field(default_factory=list)


def _run(cmd: list[str], timeout: int = 8) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _detect_ram_gb() -> float:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 * 1024)
        elif sys.platform == "darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            return int(out) / (1024 ** 3) if out else 0.0
        elif sys.platform == "win32":
            # GlobalMemoryStatusEx via ctypes — NOT `wmic`, which Windows 11 has
            # REMOVED. The wmic call returned nothing on a current Windows box and
            # the failure was swallowed into the 0.0 below, which is not a visible
            # error: it silently makes pick_quant() compute a pool of zero, so a
            # CPU/Vulkan box picks the smallest quant no matter how much RAM it has.
            # Measured 2026-08-13 on Windows 11 26200: detect_accel() reported
            # ram_gb=0.0 on a machine with plenty.
            import ctypes

            class _MemStatus(ctypes.Structure):
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

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024 ** 3)
    except Exception:  # noqa: BLE001 — best-effort probe; 0.0 is the documented miss
        pass
    return 0.0


def detect_accel() -> AccelInfo:
    info = AccelInfo(ram_gb=_detect_ram_gb())

    if sys.platform == "win32":
        info.os_family = "windows"
    elif sys.platform == "darwin":
        info.os_family = "macos"
    else:
        info.os_family = "linux"

    info.arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"

    # macOS / Apple Silicon -> Metal
    if info.os_family == "macos" and info.arch == "arm64":
        chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
        info.kind = "metal"
        info.name = chip
        info.vram_gb = info.ram_gb  # unified memory
        return info

    # NVIDIA -> CUDA
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits"])
        if out:
            best_vram = 0
            best_name = "NVIDIA GPU"
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                vram = int(float(parts[1])) if len(parts) > 1 else 0
                if vram > best_vram:
                    best_vram = vram
                    best_name = parts[0]
            cuda_ver = ""
            smi_out = _run(["nvidia-smi"])
            if smi_out:
                m = re.search(r"CUDA Version:\s*([\d.]+)", smi_out)
                if m:
                    cuda_ver = m.group(1)
            info.kind = "cuda"
            info.name = best_name
            info.vram_gb = best_vram / 1024
            info.cuda_version = cuda_ver
            return info

    # AMD ROCm -> Vulkan (llama.cpp ROCm builds exist but Vulkan is more portable)
    if shutil.which("rocm-smi") or shutil.which("rocminfo"):
        info.kind = "vulkan"
        info.name = "AMD GPU"
        info.notes.append("AMD detected — using Vulkan build")
        # Read the card's real capacity. Without this the branch returned with
        # vram_gb still 0.0, and `pick_quant` then took its "vulkan with under
        # 2GB" path and sized the quant from SYSTEM RAM -- so a 24GB card was
        # never once asked how much memory it had. It happens not to change the
        # answer for the quant table as it stands (every entry fits a 32GB
        # half-RAM pool), which is exactly why it went unnoticed; on an 8GB card
        # with 64GB of RAM it picks a quant that cannot fit on the GPU.
        smi = _run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
        if smi:
            best_mb = 0.0
            for line in smi.splitlines():
                if "total" not in line.lower():
                    continue
                for tok in re.findall(r"(\d{4,})", line):
                    val = float(tok)
                    mb = val / (1024 * 1024) if val > 1_000_000 else val
                    best_mb = max(best_mb, mb)
            if best_mb > 0:
                info.vram_gb = best_mb / 1024
                info.name = f"AMD GPU ({info.vram_gb:.0f}GB)"
        return info

    # Intel Arc / iGPU on Windows -> Vulkan
    if info.os_family == "windows":
        # dxdiag check for Intel Arc / Iris Xe
        gpu_check = _run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"])
        if gpu_check:
            names = gpu_check.strip().split("\n")
            for n in names:
                n_clean = n.strip()
                if any(x in n_clean.lower() for x in ("arc", "iris", "intel", "radeon", "amd")):
                    info.kind = "vulkan"
                    info.name = n_clean
                    info.notes.append("Using Vulkan build for non-NVIDIA GPU")
                    return info

    # Linux Intel Arc -> Vulkan (vulkaninfo present)
    if shutil.which("vulkaninfo"):
        info.kind = "vulkan"
        info.name = "Vulkan-capable GPU"
        return info

    info.kind = "cpu"
    info.name = platform.processor() or "CPU"
    return info


# ---------------------------------------------------------------------------
# Quantization selection
# ---------------------------------------------------------------------------

def pick_quant(vram_gb: float, ram_gb: float, accel_kind: str = "cuda") -> str:
    """Pick the largest quant that fits in available accelerator memory."""
    # For CPU/Vulkan-on-iGPU, we use system RAM as the memory pool
    if accel_kind in ("cpu", "vulkan") and vram_gb < 2:
        pool_gb = ram_gb * 0.5  # leave half RAM for OS + apps
    elif accel_kind == "metal":
        pool_gb = ram_gb * 0.6  # macOS unified memory, generous
    else:
        pool_gb = vram_gb * 0.85  # leave headroom for KV cache

    # Pick largest quant whose RAM estimate fits
    for q in ("Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M", "Q3_K_M"):
        if QUANTS[q]["ram_gb"] <= pool_gb:
            return q
    return "Q3_K_M"  # fallback — smallest


# ---------------------------------------------------------------------------
# llama.cpp binary acquisition
# ---------------------------------------------------------------------------

#: Archive suffixes llama.cpp ships. Windows is .zip; Linux and macOS moved to
#: .tar.gz. A zip-only filter silently excluded both before scoring.
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz")

#: Every accelerator/variant token llama.cpp puts in an asset name. The plain
#: CPU build carries NONE of these, so 'is this the CPU build' is decided by
#: their ABSENCE -- there is no `-cpu-` token on Linux to look for.
_ACCEL_TOKENS = ("cuda", "vulkan", "rocm", "sycl", "openvino", "opencl", "hip",
                 "metal", "cudart", "xcframework", "-ui")


def _is_archive(name: str) -> bool:
    return any(name.endswith(s) for s in _ARCHIVE_SUFFIXES)


def _is_plain_cpu_build(name: str) -> bool:
    """`llama-bNNN-bin-<os>-<arch>.<ext>` with no accelerator token at all."""
    return not any(tok in name for tok in _ACCEL_TOKENS)


def _pick_release_asset(assets: list, accel: AccelInfo) -> Optional[str]:
    """Pick the right llama.cpp release asset for this platform/accelerator.

    Asset naming (live, b10588):
      llama-b10588-bin-win-cuda-12.4-x64.zip
      llama-b10588-bin-win-cpu-x64.zip          <- Windows CPU HAS a tag
      llama-b10588-bin-ubuntu-vulkan-x64.tar.gz
      llama-b10588-bin-ubuntu-x64.tar.gz        <- Linux CPU has NO tag
      llama-b10588-bin-macos-arm64.tar.gz
    """
    os_tag = {"windows": "win", "linux": "ubuntu", "macos": "macos"}.get(
        accel.os_family, "ubuntu")
    arch = (accel.arch or "x64").lower()

    # macOS ships one build per arch (Metal is built in); match on arch only.
    if accel.os_family == "macos":
        for a in assets:
            name = a.get("name", "").lower()
            if "macos" in name and arch in name and _is_archive(name) \
                    and "xcframework" not in name:
                return a.get("browser_download_url")
        return None

    candidates = []
    for a in assets:
        name = a.get("name", "").lower()
        if not _is_archive(name):
            continue
        if not name.startswith("llama-") or os_tag not in name:
            continue
        if "cudart" in name or name.endswith("-ui.tar.gz"):
            continue  # runtime DLLs and the web UI are not the server
        if arch not in name:
            continue
        score = 0
        if accel.kind == "cpu":
            # Either an explicit -cpu- build (Windows) or the untagged plain
            # build (Linux). Both are the CPU build; neither name looks like
            # the other.
            if "-cpu-" in name or _is_plain_cpu_build(name):
                score = 100
        else:
            if accel.kind in name:
                score = 100
                if accel.kind == "cuda" and "cu12" in name:
                    score += 5
        if score:
            candidates.append((score, a.get("browser_download_url")))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # Accelerator requested but no such build: fall back to the CPU build for
    # this OS rather than failing. A slow model beats no model, and the
    # caller prints what it picked.
    for a in assets:
        name = a.get("name", "").lower()
        if (_is_archive(name) and name.startswith("llama-") and os_tag in name
                and arch in name and "cudart" not in name
                and ("-cpu-" in name or _is_plain_cpu_build(name))):
            return a.get("browser_download_url")
    return None

def verify_sha256(path: Path, expected: str, label: str = "artifact") -> bool:
    """Verify a downloaded artifact against an expected sha256. Fail closed.

    The vendored ODS catalog pins `gguf_sha256` for 49 of its 52 models. Carrying
    those pins and never checking them is a silent no-op: the integrity guarantee
    reads as present while nothing enforces it. A mismatching file is DELETED, so
    a corrupted or substituted download can never be picked up by the >100MB
    "looks like a real model" size check on the next run.

    An empty/absent `expected` returns True with a warning — the catalog genuinely
    has no pin for 3 multi-part models, and refusing those would break installs
    that upstream supports.
    """
    if not expected:
        print(f"  WARNING: no sha256 pin for {label}; integrity NOT verified")
        return True
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        print(f"  ERROR: cannot hash {path}: {e}", file=sys.stderr)
        return False
    actual = h.hexdigest()
    if actual == expected.strip().lower():
        print(f"  sha256 OK: {label} ({actual[:16]}...)")
        return True
    print(
        f"  ERROR: sha256 MISMATCH for {label}\n"
        f"    expected {expected}\n"
        f"    actual   {actual}\n"
        f"  Deleting the file; refusing to use an unverified model artifact.",
        file=sys.stderr,
    )
    path.unlink(missing_ok=True)
    return False


def _download(
    url: str, dest: Path, label: str = "download", expected_sha256: str = ""
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading {label}: {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AitherADK-LocalOrchestrator/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            written = 0
            chunk = 256 * 1024
            with open(dest, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    written += len(data)
                    if total:
                        pct = (written / total) * 100
                        print(f"\r    {pct:5.1f}%  ({written / 1e6:.1f} / {total / 1e6:.1f} MB)",
                              end="", flush=True)
            print()
        return verify_sha256(dest, expected_sha256, label)
    except Exception as e:
        print(f"  ERROR: download failed: {e}", file=sys.stderr)
        return False


def install_llamacpp(accel: AccelInfo, dry_run: bool = False) -> Optional[Path]:
    """Download + extract llama.cpp; return path to llama-server binary."""
    LLAMACPP_DIR.mkdir(parents=True, exist_ok=True)
    binary_name = "llama-server.exe" if accel.os_family == "windows" else "llama-server"
    existing = list(LLAMACPP_DIR.rglob(binary_name))
    if existing:
        print(f"  Found existing llama-server: {existing[0]}")
        return existing[0]

    if dry_run:
        print(f"  [DRY] Would download llama.cpp for {accel.os_family}/{accel.kind}")
        return LLAMACPP_DIR / binary_name

    print("  Querying llama.cpp latest release...")
    try:
        req = urllib.request.Request(LLAMACPP_RELEASES_API,
                                     headers={"User-Agent": "AitherADK/1.0",
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            releases = json.loads(resp.read())
        # A list now, not one release. Take the first one that actually
        # carries builds: a tag with no llama-*-bin-* asset is a UI or
        # framework release, not a server release.
        release = next(
            (r for r in releases
             if any(a.get("name", "").startswith("llama-") and "-bin-" in a.get("name", "")
                    for a in r.get("assets", []))),
            None)
        if release is None:
            print("  ERROR: none of the recent releases carries a server build",
                  file=sys.stderr)
            return None
        print(f"  Release: {release.get('tag_name')}")
    except Exception as e:
        print(f"  ERROR: GitHub API failed: {e}", file=sys.stderr)
        return None

    asset_url = _pick_release_asset(release.get("assets", []), accel)
    # SAY when the accelerator the user has is not the build they got. llama.cpp
    # ships no Linux CUDA binary, so a CUDA box lands on the CPU build -- which
    # is the right fallback, but reads as 'my GPU is not being used' unless it is
    # stated here, where the decision was made.
    if asset_url and accel.kind != "cpu" and accel.kind not in asset_url.lower():
        print(f"  NOTE: no {accel.kind} build for {accel.os_family}; using the CPU "
              f"build. For GPU on Linux, install a vulkan-capable driver or build "
              f"llama.cpp from source.")
    if not asset_url:
        print(f"  ERROR: no matching llama.cpp build for "
              f"{accel.os_family}/{accel.kind}/{accel.arch}", file=sys.stderr)
        return None

    zip_path = LLAMACPP_DIR / "llama-cpp.zip"
    if not _download(asset_url, zip_path, "llama.cpp"):
        return None

    print("  Extracting...")
    try:
        # .zip on Windows, .tar.gz on Linux and macOS. zipfile alone meant a
        # correctly chosen Linux asset still failed here, one step after the
        # matcher was fixed -- the fourth bug hiding behind the third.
        if asset_url.endswith(".tar.gz"):
            import tarfile
            with tarfile.open(zip_path, "r:gz") as tf:
                tf.extractall(LLAMACPP_DIR)
        else:
            import zipfile
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(LLAMACPP_DIR)
        zip_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"  ERROR: extract failed: {e}", file=sys.stderr)
        return None

    # A Windows CUDA build ships WITHOUT the CUDA runtime — the release has a
    # separate cudart-llama-bin-win-cuda-*.zip. Without it ggml-cuda.dll fails
    # to load and llama-server SILENTLY runs CPU-only: measured 2026-08-09,
    # Bonsai-27B at 3.85 tok/s on a 32GB-VRAM box, healthy and error-free.
    if accel.os_family == "windows" and "cuda" in asset_url.lower():
        import re as _re
        want_ver = _re.search(r"cuda[-_]?([\d.]+)", asset_url.lower())
        cudart_url = None
        for a in release.get("assets", []):
            name = a.get("name", "").lower()
            if "cudart" not in name:
                continue
            cudart_url = cudart_url or a.get("browser_download_url")
            if want_ver and want_ver.group(1) in name:
                cudart_url = a.get("browser_download_url")  # exact toolkit match wins
        if cudart_url:
            cudart_zip = LLAMACPP_DIR / "cudart.zip"
            if _download(cudart_url, cudart_zip, "CUDA runtime"):
                try:
                    import zipfile
                    with zipfile.ZipFile(cudart_zip) as z:
                        z.extractall(LLAMACPP_DIR)
                    cudart_zip.unlink(missing_ok=True)
                except Exception as e:
                    print(f"  WARNING: cudart extract failed ({e}) — server will "
                          "run CPU-only until cudart64*.dll is placed beside it",
                          file=sys.stderr)
        if not list(LLAMACPP_DIR.glob("cudart64*.dll")):
            print("  WARNING: CUDA build without cudart64*.dll — llama-server "
                  "will SILENTLY run CPU-only. Download the matching "
                  "cudart-llama-bin-win-cuda zip from the same release into "
                  f"{LLAMACPP_DIR}", file=sys.stderr)

    found = list(LLAMACPP_DIR.rglob(binary_name))
    if not found:
        print("  ERROR: llama-server binary not found after extract", file=sys.stderr)
        return None

    # Mark executable on POSIX
    if accel.os_family != "windows":
        try:
            os.chmod(found[0], 0o755)
        except Exception:
            pass

    print(f"  llama-server installed: {found[0]}")
    return found[0]


# ---------------------------------------------------------------------------
# GGUF model download
# ---------------------------------------------------------------------------

def catalog_sha256_for(filename: str) -> str:
    """Look up the vendored ODS catalog's sha256 pin for a GGUF filename.

    Returns "" when the catalog is unavailable or the file is not a catalog model
    (this module must keep working for arbitrary HF repos, and it is pure-stdlib
    by contract, so adk.ods is imported lazily and best-effort).

    This lookup is what makes the pins actually load-bearing. Adding an
    `expected_sha256` parameter and leaving every caller to pass it would have
    been inert plumbing — nothing did.
    """
    try:
        from adk.ods import OdsResolver
    except Exception:
        return ""
    try:
        for model in OdsResolver().catalog.get("models", []):
            if model.get("gguf_file") == filename:
                return str(model.get("gguf_sha256") or "")
    except Exception as e:  # catalog missing/corrupt must not block an install
        print(f"  WARNING: ODS catalog unavailable for sha256 lookup: {e}")
    return ""


def _hf_list_gguf_files(repo: str, hf_token: str = "") -> Optional[list]:
    """The repo's REAL .gguf filenames, from the HF API. None if unreachable.

    None (not []) on failure, deliberately: 'the API is down' and 'the repo
    has no GGUFs' need different fallbacks. The first retries with guessed
    names; the second moves to the next repo."""
    url = f"https://huggingface.co/api/models/{repo}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "AitherADK/1.0",
            **({"Authorization": f"Bearer {hf_token}"} if hf_token else {}),
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    return [s.get("rfilename", "") for s in data.get("siblings", [])
            if s.get("rfilename", "").endswith(".gguf")]


def _resolve_gguf(files: list, quant: str) -> tuple:
    """(filename, actual_quant) from real filenames, or (None, None).

    Case-insensitive on purpose: MaziyarPanahi writes `.Q6_K.gguf`, Mungert
    writes `-q8_0.gguf`, and a case-exact match sees only one of them.
    Excludes imatrix/bf16/f16-only artifacts, which contain no usable quant
    for a plain llama-server run."""
    def hit(q: str) -> Optional[str]:
        ql = q.lower()
        for f in files:
            fl = f.lower()
            if "imatrix" in fl:
                continue
            if ql in fl:
                return f
        return None

    found = hit(quant)
    if found:
        return found, quant
    for q in QUANT_FALLBACK_LADDER:
        if q.lower() == quant.lower():
            continue
        found = hit(q)
        if found:
            return found, q
    return None, None


def install_model(
    repo: str, quant: str, dry_run: bool = False, expected_sha256: str = ""
) -> Optional[Path]:
    """Download the requested quant GGUF from HuggingFace; return path to file.

    Args:
        expected_sha256: sha256 pin for the artifact, e.g. from the vendored ODS
            catalog (`ModelRecord.gguf_sha256`). When supplied, a mismatching
            download is deleted and treated as a failure. Empty means unpinned
            (3 of the 52 catalog models are multi-part and carry no single hash).
    """
    # Kimi-K3 dispatch: multi-shard vision model requires dedicated provisioner
    if "kimi-k3" in repo.lower():
        raise NotImplementedError(
            f"Kimi-K3 model '{repo}' requires multi-shard download. "
            "Use adk.unsloth_gguf_download or the upcoming "
            "'adk mesh serve kimi-k3' surface instead."
        )

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    hf_token = os.environ.get("HF_TOKEN", "")

    # ASK, do not guess. The guessed-convention approach shipped three
    # spellings of a filename for a repo that did not exist, and real
    # uploaders disagree with each other anyway (dot vs dash, upper vs
    # lower). The API returns what is actually there; the wanted quant is
    # matched against that, with a downward ladder when it is absent --
    # announced, because a silent substitution reads as 'wrong model'.
    candidates = []
    files = _hf_list_gguf_files(repo, hf_token)
    if files:
        resolved, actual = _resolve_gguf(files, quant)
        if resolved:
            if actual != quant:
                print(f"  NOTE: {repo} has no {quant}; using {actual} instead")
            candidates = [resolved]
        else:
            print(f"  NOTE: {repo} exists but ships no usable quant")
    elif files is None:
        print(f"  NOTE: HF API unreachable for {repo}; falling back to "
              f"guessed filenames")

    if not candidates:
        # The API said nothing usable, or was unreachable: the old guesses,
        # covering the dot AND dash conventions this time.
        repo_short = repo.split("/")[-1].replace("-GGUF", "")
        candidates = [
            f"{repo_short}-{quant}.gguf",
            f"{repo_short}.{quant}.gguf",
            f"{repo_short.lower()}-{quant.lower()}.gguf",
            f"{repo_short.replace('_', '-')}-{quant}.gguf",
        ]

    for filename in candidates:
        dest = MODELS_DIR / filename
        if dest.exists() and dest.stat().st_size > 100 * 1024 * 1024:  # >100MB sanity
            print(f"  Found existing model: {dest} ({dest.stat().st_size / 1e9:.2f} GB)")
            return dest

    if dry_run:
        print(f"  [DRY] Would download {repo} {quant}")
        return MODELS_DIR / candidates[0]

    # Try each candidate filename
    last_err = None
    for filename in candidates:
        url = HF_RESOLVE_BASE.format(repo=repo, filename=filename)
        dest = MODELS_DIR / filename
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "AitherADK/1.0",
                **({"Authorization": f"Bearer {hf_token}"} if hf_token else {}),
            })
            # HEAD-style: try to download. urllib doesn't HEAD cleanly across mirrors, just GET.
            print(f"  Trying: {url}")
            # Explicit pin wins; otherwise resolve it from the vendored catalog so
            # catalog models are verified without every caller having to opt in.
            pin = expected_sha256 or catalog_sha256_for(filename)
            if _download(url, dest, f"{quant} GGUF", expected_sha256=pin):
                if dest.stat().st_size > 100 * 1024 * 1024:
                    return dest
                else:
                    dest.unlink(missing_ok=True)
                    last_err = "file too small (likely 404 HTML)"
            else:
                # _download() now also returns False on a sha256 mismatch, and has
                # already deleted the bad file — do not fall through to the size check.
                last_err = "download failed or failed integrity verification"
        except Exception as e:
            last_err = str(e)
            continue

    print(f"  ERROR: could not download {quant} from {repo}: {last_err}", file=sys.stderr)
    print(f"  Try manually: huggingface-cli download {repo} --include '*{quant}*' --local-dir {MODELS_DIR}")
    return None


# ---------------------------------------------------------------------------
# Service installation (systemd / launchd / Task Scheduler)
# ---------------------------------------------------------------------------

def _build_server_cmd(binary: Path, model: Path, port: int, accel: AccelInfo,
                      ctx: int = DEFAULT_CTX) -> list[str]:
    cmd = [
        str(binary),
        "-m", str(model),
        "-c", str(ctx),
        "--port", str(port),
        "--host", "127.0.0.1",
        "--alias", DEFAULT_SERVED_NAME,
        "--jinja",  # use model's chat template
    ]
    # GPU layer offload — -1 = all layers
    if accel.kind in ("cuda", "vulkan", "metal"):
        cmd.extend(["-ngl", "99"])
    return cmd


def _spawn_detached(cmd: list, log_path: Path) -> bool:
    """Run llama-server as a plain detached process. The no-systemd fallback.

    A service is a convenience (survive reboots); it is not a precondition
    for serving. Containers, minimal distros and systemd-less WSL have no
    service manager, and crashing AFTER the multi-GB download -- with binary
    and weights already on disk -- was the worst place this could fail.
    """
    try:
        with open(log_path, "ab") as log:
            subprocess.Popen(
                cmd, stdout=log, stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # survives THIS process; not a reboot
            )
        return True
    except Exception as e:  # noqa: BLE001 - report, never crash post-download
        print(f"  ERROR: could not start llama-server directly: {e}",
              file=sys.stderr)
        return False


def _systemd_available() -> bool:
    """Is systemd both PRESENT and MANAGING this machine?

    shutil.which alone is not enough: systemctl can exist on a box where
    PID 1 is not systemd (a container with the binary installed), and then
    every call fails one step later with a different, stranger error."""
    if shutil.which("systemctl") is None:
        return False
    try:
        return Path("/run/systemd/system").exists()
    except OSError:
        return False


def install_service(binary: Path, model: Path, port: int, accel: AccelInfo,
                    dry_run: bool = False) -> bool:
    """Install background service for this OS."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _build_server_cmd(binary, model, port, accel)

    if accel.os_family == "linux":
        if _systemd_available():
            return _install_systemd_user(cmd, dry_run)
        # No service manager. Serve NOW as a plain process and say what the
        # difference is -- silently skipping the service would read as
        # installed-and-persistent when it is neither.
        print("  NOTE: no systemd here (container/minimal OS); starting "
              "llama-server directly. It will serve now but will NOT "
              "restart after a reboot -- re-run `adk gobbonet --setup-model` "
              "or start it yourself then.")
        if dry_run:
            return True
        return _spawn_detached(cmd, LOG_DIR / "llama-server.log")
    elif accel.os_family == "macos":
        return _install_launchd(cmd, dry_run)
    elif accel.os_family == "windows":
        return _install_windows_task(cmd, dry_run)
    return False


def _install_systemd_user(cmd: list[str], dry_run: bool) -> bool:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / "aither-orchestrator.service"
    exec_start = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    unit = f"""[Unit]
Description=AitherOS Local Orchestrator (llama.cpp + Nemotron-Orchestrator-8B)
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=append:{LOG_DIR}/orchestrator.log
StandardError=append:{LOG_DIR}/orchestrator.err

[Install]
WantedBy=default.target
"""
    if dry_run:
        print(f"  [DRY] Would write systemd unit: {unit_path}")
        print(f"  [DRY] {exec_start}")
        return True
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit)
    print(f"  Wrote: {unit_path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "aither-orchestrator.service"], check=False)
    print("  Service enabled + started (systemctl --user)")
    return True


def _install_launchd(cmd: list[str], dry_run: bool) -> bool:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.aitherium.orchestrator.plist"
    args_xml = "\n".join(f"        <string>{c}</string>" for c in cmd)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aitherium.orchestrator</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{LOG_DIR}/orchestrator.log</string>
    <key>StandardErrorPath</key><string>{LOG_DIR}/orchestrator.err</string>
</dict>
</plist>
"""
    if dry_run:
        print(f"  [DRY] Would write launchd plist: {plist_path}")
        return True
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"  Wrote: {plist_path}")
    subprocess.run(["launchctl", "unload", str(plist_path)], check=False,
                   capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=False)
    print("  Service loaded (launchctl)")
    return True


# A GUI-subsystem launcher: WScript.Shell.Run(cmd, 0, True) starts the payload with
# window style 0 (hidden) and waits, so the payload's exit code still propagates.
# `wscript.exe` is itself GUI-subsystem, so Task Scheduler allocates NO console for
# it — which is the whole point (see _install_windows_task).
_RUN_HIDDEN_VBS = (
    "' Launch a console payload with no visible window.\r\n"
    "' Generated by adk.llamacpp_setup — do not edit; it is rewritten on install.\r\n"
    "Set sh = CreateObject(\"WScript.Shell\")\r\n"
    "args = \"\"\r\n"
    "For i = 0 To WScript.Arguments.Count - 1\r\n"
    "  args = args & \"\"\"\" & WScript.Arguments(i) & \"\"\" \"\r\n"
    "Next\r\n"
    "WScript.Quit sh.Run(args, 0, True)\r\n"
)


# schtasks caps /tr at 261 characters and fails the whole create when it is
# exceeded — measured, not documented. Routing through the shim roughly doubles
# the length (two quoted paths instead of one), so a long user profile can push a
# previously-fine task over the edge. Callers get a clear error rather than a
# task that silently was never created.
_SCHTASKS_TR_MAX = 261


def write_hidden_launch_shim(directory: Path) -> Path:
    """Write ``run-hidden.vbs`` into *directory* and return its path.

    Public so the other installers in this package share ONE copy of the fix.
    The console-window defect this shim exists to prevent propagated by
    copy-paste — ``agent_daemon._install_windows_task``'s own docstring says it
    "mirrors" the version here — so a fourth private copy is how it comes back.
    """
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "run-hidden.vbs"
    shim.write_text(_RUN_HIDDEN_VBS)
    return shim


def hidden_task_run(shim: Path, payload: Path) -> str:
    """The ``schtasks /tr`` value that launches *payload* with no visible window."""
    value = f'wscript.exe //B //Nologo "{shim}" "{payload}"'
    if len(value) > _SCHTASKS_TR_MAX:
        raise ValueError(
            f"schtasks /tr would be {len(value)} chars, over the {_SCHTASKS_TR_MAX} "
            f"limit — schtasks would reject the create. Shorten the install path."
        )
    return value


def _install_windows_task(cmd: list[str], dry_run: bool) -> bool:
    # Build a wrapper .cmd so we can capture logs cleanly
    wrapper = LLAMACPP_DIR / "aither-orchestrator.cmd"
    shim = LLAMACPP_DIR / "run-hidden.vbs"
    log_out = LOG_DIR / "orchestrator.log"
    cmd_line = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    wrapper_content = f"@echo off\r\ncd /d \"%~dp0\"\r\n{cmd_line} 1>>\"{log_out}\" 2>&1\r\n"

    # Route the task through the GUI-subsystem shim rather than at the .cmd directly.
    #
    # A task with an INTERACTIVE logon trigger (`/sc onlogon` + `/rl limited`) that
    # launches a console-subsystem program makes Task Scheduler open a real console
    # on the logged-on desktop, and that console TAKES FOCUS — it eats keystrokes and
    # clicks for as long as the program runs. llama-server runs FOREVER, so this is
    # not a flash at logon: it is a permanent console window sitting on the user's
    # desktop stealing input. On an unattended box that is merely ugly; on a shop
    # counter or a kiosk it makes the machine unusable.
    #
    # It is invisible to every cheap check: schtasks reports the task created, the
    # task runs on time, the server serves, and the log file fills up normally. The
    # only signal is a human being interrupted.
    #
    # `-WindowStyle Hidden` is NOT a fix — the console is allocated before any shell
    # can hide it, so it still appears. The shim works because wscript.exe is
    # GUI-subsystem, so no console is ever allocated to begin with.
    task_run = hidden_task_run(shim, wrapper)

    if dry_run:
        print(f"  [DRY] Would write wrapper: {wrapper}")
        print(f"  [DRY] Would write hidden-launch shim: {shim}")
        print("  [DRY] schtasks /create /tn AitherOrchestrator /sc onlogon ...")
        print(f"  [DRY]   /tr {task_run}")
        return True
    wrapper.write_text(wrapper_content)
    write_hidden_launch_shim(LLAMACPP_DIR)
    print(f"  Wrote wrapper: {wrapper}")
    print(f"  Wrote hidden-launch shim: {shim}")
    # Create scheduled task — runs at user logon, restart on failure
    rc = subprocess.run([
        "schtasks", "/create", "/f",
        "/tn", "AitherOrchestrator",
        "/tr", task_run,
        "/sc", "onlogon",
        "/rl", "limited",
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rc.returncode != 0:
        print(f"  WARN: schtasks failed: {rc.stderr}", file=sys.stderr)
        print("  Falling back to inline launch — service will not restart on reboot.")
        # Fire-and-forget start. CREATE_NO_WINDOW, not DETACHED_PROCESS: this path
        # runs from whatever shell invoked the installer, and a console payload
        # started without it pops a window here too.
        subprocess.Popen([str(wrapper)], creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True
    print("  Task scheduled (runs at user logon, no visible window)")
    # Start now too
    subprocess.run(["schtasks", "/run", "/tn", "AitherOrchestrator"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    return True


# ---------------------------------------------------------------------------
# Config registration
# ---------------------------------------------------------------------------

def register_config(port: int, model_path: Path, quant: str, accel: AccelInfo) -> None:
    AITHER_HOME.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            cfg = {}
    cfg["default_backend"] = "openai"
    cfg["setup_backend"] = "llamacpp"
    cfg["inference_url"] = f"http://localhost:{port}/v1"
    cfg["orchestrator_model"] = DEFAULT_SERVED_NAME
    cfg["llamacpp"] = {
        "port": port,
        "model_path": str(model_path),
        "quant": quant,
        "accel": accel.kind,
        "accel_name": accel.name,
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"  Config saved: {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# Health + status
# ---------------------------------------------------------------------------

@dataclass
class StatusResult:
    running: bool
    port: int
    url: str
    model: str = ""
    error: str = ""


def status(port: int = DEFAULT_PORT) -> StatusResult:
    url = f"http://localhost:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            models = [m.get("id", "") for m in data.get("data", [])]
            return StatusResult(running=True, port=port, url=url,
                                model=models[0] if models else "")
    except Exception as e:
        return StatusResult(running=False, port=port, url=url, error=str(e))


def smoke_test(port: int = DEFAULT_PORT, timeout: int = 60) -> bool:
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = json.dumps({
        "model": DEFAULT_SERVED_NAME,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 5,
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  Smoke test: {content!r}")
            return bool(content)
    except Exception as e:
        print(f"  Smoke test failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall(port: int = DEFAULT_PORT, purge: bool = False) -> bool:
    osf = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
    if osf == "linux":
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "aither-orchestrator.service"], check=False)
        unit = Path.home() / ".config" / "systemd" / "user" / "aither-orchestrator.service"
        unit.unlink(missing_ok=True)
    elif osf == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.aitherium.orchestrator.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
            plist.unlink(missing_ok=True)
    elif osf == "windows":
        subprocess.run(["schtasks", "/delete", "/tn", "AitherOrchestrator", "/f"],
                       check=False, capture_output=True)
    if purge:
        shutil.rmtree(LLAMACPP_DIR, ignore_errors=True)
        shutil.rmtree(MODELS_DIR, ignore_errors=True)
    print("  Uninstalled.")
    return True


# ---------------------------------------------------------------------------
# Main install flow
# ---------------------------------------------------------------------------

@dataclass
class InstallResult:
    success: bool
    port: int = DEFAULT_PORT
    binary: Optional[Path] = None
    model: Optional[Path] = None
    quant: str = ""
    accel: Optional[AccelInfo] = None
    service_installed: bool = False
    error: str = ""


def install(
    quant: Optional[str] = None,
    port: int = DEFAULT_PORT,
    model_repo: str = DEFAULT_MODEL_REPO,
    service: bool = True,
    dry_run: bool = False,
) -> InstallResult:
    """Install local orchestrator end-to-end. Returns InstallResult."""
    print()
    print("=" * 60)
    print("  AitherOS Local Orchestrator — llama.cpp + Nemotron-8B")
    print("=" * 60)

    # 1. Detect accelerator
    accel = detect_accel()
    print(f"\n  [1/5] Hardware: {accel.os_family}/{accel.arch}, accel={accel.kind}, "
          f"GPU={accel.name}, VRAM={accel.vram_gb:.1f}GB, RAM={accel.ram_gb:.1f}GB")
    for n in accel.notes:
        print(f"        {n}")

    # 2. Pick quant
    if not quant:
        quant = pick_quant(accel.vram_gb, accel.ram_gb, accel.kind)
    if quant not in QUANTS:
        return InstallResult(success=False, error=f"unknown quant {quant!r}")
    qmeta = QUANTS[quant]
    print(f"  [2/5] Quant: {quant} (~{qmeta['size_gb']:.1f} GB on disk, "
          f"~{qmeta['ram_gb']:.1f} GB working — {qmeta['quality']})")

    # 3. Install llama.cpp
    print(f"  [3/5] llama.cpp:")
    binary = install_llamacpp(accel, dry_run=dry_run)
    if not binary:
        return InstallResult(success=False, accel=accel,
                             error="llama.cpp install failed")

    # 4. Download model
    print(f"  [4/5] Model:")
    model = install_model(model_repo, quant, dry_run=dry_run)
    # Fallback repos apply ONLY when the caller is on the default. A user who
    # pinned --model-repo asked for THAT model; silently handing them a
    # Nemotron conversion from someone else would be worse than failing.
    if not model and model_repo == DEFAULT_MODEL_REPO:
        for _fb in DEFAULT_MODEL_REPO_FALLBACKS:
            print(f"  Primary repo yielded nothing; trying {_fb}")
            model = install_model(_fb, quant, dry_run=dry_run)
            if model:
                break
    if not model:
        return InstallResult(success=False, accel=accel, binary=binary,
                             error="model download failed")

    # 5. Service + config
    print(f"  [5/5] Service:")
    svc_ok = False
    if service:
        svc_ok = install_service(binary, model, port, accel, dry_run=dry_run)
    else:
        print(f"  Skipping service install (--no-service)")
        cmd = _build_server_cmd(binary, model, port, accel)
        print(f"  Run manually: {' '.join(cmd)}")

    if not dry_run:
        register_config(port, model, quant, accel)

    print()
    print("=" * 60)
    print(f"  Endpoint: http://localhost:{port}/v1  (OpenAI-compatible)")
    print(f"  Model:    {DEFAULT_SERVED_NAME}  ({quant})")
    print(f"  Logs:     {LOG_DIR}")
    if accel.os_family == "linux":
        print(f"  Manage:   systemctl --user status aither-orchestrator")
    elif accel.os_family == "macos":
        print(f"  Manage:   launchctl list | grep aitherium")
    else:
        print(f"  Manage:   schtasks /query /tn AitherOrchestrator")
    print("=" * 60)
    print()
    return InstallResult(success=True, port=port, binary=binary, model=model,
                         quant=quant, accel=accel, service_installed=svc_ok)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="aither-local-orchestrator",
        description="Install + run a local Nemotron-Orchestrator-8B via llama.cpp.",
    )
    sub = p.add_subparsers(dest="command")

    pi = sub.add_parser("install", help="Download + install + start the service")
    pi.add_argument("--quant", choices=list(QUANTS.keys()),
                    help="GGUF quant (auto-picked from hardware if omitted)")
    pi.add_argument("--port", type=int, default=DEFAULT_PORT)
    pi.add_argument("--model-repo", default=DEFAULT_MODEL_REPO)
    pi.add_argument("--no-service", action="store_true",
                    help="Skip auto-start service install — print run command instead")
    pi.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Check if local orchestrator is running")
    sub.add_parser("smoke", help="Run a chat completion to verify end-to-end")

    pu = sub.add_parser("uninstall", help="Remove service and (optionally) all files")
    pu.add_argument("--purge", action="store_true",
                    help="Also delete llama.cpp binary and GGUF model")

    args = p.parse_args(argv)
    cmd = args.command or "install"

    if cmd == "install":
        r = install(
            quant=getattr(args, "quant", None),
            port=getattr(args, "port", DEFAULT_PORT),
            model_repo=getattr(args, "model_repo", DEFAULT_MODEL_REPO),
            service=not getattr(args, "no_service", False),
            dry_run=getattr(args, "dry_run", False),
        )
        return 0 if r.success else 1
    elif cmd == "status":
        s = status()
        if s.running:
            print(f"  Running on {s.url}  (model: {s.model})")
            return 0
        print(f"  Not running. Last error: {s.error}")
        return 1
    elif cmd == "smoke":
        return 0 if smoke_test() else 1
    elif cmd == "uninstall":
        return 0 if uninstall(purge=getattr(args, "purge", False)) else 1
    else:
        p.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
