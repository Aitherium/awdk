"""
Unsloth Fork llama.cpp Provisioner
===================================

Build and provision the Unsloth fork of llama.cpp with Kimi-K3 vision support.

Handles:
  1. Git clone of unsloth/llama.cpp fork
  2. Fetch + checkout PR#48 branch
  3. CMake configure with CUDA/RPC options
  4. Build llama-server, llama-gguf-split, rpc-server
  5. Record pinned SHA for reproducibility

Public API:
    UNSLOTH_LLAMACPP_REPO: str
    KIMI_K3_BRANCH_REF: str
    KIMI_K3_LOCAL_BRANCH: str
    PINNED_SHA: str (empty placeholder)
    plan_build(build_dir, cuda, rpc, dry_run) -> dict
    install_unsloth_llamacpp(build_dir, cuda, rpc) -> dict
    verify_kimi_binary(bin_dir) -> dict
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

UNSLOTH_LLAMACPP_REPO = "https://github.com/unslothai/llama.cpp"
KIMI_K3_BRANCH_REF = "pull/48/head"
KIMI_K3_LOCAL_BRANCH = "kimi-k3-fullsize-vision"
# Record the pinned SHA on first successful build; if set, checkout that SHA
PINNED_SHA = ""


def plan_build(
    build_dir: str | Path,
    cuda: bool = True,
    rpc: bool = True,
    dry_run: bool = True,
) -> dict:
    """
    Plan the build commands without executing.

    Args:
        build_dir: Directory to clone + build in
        cuda: Enable CUDA acceleration (-DGGML_CUDA=ON)
        rpc: Enable RPC server support (-DGGML_RPC=ON)
        dry_run: If True, return the plan; if False, still return plan
                 (caller decides execution)

    Returns:
        Dict with keys:
          - "commands": list of command lists (each item is argv list)
          - "build_dir": destination directory
          - "cuda_enabled": bool
          - "rpc_enabled": bool
    """
    build_dir = Path(build_dir)
    ggml_cuda = "ON" if cuda else "OFF"
    ggml_rpc = "ON" if rpc else "OFF"

    commands = [
        ["git", "clone", UNSLOTH_LLAMACPP_REPO, str(build_dir)],
        [
            "git", "-C", str(build_dir),
            "fetch", "origin",
            f"{KIMI_K3_BRANCH_REF}:{KIMI_K3_LOCAL_BRANCH}",
        ],
        [
            "git", "-C", str(build_dir),
            "checkout", KIMI_K3_LOCAL_BRANCH,
        ],
        [
            "cmake", "-B", str(build_dir / "build"),
            "-S", str(build_dir),
            "-DBUILD_SHARED_LIBS=OFF",
            f"-DGGML_CUDA={ggml_cuda}",
            f"-DGGML_RPC={ggml_rpc}",
        ],
        [
            "cmake", "--build", str(build_dir / "build"),
            "--target",
            "llama-cli", "llama-mtmd-cli", "llama-server",
            "llama-gguf-split", "rpc-server",
        ],
    ]

    return {
        "commands": commands,
        "build_dir": str(build_dir),
        "cuda_enabled": cuda,
        "rpc_enabled": rpc,
    }


def install_unsloth_llamacpp(
    build_dir: str | Path,
    cuda: bool = True,
    rpc: bool = True,
) -> dict:
    """
    Execute the full build pipeline for Unsloth llama.cpp.

    Idempotent: skips clone if directory exists; fetch+checkout always run.
    Records pinned SHA to {build_dir}/.unsloth-pin.json on success.

    Args:
        build_dir: Directory to clone + build in
        cuda: Enable CUDA
        rpc: Enable RPC server

    Returns:
        Dict with keys:
          - "success": bool
          - "binaries": dict[str, Path] with keys llama-server, rpc-server, etc.
          - "sha": resolved HEAD commit SHA
          - "cuda": bool
          - "rpc": bool
          - "error": str (on failure)

    Raises:
        subprocess.CalledProcessError: If any build step fails
        OSError: If output file discovery fails
    """
    build_dir = Path(build_dir)
    result = {
        "success": False,
        "binaries": {},
        "sha": "",
        "cuda": cuda,
        "rpc": rpc,
        "error": "",
    }

    plan = plan_build(build_dir, cuda, rpc, dry_run=False)

    # Clone if needed
    if not build_dir.exists():
        cmd = plan["commands"][0]
        subprocess.run(cmd, check=True)

    # Fetch + checkout
    try:
        cmd = plan["commands"][1]
        subprocess.run(cmd, check=True, text=True,
                       capture_output=True, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        result["error"] = f"git fetch failed: {e.stderr}"
        return result

    try:
        cmd = plan["commands"][2]
        subprocess.run(cmd, check=True, text=True,
                       capture_output=True, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        result["error"] = f"git checkout failed: {e.stderr}"
        return result

    # Get HEAD SHA after checkout
    try:
        sha_output = subprocess.run(
            ["git", "-C", str(build_dir), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        result["sha"] = sha_output.stdout.strip()
    except subprocess.CalledProcessError as e:
        result["error"] = f"git rev-parse failed: {e}"
        return result

    # CMake configure
    try:
        cmd = plan["commands"][3]
        subprocess.run(cmd, check=True, text=True,
                       capture_output=True, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        result["error"] = f"cmake configure failed: {e.stderr}"
        return result

    # CMake build
    try:
        cmd = plan["commands"][4]
        subprocess.run(cmd, check=True, text=True,
                       capture_output=True, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        result["error"] = f"cmake build failed: {e.stderr}"
        return result

    # Locate binaries
    build_out_dir = build_dir / "build"
    bin_candidates = {
        "llama-server": ["llama-server", "llama-server.exe"],
        "llama-cli": ["llama-cli", "llama-cli.exe"],
        "llama-gguf-split": ["llama-gguf-split",
                             "llama-gguf-split.exe"],
        "rpc-server": ["rpc-server", "rpc-server.exe"],
    }

    for name, exes in bin_candidates.items():
        found = None
        for exe in exes:
            candidate = build_out_dir / "bin" / exe
            if candidate.exists():
                found = candidate
                break
            candidate = build_out_dir / exe
            if candidate.exists():
                found = candidate
                break
        if found:
            result["binaries"][name] = str(found)

    # Record pinned SHA
    pin_path = build_dir / ".unsloth-pin.json"
    pin_data = {
        "sha": result["sha"],
        "cuda": cuda,
        "rpc": rpc,
    }
    pin_path.write_text(json.dumps(pin_data, indent=2))

    result["success"] = len(result["binaries"]) > 0
    return result


def verify_kimi_binary(bin_dir: str | Path) -> dict:
    """
    Verify that Kimi-K3 vision support is built into llama-server.

    Checks:
      - llama-server binary exists
      - rpc-server binary exists (for distributed inference)
      - llama-server --help mentions --mmproj (vision support)

    Args:
        bin_dir: Directory containing binaries

    Returns:
        Dict with keys:
          - "ok": bool
          - "reason": str (explanation if not ok)
          - "has_vision": bool (--mmproj present)
          - "has_rpc": bool (rpc-server exists)
    """
    bin_dir = Path(bin_dir)

    result = {
        "ok": False,
        "reason": "",
        "has_vision": False,
        "has_rpc": False,
    }

    # Check llama-server existence
    llama_server = None
    for name in ["llama-server", "llama-server.exe"]:
        candidate = bin_dir / name
        if candidate.exists():
            llama_server = candidate
            break

    if not llama_server:
        result["reason"] = (
            f"llama-server not found in {bin_dir}"
        )
        return result

    # Check rpc-server existence
    rpc_server = None
    for name in ["rpc-server", "rpc-server.exe"]:
        candidate = bin_dir / name
        if candidate.exists():
            rpc_server = candidate
            break

    result["has_rpc"] = rpc_server is not None

    # Probe llama-server for vision support
    try:
        help_output = subprocess.run(
            [str(llama_server), "--help"],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
            encoding="utf-8",
        )
        if "--mmproj" in help_output.stdout or \
           "--mmproj" in help_output.stderr:
            result["has_vision"] = True
    except Exception as e:
        result["reason"] = f"Failed to probe llama-server: {e}"
        return result

    if not result["has_vision"]:
        result["reason"] = (
            "llama-server does not mention --mmproj; "
            "vision support not built"
        )
        return result

    result["ok"] = True
    return result
