"""``adk bonsai setup --device phone`` -- a small Bonsai model on a phone CPU.

For the Pixel "Linux terminal" (a Debian arm64 VM) or any aarch64/x64 Linux box
with no GPU. It is OPTIONAL: nothing in Aither Learn needs it (the tutor's hints
are canned when no model answers). It reuses :mod:`adk.llamacpp_setup` for the
llama.cpp binary (the release's plain ``ubuntu-arm64`` CPU build) and fetches a
Bonsai 1 Q1_0 GGUF from weights.aitherium.com -- 1.7B (~250 MB) by default, 4B
(~570 MB) on request. Bonsai 2 is NOT offered: it needs the PrismML llama.cpp
fork and 12 GB+ of RAM.

    adk bonsai setup --device phone [--model 1.7b|4b] [--port 8080] [--no-start] [--dry-run]
    adk bonsai status [--port 8080]

It never changes ``default_backend``; it records the endpoint under
``bonsai_phone`` in ~/.aither/config.json, and ``--use`` opts in to routing the
adk's own chat through it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

WEIGHTS_BASE = "https://weights.aitherium.com"
#: size_bytes: the published byte count (awdk/adk/packs/gobbonet/catalog.py, measured
#: by a ranged request). sha256: pinned where one was measured (1.7B, the AFRL offline
#: proof run); the 4B has no pin yet, so it is checked by exact size only.
MODELS: Dict[str, Dict[str, Any]] = {
    "1.7b": {
        "file": "Bonsai-1.7B-Q1_0.gguf",
        "disk_mb": 250,
        "ram_gb": 2,
        "size_bytes": 248302272,
        "sha256": "3d7c6c90dd98717a203adb22d5eacd2581850e40aa5327e144b97766cae5f7e3",
    },
    "4b": {
        "file": "Bonsai-4B-Q1_0.gguf",
        "disk_mb": 570,
        "ram_gb": 4,
        "size_bytes": 572270624,
        "sha256": "",
    },
}
DEFAULT_MODEL = "1.7b"
DEFAULT_PORT = 8080
PHONE_CTX = 2048
SERVED_NAME = "bonsai-phone"
GGUF_MAGIC = b"GGUF"

BUILD_FROM_SOURCE = (
    "sudo apt install -y build-essential cmake git",
    "git clone --depth 1 https://github.com/ggml-org/llama.cpp ~/llama.cpp",
    "cmake -S ~/llama.cpp -B ~/llama.cpp/build -DLLAMA_CURL=OFF",
    "cmake --build ~/llama.cpp/build --target llama-server -j4",
    "adk bonsai setup --device phone --server ~/llama.cpp/build/bin/llama-server",
)


def phone_threads(cpu_count: Optional[int] = None) -> int:
    """Leave cores for the phone itself: at most 4 threads, at least 1."""
    n = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    return max(1, min(4, n - 1 if n > 2 else n))


def server_cmd(binary: Path, model: Path, port: int, threads: int) -> List[str]:
    """llama-server on loopback only, a short context, CPU threads capped."""
    return [
        str(binary),
        "-m",
        str(model),
        "-c",
        str(PHONE_CTX),
        "-t",
        str(threads),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--alias",
        SERVED_NAME,
    ]


def is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == GGUF_MAGIC
    except OSError:
        return False


def model_complete(path: Path, spec: Dict[str, Any]) -> bool:
    """A whole model: GGUF magic AND the exact published size. The magic alone is
    in the first 4 bytes, so a download cut off mid-way would pass it."""
    if not is_gguf(path):
        return False
    try:
        return path.stat().st_size == int(spec["size_bytes"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


def fetch_model(url: str, model: Path, spec: Dict[str, Any]) -> bool:
    """Download to ``<file>.part`` and rename onto ``model`` only when the size (and
    the sha256, where pinned) match. A kill mid-download leaves only the .part file,
    which is never taken for a model; Ctrl-C removes it."""
    from adk import llamacpp_setup as lc

    part = model.with_name(model.name + ".part")
    try:
        part.unlink(missing_ok=True)
        ok = lc._download(url, part, spec["file"], expected_sha256=spec.get("sha256") or "")
        if not ok or not model_complete(part, spec):
            return False
        os.replace(part, model)
        return True
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


def binary_runs(binary: Path) -> bool:
    """A prebuilt Ubuntu binary can need a newer glibc than the Debian VM has;
    ``--version`` exits non-zero (or cannot exec) when it does."""
    try:
        res = subprocess.run([str(binary), "--version"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def write_launcher(cmd: List[str], home: Optional[Path] = None) -> Path:
    base = (home or Path.home()) / ".aither" / "bin"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "bonsai-phone.sh"
    quoted = " ".join("'" + c.replace("'", "'\\''") + "'" for c in cmd)
    path.write_text(
        "#!/bin/sh\n# Written by adk bonsai setup --device phone\nexec " + quoted + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o755)
    except OSError:
        print(f"  NOTE: could not mark {path} executable; run: sh {path}")
    return path


def _record(port: int, model: Path, use: bool) -> None:
    from adk.config import save_saved_config  # merges into what is already there

    update: Dict[str, Any] = {
        "bonsai_phone": {
            "url": f"http://127.0.0.1:{port}/v1",
            "model": SERVED_NAME,
            "model_path": str(model),
        }
    }
    if use:
        update.update(
            {
                "default_backend": "openai",
                "inference_url": f"http://127.0.0.1:{port}/v1",
                "orchestrator_model": SERVED_NAME,
            }
        )
    save_saved_config(update)


def cmd_setup(args: argparse.Namespace) -> int:
    from adk import llamacpp_setup as lc

    key = (getattr(args, "model", None) or DEFAULT_MODEL).lower()
    spec = MODELS.get(key)
    if spec is None:
        print(f"  Unknown model {key!r}; choose one of: {', '.join(MODELS)}")
        return 2
    dry = bool(getattr(args, "dry_run", False))
    port = int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT)

    accel = lc.detect_accel()
    if getattr(args, "device", "phone") == "phone":
        accel.kind, accel.name = "cpu", "phone CPU"
    print(f"  Device: {accel.os_family}/{accel.arch}, {accel.ram_gb:.1f} GB RAM, CPU build")
    if accel.os_family != "linux":
        print(
            "  --device phone targets Linux (the Pixel Linux terminal). Use 'adk setup' elsewhere."
        )
        return 2
    if accel.arch != "arm64":
        print(
            "  NOTE: not an arm64 machine; continuing with the x64 CPU build (useful for testing)."
        )
    if accel.ram_gb and accel.ram_gb < spec["ram_gb"]:
        print(
            f"  NOTE: {spec['file']} wants ~{spec['ram_gb']} GB; "
            f"this VM reports {accel.ram_gb:.1f} GB. "
            "Try --model 1.7b or give the Linux terminal more memory."
        )

    server = getattr(args, "server", "") or ""
    binary: Optional[Path] = (
        Path(os.path.expanduser(server)) if server else lc.install_llamacpp(accel, dry)
    )
    if binary is None:
        print("  Could not fetch a llama.cpp build. Build it on the phone instead:")
        for line in BUILD_FROM_SOURCE:
            print(f"    {line}")
        return 1
    if not dry and not binary_runs(binary):
        print(f"  {binary} does not run here (often a glibc mismatch). Build it on the phone:")
        for line in BUILD_FROM_SOURCE:
            print(f"    {line}")
        return 1

    model = lc.MODELS_DIR / spec["file"]
    url = f"{WEIGHTS_BASE}/{spec['file']}"
    if dry:
        print(f"  [DRY] Would download {url} (~{spec['disk_mb']} MB) to {model}")
    elif model_complete(model, spec):
        print(f"  Model already here: {model}")
    else:
        if model.exists():
            print(f"  {model.name} is not complete; fetching it again.")
            model.unlink(missing_ok=True)
        if (
            shutil.disk_usage(model.parent if model.parent.exists() else Path.home()).free
            < spec["disk_mb"] * 2 * 1024 * 1024
        ):
            print(f"  Not enough free space for {spec['file']} (~{spec['disk_mb']} MB).")
            return 1
        if not fetch_model(url, model, spec):
            print("  The model download did not finish. Run the same command again.")
            return 1

    cmd = server_cmd(binary, model, port, phone_threads())
    if dry:
        print("  [DRY] Would run: " + " ".join(cmd))
        return 0
    launcher = write_launcher(cmd)
    _record(port, model, bool(getattr(args, "use", False)))
    print(f"  Launcher: {launcher}")
    if getattr(args, "no_start", False):
        print(f"  Start it with: {launcher}")
        return 0
    lc.LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not lc._spawn_detached(cmd, lc.LOG_DIR / "bonsai-phone.log"):
        return 1
    print(f"  Started on http://127.0.0.1:{port}/v1 (model {SERVED_NAME}).")
    print("  It stops when the Linux terminal closes; run the launcher to start it again.")
    print("  Check it: adk bonsai status")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    port = int(getattr(args, "port", DEFAULT_PORT) or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 -- loopback only
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"  Not running on port {port} ({type(exc).__name__}).")
        return 1
    names = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    print(f"  Running on port {port}: {', '.join(names) or 'no model listed'}")
    return 0


def register_parser(sub: Any) -> None:
    p = sub.add_parser(
        "bonsai", help="Small on-device Bonsai model (llama.cpp, CPU; phone-friendly)"
    )
    bsub = p.add_subparsers(dest="bonsai_action")
    s = bsub.add_parser("setup", help="Install llama.cpp + a Bonsai 1 Q1_0 model and start it")
    s.add_argument(
        "--device",
        choices=["phone", "auto"],
        default="phone",
        help="phone: CPU build, 2k context, <=4 threads (default)",
    )
    s.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--server", default="", help="Use this llama-server binary (e.g. one you built)")
    s.add_argument("--use", action="store_true", help="Also make it the adk's default chat backend")
    s.add_argument("--no-start", action="store_true", help="Install only; print how to start it")
    s.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")
    st = bsub.add_parser("status", help="Is the local Bonsai server answering?")
    st.add_argument("--port", type=int, default=DEFAULT_PORT)


def cmd_bonsai(args: argparse.Namespace) -> int:
    action = getattr(args, "bonsai_action", None)
    if action == "setup":
        return cmd_setup(args)
    if action == "status":
        return cmd_status(args)
    print("  Usage: adk bonsai setup --device phone | adk bonsai status")
    return 2


if __name__ == "__main__":  # pragma: no cover
    _p = argparse.ArgumentParser(prog="adk")
    register_parser(_p.add_subparsers(dest="command"))
    sys.exit(cmd_bonsai(_p.parse_args()))
