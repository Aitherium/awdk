"""
AitherADK Ollama Backend Setup
==============================

Minimal pure-stdlib Ollama wrapper for local inference backends.
Complements llamacpp_setup.py with an alternative that doesn't require
binary installation (Ollama runs standalone).

Public API:
    is_installed() -> bool
    ensure_running() -> bool
    pull(model: str) -> bool
    smoke_test(model: str, port: int = 11434) -> bool
    register_config(model: str, port: int = 11434) -> None
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_OLLAMA_MODEL = "gemma4:e2b"
DEFAULT_OLLAMA_PORT = 11434
OLLAMA_HOST = "http://localhost:11434"

# ---------------------------------------------------------------------------
# Ollama detection and management
# ---------------------------------------------------------------------------


def _ollama_bin() -> Optional[str]:
    """Locate the ollama binary on PATH or in known install locations.

    PATH can be stale in the current process right after a bootstrap install,
    so we also probe the platform's default install directory.
    """
    found = shutil.which("ollama")
    if found:
        return found
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        candidates.append(Path("C:/Program Files/Ollama/ollama.exe"))
    elif sys.platform == "darwin":
        candidates += [Path("/opt/homebrew/bin/ollama"), Path("/usr/local/bin/ollama")]
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def is_installed() -> bool:
    """Check if the ollama binary is available."""
    return _ollama_bin() is not None


def ensure_installed() -> bool:
    """Bootstrap-install Ollama if it is missing.

    The AitherZero principle: automate downloading + bootstrapping every
    dependency rather than telling the user to go install it. Uses the
    platform's native package manager / official installer.
    """
    if is_installed():
        return True
    print("  Ollama not found — bootstrapping install (one-time setup)...")
    try:
        if sys.platform == "win32":
            if not shutil.which("winget"):
                print(
                    "  ERROR: winget unavailable. Install Ollama from "
                    "https://ollama.com/download then re-run.",
                    file=sys.stderr,
                )
                return False
            subprocess.run(
                ["winget", "install", "--id", "Ollama.Ollama", "-e", "--silent",
                 "--accept-source-agreements", "--accept-package-agreements"],
                timeout=1200,
            )
        elif sys.platform == "darwin":
            if not shutil.which("brew"):
                print(
                    "  ERROR: Homebrew not found. Install Ollama from "
                    "https://ollama.com/download then re-run.",
                    file=sys.stderr,
                )
                return False
            subprocess.run(["brew", "install", "ollama"], timeout=1200)
        else:  # linux
            subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, timeout=1200,
            )
    except subprocess.TimeoutExpired:
        print("  ERROR: Ollama install timed out", file=sys.stderr)
        return False
    except OSError as e:
        print(f"  ERROR: Ollama install failed: {e}", file=sys.stderr)
        return False
    if is_installed():
        print("  Ollama installed.")
        return True
    print(
        "  ERROR: Ollama installed but binary not found on PATH — open a new "
        "terminal and re-run, or install from https://ollama.com/download",
        file=sys.stderr,
    )
    return False


def ensure_running() -> bool:
    """
    Ensure Ollama is running on localhost:11434.

    If Ollama is installed but not serving, attempt to start it.
    Returns True if Ollama is (now) running, False otherwise.
    """
    # Check if already running
    if _is_service_running():
        return True

    if not ensure_installed():
        return False

    # Try to start Ollama
    print("  Starting Ollama...")
    try:
        if sys.platform == "win32":
            # Windows: start ollama serve in background (detached)
            subprocess.Popen(
                [_ollama_bin() or "ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            # POSIX: nohup the process
            subprocess.Popen(
                [_ollama_bin() or "ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        # Wait up to 10 seconds for it to become available
        import time

        for _ in range(20):  # 10 seconds with 0.5s polling
            time.sleep(0.5)
            if _is_service_running():
                print("  Ollama started successfully")
                return True
        print("  Ollama started but not responding yet", file=sys.stderr)
        return True  # Give benefit of doubt; client may connect later
    except Exception as e:
        print(f"  ERROR: could not start Ollama: {e}", file=sys.stderr)
        return False


def _is_service_running() -> bool:
    """Check if Ollama API is responding."""
    try:
        url = f"{OLLAMA_HOST}/api/tags"
        req = urllib.request.Request(url, headers={"User-Agent": "AitherADK/1.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def pull(model: str) -> bool:
    """
    Download/pull a model from Ollama's registry.

    Uses subprocess to run `ollama pull <model>` and streams output.
    Returns True if successful.
    """
    if not ensure_installed():
        return False

    print(f"  Pulling model: {model}")
    try:
        result = subprocess.run(
            [_ollama_bin() or "ollama", "pull", model],
            capture_output=False,
            text=True,
            timeout=3600,  # 1 hour for large models
        )
        if result.returncode == 0:
            print(f"  Model pulled: {model}")
            return True
        print(f"  ERROR: ollama pull failed with code {result.returncode}",
              file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("  ERROR: model pull timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  ERROR: could not pull model: {e}", file=sys.stderr)
        return False


def smoke_test(model: str, port: int = DEFAULT_OLLAMA_PORT) -> bool:
    """
    Run a quick chat completion against the model.

    Uses OpenAI-compatible /v1/chat/completions endpoint.
    Returns True if the model responds.
    """
    url = f"http://localhost:{port}/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        "max_tokens": 64,
    }).encode()

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "AitherADK/1.0"},
        )
        # Generous timeout: the first call cold-loads the model into VRAM.
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
            msg = data.get("choices", [{}])[0].get("message", {}) or {}
            # Reasoning models may emit `reasoning`/`reasoning_content` with an
            # empty `content` at low token budgets — any output means it's alive.
            text = (
                msg.get("content")
                or msg.get("reasoning_content")
                or msg.get("reasoning")
                or ""
            ).strip()
            if text:
                print(f"  Smoke test: {text[:60]!r}")
                return True
            return False
    except Exception as e:
        print(f"  Smoke test failed: {e}", file=sys.stderr)
        return False


def register_config(model: str, port: int = DEFAULT_OLLAMA_PORT) -> None:
    """Save Ollama endpoint to ADK config."""
    from adk.config import save_saved_config

    endpoint = f"http://localhost:{port}/v1"
    save_saved_config({
        "setup_backend": "ollama",
        "inference_url": endpoint,
        "ollama_model": model,
        "ollama_port": port,
    })
    print(f"  Config saved: Ollama endpoint at {endpoint}")
