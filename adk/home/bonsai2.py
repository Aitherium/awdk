"""Bonsai 2 27B as Hearth's local brain, served by the PrismML llama.cpp build we ship.

``adk home model --local bonsai2`` runs :func:`install_and_start`: pick the release
asset and GGUF for this hardware, download both with resume, verify each against a
pinned sha256, start ``llama-server`` on 127.0.0.1 and point Hearth at it.

Why the fork and only the fork: Bonsai 2's weights carry a Walsh-Hadamard rotation
(``prism.hadamard.*`` metadata) the runtime must undo on the activations. Stock
llama.cpp loads the file and emits gibberish, so there is NO fallback to a
``llama-server`` found on PATH -- the binary this runs is always the one it unpacked
from a checksum-verified PrismML asset, and its ``--version`` must name the pinned
commit before it is started.

Pins: the same release the AitherOS standalone bundle and ``install-bonsai.sh``
ship. Asset digests are GitHub's own ``digest`` field for each release asset; GGUF
digests are the Hugging Face LFS oids, which ARE the sha256 of the bytes (the
PTQ1_0 one matches ``ladder.json``). Re-read both with
``gh api repos/PrismML-Eng/llama.cpp/releases/tags/<tag>`` and
``https://huggingface.co/api/models/prism-ml/Ternary-Bonsai-2-27B-gguf/tree/main``
before bumping.

Never kills a process it did not start: :func:`stop` only signals the pid it
recorded, and only while that pid's command line still names its own binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import HomeError

RELEASE = "prism-b10685-7dffb15"
RELEASE_COMMIT = "7dffb15"
RELEASE_BASE = f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{RELEASE}"

#: (os, arch, backend) -> [(asset, sha256), ...]; every archive is unpacked into one
#: directory (Windows CUDA needs the cudart zip beside the build).
ASSETS: Dict[Tuple[str, str, str], List[Tuple[str, str]]] = {
    ("linux", "x64", "cuda"): [(
        f"llama-{RELEASE}-bin-linux-cuda-12.4-x64.tar.gz",
        "29326793ab8a8d0b42d348bcd0ba0a5f4513220d29d1a1d40e943e357c666386")],
    ("linux", "x64", "vulkan"): [(
        f"llama-{RELEASE}-bin-ubuntu-vulkan-x64.tar.gz",
        "20aec2cce7e07b1df8a21a4bfef4b88db085210dac01b8a32374b5ed756f50e4")],
    ("linux", "x64", "cpu"): [(
        f"llama-{RELEASE}-bin-ubuntu-x64.tar.gz",
        "a1fd3a575e70532567845815a042428831771661b857e80f06422fb08904cb7f")],
    ("linux", "arm64", "vulkan"): [(
        f"llama-{RELEASE}-bin-ubuntu-vulkan-arm64.tar.gz",
        "ae3d154bab4632b0cd47d2c00964ad12dfe06ab7e0ad535ddb8a61fb330fb64c")],
    ("linux", "arm64", "cpu"): [(
        f"llama-{RELEASE}-bin-ubuntu-arm64.tar.gz",
        "238f34e59c955eed38433ac5bfc0406ff48c4452768c2cc691c630678c34700d")],
    ("darwin", "arm64", "metal"): [(
        f"llama-{RELEASE}-bin-macos-arm64.tar.gz",
        "7fffa7a40c74f3e9bd78f3f2f9f12f9befb7b13af45d5a69c239cf3fd37b9045")],
    ("darwin", "x64", "cpu"): [(
        f"llama-{RELEASE}-bin-macos-x64.tar.gz",
        "b674befce466c4938e7e70a7b13009c7df2e76b072525495a78851c987a5121a")],
    ("windows", "x64", "cuda"): [
        (f"llama-{RELEASE}-bin-win-cuda-12.4-x64.zip",
         "7aa73f2c52081a7280fa4b02660ba902b964e461b0cfc0b652935e4a59b01c6e"),
        ("cudart-llama-bin-win-cuda-12.4-x64.zip",
         "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6")],
    ("windows", "x64", "vulkan"): [(
        f"llama-{RELEASE}-bin-win-vulkan-x64.zip",
        "f1e8090392390e5c26b184ff90f5350a5e6421bfdd74606f12c8c5de9c668b2b")],
    ("windows", "x64", "cpu"): [(
        f"llama-{RELEASE}-bin-win-cpu-x64.zip",
        "f877b5539021119714889be0e4aca8c1f97f399c2fe502b21e2113b3c9fba822")],
}


@dataclass(frozen=True)
class Weights:
    file: str
    sha256: str
    size: int


#: PQ2_0 is the CUDA/Metal-fast format; PTQ1_0 (1.75 bpw) is 1.26 GB smaller and the
#: right one for Vulkan, CPU and small GPUs. Legacy ``Q2_0`` is refused by this build.
GGUFS: Dict[str, Weights] = {
    "PQ2_0": Weights("Ternary-Bonsai-2-27B-PQ2_0.gguf",
                     "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1",
                     7206168928),
    "PTQ1_0": Weights("Ternary-Bonsai-2-27B-PTQ1_0.gguf",
                      "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3",
                      5946648928),
}
#: Mirror first (a byte copy of the HF repo), Hugging Face second; the sha256 decides.
GGUF_SOURCES = ("https://weights.aitherium.com",
                "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main")

ALIAS = "bonsai2-27b"
DEFAULT_PORT = 8088          # not 8080: install-bonsai.sh's server lives there
MIN_RAM_GB = 12              # install-bonsai.sh's floor for the 27B tier
N_LAYERS = 64                # 48 Gated DeltaNet + 16 full-attention blocks
#: KV bytes per context token across the 16 full-attention layers, with slack for the
#: compute buffer. Re-measure from llama-server's "KV self size" log line.
KV_BYTES_PER_TOKEN = 96 * 1024
CTX_MIN, CTX_MAX = 4096, 65536
#: Below this a Hearth turn (persona + tools + history) does not fit; a GPU that can
#: only hold PQ2_0 with less gets PTQ1_0 and the room it leaves.
MIN_USEFUL_CTX = 16384
GIB = 1024 ** 3

STATE_FILE = "server.json"
STAMP_FILE = "verified.json"
DATA_ENV = "AITHER_BONSAI2_HOME"


# ----------------------------------------------------------------- hardware + plan

@dataclass
class Hardware:
    os: str                      # linux | darwin | windows
    arch: str                    # x64 | arm64
    ram_gb: float
    vram_total_gb: float = 0.0   # NVIDIA only (nvidia-smi); 0 when none
    vram_free_gb: float = 0.0
    cuda_level: Tuple[int, int] = (0, 0)
    vulkan: bool = False


@dataclass
class Plan:
    os: str
    arch: str
    backend: str
    quant: str
    assets: List[Tuple[str, str]]
    weights: Weights
    ctx: int
    ngl: int
    port: int
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["weights"] = asdict(self.weights)
        d["release"] = RELEASE
        return d


def _norm_arch(machine: str) -> str:
    m = machine.lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _ram_gb() -> float:
    try:
        if sys.platform == "win32":
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
            return ms.ullTotalPhys / GIB
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GIB
    except (OSError, ValueError, AttributeError):
        return 0.0


def _nvidia() -> Tuple[float, float, Tuple[int, int]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0.0, 0.0, (0, 0)
    try:
        q = subprocess.run([exe, "--query-gpu=memory.total,memory.free",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        first = (q.stdout.strip().splitlines() or [""])[0]
        total, free = (float(x) / 1024 for x in first.split(","))
        banner = subprocess.run([exe], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0, 0.0, (0, 0)
    # "CUDA Version: 12.8" (5xx drivers) or "CUDA UMD Version: 13.4" (6xx).
    m = re.search(r"CUDA [A-Za-z ]*Version:\s*(\d+)\.(\d+)", banner)
    return total, free, ((int(m.group(1)), int(m.group(2))) if m else (0, 0))


def _has_vulkan(os_name: str) -> bool:
    if os_name == "windows":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root, "System32", "vulkan-1.dll").exists()
    return (Path("/dev/dri").exists() or bool(shutil.which("vulkaninfo"))
            or bool(shutil.which("nvidia-smi")))


def detect() -> Hardware:
    os_name = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    total, free, cuda = _nvidia() if os_name != "darwin" else (0.0, 0.0, (0, 0))
    return Hardware(os=os_name, arch=_norm_arch(platform.machine()), ram_gb=_ram_gb(),
                    vram_total_gb=total, vram_free_gb=free, cuda_level=cuda,
                    vulkan=_has_vulkan(os_name))


def _ctx_for(budget_bytes: float) -> int:
    """Largest power-of-two context in [CTX_MIN, CTX_MAX] whose KV fits the budget."""
    ctx = CTX_MAX
    while ctx > CTX_MIN and ctx * KV_BYTES_PER_TOKEN > budget_bytes:
        ctx //= 2
    return ctx


def plan(hw: Hardware, quant: str = "auto", backend: str = "auto",
         port: int = DEFAULT_PORT, ctx: int = 0) -> Plan:
    """Choose asset, weights, context and offload for this machine. Pure: no I/O."""
    notes: List[str] = []
    if hw.ram_gb and hw.ram_gb < MIN_RAM_GB:
        raise HomeError(f"Bonsai 2 27B needs at least {MIN_RAM_GB} GB of RAM; this machine "
                        f"reports {hw.ram_gb:.0f} GB. Use `adk home model --local bonsai` "
                        "(install-bonsai.sh picks a smaller Bonsai) instead.")
    cuda_ok = hw.cuda_level >= (12, 4)
    forced = backend != "auto"
    if backend == "auto":
        if hw.os == "darwin":
            backend = "metal" if hw.arch == "arm64" else "cpu"
        elif hw.arch == "x64" and hw.vram_total_gb >= 10 and cuda_ok:
            backend = "cuda"
        elif hw.vulkan:
            backend = "vulkan"
        else:
            backend = "cpu"
    assets = ASSETS.get((hw.os, hw.arch, backend))
    if assets is None:
        have = sorted(b for (o, a, b) in ASSETS if (o, a) == (hw.os, hw.arch))
        if not have:
            raise HomeError(f"no PrismML {RELEASE} build for {hw.os}/{hw.arch}; Bonsai 2 "
                            "needs that fork (stock llama.cpp emits gibberish), so there "
                            "is nothing to fall back to. Supported: linux x64/arm64, "
                            "windows x64, macOS arm64/x64.")
        raise HomeError(f"--backend {backend} has no PrismML build for {hw.os}/{hw.arch}; "
                        f"choose one of: {', '.join(have)}")
    if quant == "auto":
        fast = (backend == "cuda" and hw.vram_total_gb >= 10) or \
               (backend == "metal" and hw.ram_gb >= 16)
        quant = "PQ2_0" if fast else "PTQ1_0"
        # A shared GPU where PQ2_0 does not fit: the smaller file puts more (or all)
        # layers on the GPU, which beats the bigger one split across the PCIe bus.
        spare = hw.vram_free_gb * GIB - 1.5 * GIB
        if quant == "PQ2_0" and backend == "cuda" and hw.vram_free_gb and \
                spare < GGUFS["PQ2_0"].size + MIN_USEFUL_CTX * KV_BYTES_PER_TOKEN:
            quant = "PTQ1_0"
            notes.append(f"{hw.vram_free_gb:.1f} GB VRAM free: PQ2_0 does not fit on the "
                         "GPU, PTQ1_0 puts more of the model there")
    if quant not in GGUFS:
        raise HomeError(f"--quant must be one of {', '.join(GGUFS)} (legacy Q2_0 is "
                        f"refused by {RELEASE})")
    w = GGUFS[quant]
    model_bytes = w.size
    if backend == "cpu":
        ngl = 0
        budget = hw.ram_gb * GIB * 0.75 - model_bytes
    elif backend == "cuda" and hw.vram_free_gb:
        free = hw.vram_free_gb * GIB - 1.5 * GIB          # CUDA context + compute
        if free >= model_bytes:
            ngl, budget = 99, free - model_bytes
        else:
            # Each offloaded layer brings its share of the KV cache to the GPU too, so
            # fix the context first and fit layers to weights + KV.
            if ctx <= 0:
                ctx = MIN_USEFUL_CTX
            per_ctx = model_bytes + ctx * KV_BYTES_PER_TOKEN
            ngl = max(0, min(N_LAYERS - 1, int(N_LAYERS * free / per_ctx)))
            budget = hw.ram_gb * GIB * 0.75 - model_bytes
            notes.append(f"only {hw.vram_free_gb:.1f} GB VRAM free: {ngl}/{N_LAYERS} "
                         "layers on the GPU, the rest on CPU (slower)")
    else:
        # Vulkan/Metal: no portable free-VRAM query; Metal shares system RAM.
        ngl = 99
        budget = (hw.ram_gb * GIB * 0.6 - model_bytes) if backend == "metal" \
            else 4 * GIB
    if ngl == 0:
        # Measured 2026-10-01 (16 cores, prism-b10685, PTQ1_0): 0.53 tok/s prompt and
        # 0.45 tok/s generation on the CPU -- minutes per Hearth reply. Never chosen
        # silently; --backend cpu (or cuda on a full GPU) runs it anyway.
        slow = ("the CPU runs Bonsai 2 at ~0.5 tokens/s (measured), minutes per reply; "
                "free GPU memory or use `adk home model --local bonsai` (a smaller "
                "Bonsai that runs well on a CPU)")
        if not forced:
            raise HomeError(f"no GPU memory for Bonsai 2 here ({hw.vram_free_gb:.1f} GB "
                            f"free): {slow}. `--backend cpu` runs it anyway.")
        notes.append(slow)
    if ctx <= 0:
        ctx = _ctx_for(max(0.0, budget))
    if backend == "vulkan" and quant == "PQ2_0":
        notes.append("PQ2_0 kernels are CUDA/Metal-fast; on Vulkan PTQ1_0 is smaller "
                     "and usually as quick")
    return Plan(os=hw.os, arch=hw.arch, backend=backend, quant=quant, assets=assets,
                weights=w, ctx=ctx, ngl=ngl, port=port, notes=notes)


# ------------------------------------------------------------------- data + fetch

def data_dir(override: str = "") -> Path:
    raw = override or os.environ.get(DATA_ENV, "")
    if raw:
        return Path(raw).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Aitherium" / "bonsai2"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Aitherium" / "bonsai2"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "aitherium" / "bonsai2"


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _stamps(root: Path) -> Dict[str, Any]:
    try:
        return json.loads((root / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _stamp_ok(root: Path, path: Path, sha: str) -> bool:
    """A file verified before and untouched since need not be re-hashed (7 GB)."""
    s = _stamps(root).get(path.name) or {}
    try:
        st = path.stat()
    except OSError:
        return False
    return (s.get("sha256") == sha and s.get("size") == st.st_size
            and s.get("mtime_ns") == st.st_mtime_ns)


def _stamp(root: Path, path: Path, sha: str) -> None:
    stamps = _stamps(root)
    st = path.stat()
    stamps[path.name] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    (root / STAMP_FILE).write_text(json.dumps(stamps, indent=1), encoding="utf-8")


Say = Callable[[str], None]


def _download(url: str, part: Path, say: Say, expected_size: int = 0) -> None:
    """Fetch ``url`` into ``part``, resuming from its current length (HTTP Range)."""
    import httpx

    have = part.stat().st_size if part.exists() else 0
    if expected_size and have > expected_size:
        part.unlink()
        have = 0
    if expected_size and have == expected_size:
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                      timeout=httpx.Timeout(60.0, read=300.0)) as r:
        if r.status_code == 416 and have:
            return                          # already complete
        if r.status_code == 200 and have:
            have = 0                        # server ignored Range: start over
        elif r.status_code not in (200, 206):
            raise HomeError(f"{url} -> HTTP {r.status_code}")
        # The mirror streams chunked (no content-length): trust the pinned size.
        total = expected_size or (int(r.headers.get("content-length", 0)) + have)
        mode = "ab" if have else "wb"
        last = time.monotonic()
        done = have
        with open(part, mode) as f:
            for chunk in r.iter_bytes(4 * 1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 10 and total:
                    say(f"    {done / GIB:.2f} / {total / GIB:.2f} GB")
                    last = time.monotonic()


def _download_resuming(url: str, part: Path, say: Say, expected_size: int,
                       errors: List[str], attempts: int = 8) -> bool:
    """Multi-GB streams drop mid-file (measured: both sources broke past 6 GB on
    2026-10-01). Retry the SAME source from where it stopped while each attempt
    makes progress; give up on it after ``attempts`` stalls."""
    stalls = 0
    while True:
        before = part.stat().st_size if part.exists() else 0
        try:
            _download(url, part, say, expected_size)
            return True
        except HomeError as exc:
            errors.append(f"{url}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 -- network; retried, then reported
            after = part.stat().st_size if part.exists() else 0
            stalls = 0 if after > before else stalls + 1
            if stalls >= attempts:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                return False
            say(f"    connection dropped at {after / GIB:.2f} GB "
                f"({type(exc).__name__}); resuming")
            time.sleep(min(30, 2 ** stalls))


def fetch_verified(urls: List[str], dest: Path, sha: str, say: Say,
                   expected_size: int = 0) -> Path:
    """Download ``dest`` from the first source that yields bytes matching ``sha``.

    Resumes a ``.part`` left by an interrupted run. A checksum mismatch deletes the
    bytes and tries the next source; if none matches it REFUSES -- an unverified
    file is never moved into place.
    """
    root = dest.parent
    root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if _stamp_ok(root, dest, sha) or sha256_file(dest) == sha:
            _stamp(root, dest, sha)
            return dest
        say(f"  {dest.name}: on-disk copy fails its sha256 -- re-downloading")
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    errors: List[str] = []
    for url in urls:
        say(f"  fetching {dest.name} from {url.split('/')[2]}")
        if not _download_resuming(url, part, say, expected_size, errors):
            continue
        got = sha256_file(part)
        if got != sha:
            part.unlink()
            errors.append(f"{url}: sha256 {got} != pinned {sha}")
            continue
        os.replace(part, dest)
        _stamp(root, dest, sha)
        return dest
    raise HomeError(f"refusing {dest.name}: no source produced the pinned bytes\n  "
                    + "\n  ".join(errors))


def _safe_extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise HomeError(f"{archive.name}: unsafe path {name!r}")
            z.extractall(into)
        return
    with tarfile.open(archive, "r:gz") as t:
        for m in t.getmembers():
            if not (root / m.name).resolve().is_relative_to(root) or m.isdev():
                raise HomeError(f"{archive.name}: unsafe member {m.name!r}")
            if (m.issym() or m.islnk()) and not \
                    (root / m.name).parent.joinpath(m.linkname).resolve().is_relative_to(root):
                raise HomeError(f"{archive.name}: link escapes the archive {m.name!r}")
        t.extractall(into)


def _server_name(os_name: str) -> str:
    return "llama-server.exe" if os_name == "windows" else "llama-server"


def find_server(bin_dir: Path, os_name: str) -> Path:
    name = _server_name(os_name)
    hits = sorted(bin_dir.rglob(name), key=lambda p: len(p.parts))
    if not hits:
        raise HomeError(f"{RELEASE} archive unpacked to {bin_dir} holds no {name}")
    return hits[0]


def _server_env(server: Path) -> Dict[str, str]:
    env = dict(os.environ)
    d = str(server.parent)
    if sys.platform == "darwin":
        env["DYLD_LIBRARY_PATH"] = d + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
    elif sys.platform != "win32":
        env["LD_LIBRARY_PATH"] = d + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def check_prism_build(server: Path) -> str:
    """Refuse any binary whose ``--version`` does not name the pinned PrismML commit.

    ggml loads its backends from the cwd, so run from the binary's own directory.
    """
    try:
        r = subprocess.run([str(server), "--version"], cwd=str(server.parent),
                           env=_server_env(server), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HomeError(f"{server} does not run here: {exc}") from exc
    out = (r.stdout + r.stderr).strip()
    if RELEASE_COMMIT not in out:
        raise HomeError(f"{server} is not the PrismML {RELEASE} build (--version said "
                        f"{out[-300:]!r}); stock llama.cpp emits gibberish on Bonsai 2, "
                        "so it is never used as a fallback")
    return out


def install(p: Plan, root: Path, say: Say) -> Tuple[Path, Path]:
    """-> (llama-server, gguf), both from pinned, verified bytes."""
    dl = root / "downloads"
    bin_dir = root / "bin" / f"{RELEASE}-{p.os}-{p.arch}-{p.backend}"
    marker = bin_dir / ".complete"
    if not marker.exists():
        for asset, sha in p.assets:
            archive = fetch_verified([f"{RELEASE_BASE}/{asset}"], dl / asset, sha, say)
            _safe_extract(archive, bin_dir)
        marker.write_text(json.dumps([a for a, _ in p.assets]), encoding="utf-8")
    server = find_server(bin_dir, p.os)
    if p.os != "windows":
        server.chmod(server.stat().st_mode | 0o111)
    say(f"  server: {check_prism_build(server).splitlines()[0]}")
    w = p.weights
    gguf = fetch_verified([f"{base}/{w.file}" for base in GGUF_SOURCES],
                          root / "models" / w.file, w.sha256, say, w.size)
    return server, gguf


# ------------------------------------------------------------------- run + stop

def server_args(p: Plan, gguf: Path) -> List[str]:
    """llama-server flags. --reasoning-budget bounds the <think> block Bonsai's Qwen
    template force-opens (unbounded, content comes back empty); sampling is PrismML's
    recommendation for the thinking models; -np 1 gives the one slot the full ctx."""
    return ["-m", str(gguf), "--host", "127.0.0.1", "--port", str(p.port),
            "--alias", ALIAS, "-c", str(p.ctx), "-np", "1", "-ngl", str(p.ngl),
            "-fa", "on", "--reasoning-budget", "2048",
            "--temp", "1.0", "--top-p", "0.95", "--top-k", "20"]


def _read_state(root: Path) -> Dict[str, Any]:
    try:
        return json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cmdline(pid: int) -> str:
    """The process's command line, or '' when it is gone or unreadable."""
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        psutil = None  # not a dependency; fall through to the per-OS readers
    if psutil is not None:
        try:
            return " ".join(psutil.Process(pid).cmdline())
        except Exception:  # noqa: BLE001 -- NoSuchProcess / AccessDenied
            return ""
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            return ""
    if sys.platform == "win32":
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                            ".CommandLine"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()
    r = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                       capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10)
    return r.stdout.strip()


def ours_running(root: Path) -> Optional[Dict[str, Any]]:
    """The recorded server, if its pid is alive AND still runs our binary."""
    st = _read_state(root)
    pid, server = st.get("pid"), st.get("server", "")
    if not pid or not server:
        return None
    cmd = _cmdline(int(pid)).replace("\\", "/").lower()
    return st if server.replace("\\", "/").lower() in cmd else None


def _served_models(port: int) -> Optional[List[str]]:
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=3.0)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return []
    try:
        return [str(m.get("id")) for m in r.json().get("data") or [] if isinstance(m, dict)]
    except (ValueError, AttributeError):
        return []


def start(p: Plan, server: Path, gguf: Path, root: Path, say: Say,
          wait_s: float = 900.0) -> Dict[str, Any]:
    """Start llama-server unless our own is already up. Never touches another process."""
    running = ours_running(root)
    served = _served_models(p.port)
    if running and served is not None and ALIAS in served:
        say(f"  already serving {ALIAS} on :{p.port} (pid {running['pid']})")
        return running
    if served is not None:
        raise HomeError(f"127.0.0.1:{p.port} is already in use by a server this did not "
                        "start; it was left alone. Pass --port to use another port.")
    log = root / "server.log"
    argv = [str(server)] + server_args(p, gguf)
    kw: Dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                               | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    with open(log, "ab") as logf:
        proc = subprocess.Popen(argv, cwd=str(server.parent), env=_server_env(server),
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **kw)
    state = {"pid": proc.pid, "port": p.port, "server": str(server), "gguf": str(gguf),
             "argv": argv, "plan": p.to_dict(), "started": time.time()}
    (root / STATE_FILE).write_text(json.dumps(state, indent=1), encoding="utf-8")
    say(f"  started llama-server pid {proc.pid} on 127.0.0.1:{p.port}; loading "
        f"{gguf.name} (log: {log})")
    import httpx

    deadline = time.monotonic() + wait_s
    last = "no answer yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise HomeError(f"llama-server exited {proc.returncode} while loading:\n{tail}")
        try:
            if httpx.get(f"http://127.0.0.1:{p.port}/health", timeout=3.0).status_code == 200:
                say("  serving.")
                return state
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}"   # still loading; reported if it never comes up
        time.sleep(2)
    raise HomeError(f"llama-server did not become healthy in {wait_s:.0f}s (last: {last}); "
                    f"it is still running as pid {proc.pid}. Log: {log}")


def stop(root: Path) -> str:
    st = _read_state(root)
    if not st.get("pid"):
        return "no Bonsai 2 server recorded"
    if ours_running(root) is None:
        return (f"pid {st['pid']} is gone or is no longer our llama-server; "
                "nothing was signalled")
    import signal

    os.kill(int(st["pid"]), signal.SIGTERM)
    return f"stopped llama-server pid {st['pid']}"


def ensure_running(root: Path, say: Say) -> Optional[Dict[str, Any]]:
    """Restart the recorded server after a reboot, offline (no downloads)."""
    st = _read_state(root)
    if not st.get("plan"):
        return None
    pd = dict(st["plan"])
    pd.pop("release", None)
    pd["weights"] = Weights(**pd["weights"])
    pd["assets"] = [tuple(a) for a in pd["assets"]]
    p = Plan(**pd)
    server, gguf = Path(st["server"]), Path(st["gguf"])
    if not (server.exists() and gguf.exists()):
        raise HomeError("Bonsai 2 files are missing; run `adk home model --local bonsai2`")
    return start(p, server, gguf, root, say)


def install_and_start(quant: str = "auto", backend: str = "auto", port: int = DEFAULT_PORT,
                      ctx: int = 0, root_override: str = "", dry_run: bool = False,
                      say: Say = print, hw: Optional[Hardware] = None) -> Dict[str, Any]:
    hw = hw or detect()
    p = plan(hw, quant=quant, backend=backend, port=port, ctx=ctx)
    root = data_dir(root_override)
    say(f"Bonsai 2 27B: {p.quant} on {p.backend} ({hw.os}/{hw.arch}, RAM "
        f"{hw.ram_gb:.0f} GB, VRAM {hw.vram_free_gb:.1f}/{hw.vram_total_gb:.1f} GB free), "
        f"ctx {p.ctx}, -ngl {p.ngl}, PrismML {RELEASE}")
    for n in p.notes:
        say(f"  note: {n}")
    out = {"plan": p.to_dict(), "data_dir": str(root),
           "base_url": f"http://127.0.0.1:{p.port}/v1", "model": ALIAS}
    if dry_run:
        return out
    root.mkdir(parents=True, exist_ok=True)
    server, gguf = install(p, root, say)
    st = start(p, server, gguf, root, say)
    out["pid"] = st.get("pid")
    return out
