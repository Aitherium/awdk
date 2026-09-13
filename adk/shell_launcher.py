"""AitherShell binary launcher — download and run the compiled CLI.

The binary is published as a GitHub Release asset on Aitherium/awdk
(tag: shell-cli-v*). `adk shell` downloads the platform-appropriate
binary on first use, verifies its SHA256 checksum, caches it in ~/.aither/bin/,
and launches it.

For offline deployments, bundled binaries in adk/shell_binaries/<platform>/
are used as a fallback when network is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import subprocess
import sys
from pathlib import Path

_REPO = "Aitherium/awdk"
_CACHE_DIR = Path.home() / ".aither" / "bin"

_PLATFORM_BINARY = {
    ("Windows", "AMD64"): "aither-shell-win64.exe",
    ("Windows", "x86_64"): "aither-shell-win64.exe",
    ("Linux", "x86_64"): "aither-shell-linux-x64",
    ("Linux", "aarch64"): "aither-shell-linux-x64",  # fallback — no ARM build yet
    ("Darwin", "arm64"): "aither-shell-macos-arm64",
    ("Darwin", "x86_64"): "aither-shell-macos-arm64",
}

_PLATFORM_DIR_NAMES = {
    ("Windows", "AMD64"): "win-x64",
    ("Windows", "x86_64"): "win-x64",
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-x64",
    ("Darwin", "arm64"): "mac-arm64",
    ("Darwin", "x86_64"): "mac-x64",
}


def _get_binary_name() -> str:
    key = (platform.system(), platform.machine())
    name = _PLATFORM_BINARY.get(key)
    if not name:
        print(f"Unsupported platform: {key[0]} {key[1]}")
        sys.exit(1)
    return name


def _get_binary_path() -> Path:
    name = _get_binary_name()
    return _CACHE_DIR / name


def _get_platform_dir_name() -> str:
    """Get the bundled binary directory name for this platform."""
    key = (platform.system(), platform.machine())
    name = _PLATFORM_DIR_NAMES.get(key)
    if not name:
        return ""
    return name


def _verify_checksum(file_path: Path, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of a file."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest().lower() == expected_sha256.lower()
    except (OSError, IOError):
        return False


def _parse_checksums(checksums_text: str) -> dict[str, str]:
    """Parse SHA256 checksums from checksums.sha256 file format.

    Expected format (one checksum per line):
        <sha256_hex>  <filename>
    """
    checksums = {}
    for line in checksums_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            sha256_hex = parts[0]
            filename = parts[1]
            checksums[filename] = sha256_hex
    return checksums


def _get_bundled_binary() -> Path | None:
    """Look for a bundled binary in adk/shell_binaries/<platform-name>.

    Returns the path if found, None if not available or offline fallback
    is not configured.
    """
    platform_dir = _get_platform_dir_name()
    if not platform_dir:
        return None

    bundled_dir = Path(__file__).parent / "shell_binaries" / platform_dir
    binary_name = _get_binary_name()
    binary_path = bundled_dir / binary_name

    if not binary_path.exists():
        return None

    # Verify the bundled binary against checksums.sha256 in the same directory —
    # defends against a tampered checkout / shared-machine repo write.
    checksums_path = bundled_dir / "checksums.sha256"
    if checksums_path.exists():
        try:
            expected = _parse_checksums(checksums_path.read_text()).get(binary_name)
        except OSError:
            expected = None
        if expected and _verify_checksum(binary_path, expected):
            return binary_path
        print(
            f"Warning: bundled shell binary in {bundled_dir} failed checksum "
            "verification — ignoring it."
        )
        return None

    print(
        f"Warning: no checksums.sha256 in {bundled_dir} to verify the bundled "
        "shell binary — ignoring it (set AITHER_SHELL_REQUIRE_CHECKSUM=0 to override)."
    )
    return None


def _download_binary(version: str = "latest") -> Path:
    """Download the AitherShell binary from GitHub Releases.

    Downloads the binary for the current platform, verifies its SHA256 checksum
    against checksums.sha256, and caches it in ~/.aither/bin/.

    Raises SystemExit on failure.
    """
    import httpx

    name = _get_binary_name()
    dest = _CACHE_DIR / name
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Checksum verification is MANDATORY by default (secure-by-default); only an
    # explicit AITHER_SHELL_REQUIRE_CHECKSUM=0/false/no opts out.
    require_checksum_env = os.environ.get("AITHER_SHELL_REQUIRE_CHECKSUM", "1").lower()
    require_checksum = require_checksum_env not in ("0", "false", "no")

    print(f"Fetching release info from {_REPO}...")
    # Shell releases have used two tag prefixes over time: "shell-v*" (current,
    # from release-aithershell) and "shell-cli-v*" (release-shell-cli). Accept both.
    try:
        if version != "latest":
            release = None
            for tag_prefix in ("shell-v", "shell-cli-v"):
                api_url = (
                    f"https://api.github.com/repos/{_REPO}/releases/tags/"
                    f"{tag_prefix}{version}"
                )
                resp = httpx.get(api_url, follow_redirects=True, timeout=30)
                if resp.status_code == 200:
                    release = resp.json()
                    break
            if not release:
                print(f"No shell release found for version {version}")
                sys.exit(1)
        else:
            api_url = f"https://api.github.com/repos/{_REPO}/releases?per_page=20"
            resp = httpx.get(api_url, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            release = None
            for r in resp.json():
                tag = r.get("tag_name", "")
                if tag.startswith(("shell-v", "shell-cli-v")) and not r.get("draft"):
                    release = r
                    break
            if not release:
                print("No shell release found")
                sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"Failed to fetch release: {e}")
        sys.exit(1)

    asset_url = None
    checksums_url = None
    # Checksum manifest name differs by release channel: checksums.sha256
    # (shell-cli releases) vs SHA256SUMS.txt (shell-v releases). Same format.
    for asset in release.get("assets", []):
        if asset["name"] == name:
            asset_url = asset["browser_download_url"]
        elif asset["name"] in ("checksums.sha256", "SHA256SUMS.txt"):
            checksums_url = asset["browser_download_url"]

    if not asset_url:
        print(
            f"Binary '{name}' not found in release {release.get('tag_name', '?')}"
        )
        print(f"Available assets: {[a['name'] for a in release.get('assets', [])]}")
        sys.exit(1)

    print(f"Downloading {name} ({release.get('tag_name', '')})...")
    try:
        with httpx.stream("GET", asset_url, follow_redirects=True, timeout=120) as dl:
            dl.raise_for_status()
            total = int(dl.headers.get("content-length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                for chunk in dl.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        print(
                            f"\r  {pct}% ({downloaded // 1024 // 1024}MB)",
                            end="",
                            flush=True,
                        )
            print()
    except Exception as e:
        print(f"Download failed: {e}")
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        sys.exit(1)

    checksums_map = {}
    if checksums_url:
        try:
            print("Verifying checksum...")
            checksums_resp = httpx.get(
                checksums_url, follow_redirects=True, timeout=30
            )
            checksums_resp.raise_for_status()
            checksums_map = _parse_checksums(checksums_resp.text)
        except Exception as e:
            if require_checksum:
                print(f"Failed to fetch checksums: {e}")
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                sys.exit(1)
            print(
                "Warning: Could not verify checksum (checksums.sha256 not available)"
            )
    else:
        if require_checksum:
            print("Error: no checksum manifest (checksums.sha256 / SHA256SUMS.txt) in release")
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            sys.exit(1)
        print("Warning: checksums.sha256 not found in release (skipping verification)")

    if checksums_map:
        expected_sha256 = checksums_map.get(name)
        if expected_sha256:
            if not _verify_checksum(dest, expected_sha256):
                print(f"ERROR: Checksum verification failed for {name}")
                print(f"  Expected: {expected_sha256}")
                actual = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        actual.update(chunk)
                print(f"  Got:      {actual.hexdigest()}")
                try:
                    dest.unlink()
                except OSError:
                    pass
                print("\nThe download may be corrupted. Try again with:")
                print("  adk shell --install")
                sys.exit(1)
        else:
            print(
                f"Warning: No checksum entry found for {name} "
                f"(available: {', '.join(checksums_map.keys())})"
            )

    # Persist the verified checksums alongside the cached binary so it can be
    # re-verified on every later load (shared-machine tamper defense).
    if checksums_map:
        try:
            (_CACHE_DIR / "checksums.sha256").write_text(
                "\n".join(f"{h}  {n}" for n, h in checksums_map.items()) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    if platform.system() != "Windows":
        try:
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass

    print(f"Installed: {dest}")
    return dest


def _preflight_check() -> tuple[bool, str]:
    """Verify at least one LLM backend is reachable before launching shell."""
    import urllib.request
    import urllib.error

    def _models_ok(base: str) -> bool:
        base = base.rstrip("/")
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, ConnectionError, OSError):
            return False

    # Check custom LLM base URL (e.g., AITHER_LLM_BASE_URL for DGX Spark :8124).
    # Accepts the value with or without a trailing /v1 — Config.llm_base_url
    # wants it WITH, and the same env var feeds both (2026-09-12).
    custom_url = os.environ.get("AITHER_LLM_BASE_URL", "").strip()
    if custom_url and _models_ok(custom_url):
        return True, f"custom:{custom_url}"

    # The backend the user configured (adk backend set / install-bonsai.sh
    # --with-adk). Until 2026-09-12 this preflight never read it, so `adk up`
    # refused with "no backend" on a box whose Bonsai answered on :8080.
    try:
        from adk.config import load_saved_config as _lsc
        _saved = _lsc()
        _u = (_saved.get("inference_url") or "").strip()
        if _saved.get("default_backend") and _u and _models_ok(_u):
            return True, f"configured:{_saved.get('default_backend')} {_u}"
    except Exception:  # noqa: BLE001 — preflight is best-effort
        pass

    # Self-hosted loopback servers (same ladder as LLMRouter / adk status).
    try:
        from adk.local_inference import SELFHOST_PORTS
    except ImportError:
        SELFHOST_PORTS = (8080, 8090, 8092, 8889, 8081, 8000)
    for port in SELFHOST_PORTS:
        if _models_ok(f"http://127.0.0.1:{port}"):
            return True, f"local on :{port}"

    # Check vLLM ports
    for port in (8200, 8201, 8000):
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True, f"vLLM on :{port}"
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

    # Check Ollama
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return True, "Ollama"
    except (urllib.error.URLError, ConnectionError, OSError):
        pass

    # Check Genesis
    try:
        req = urllib.request.Request("http://localhost:8001/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return True, "Genesis"
    except (urllib.error.URLError, ConnectionError, OSError):
        pass

    # Check for API keys in config
    try:
        from adk.config import load_saved_config
    except ImportError:
        load_saved_config = None
    if load_saved_config is not None:
        try:
            saved = load_saved_config()
            if saved.get("api_key") or saved.get("reasoning_api_key"):
                return True, "cloud API key"
        except (OSError, ValueError):
            pass

    if os.environ.get("AITHER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        return True, "cloud API key"

    return False, ""


def _cached_binary_verified(binary: Path) -> bool:
    """True if the cached binary matches the checksum stored beside it.

    Returns False (forcing a re-download) when no stored checksum exists or the
    hash does not match — unless checksum enforcement is explicitly disabled via
    AITHER_SHELL_REQUIRE_CHECKSUM=0/false/no.
    """
    require_env = os.environ.get("AITHER_SHELL_REQUIRE_CHECKSUM", "1").lower()
    if require_env in ("0", "false", "no"):
        return True
    checksums_path = _CACHE_DIR / "checksums.sha256"
    if not checksums_path.exists():
        print("Warning: no stored checksum for the cached shell binary — re-downloading...")
        return False
    try:
        expected = _parse_checksums(checksums_path.read_text()).get(binary.name)
    except OSError:
        expected = None
    if expected and _verify_checksum(binary, expected):
        return True
    print("Warning: cached shell binary failed checksum verification — re-downloading...")
    return False


def cmd_shell(args) -> int:
    """Launch AitherShell interactive terminal.

    Fallback priority:
      1. Cached binary in ~/.aither/bin/
      2. Download from GitHub (with checksum verification)
      3. Bundled offline binary in adk/shell_binaries/<platform>/
      4. Fall back to Python REPL
    """
    binary = _get_binary_path()

    if getattr(args, "install", False):
        _download_binary()
        return 0

    # Re-verify the cached binary against the stored checksum on every load
    # (shared-machine tamper defense). If it can't be verified, drop it so the
    # download path re-fetches a clean, checksum-verified copy.
    if binary.exists() and not _cached_binary_verified(binary):
        try:
            binary.unlink()
        except OSError:
            pass

    if not binary.exists():
        print("AitherShell not found — attempting download...")
        try:
            _download_binary()
        except SystemExit:
            bundled = _get_bundled_binary()
            if bundled:
                print(f"Using bundled binary: {bundled}")
                binary = bundled
            else:
                print("\nOffline fallback unavailable.")
                print("\nTo install manually:")
                binary_name = _get_binary_name()
                print(
                    f"  1. Download {binary_name} from:"
                    f"     https://github.com/{_REPO}/releases/latest"
                )
                print(f"  2. Place in: {_CACHE_DIR}/{binary_name}")
                print("  3. Make executable (on Unix): chmod +x <file>")
                print("\nOr run: adk shell --install")
                return 1

    if not binary.exists():
        bundled = _get_bundled_binary()
        if bundled:
            print(f"Using bundled binary: {bundled}")
            binary = bundled
        else:
            print("AitherShell binary not found.")
            print("\nTo install:")
            print("  adk shell --install")
            print(f"\nOr manually download from: https://github.com/{_REPO}/releases")
            return 1

    # Pre-flight: verify a backend is reachable (SHELL-3 fix)
    ok, backend = _preflight_check()
    if not ok:
        print("Warning: No LLM backend detected.")
        print("  Run 'adk setup' to configure local inference, or")
        print("  Run 'adk login' to connect to Aitherium cloud.")
        print()

    # Build command
    cmd = [str(binary)]

    # Pass full config via env vars (SHELL-4 fix)
    try:
        from adk.config import load_saved_config
    except ImportError:
        load_saved_config = None
    if load_saved_config is not None:
        try:
            saved = load_saved_config()
            env_map = {
                "api_key": "AITHER_API_KEY",
                "tenant_id": "AITHER_TENANT_ID",
                "inference_url": "AITHER_INFERENCE_URL",
                "reasoning_url": "AITHER_REASONING_URL",
                "reasoning_backend": "AITHER_REASONING_BACKEND",
            }
            for config_key, env_key in env_map.items():
                val = saved.get(config_key, "")
                if val and env_key not in os.environ:
                    os.environ[env_key] = val
        except (OSError, ValueError):
            pass

    # Set backend URL — AITHER_API_URL is canonical, AITHER_GENESIS_URL is legacy
    api_url = getattr(args, "api_url", None) or getattr(args, "genesis", None)
    if api_url:
        os.environ["AITHER_API_URL"] = api_url
    elif "AITHER_API_URL" not in os.environ and "AITHER_GENESIS_URL" not in os.environ:
        # Auto-detect: if ADK server is running locally, point shell at it
        try:
            from adk.config import Config
            cfg = Config.from_env()
            if cfg.server_port:
                os.environ["AITHER_API_URL"] = f"http://127.0.0.1:{cfg.server_port}"
        except ImportError:
            pass  # adk.config not available — shell uses its own defaults

    shell_args = getattr(args, "shell_args", [])
    if shell_args:
        cmd.extend(shell_args)

    try:
        result = subprocess.run(cmd)
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError:
        print(f"Binary not found: {binary}")
        print("Falling back to Python REPL...")
        return _run_python_repl(args)


def _run_python_repl(args) -> int:
    """Fall back to the built-in Python REPL (merged from aithershell)."""
    try:
        from adk.shell.cli import entry
        return entry(standalone_mode=False) or 0
    except ImportError:
        print("Python shell not available. Install with: pip install awdk[shell]")
        return 1
    except KeyboardInterrupt:
        return 0
