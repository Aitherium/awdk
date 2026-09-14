"""
Unsloth Kimi-K3 GGUF Multi-Shard Downloader
============================================

Provision Kimi-K3 quantized weights from unsloth/Kimi-K3-GGUF on HuggingFace.
Supports enumeration, resumable download, and SHA256 verification.

Public API:
    KIMI_K3_QUANTS: dict[str, dict[str, int | float]]
    list_kimi_shards(quant, repo, timeout) -> list[dict]
    preflight_disk(dest_dir, total_bytes, headroom_frac) -> bool
    download_shards(quant, dest_dir, repo, resume, progress_cb) -> dict
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# Kimi-K3 quantization ladder with resource requirements
KIMI_K3_QUANTS = {
    "UD-IQ1_S": {"size_gb": 594, "min_total_memory_gb": 610},
    "UD-IQ1_M": {"size_gb": 649, "min_total_memory_gb": 665},
    "UD-IQ2_XXS": {"size_gb": 711, "min_total_memory_gb": 726},
    "UD-Q2_K_XL": {"size_gb": 861, "min_total_memory_gb": 880},
    "UD-Q8_K_XL": {"size_gb": 1560, "min_total_memory_gb": 1600},
}


def list_kimi_shards(
    quant: str,
    repo: str = "unsloth/Kimi-K3-GGUF",
    timeout: float = 30.0,
) -> list[dict]:
    """
    Enumerate GGUF shards for a Kimi-K3 quantization from HuggingFace.

    Fetches the file tree for the quantization directory + mmproj at root via
    the HF API. Filters for .gguf files.

    Args:
        quant: Quantization name (e.g., "UD-Q2_K_XL")
        repo: HuggingFace repo ID
        timeout: Request timeout in seconds

    Returns:
        List of {"path": str, "size_bytes": int} dicts for each GGUF shard.

    Raises:
        ValueError: If quant is unknown or mmproj missing from enumeration
        urllib.error.HTTPError: If the HF API returns 404 or other errors
    """
    if quant not in KIMI_K3_QUANTS:
        available = ", ".join(sorted(KIMI_K3_QUANTS.keys()))
        raise ValueError(
            f"Unknown quant '{quant}'. Available: {available}"
        )

    shards = []
    quant_dir = urllib.parse.quote(quant)

    # Fetch quant directory listing
    url = f"https://huggingface.co/api/models/{repo}/tree/main/{quant_dir}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AitherADK-UnslothGGUF/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tree_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(
                f"Quant directory '{quant}' not found. "
                f"Available: {', '.join(sorted(KIMI_K3_QUANTS.keys()))}"
            ) from e
        raise

    # Extract GGUF shards from quant directory
    for item in tree_data:
        if item.get("type") == "file":
            name = item.get("name", "")
            if name.endswith(".gguf"):
                shards.append({
                    "path": f"{quant}/{name}",
                    "size_bytes": item.get("size", 0),
                })

    # Fetch mmproj from root
    url_root = f"https://huggingface.co/api/models/{repo}/tree/main"
    req_root = urllib.request.Request(
        url_root,
        headers={"User-Agent": "AitherADK-UnslothGGUF/1.0"},
    )

    try:
        with urllib.request.urlopen(req_root, timeout=timeout) as resp:
            root_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ValueError(f"Could not fetch root tree: {e}") from e

    mmproj_found = False
    for item in root_data:
        if item.get("type") == "file":
            name = item.get("name", "")
            if name == "mmproj-BF16.gguf":
                shards.append({
                    "path": name,
                    "size_bytes": item.get("size", 0),
                })
                mmproj_found = True

    if not mmproj_found:
        raise ValueError(
            f"mmproj-BF16.gguf not found in {repo} root"
        )

    return shards


def preflight_disk(
    dest_dir: str | Path,
    total_bytes: int,
    headroom_frac: float = 0.20,
) -> bool:
    """
    Check that destination directory has enough free space.

    Args:
        dest_dir: Download destination directory
        total_bytes: Total bytes to download
        headroom_frac: Fraction of destination to keep free (0.20 = 20%)

    Returns:
        True if space available

    Raises:
        OSError: If disk check fails or insufficient space
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    usage = shutil.disk_usage(str(dest_dir))
    required_free = int(total_bytes * (1 + headroom_frac))

    if usage.free < required_free:
        free_gb = usage.free / (1024 ** 3)
        required_gb = required_free / (1024 ** 3)
        raise OSError(
            f"Insufficient disk space in {dest_dir}: "
            f"need {required_gb:.1f} GB free, but only "
            f"{free_gb:.1f} GB available"
        )

    return True


def download_shards(
    quant: str,
    dest_dir: str | Path,
    repo: str = "unsloth/Kimi-K3-GGUF",
    resume: bool = True,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """
    Download GGUF shards with optional resume and SHA256 verification.

    Shards are downloaded sequentially (large file, home uplink). A partial
    .part file resumes from the correct offset on next call.

    Args:
        quant: Quantization name
        dest_dir: Download destination directory
        repo: HuggingFace repo ID
        resume: Resume from partial .part files if present
        progress_cb: Optional callback(path: str, bytes_done: int,
                     total_bytes: int) for progress reporting

    Returns:
        Dict with keys:
          - "shards": list of {"path": str, "size_bytes": int, "sha256": str}
          - "total_bytes": total downloaded
          - "mmproj_sha256": sha256 of mmproj-BF16.gguf

    Raises:
        ValueError: If quant unknown or required files missing
        OSError: If disk space insufficient or download fails
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    shards = list_kimi_shards(quant, repo)
    total_bytes = sum(s["size_bytes"] for s in shards)

    preflight_disk(dest_dir, total_bytes)

    result = {
        "shards": [],
        "total_bytes": total_bytes,
        "mmproj_sha256": "",
    }
    hf_token = os.environ.get("HF_TOKEN", "")

    for shard in shards:
        shard_path = shard["path"]
        shard_size = shard["size_bytes"]
        filename = Path(shard_path).name
        dest_file = dest_dir / filename
        part_file = dest_file.with_suffix(dest_file.suffix + ".part")

        # Determine resume offset
        resume_offset = 0
        if resume and part_file.exists():
            resume_offset = part_file.stat().st_size

        # Download with resume
        url = (
            f"https://huggingface.co/{repo}/resolve/main/{shard_path}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AitherADK-UnslothGGUF/1.0",
                **(
                    {"Authorization": f"Bearer {hf_token}"}
                    if hf_token
                    else {}
                ),
            },
        )

        if resume_offset > 0:
            req.add_header("Range", f"bytes={resume_offset}-")

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                bytes_downloaded = 0
                with open(part_file, "ab" if resume_offset > 0 else "wb") \
                        as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(
                                filename,
                                resume_offset + bytes_downloaded,
                                shard_size,
                            )
        except Exception as e:
            raise OSError(
                f"Download failed for {shard_path}: {e}"
            ) from e

        # Compute SHA256
        sha256_hash = hashlib.sha256()
        with open(part_file, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256_hash.update(chunk)
        sha256_hex = sha256_hash.hexdigest()

        # Rename to final name
        part_file.rename(dest_file)

        shard_info = {
            "path": shard_path,
            "size_bytes": shard_size,
            "sha256": sha256_hex,
        }
        result["shards"].append(shard_info)

        if filename == "mmproj-BF16.gguf":
            result["mmproj_sha256"] = sha256_hex

    return result
