"""AitherRoom binary launcher — download and run the compiled Room service.

The binary is published as a GitHub Release asset on Aitherium/awdk
(tag: room-cli-v*). `adk up` downloads the platform-appropriate
binary on first use, verifies its SHA256 checksum, caches it in ~/.aither/bin/,
and supervises it as a native process.

For offline deployments, bundled binaries in adk/room_binaries/<platform>/
are used as a fallback when network is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
from pathlib import Path

_REPO = "Aitherium/awdk"
_CACHE_DIR = Path.home() / ".aither" / "bin"

_PLATFORM_BINARY = {
    ("Windows", "AMD64"): "aither-room-win64.exe",
    ("Windows", "x86_64"): "aither-room-win64.exe",
    ("Linux", "x86_64"): "aither-room-linux-x64",
    ("Linux", "aarch64"): "aither-room-linux-x64",  # fallback — no ARM build yet
    ("Darwin", "arm64"): "aither-room-macos-arm64",
    ("Darwin", "x86_64"): "aither-room-macos-x64",
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
    """Look for a bundled binary in adk/room_binaries/<platform-name>.

    Verifies the bundled binary against checksums.sha256 in the same directory.
    Returns the path if found and valid, None if not available or checksum fails.
    """
    platform_dir = _get_platform_dir_name()
    if not platform_dir:
        return None

    bundled_dir = Path(__file__).parent / "room_binaries" / platform_dir
    binary_name = _get_binary_name()
    binary_path = bundled_dir / binary_name

    if not binary_path.exists():
        return None

    # Verify checksum of bundled binary
    checksums_path = bundled_dir / "checksums.sha256"
    if checksums_path.exists():
        try:
            checksums_text = checksums_path.read_text()
            checksums_map = _parse_checksums(checksums_text)
            expected_sha256 = checksums_map.get(binary_name)
            if expected_sha256:
                if _verify_checksum(binary_path, expected_sha256):
                    return binary_path
                else:
                    print(
                        f"Warning: Bundled binary checksum verification failed for {binary_name}"
                    )
                    return None
            else:
                print(
                    f"Warning: No checksum entry found for {binary_name} in bundled checksums"
                )
                return None
        except Exception as e:
            print(f"Warning: Could not verify bundled binary checksum: {e}")
            return None
    else:
        print(
            f"Warning: No checksums.sha256 found in {bundled_dir} for bundled binary verification"
        )
        return None


def _download_binary(version: str = "latest") -> Path:
    """Download the AitherRoom binary from GitHub Releases.

    Downloads the binary for the current platform, verifies its SHA256 checksum
    against checksums.sha256, and caches it in ~/.aither/bin/.

    Raises SystemExit on failure.
    """
    import httpx

    name = _get_binary_name()
    dest = _CACHE_DIR / name
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Checksum verification is MANDATORY by default (secure by default)
    # Only allow opt-out via explicit AITHER_ROOM_REQUIRE_CHECKSUM=0
    require_checksum_env = os.environ.get("AITHER_ROOM_REQUIRE_CHECKSUM", "1").lower()
    require_checksum = require_checksum_env not in ("0", "false", "no")

    print(f"Fetching release info from {_REPO}...")
    try:
        if version != "latest":
            api_url = (
                f"https://api.github.com/repos/{_REPO}/releases/tags/room-cli-v{version}"
            )
            resp = httpx.get(api_url, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            release = resp.json()
        else:
            api_url = f"https://api.github.com/repos/{_REPO}/releases?per_page=20"
            resp = httpx.get(api_url, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            release = None
            for r in resp.json():
                tag = r.get("tag_name", "")
                if tag.startswith("room-cli-v") and not r.get("draft"):
                    release = r
                    break
            if not release:
                print("No room-cli release found")
                sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"Failed to fetch release: {e}")
        sys.exit(1)

    asset_url = None
    checksums_url = None
    for asset in release.get("assets", []):
        if asset["name"] == name:
            asset_url = asset["browser_download_url"]
        elif asset["name"] == "checksums.sha256":
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
            print("Error: checksums.sha256 not found in release")
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
                print("  adk room --install")
                sys.exit(1)
        else:
            print(
                f"Warning: No checksum entry found for {name} "
                f"(available: {', '.join(checksums_map.keys())})"
            )

    if platform.system() != "Windows":
        try:
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass

    # Store the checksums alongside the cached binary for future verification
    if checksums_map:
        checksums_file = _CACHE_DIR / "checksums.sha256"
        try:
            checksums_content = "\n".join(
                f"{sha256}  {filename}"
                for filename, sha256 in checksums_map.items()
            )
            checksums_file.write_text(checksums_content)
        except Exception as e:
            print(f"Warning: Could not save checksums for future verification: {e}")

    print(f"Installed: {dest}")
    return dest


def get_room_binary() -> Path | None:
    """Get the Room binary, downloading if necessary.

    Resolution order:
      1. Cached binary in ~/.aither/bin/ (with checksum re-verification)
      2. Download from GitHub (with checksum verification)
      3. Bundled offline binary in adk/room_binaries/<platform>/
         (with checksum verification)
      4. None (Room not available)

    Returns the path to the binary or None if no option succeeded.
    """
    binary = _get_binary_path()

    # Try cached binary first, but re-verify its checksum
    if binary.exists():
        checksums_path = _CACHE_DIR / "checksums.sha256"
        if checksums_path.exists():
            try:
                checksums_text = checksums_path.read_text()
                checksums_map = _parse_checksums(checksums_text)
                expected_sha256 = checksums_map.get(binary.name)
                if expected_sha256:
                    if _verify_checksum(binary, expected_sha256):
                        return binary
                    else:
                        print(
                            "Warning: Cached binary checksum verification failed, "
                            "re-downloading..."
                        )
                        try:
                            binary.unlink()
                        except OSError:
                            pass
                else:
                    print("Warning: No cached checksum found, re-downloading...")
                    try:
                        binary.unlink()
                    except OSError:
                        pass
            except Exception as e:
                print(f"Warning: Could not verify cached binary: {e}, re-downloading...")
                try:
                    binary.unlink()
                except OSError:
                    pass
        else:
            # No checksums file, treat cached binary as unverified
            print("Warning: No checksum file found for cached binary, re-downloading...")
            try:
                binary.unlink()
            except OSError:
                pass

    # Try to download
    print("AitherRoom binary not found — attempting download...")
    try:
        _download_binary()
        return binary
    except SystemExit:
        pass

    # Fall back to bundled binary
    bundled = _get_bundled_binary()
    if bundled:
        print(f"Using bundled binary: {bundled}")
        return bundled

    return None
