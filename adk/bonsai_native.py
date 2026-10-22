"""``adk bonsai-local`` without containers: every Bonsai size, natively, on Windows/macOS/Linux.

What it does, in order (each step is idempotent, so a re-run resumes):

  1. measure   RAM and GPU memory (``llamacpp_setup.detect_accel``)
  2. choose    the size: ``--model <id>`` or ``--auto`` = the largest size that fits
               (on the GPU when there is one, else in system RAM) -- ``plan_sizes``
  3. server    the PrismML llama.cpp fork, pinned to ``PRISM_RELEASE``, sha256-checked
               against the digest GitHub publishes for that asset. ONE binary serves
               every size: Bonsai 1 Q1_0 needs the fork's 1-bit kernels, and Bonsai 2
               needs its Walsh-Hadamard transform -- a stock llama.cpp loads a Bonsai 2
               file without a warning and answers in gibberish.
  4. weights   from weights.aitherium.com (HuggingFace as the fallback rung), size- and
               sha256-checked; a file that fails either check is deleted
  5. autostart a hidden Task Scheduler task (Windows), a systemd user unit (Linux) or a
               LaunchAgent (macOS), started now
  6. prove it  ``/health``, one chat answer AND one tool call (``smoke_test``)

The server binds 127.0.0.1 only, serves ONE request at a time (``-np 1``) and answers
as the model name ``bonsai-local`` on :8090 -- the port the ``bonsai-local`` backend
preset already dials.

State lives in ``~/.aither/bonsai-local.json``; ``status_report()`` is the one JSON
answer the Desk app reads (``adk bonsai-local --status --json``).

Stdlib only, Python 3.10-compatible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk import llamacpp_setup as lcs

# ---------------------------------------------------------------------------
# What we serve
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8090
SERVED_NAME = "bonsai-local"
DEFAULT_CTX = 8192
WEIGHTS_BASE = "https://weights.aitherium.com"
HF_BASE = "https://huggingface.co/{repo}/resolve/main/{file}"

#: The PrismML fork release every size is served with. prism-b10685 is the first
#: build with PTQ1_0 and the shared Hadamard transform (PRs #148, #150). The next tag
#: (prism-b10687) has no llama-* archives at all, so do not bump without listing the
#: release's assets first.
PRISM_RELEASE = "prism-b10685-7dffb15"
PRISM_DOWNLOAD = "https://github.com/PrismML-Eng/llama.cpp/releases/download/" + PRISM_RELEASE

#: (os, arch, build) -> (asset, sha256). Digests are the ones GitHub publishes for the
#: release's assets (``gh api repos/PrismML-Eng/llama.cpp/releases/tags/<tag>``).
PRISM_ASSETS: Dict[Tuple[str, str, str], Tuple[str, str]] = {
    ("windows", "x64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-win-cpu-x64.zip",
        "f877b5539021119714889be0e4aca8c1f97f399c2fe502b21e2113b3c9fba822"),
    ("windows", "arm64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-win-cpu-arm64.zip",
        "169a55f0c02e8784810c5e92faa54e4717231a9c2d99fd38b4eb7e4f71025755"),
    ("windows", "x64", "vulkan"): (
        "llama-prism-b10685-7dffb15-bin-win-vulkan-x64.zip",
        "f1e8090392390e5c26b184ff90f5350a5e6421bfdd74606f12c8c5de9c668b2b"),
    ("windows", "x64", "cuda"): (
        "llama-prism-b10685-7dffb15-bin-win-cuda-12.4-x64.zip",
        "7aa73f2c52081a7280fa4b02660ba902b964e461b0cfc0b652935e4a59b01c6e"),
    ("linux", "x64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-ubuntu-x64.tar.gz",
        "a1fd3a575e70532567845815a042428831771661b857e80f06422fb08904cb7f"),
    ("linux", "arm64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-ubuntu-arm64.tar.gz",
        "238f34e59c955eed38433ac5bfc0406ff48c4452768c2cc691c630678c34700d"),
    ("linux", "x64", "vulkan"): (
        "llama-prism-b10685-7dffb15-bin-ubuntu-vulkan-x64.tar.gz",
        "20aec2cce7e07b1df8a21a4bfef4b88db085210dac01b8a32374b5ed756f50e4"),
    ("linux", "arm64", "vulkan"): (
        "llama-prism-b10685-7dffb15-bin-ubuntu-vulkan-arm64.tar.gz",
        "ae3d154bab4632b0cd47d2c00964ad12dfe06ab7e0ad535ddb8a61fb330fb64c"),
    ("linux", "x64", "cuda"): (
        "llama-prism-b10685-7dffb15-bin-linux-cuda-12.4-x64.tar.gz",
        "29326793ab8a8d0b42d348bcd0ba0a5f4513220d29d1a1d40e943e357c666386"),
    ("macos", "arm64", "metal"): (
        "llama-prism-b10685-7dffb15-bin-macos-arm64.tar.gz",
        "7fffa7a40c74f3e9bd78f3f2f9f12f9befb7b13af45d5a69c239cf3fd37b9045"),
    ("macos", "arm64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-macos-arm64.tar.gz",
        "7fffa7a40c74f3e9bd78f3f2f9f12f9befb7b13af45d5a69c239cf3fd37b9045"),
    ("macos", "x64", "cpu"): (
        "llama-prism-b10685-7dffb15-bin-macos-x64.tar.gz",
        "b674befce466c4938e7e70a7b13009c7df2e76b072525495a78851c987a5121a"),
}

#: The Windows CUDA build ships without the CUDA runtime; without these DLLs beside the
#: exe, llama-server silently runs on the CPU.
PRISM_WIN_CUDART = (
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
)


@dataclass(frozen=True)
class WeightFile:
    """One GGUF: its name on the mirror and on HuggingFace, its exact size and sha256."""

    file: str
    hf_repo: str
    size_bytes: int
    sha256: str
    #: Working memory (GB) to serve it at DEFAULT_CTX: weights + KV cache + runtime.
    ram_gb: float
    vram_gb: float


@dataclass(frozen=True)
class BonsaiSize:
    """One size Jason can pick. Bonsai 2 has two weight files; the hardware picks one."""

    id: str
    label: str
    generation: int
    params: str
    #: quant -> file. Bonsai 1: {"Q1_0"}; Bonsai 2: {"PQ2_0", "PTQ1_0"}.
    weights: Dict[str, WeightFile]


# Sizes and sha256 are HuggingFace's LFS records for prism-ml's repos, and the mirror
# answers the same byte counts (ranged GET, 2026-10-22). ram_gb/vram_gb follow the
# selfhost-bonsai sizing table; the 27B's 8 GB class was measured on the fleet (7.1 GB
# resident at 131k context).
BONSAI_SIZES: Dict[str, BonsaiSize] = {
    "bonsai-1.7b": BonsaiSize(
        "bonsai-1.7b", "Bonsai 1.7B", 1, "1.7B",
        {"Q1_0": WeightFile(
            "Bonsai-1.7B-Q1_0.gguf", "prism-ml/Bonsai-1.7B-gguf", 248_302_272,
            "3d7c6c90dd98717a203adb22d5eacd2581850e40aa5327e144b97766cae5f7e3", 2.0, 1.5)},
    ),
    "bonsai-4b": BonsaiSize(
        "bonsai-4b", "Bonsai 4B", 1, "4B",
        {"Q1_0": WeightFile(
            "Bonsai-4B-Q1_0.gguf", "prism-ml/Bonsai-4B-gguf", 572_270_624,
            "4524b3f997f0f06444e568d1f26e2efd69effa3218c7ad3047432fb171e42168", 3.0, 2.5)},
    ),
    "bonsai-8b": BonsaiSize(
        "bonsai-8b", "Bonsai 8B", 1, "8B",
        {"Q1_0": WeightFile(
            "Bonsai-8B-Q1_0.gguf", "prism-ml/Bonsai-8B-gguf", 1_158_654_496,
            "284a335aa3fb2ced3b1b01fcb40b08aa783e3b70832767f0dd2e3fdfa134bd54", 5.0, 4.0)},
    ),
    "bonsai-27b": BonsaiSize(
        "bonsai-27b", "Bonsai 27B (first generation)", 1, "27B",
        {"Q1_0": WeightFile(
            "Bonsai-27B-Q1_0.gguf", "prism-ml/Bonsai-27B-gguf", 3_803_452_480,
            "17ef842e47450caeb8eaa3ebfbbab5d2f2278b62b79be107985fb69a2f819aa0", 8.0, 7.0)},
    ),
    "bonsai2-27b": BonsaiSize(
        "bonsai2-27b", "Bonsai 2 27B", 2, "27B",
        {
            # CUDA / Metal fast lane.
            "PQ2_0": WeightFile(
                "Ternary-Bonsai-2-27B-PQ2_0.gguf", "prism-ml/Ternary-Bonsai-2-27B-gguf",
                7_206_168_928,
                "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1", 12.0, 10.0),
            # CPU / Vulkan: the smallest Bonsai 2 file.
            "PTQ1_0": WeightFile(
                "Ternary-Bonsai-2-27B-PTQ1_0.gguf", "prism-ml/Ternary-Bonsai-2-27B-gguf",
                5_946_648_928,
                "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3", 12.0, 8.0),
        },
    ),
}

#: Largest first: the order ``--auto`` tries them in.
SIZE_ORDER = ["bonsai2-27b", "bonsai-27b", "bonsai-8b", "bonsai-4b", "bonsai-1.7b"]

#: Share of each memory pool a model may take. The rest stays with the OS, the
#: browser and the apps the person is actually using.
GPU_SHARE = 0.85
RAM_SHARE = 0.5
UNIFIED_SHARE = 0.6  # Apple Silicon: one pool for both


def home_dir() -> Path:
    return Path.home() / ".aither"


def state_path() -> Path:
    return home_dir() / "bonsai-local.json"


def models_dir() -> Path:
    return home_dir() / "models"


def server_dir(build: str) -> Path:
    return home_dir() / "bonsai" / f"{PRISM_RELEASE}-{build}"


def log_path() -> Path:
    return home_dir() / "logs" / "bonsai-local.log"


# ---------------------------------------------------------------------------
# Choosing: which size, where, which file
# ---------------------------------------------------------------------------

@dataclass
class SizePlan:
    """How one size would run on this machine."""

    id: str
    label: str
    generation: int
    quant: str
    file: str
    disk_bytes: int
    fits: bool
    #: "gpu" | "cpu" | "" (does not fit)
    placement: str
    #: The llama.cpp build to run it with: cpu | cuda | vulkan | metal
    build: str
    need_gb: float
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _has_gpu(accel: lcs.AccelInfo) -> bool:
    return accel.kind in ("cuda", "metal") or (accel.kind == "vulkan" and accel.vram_gb >= 2)


def plan_size(size: BonsaiSize, accel: lcs.AccelInfo) -> SizePlan:
    """Where ``size`` runs on this hardware (GPU first, else system RAM), or that it does not fit.

    Bonsai 2's file follows the placement: PQ2_0 on a CUDA/Metal GPU, PTQ1_0 everywhere
    else (CPU and Vulkan), as PrismML ships them.
    """
    def plan(quant: str, fits: bool, placement: str, build: str, need: float,
             reason: str) -> SizePlan:
        w = size.weights[quant]
        return SizePlan(size.id, size.label, size.generation, quant, w.file, w.size_bytes,
                        fits, placement, build, need, reason)

    small_q = "PTQ1_0" if size.generation == 2 else "Q1_0"
    fast_q = "PQ2_0" if size.generation == 2 else "Q1_0"

    if accel.kind == "metal":
        pool = accel.ram_gb * UNIFIED_SHARE
        need = size.weights[fast_q].ram_gb
        if need <= pool:
            return plan(fast_q, True, "gpu", "metal", need,
                        f"needs {need:g} GB of {pool:.1f} GB usable unified memory")
        return plan(fast_q, False, "", "", need,
                    f"needs {need:g} GB; this Mac has {pool:.1f} GB to spare")

    if _has_gpu(accel):
        gpu_q = fast_q if accel.kind == "cuda" else small_q
        need = size.weights[gpu_q].vram_gb
        pool = accel.vram_gb * GPU_SHARE
        if need <= pool:
            return plan(gpu_q, True, "gpu", accel.kind, need,
                        f"needs {need:g} GB of {pool:.1f} GB usable GPU memory")

    need = size.weights[small_q].ram_gb
    pool = accel.ram_gb * RAM_SHARE
    if need <= pool:
        return plan(small_q, True, "cpu", "cpu", need,
                    f"runs on the processor: needs {need:g} GB of {pool:.1f} GB usable memory")
    return plan(small_q, False, "", "", need,
                f"needs {need:g} GB; this computer has {pool:.1f} GB to spare")


def plan_sizes(accel: lcs.AccelInfo) -> List[SizePlan]:
    """Every size, largest first, with where it would run here."""
    return [plan_size(BONSAI_SIZES[i], accel) for i in SIZE_ORDER]


def recommend(accel: lcs.AccelInfo, plans: Optional[List[SizePlan]] = None) -> Optional[SizePlan]:
    """``--auto``: the largest size that fits on the GPU; with no GPU fit, the largest in RAM.

    A GPU box prefers what the GPU can hold over a bigger size in system RAM: the same
    model on the processor is the slow path, so it is the fallback, not the pick.
    None when not even the 1.7B fits.
    """
    plans = plans if plans is not None else plan_sizes(accel)
    gpu = [p for p in plans if p.fits and p.placement == "gpu"]
    if gpu:
        return gpu[0]
    cpu = [p for p in plans if p.fits]
    return cpu[0] if cpu else None


def override_placement(plan: SizePlan, size: BonsaiSize, accel: lcs.AccelInfo,
                       backend: str = "", quant: str = "") -> SizePlan:
    """Apply ``--backend`` / ``--quant`` to a plan. Explicit choices win over the fit check."""
    if backend:
        placement = "cpu" if backend == "cpu" else "gpu"
        if not quant and size.generation == 2:
            quant = "PQ2_0" if backend in ("cuda", "metal") else "PTQ1_0"
        plan = SizePlan(plan.id, plan.label, plan.generation, plan.quant, plan.file,
                        plan.disk_bytes, True, placement, backend, plan.need_gb,
                        f"--backend {backend}")
    if quant:
        if quant not in size.weights:
            raise ValueError(f"{size.label} ships {', '.join(size.weights)}, not {quant}")
        w = size.weights[quant]
        plan = SizePlan(plan.id, plan.label, plan.generation, quant, w.file, w.size_bytes,
                        True, plan.placement or "cpu", plan.build or "cpu", plan.need_gb,
                        plan.reason)
    return plan


# ---------------------------------------------------------------------------
# The server binary
# ---------------------------------------------------------------------------

def server_asset(os_family: str, arch: str, build: str) -> Tuple[str, str, str]:
    """(url, sha256, actual_build) for this platform; an unshipped build falls to the CPU one."""
    key = (os_family, arch or "x64", build)
    if key not in PRISM_ASSETS:
        key = (os_family, arch or "x64", "cpu")
    if key not in PRISM_ASSETS:
        raise LookupError(f"the PrismML fork ships no build for {os_family}/{arch}")
    asset, sha = PRISM_ASSETS[key]
    return f"{PRISM_DOWNLOAD}/{asset}", sha, key[2]


def _binary_name(os_family: str) -> str:
    return "llama-server.exe" if os_family == "windows" else "llama-server"


def _safe_extract(archive: Path, dest: Path) -> None:
    """Unpack a release archive, refusing any member that would land outside ``dest``."""
    root = dest.resolve()

    def inside(name: str) -> bool:
        target = (dest / name).resolve()
        return target == root or root in target.parents

    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()

            def link_ok(m: tarfile.TarInfo) -> bool:
                if m.issym():  # relative to the link's own directory (lib*.so -> lib*.so.0)
                    return inside(os.path.join(os.path.dirname(m.name), m.linkname))
                if m.islnk():  # relative to the archive root
                    return inside(m.linkname)
                return True

            bad = [m.name for m in members if not inside(m.name) or not link_ok(m)]
            if bad:
                raise ValueError(f"archive member outside the install dir: {bad[0]}")
            tf.extractall(dest)  # noqa: S202 - every member checked above
    else:
        with zipfile.ZipFile(archive) as zf:
            bad = [n for n in zf.namelist() if not inside(n)]
            if bad:
                raise ValueError(f"archive member outside the install dir: {bad[0]}")
            zf.extractall(dest)  # noqa: S202 - every member checked above


def find_server(build: str, os_family: str) -> Optional[Path]:
    d = server_dir(build)
    found = sorted(d.rglob(_binary_name(os_family))) if d.is_dir() else []
    return found[0] if found else None


def install_server(accel: lcs.AccelInfo, build: str, dry_run: bool = False,
                   download: Callable[..., bool] = lcs._download) -> Optional[Path]:
    """The pinned PrismML llama-server for ``build``, downloaded and sha256-checked once."""
    url, sha, actual = server_asset(accel.os_family, accel.arch, build)
    if actual != build:
        print(f"  NOTE: the PrismML fork ships no {build} build for {accel.os_family}; "
              "using its CPU build")
    existing = find_server(actual, accel.os_family)
    if existing:
        print(f"  Server: {existing} ({PRISM_RELEASE}, {actual})")
        return existing
    dest = server_dir(actual)
    if dry_run:
        print(f"  [DRY] Would download {url}")
        return dest / _binary_name(accel.os_family)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / url.rsplit("/", 1)[-1]
    if not download(url, archive, f"llama.cpp {PRISM_RELEASE} ({actual})", expected_sha256=sha):
        return None
    try:
        _safe_extract(archive, dest)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as e:
        print(f"  ERROR: could not unpack {archive.name}: {e}", file=sys.stderr)
        return None
    finally:
        archive.unlink(missing_ok=True)
    if accel.os_family == "windows" and actual == "cuda":
        rt_name, rt_sha = PRISM_WIN_CUDART
        rt = dest / rt_name
        if download(f"{PRISM_DOWNLOAD}/{rt_name}", rt, "CUDA runtime", expected_sha256=rt_sha):
            try:
                binary_dir = (find_server(actual, accel.os_family) or dest).parent
                _safe_extract(rt, binary_dir)
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                print(f"  WARNING: CUDA runtime unpack failed ({e}); the server will run "
                      "on the processor", file=sys.stderr)
            finally:
                rt.unlink(missing_ok=True)
    binary = find_server(actual, accel.os_family)
    if binary is None:
        print("  ERROR: llama-server is not in the release archive", file=sys.stderr)
        return None
    if accel.os_family != "windows":
        for exe in binary.parent.iterdir():
            if exe.is_file() and (exe.name.startswith("llama-") or exe.suffix in ("", ".so")):
                exe.chmod(exe.stat().st_mode | 0o755)
    print(f"  Server: {binary} ({PRISM_RELEASE}, {actual})")
    return binary


# ---------------------------------------------------------------------------
# The weights
# ---------------------------------------------------------------------------

def weights_present(w: WeightFile, path: Optional[Path] = None) -> bool:
    """On disk at the catalogued size. The sha256 was checked when it was downloaded."""
    p = path or (models_dir() / w.file)
    try:
        return p.is_file() and p.stat().st_size == w.size_bytes
    except OSError:
        return False


def _download_resumable(url: str, dest: Path, w: WeightFile,
                        progress: Callable[[str], None] = print) -> bool:
    """Stream ``url`` into ``dest.part`` (resuming one left by an earlier run), then verify.

    Size AND sha256 must match before the file gets its real name; a mismatch deletes it.
    """
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    stalls = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if have >= w.size_bytes:
            break
        headers = {"User-Agent": "Mozilla/5.0 (awdk bonsai-local)"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https only
                if have and resp.status != 206:
                    have = 0  # the server ignored Range: start over
                with open(part, "ab" if have else "wb") as f:
                    last_pct = -1
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        pct = int(have * 100 / w.size_bytes)
                        if pct != last_pct:
                            last_pct = pct
                            progress(f"  Downloading {w.file}: {pct}% "
                                     f"({have / 1e9:.2f} of {w.size_bytes / 1e9:.2f} GB)")
            stalls = 0
        except urllib.error.HTTPError as e:
            print(f"  {url}: HTTP {e.code}", file=sys.stderr)
            return False
        except (OSError, ConnectionError) as e:
            stalls += 1
            if stalls >= 5:
                print(f"  Download kept failing ({e}); run the same command again to resume.",
                      file=sys.stderr)
                return False
            time.sleep(min(30, 3 * stalls))
    if part.stat().st_size != w.size_bytes:
        print(f"  ERROR: {w.file} is {part.stat().st_size} bytes, expected {w.size_bytes}; "
              "deleted it", file=sys.stderr)
        part.unlink(missing_ok=True)
        return False
    if not lcs.verify_sha256(part, w.sha256, w.file):
        return False  # verify_sha256 already deleted it
    os.replace(part, dest)
    return True


def install_weights(w: WeightFile, dry_run: bool = False,
                    fetch: Callable[..., bool] = _download_resumable) -> Optional[Path]:
    """The GGUF in ~/.aither/models: the mirror first, HuggingFace if the mirror fails."""
    dest = models_dir() / w.file
    if weights_present(w, dest):
        print(f"  Weights: {dest} (already here)")
        return dest
    urls = [f"{WEIGHTS_BASE}/{w.file}", HF_BASE.format(repo=w.hf_repo, file=w.file)]
    if dry_run:
        print(f"  [DRY] Would download {urls[0]} ({w.size_bytes / 1e9:.2f} GB)")
        return dest
    if dest.exists():
        print(f"  ERROR: {dest} exists but is not the catalogued {w.file}; move it and "
              "run again. Nothing was changed.", file=sys.stderr)
        return None
    for url in urls:
        print(f"  Weights: {url}")
        if fetch(url, dest, w):
            return dest
    return None


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

def server_argv(binary: Path, model: Path, port: int, plan: SizePlan,
                ctx: int = DEFAULT_CTX) -> List[str]:
    """llama-server on loopback, one request at a time, tool calling on (``--jinja``)."""
    argv = [str(binary), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
            "-np", "1", "-c", str(ctx), "--jinja", "--alias", SERVED_NAME]
    argv += ["-ngl", "99" if plan.placement == "gpu" else "0"]
    if plan.generation == 2:
        # PrismML's sampling for Bonsai 2, and a bound on its <think> block.
        argv += ["-fa", "on", "--temp", "1.0", "--top-p", "0.95", "--top-k", "20",
                 "--reasoning-budget", "2048"]
    return argv


def _http_json(url: str, body: Optional[dict] = None, timeout: float = 5.0) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - loopback
        return json.loads(r.read() or b"null")


def serving(port: int) -> bool:
    """OUR model answers on ``port`` -- not merely something listening there."""
    try:
        data = _http_json(f"http://127.0.0.1:{port}/v1/models", timeout=3)
    except (OSError, ValueError):
        return False
    ids = [m.get("id") for m in (data or {}).get("data", []) if isinstance(m, dict)]
    return SERVED_NAME in ids


def wait_until_serving(port: int, timeout_s: float = 300.0,
                       sleep: Callable[[float], None] = time.sleep) -> bool:
    waited = 0.0
    while waited < timeout_s:
        if serving(port):
            return True
        sleep(2)
        waited += 2
    return False


SMOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}


def smoke_test(port: int, timeout: float = 600.0,
               post: Callable[..., Any] = _http_json) -> Dict[str, Any]:
    """One plain answer and one tool call. Both must succeed.

    The tool call is the real test: a model that chats but never calls a tool cannot
    drive an agent. ``tool_choice: required`` makes the server constrain the output to
    a call, so a pass proves the template, the parser and the model agree.
    """
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    out: Dict[str, Any] = {"chat": False, "tool_call": False}
    t0 = time.monotonic()
    try:
        r = post(url, {"model": SERVED_NAME, "max_tokens": 600,
                       "messages": [{"role": "user", "content": "Say hello in five words."}]},
                 timeout=timeout)
        msg = (r.get("choices") or [{}])[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        out["chat"] = bool(text or msg.get("reasoning_content"))
        out["chat_text"] = text[:200]
        out["chat_seconds"] = round(time.monotonic() - t0, 1)
    except (OSError, ValueError, AttributeError) as e:
        out["error"] = f"chat: {e}"
        return out
    t1 = time.monotonic()
    try:
        r = post(url, {"model": SERVED_NAME, "max_tokens": 600, "tools": [SMOKE_TOOL],
                       "tool_choice": "required",
                       "messages": [{"role": "user",
                                     "content": "What is the weather in Paris right now?"}]},
                 timeout=timeout)
        msg = (r.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        fn = (calls[0].get("function") or {}) if calls else {}
        out["tool_call"] = fn.get("name") == "get_weather"
        out["tool_call_args"] = str(fn.get("arguments", ""))[:200]
        out["tool_seconds"] = round(time.monotonic() - t1, 1)
    except (OSError, ValueError, AttributeError) as e:
        out["error"] = f"tool call: {e}"
    return out


# ---------------------------------------------------------------------------
# Autostart + stop
# ---------------------------------------------------------------------------

TASK_NAME = "AitherBonsaiLocal"
UNIT_NAME = "aither-bonsai-local.service"
LAUNCHD_LABEL = "com.aitherium.bonsai-local"


def install_autostart(argv: List[str], os_family: str, dry_run: bool = False) -> bool:
    """Start at sign-in (no visible window on Windows) and start it now."""
    log_path().parent.mkdir(parents=True, exist_ok=True)
    if os_family == "windows":
        return lcs._install_windows_task(argv, dry_run, task_name=TASK_NAME,
                                         wrapper_dir=home_dir() / "bonsai",
                                         log_stem="bonsai-local")
    if os_family == "macos":
        return lcs._install_launchd(argv, dry_run, label=LAUNCHD_LABEL, log_stem="bonsai-local")
    if lcs._systemd_available():
        return lcs._install_systemd_user(argv, dry_run, unit_name=UNIT_NAME,
                                         description="Aither local Bonsai model (llama.cpp)",
                                         log_stem="bonsai-local")
    print("  NOTE: no systemd here; starting the server now. It will not come back after "
          "a reboot -- run `adk bonsai-local` again then.")
    return True if dry_run else start_now(argv)


def start_now(argv: List[str]) -> bool:
    """Start the server detached, with no window, logging to ~/.aither/logs."""
    log_path().parent.mkdir(parents=True, exist_ok=True)
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "cwd": str(Path(argv[0]).parent)}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, no window
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log_path(), "ab") as log:
            subprocess.Popen(argv, stdout=log, stderr=log, **kwargs)  # noqa: S603
    except OSError as e:
        print(f"  ERROR: could not start {argv[0]}: {e}", file=sys.stderr)
        return False
    return True


def remove_autostart(os_family: str) -> None:
    if os_family == "windows":
        for verb in ("/end", "/delete"):
            extra = ["/f"] if verb == "/delete" else []
            subprocess.run(["schtasks", verb, "/tn", TASK_NAME, *extra], capture_output=True,
                           check=False, creationflags=0x08000000)
    elif os_family == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
            plist.unlink(missing_ok=True)
    elif shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME],
                       capture_output=True, check=False)
        (Path.home() / ".config" / "systemd" / "user" / UNIT_NAME).unlink(missing_ok=True)


def stop_server(binary: str, os_family: str) -> None:
    """Stop only processes running OUR binary -- never another llama-server on this box."""
    if not binary:
        return
    if os_family == "windows":
        script = ("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
                  "Where-Object { $_.ExecutablePath -eq $env:AW_BONSAI_BIN } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, check=False, creationflags=0x08000000,
                       env={**os.environ, "AW_BONSAI_BIN": binary})
    elif shutil.which("pkill"):
        subprocess.run(["pkill", "-f", binary], capture_output=True, check=False)


# ---------------------------------------------------------------------------
# State + status
# ---------------------------------------------------------------------------

def read_state() -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(state: Dict[str, Any]) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_path())


def status_report(accel: Optional[lcs.AccelInfo] = None,
                  is_serving: Callable[[int], bool] = serving) -> Dict[str, Any]:
    """Everything the Desk's Local AI screen shows, in one JSON object."""
    accel = accel or lcs.detect_accel()
    plans = plan_sizes(accel)
    rec = recommend(accel, plans)
    state = read_state()
    port = int((state or {}).get("port") or DEFAULT_PORT)
    downloaded = {p.id: weights_present(BONSAI_SIZES[p.id].weights[p.quant]) for p in plans}
    return {
        "installed": state is not None,
        "running": bool(state) and is_serving(port),
        "model": (state or {}).get("model"),
        "quant": (state or {}).get("quant"),
        "placement": (state or {}).get("placement"),
        "port": port,
        "endpoint": f"http://127.0.0.1:{port}/v1",
        "served_name": SERVED_NAME,
        "recommended": rec.id if rec else None,
        "hardware": {"kind": accel.kind, "gpu": accel.name, "vram_gb": round(accel.vram_gb, 1),
                     "ram_gb": round(accel.ram_gb, 1), "os": accel.os_family,
                     "arch": accel.arch},
        "sizes": [{**p.as_dict(), "downloaded": downloaded[p.id]} for p in plans],
        "release": PRISM_RELEASE,
    }


# ---------------------------------------------------------------------------
# The flows
# ---------------------------------------------------------------------------

@dataclass
class InstallOutcome:
    ok: bool
    model: str = ""
    port: int = DEFAULT_PORT
    error: str = ""
    smoke: Optional[Dict[str, Any]] = None


def install(model: str = "", auto: bool = False, port: int = DEFAULT_PORT,
            backend: str = "", quant: str = "", autostart: bool = True,
            smoke: bool = True, dry_run: bool = False,
            accel: Optional[lcs.AccelInfo] = None) -> InstallOutcome:
    """Install (or switch to) one size and prove it serves. See the module docstring."""
    accel = accel or lcs.detect_accel()
    print(f"  Hardware: {accel.os_family}/{accel.arch}, {accel.kind} ({accel.name}), "
          f"GPU memory {accel.vram_gb:.1f} GB, RAM {accel.ram_gb:.1f} GB")
    plans = {p.id: p for p in plan_sizes(accel)}
    if auto or not model:
        rec = recommend(accel, list(plans.values()))
        if rec is None:
            return InstallOutcome(False, error="no Bonsai size fits this computer's memory")
        model = rec.id
        print(f"  Picked: {rec.label} -- the largest that fits ({rec.reason})")
    if model not in BONSAI_SIZES:
        return InstallOutcome(False, error=f"unknown size {model!r}; choose one of "
                                           + ", ".join(SIZE_ORDER))
    size = BONSAI_SIZES[model]
    plan = plans[model]
    try:
        plan = override_placement(plan, size, accel, backend=backend, quant=quant)
    except ValueError as e:
        return InstallOutcome(False, model=model, error=str(e))
    if not plan.fits:
        return InstallOutcome(False, model=model,
                              error=f"{size.label} does not fit: {plan.reason}. Pick a "
                                    "smaller size, or force it with --backend cpu")
    print(f"  Size: {size.label} {plan.quant} on the {plan.placement.upper()} "
          f"({plan.disk_bytes / 1e9:.2f} GB download)")

    binary = install_server(accel, plan.build, dry_run=dry_run)
    if binary is None:
        return InstallOutcome(False, model=model, error="could not install the model server")
    weights = install_weights(size.weights[plan.quant], dry_run=dry_run)
    if weights is None:
        return InstallOutcome(False, model=model, error="could not download the model")

    argv = server_argv(binary, weights, port, plan)
    if dry_run:
        print("  [DRY] Would serve: " + " ".join(argv))
        return InstallOutcome(True, model=model, port=port)

    # Switching sizes: the old server holds the port until it is stopped.
    previous = read_state()
    if previous:
        remove_autostart(accel.os_family)
        stop_server(str(previous.get("binary") or ""), accel.os_family)
        time.sleep(1)
    elif serving(port):
        return InstallOutcome(False, model=model,
                              error=f"another bonsai-local server already answers :{port}")

    if autostart:
        started = install_autostart(argv, accel.os_family)
    else:
        started = start_now(argv)
    if not started:
        return InstallOutcome(False, model=model, error="could not start the model server")
    write_state({"model": model, "quant": plan.quant, "placement": plan.placement,
                 "build": plan.build, "port": port, "binary": str(binary),
                 "weights": str(weights), "argv": argv, "cwd": str(binary.parent),
                 "release": PRISM_RELEASE, "autostart": autostart,
                 "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    print(f"  Waiting for {size.label} to load ...")
    if not wait_until_serving(port):
        return InstallOutcome(False, model=model, port=port,
                              error=f"the server did not answer on :{port}; see {log_path()}")
    if not smoke:
        return InstallOutcome(True, model=model, port=port)
    print("  Checking it answers and calls a tool ...")
    result = smoke_test(port)
    ok = bool(result.get("chat") and result.get("tool_call"))
    if ok:
        print(f"  Smoke test passed: chat {result.get('chat_seconds')} s, "
              f"tool call {result.get('tool_seconds')} s")
    return InstallOutcome(ok, model=model, port=port, smoke=result,
                          error="" if ok else f"smoke test failed: {result}")


def stop() -> bool:
    """Stop the server (autostart stays, so it comes back at the next sign-in)."""
    state = read_state()
    if not state:
        return False
    accel_os = "windows" if sys.platform == "win32" else (
        "macos" if sys.platform == "darwin" else "linux")
    if accel_os == "windows":
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True,
                       check=False, creationflags=0x08000000)
    stop_server(str(state.get("binary") or ""), accel_os)
    return True


def remove(purge: bool = False) -> bool:
    """Stop it, remove autostart, forget it; ``purge`` also deletes weights and server."""
    state = read_state()
    os_family = "windows" if sys.platform == "win32" else (
        "macos" if sys.platform == "darwin" else "linux")
    remove_autostart(os_family)
    if state:
        stop_server(str(state.get("binary") or ""), os_family)
    state_path().unlink(missing_ok=True)
    if purge:
        time.sleep(1)  # Windows holds the exe and the mapped GGUF until the process is gone
        for size in BONSAI_SIZES.values():
            for w in size.weights.values():
                (models_dir() / w.file).unlink(missing_ok=True)
                (models_dir() / (w.file + ".part")).unlink(missing_ok=True)
        shutil.rmtree(home_dir() / "bonsai", ignore_errors=True)
    return state is not None
