"""Plan B Ledger — brain bootstrap.

Acquires and starts the local bonsai brain with zero cloud dependency:
  1. If something already answers on the endpoint, done.
  2. llama-server binary: reuse an existing install (~/.aither/llamacpp — the
     adk convention — or ~/.aither/planb/llamacpp), else download the latest
     llama.cpp release build for this platform from GitHub.
  3. Model weights: the Aitherium self-hosted mirror FIRST (Cloudflare Worker
     over the aitherkvcache releases — HuggingFace has gated the upstream repo
     before), HF as fallback. Resumable (HTTP Range), size-checked.
  4. Launch llama-server on the profile port (8090), verify /v1/models AND one
     real completion — a listening socket is not a working brain.

All stdlib. Every step prints what it is doing; every failure says what to do.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import ledger

MIRROR = "https://bonsai-weights.alexander-parkhurst.workers.dev"
HF_BASE = "https://huggingface.co/prism-ml/{repo}/resolve/main/{fname}"
RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

PORT = int(os.environ.get("PLANB_LLM_PORT", "8090"))
MODELS_DIR = ledger.DATA_DIR / "models"
LLAMA_DIRS = [Path.home() / ".aither" / "llamacpp",
              ledger.DATA_DIR / "llamacpp"]

# Mirrors the awkit bonsai catalogue (webml/bonsai-models.ts).
MODELS = {
    "bonsai-1.7b": {"fname": "Bonsai-1.7B-Q1_0.gguf", "repo": "Bonsai-1.7B-gguf",
                    "size_mb": 236, "min_ram_gb": 2},
    "bonsai-4b": {"fname": "Bonsai-4B-Q1_0.gguf", "repo": "Bonsai-4B-gguf",
                  "size_mb": 545, "min_ram_gb": 3},
    "bonsai-8b": {"fname": "Bonsai-8B-Q1_0.gguf", "repo": "Bonsai-8B-gguf",
                  "size_mb": 1104, "min_ram_gb": 5},
    "bonsai-27b": {"fname": "Bonsai-27B-Q1_0.gguf", "repo": "Bonsai-27B-gguf",
                   "size_mb": 3627, "min_ram_gb": 6},
}


def _say(msg: str) -> None:
    print(f"  [brain] {msg}", flush=True)


def _ram_gb() -> float:
    try:
        if platform.system() == "Windows":
            import ctypes

            class MemStat(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong)]

            stat = MemStat()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullTotalPhys / (1024 ** 3)
        pages = os.sysconf("SC_PHYS_PAGES")
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
    except Exception:  # noqa: BLE001 — unknown RAM just means conservative pick
        return 4.0


def pick_model(requested: str = "auto") -> str:
    if requested != "auto":
        if requested not in MODELS:
            raise SystemExit(f"unknown model '{requested}' — one of: {', '.join(MODELS)}")
        return requested
    ram = _ram_gb()
    for mid in ("bonsai-27b", "bonsai-8b", "bonsai-4b"):
        if ram >= MODELS[mid]["min_ram_gb"] + 2:  # headroom for the OS
            return mid
    return "bonsai-1.7b"


def endpoint_live(port: int = PORT, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — dead endpoint is the normal case here
        return False


def _download(url: str, dest: Path, expect_mb: int) -> bool:
    """Resumable download with progress. True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    expect = expect_mb * 1024 * 1024
    if have >= expect * 0.98:
        _say(f"already have {dest.name} ({have // 2**20} MB)")
        return True
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
        _say(f"resuming {dest.name} at {have // 2**20} MB")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            mode = "ab" if have and resp.status == 206 else "wb"
            done = have if mode == "ab" else 0
            with open(dest, mode) as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    print(f"\r  [brain] {dest.name}: {done // 2**20} / {expect_mb} MB",
                          end="", flush=True)
        print()
        return dest.stat().st_size >= expect * 0.98
    except (urllib.error.URLError, OSError) as exc:
        print()
        _say(f"download failed from {url.split('/')[2]}: {exc}")
        return False


def ensure_model(model_id: str) -> Path | None:
    info = MODELS[model_id]
    dest = MODELS_DIR / info["fname"]
    for url in (f"{MIRROR}/{info['fname']}",
                HF_BASE.format(repo=info["repo"], fname=info["fname"])):
        if _download(url, dest, info["size_mb"]):
            return dest
    return None


def find_llama_server() -> Path | None:
    name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    for d in LLAMA_DIRS:
        if d.exists():
            hits = list(d.rglob(name))
            if hits:
                return hits[0]
    return None


def install_llama_server() -> Path | None:
    """Download the latest llama.cpp release build for this platform."""
    existing = find_llama_server()
    if existing:
        _say(f"llama-server found: {existing}")
        # A CUDA build without the cudart runtime SILENTLY runs CPU-only —
        # measured here: 27B at 3.85 tok/s on a 32GB-VRAM box, no error
        # anywhere. Say it loudly; the fix is the release's cudart zip.
        d = existing.parent
        if list(d.glob("ggml-cuda*.dll")) and not list(d.glob("cudart64*.dll")):
            _say("WARNING: CUDA build with no cudart64*.dll beside it — it will "
                 "fall back to CPU silently. Drop the matching "
                 "cudart-llama-bin-win-cuda zip from the same llama.cpp release "
                 f"into {d}")
        return existing
    _say("downloading llama.cpp server build (GitHub latest release)...")
    try:
        req = urllib.request.Request(RELEASES_API,
                                     headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            assets = json.loads(r.read()).get("assets", [])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _say(f"cannot reach GitHub releases: {exc}")
        return None
    sysname = platform.system()
    os_tag = {"Windows": "win", "Linux": "ubuntu", "Darwin": "macos"}.get(sysname, "ubuntu")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    url = None
    for want in (f"{os_tag}-vulkan-{arch}", f"{os_tag}-cpu-{arch}",
                 f"{os_tag}-{arch}"):
        for a in assets:
            name = a.get("name", "").lower()
            if name.endswith(".zip") and want in name:
                url = a.get("browser_download_url")
                break
        if url:
            break
    if not url:
        _say("no matching llama.cpp build for this platform — install it manually "
             "and re-run (https://github.com/ggml-org/llama.cpp/releases)")
        return None
    dest_dir = LLAMA_DIRS[1]
    zip_path = dest_dir / "llama-cpp.zip"
    if not _download(url, zip_path, 1):  # size unknown; 1MB floor disables the skip
        return None
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)
    zip_path.unlink(missing_ok=True)
    if sysname != "Windows":
        for f in dest_dir.rglob("llama-server"):
            f.chmod(0o755)
    return find_llama_server()


def launch(binary: Path, model: Path, port: int = PORT) -> bool:
    _say(f"starting llama-server on :{port} with {model.name} ...")
    ledger.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = ledger.DATA_DIR / "llama-server.log"
    # -ngl 999 offloads every layer a GPU build can take; CPU-only builds
    # ignore it with a warning. Without it a CUDA/vulkan build still runs
    # 100% CPU — measured here: 27B at 8 tok/s grinding every core.
    cmd = [str(binary), "-m", str(model), "--port", str(port),
           "--host", "127.0.0.1", "--ctx-size", "4096", "-ngl", "999"]
    # cwd MUST be the binary's directory. ggml loads its backends (ggml-cuda.dll,
    # ggml-vulkan.dll) at runtime through the backend registry, and that lookup
    # follows the WORKING directory — launch from anywhere else and it silently
    # finds only the CPU backend. Measured 2026-08-09: same binary, same -ngl 999,
    # 3.84 t/s from another cwd vs 21.30 t/s from the binary dir, with no error
    # and a healthy /health either way.
    kwargs: dict = {"stdout": open(log, "ab"), "stderr": subprocess.STDOUT,
                    "cwd": str(binary.parent)}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000208  # DETACHED_PROCESS | ABOVE_NORMAL off
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    (ledger.DATA_DIR / "llama-server.pid").write_text(str(proc.pid), encoding="utf-8")
    for _ in range(120):  # model load can take a while on first mmap
        if endpoint_live(port):
            return _smoke(port)
        if proc.poll() is not None:
            _say(f"llama-server exited early — see {log}")
            return False
        time.sleep(2)
    _say(f"server never answered on :{port} — see {log}")
    return False


def _smoke(port: int) -> bool:
    """A listening socket is not a brain — require one real completion."""
    body = json.dumps({"model": "bonsai", "max_tokens": 8,
                       "messages": [{"role": "user", "content": "Say OK"}]}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            msg = json.loads(r.read())["choices"][0]["message"]
        # Reasoning models may put every token in the think channel; either
        # channel counts, but EMPTY BOTH is a failure — a listening socket
        # that emits nothing is not a working brain.
        text = (msg.get("content") or "") + (msg.get("reasoning_content") or "")
        if not text.strip():
            _say("smoke completion returned no text — not bootstrapped")
            return False
        _say(f"smoke completion ok: {text.strip()[:40]!r}")
        return True
    except Exception as exc:  # noqa: BLE001 — smoke failure = not bootstrapped
        _say(f"smoke completion failed: {exc}")
        return False


def bootstrap(model_id: str = "auto", port: int = PORT) -> bool:
    if endpoint_live(port):
        _say(f"brain already live on :{port} — nothing to do")
        return True
    chosen = pick_model(model_id)
    _say(f"model: {chosen} ({MODELS[chosen]['size_mb']} MB, "
         f"RAM {_ram_gb():.0f} GB detected)")
    binary = install_llama_server()
    if binary is None:
        return False
    model = ensure_model(chosen)
    if model is None:
        _say("could not fetch weights from the mirror or HuggingFace — "
             "check the network and re-run; the ledger still works on the "
             "pattern brain meanwhile")
        return False
    return launch(binary, model, port)


if __name__ == "__main__":
    ok = bootstrap(sys.argv[1] if len(sys.argv) > 1 else "auto")
    sys.exit(0 if ok else 1)
