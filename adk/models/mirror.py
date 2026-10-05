"""
Mirror client for downloading quantized model weights from https://weights.aitherium.com.

The mirror serves GGUF format model files and supports resumable downloads via HTTP Range
headers. Downloads are rate-limited by default to avoid saturating home connections.
"""

import http.client
import urllib.request
import urllib.error
import hashlib
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, BinaryIO


# Conservative rate limit: don't saturate a home connection.
# Typical home upload is ~10 Mbps = 1.25 MB/s.
# Set default to 1/4 of that (256 KB/s) to leave headroom for other traffic.
DEFAULT_RATE_LIMIT_BYTES_PER_SEC = 256 * 1024


@dataclass
class WeightCatalogEntry:
    """A model weight file in the mirror catalog."""
    filename: str
    human_name: str
    family: str  # e.g. "Bonsai", "DeepSeek", "Qwen"
    quantization: str  # e.g. "Q4_K_M", "Q1_0", "IQ2_M"
    approx_size_bytes: int
    min_vram_gb: int  # minimum GPU VRAM needed
    sha256: Optional[str] = None  # optional SHA256 hash for verification


# Catalog of known weight files.
#
# Sizes are estimates based on model parameters and quantization schemes.
# All sizes marked "not measured" should be verified against actual downloads.
# See comments for derivation of each estimate.
CATALOG: Dict[str, WeightCatalogEntry] = {
    "Bonsai-27B-Q1_0.gguf": WeightCatalogEntry(
        filename="Bonsai-27B-Q1_0.gguf",
        human_name="Bonsai 27B (1-bit quantized)",
        family="Bonsai",
        quantization="Q1_0",
        # Measured 2026-08-16 via Content-Range header: 3,803,452,480 bytes (3.54 GB).
        approx_size_bytes=3_803_452_480,
        min_vram_gb=8,
    ),
    # The DEFAULT orchestrator (see DEFAULT_ORCHESTRATOR_FILE below for why Q8_0).
    "aither-orchestrator-v18-Q8_0.gguf": WeightCatalogEntry(
        filename="aither-orchestrator-v18-Q8_0.gguf",
        human_name="AitherOrchestrator v18 (8-bit quantized, default)",
        family="AitherOrchestrator",
        quantization="Q8_0",
        # Measured 2026-10-04 via HEAD on the mirror: 8,709,518,176 bytes (8.11 GiB),
        # equal to awrtifact.yaml's orchestrator-v18-q8 total.
        approx_size_bytes=8_709_518_176,
        min_vram_gb=12,
        # sha256 of the full file, measured 2026-10-05 (resumed download, exact size).
        sha256="c4197b0e4e514d66cedc5671939c0e938740fe17982ae1082d56a484bba188ef",
    ),
    # The v18 build at Q4_K_M: what the llama.cpp installer serves on a device that
    # cannot fit Q8_0 (same lineage as the default, unlike the pre-v18 Q4 below).
    # No sha256: the artifact manifest publishes sizes only, never invent a pin.
    "aither-orchestrator-v18-Q4_K_M.gguf": WeightCatalogEntry(
        filename="aither-orchestrator-v18-Q4_K_M.gguf",
        human_name="AitherOrchestrator v18 (4-bit quantized, small devices)",
        family="AitherOrchestrator",
        quantization="Q4_K_M",
        # Measured 2026-10-04 via Range GET on the mirror: 5,027,783,520 bytes,
        # equal to awrtifact.yaml's orchestrator-v18-q4 total.
        approx_size_bytes=5_027_783_520,
        min_vram_gb=8,
        # sha256 of the full file, measured 2026-10-05 (resumed download, exact size).
        sha256="8a99577cd97222294d88e5957c4151adc114f59937b84866a6f4e1e2ac0a2d52",
    ),
    # Small-device option only. NOT the v18 build (v18-Q4_K_M is 5,027,783,520 B).
    "aither-orchestrator-Q4_K_M.gguf": WeightCatalogEntry(
        filename="aither-orchestrator-Q4_K_M.gguf",
        human_name="AitherOrchestrator (4-bit quantized, small devices)",
        family="AitherOrchestrator",
        quantization="Q4_K_M",
        # Measured 2026-08-16 via Content-Range header: 5,027,783,808 bytes (4.68 GB).
        approx_size_bytes=5_027_783_808,
        # 4.7 GiB of weights plus KV cache fits an 8 GB card; at 12 (the old value,
        # shared with Q8_0) it could never fit a box the Q8_0 default does not.
        min_vram_gb=8,
    ),
    "gemma4-12b-Q4_K_M.gguf": WeightCatalogEntry(
        filename="gemma4-12b-Q4_K_M.gguf",
        human_name="Gemma 4 12B (4-bit quantized)",
        family="Gemma",
        quantization="Q4_K_M",
        # Measured 2026-08-16: Content-Range from mirror = 7662533088 bytes (~7.13 GB).
        # This is likely a larger variant or different quantization than initially estimated.
        approx_size_bytes=7_662_533_088,
        min_vram_gb=8,
    ),
    "qwen36-27b-Q4_K_M.gguf": WeightCatalogEntry(
        filename="qwen36-27b-Q4_K_M.gguf",
        human_name="Qwen3.6 27B (4-bit quantized)",
        family="Qwen",
        quantization="Q4_K_M",
        # Measured 2026-08-16 via Content-Range header: 16,817,244,384 bytes (15.66 GB).
        approx_size_bytes=16_817_244_384,
        min_vram_gb=12,
    ),
    # DeepSeek V4 Flash is split into 3 parts (chunked by the mirror for files >2GB).
    # The worker (index.js) stitches ranges per-part behind the original filename.
    "DeepSeek-V4-Flash-0731-UD-IQ2_M-00001-of-00003.gguf": WeightCatalogEntry(
        filename="DeepSeek-V4-Flash-0731-UD-IQ2_M-00001-of-00003.gguf",
        human_name="DeepSeek V4 Flash (part 1 of 3, IQ2 quantized)",
        family="DeepSeek",
        quantization="IQ2_M",
        # Measured 2026-08-16 via Content-Range header: 5,257,664 bytes (5.26 MB).
        # This appears to be a header/preamble chunk.
        approx_size_bytes=5_257_664,
        min_vram_gb=24,
    ),
    "DeepSeek-V4-Flash-0731-UD-IQ2_M-00002-of-00003.gguf": WeightCatalogEntry(
        filename="DeepSeek-V4-Flash-0731-UD-IQ2_M-00002-of-00003.gguf",
        human_name="DeepSeek V4 Flash (part 2 of 3, IQ2 quantized)",
        family="DeepSeek",
        quantization="IQ2_M",
        # Measured 2026-08-16 via Content-Range header: 49,956,780,160 bytes (46.53 GB).
        approx_size_bytes=49_956_780_160,
        min_vram_gb=24,
    ),
    "DeepSeek-V4-Flash-0731-UD-IQ2_M-00003-of-00003.gguf": WeightCatalogEntry(
        filename="DeepSeek-V4-Flash-0731-UD-IQ2_M-00003-of-00003.gguf",
        human_name="DeepSeek V4 Flash (part 3 of 3, IQ2 quantized)",
        family="DeepSeek",
        quantization="IQ2_M",
        # Measured 2026-08-16 via Content-Range header: 40,964,890,464 bytes (38.15 GB).
        approx_size_bytes=40_964_890_464,
        min_vram_gb=24,
    ),
}

MIRROR_BASE_URL = "https://weights.aitherium.com"

#: The orchestrator the adk installs and serves by default: the v18 build at Q8_0.
#: Owner decision 2026-10-05, from experiment run_e18eab0164e74e0b (v18 orchestrator on
#: llama.cpp vs the f16 reference): Q8_0 PASS, top-1 agreement 0.9976, exact 0.9375,
#: 54 tok/s; Q4_K_M FAIL, top-1 0.9598, exact 0.8125, 83 tok/s; f16 32.5 tok/s.
#: Q8_0 is ~8.7 GB on disk vs ~5.0 GB for Q4_K_M.
DEFAULT_ORCHESTRATOR_FILE = "aither-orchestrator-v18-Q8_0.gguf"
#: Still selectable by name for devices that cannot fit Q8_0; never the default.
SMALL_DEVICE_ORCHESTRATOR_FILE = "aither-orchestrator-Q4_K_M.gguf"
#: The v18 Q4_K_M: the llama.cpp installer's pick when Q8_0 does not fit the device.
V18_SMALL_DEVICE_ORCHESTRATOR_FILE = "aither-orchestrator-v18-Q4_K_M.gguf"
#: quant -> v18 file. The installer maps its fit-based quant choice onto this table.
V18_ORCHESTRATOR_FILES: Dict[str, str] = {
    "Q8_0": DEFAULT_ORCHESTRATOR_FILE,
    "Q4_K_M": V18_SMALL_DEVICE_ORCHESTRATOR_FILE,
}


class MirrorError(Exception):
    """Base exception for mirror operations."""
    pass


class MirrorFileNotFoundError(MirrorError):
    """File not found in the mirror catalog or on the mirror."""
    pass


class MirrorNetworkError(MirrorError):
    """Network error communicating with the mirror."""
    pass


class MirrorVerificationError(MirrorError):
    """Downloaded file failed verification (size or hash mismatch)."""
    pass


class RateLimiter:
    """Paces reads to respect a byte-per-second rate limit."""

    def __init__(self, bytes_per_sec: int):
        """
        Args:
            bytes_per_sec: Maximum bytes per second to allow.
        """
        self.bytes_per_sec = bytes_per_sec
        self.start_time = time.time()
        self.bytes_read = 0

    def pace(self, bytes_read: int) -> None:
        """
        Sleep if needed to maintain the rate limit.

        Args:
            bytes_read: Number of bytes just read.
        """
        self.bytes_read += bytes_read
        elapsed = time.time() - self.start_time

        # Calculate target time to read bytes_read at the desired rate
        target_time = self.bytes_read / self.bytes_per_sec

        if target_time > elapsed:
            # We're ahead of schedule; sleep
            time.sleep(target_time - elapsed)


class MirrorClient:
    """Client for downloading weights from the mirror."""

    def __init__(
        self,
        rate_limit_bytes_per_sec: int = DEFAULT_RATE_LIMIT_BYTES_PER_SEC,
        chunk_size: int = 64 * 1024,
    ):
        """
        Initialize the mirror client.

        Args:
            rate_limit_bytes_per_sec: Max bytes/sec to download. Default is 256 KB/s.
                Set to 0 for unlimited. Rate limiting is mandatory by default.
            chunk_size: Read chunk size for paced downloads (default 64 KB).
        """
        self.rate_limit_bytes_per_sec = rate_limit_bytes_per_sec
        self.chunk_size = chunk_size
        self.limiter = (
            RateLimiter(rate_limit_bytes_per_sec)
            if rate_limit_bytes_per_sec > 0
            else None
        )

    def download(
        self, filename: str, dest_path: str, verify: bool = True
    ) -> str:
        """
        Download a weight file from the mirror, with resumable support.

        Partial downloads are stored with a .partial suffix and resumed from their
        current length if interrupted.

        Args:
            filename: The model filename (e.g., "gemma4-12b-Q4_K_M.gguf")
            dest_path: Where to save the file
            verify: Whether to verify size (and hash if available)

        Returns:
            The final destination path (without .partial suffix)

        Raises:
            MirrorFileNotFoundError: File not found in mirror catalog or mirror returns 404
            MirrorNetworkError: Network or HTTP error
            MirrorVerificationError: Size or hash verification failed
        """
        # Check catalog
        if filename not in CATALOG:
            raise MirrorFileNotFoundError(
                f"File not in mirror catalog: {filename}"
            )

        entry = CATALOG[filename]
        url = f"{MIRROR_BASE_URL}/{filename}"
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        partial_path = dest_path.with_suffix(dest_path.suffix + ".partial")

        # Check for existing partial file
        start_byte = 0
        if partial_path.exists():
            start_byte = partial_path.stat().st_size

        # Resume in-process until the server's own byte total is on disk. A stream that
        # drops mid-file used to end the install with a short .partial (and, inside the
        # 1% size tolerance, a truncated GGUF passed _verify). Give up only after
        # MAX_STALLS attempts in a row that add no bytes.
        total: Optional[int] = None
        stalls = 0
        while True:
            try:
                total = self._download_with_resume(url, entry, partial_path, start_byte) or total
            except MirrorError:
                raise
            except Exception as e:
                raise MirrorNetworkError(f"Download failed: {e}") from e
            have = partial_path.stat().st_size if partial_path.exists() else 0
            if total is None or have >= total:
                break
            stalls = 0 if have > start_byte else stalls + 1
            if stalls >= self.MAX_STALLS:
                raise MirrorNetworkError(
                    f"Download stalled at {have:,} of {total:,} bytes after "
                    f"{self.MAX_STALLS} attempts with no progress (resume by re-running)"
                )
            start_byte = have
            time.sleep(min(2 + stalls * 5, 30))
        if total is not None and partial_path.stat().st_size != total:
            raise MirrorVerificationError(
                f"Size mismatch: server total {total:,} bytes, "
                f"got {partial_path.stat().st_size:,}"
            )

        # Verify
        if verify:
            self._verify(partial_path, entry)

        # Move to final location
        if dest_path.exists():
            dest_path.unlink()
        partial_path.rename(dest_path)
        return str(dest_path)

    MAX_STALLS = 8

    @staticmethod
    def _total_bytes(resp, start_byte: int) -> Optional[int]:
        """The file's full size from Content-Range, else start + Content-Length."""
        m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
        if m:
            return int(m.group(1))
        length = resp.headers.get("Content-Length")
        return start_byte + int(length) if length and length.isdigit() else None

    def _download_with_resume(
        self,
        url: str,
        entry: WeightCatalogEntry,
        dest_path: Path,
        start_byte: int,
    ) -> Optional[int]:
        """Download with HTTP Range support for resumability.

        Returns the server-reported total size (None if it sent none). A connection
        that drops mid-body leaves the bytes received so far for the caller to resume.
        """
        headers = {
            # User-Agent needed to bypass Cloudflare bot challenge (CF returns 403 to Python-urllib)
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        if start_byte > 0:
            headers["Range"] = f"bytes={start_byte}-"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                # Stream download with rate limiting
                total = self._total_bytes(resp, start_byte)
                if start_byte > 0 and resp.status != 206:
                    start_byte, total = 0, self._total_bytes(resp, 0)  # Range ignored
                mode = "ab" if start_byte > 0 else "wb"
                with open(dest_path, mode) as f:
                    try:
                        self._stream_download(resp, f)
                    except (http.client.IncompleteRead, ConnectionError, TimeoutError):
                        pass  # keep what arrived; download() resumes from here
                return total

        except urllib.error.HTTPError as e:
            if e.code == 404:
                body = e.read().decode("utf-8", errors="replace")
                if "not a known weight file" in body:
                    raise MirrorFileNotFoundError(f"Mirror: {body}")
                raise MirrorFileNotFoundError(f"Not found: {url}")
            raise MirrorNetworkError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise MirrorNetworkError(f"Network error: {e.reason}") from e

    def _stream_download(self, resp, dest_file: BinaryIO) -> None:
        """Stream response to file with optional rate limiting."""
        while True:
            chunk = resp.read(self.chunk_size)
            if not chunk:
                break

            dest_file.write(chunk)
            dest_file.flush()

            if self.limiter:
                self.limiter.pace(len(chunk))

    def _verify(self, path: Path, entry: WeightCatalogEntry) -> None:
        """
        Verify downloaded file against catalog entry.

        Checks both size and optional SHA256 hash.

        Args:
            path: Path to the downloaded file
            entry: The catalog entry for this file

        Raises:
            MirrorVerificationError: Verification failed
        """
        actual_size = path.stat().st_size
        expected_size = entry.approx_size_bytes

        # Size check (within tolerance for approximate sizes).
        # For files >1GB, allow 1% tolerance. For smaller files, allow 100 KB.
        tolerance = max(1024 * 100, expected_size // 100)
        if abs(actual_size - expected_size) > tolerance:
            raise MirrorVerificationError(
                f"Size mismatch: expected ~{expected_size:,} bytes, "
                f"got {actual_size:,} bytes (diff: {actual_size - expected_size:+,})"
            )

        # Hash check if available
        if entry.sha256:
            actual_hash = self._compute_sha256(path)
            if actual_hash != entry.sha256:
                raise MirrorVerificationError(
                    f"Hash mismatch: expected {entry.sha256}, got {actual_hash}"
                )

    @staticmethod
    def _compute_sha256(path: Path) -> str:
        """Compute SHA256 of file."""
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python mirror.py <filename> [--test-range <bytes>]")
        print(f"\nKnown files:")
        for fname, entry in CATALOG.items():
            print(f"  {fname}")
            print(
                f"    {entry.human_name} | {entry.min_vram_gb}GB VRAM | "
                f"~{entry.approx_size_bytes / (1024**3):.2f} GB"
            )
        print("\nExample (test with 4KB range):")
        print("  python mirror.py gemma4-12b-Q4_K_M.gguf --test-range 4096")
        sys.exit(1)

    filename = sys.argv[1]
    test_bytes = 4096

    if len(sys.argv) > 3 and sys.argv[2] == "--test-range":
        test_bytes = int(sys.argv[3])

    if filename not in CATALOG:
        print(f"✗ File not in catalog: {filename}")
        print(f"Known files: {', '.join(CATALOG.keys())}")
        sys.exit(1)

    entry = CATALOG[filename]
    url = f"{MIRROR_BASE_URL}/{filename}"

    print(f"Testing {filename}...")
    print(f"URL: {url}")
    print(
        f"Expected size: ~{entry.approx_size_bytes:,} bytes "
        f"({entry.approx_size_bytes / (1024**3):.2f} GB)"
    )
    print(f"Min VRAM needed: {entry.min_vram_gb} GB")

    try:
        # Test with Range header to verify resumable support
        print(f"\nFetching first {test_bytes} bytes with Range header...")
        req = urllib.request.Request(
            url,
            headers={
                "Range": f"bytes=0-{test_bytes - 1}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"✓ HTTP Status: {resp.status}")

            resp_headers = dict(resp.headers)
            print(f"✓ Accept-Ranges: {resp_headers.get('Accept-Ranges', 'not set')}")
            print(f"✓ Content-Range: {resp_headers.get('Content-Range', 'not set')}")
            print(
                f"✓ Content-Length: {resp_headers.get('Content-Length', 'not set')}"
            )

            data = resp.read()
            print(f"✓ Received {len(data)} bytes")

            if resp.status == 206:
                print(f"✓ Server supports Range requests (206 Partial Content)")
            elif resp.status == 200:
                print(
                    f"⚠ Server returned 200 (full content) instead of 206 "
                    f"for Range request"
                )

            # Verify it looks like GGUF
            if data.startswith(b"GGUF"):
                print(f"✓ File starts with GGUF magic bytes")
            else:
                print(
                    f"⚠ File does not start with GGUF magic bytes: "
                    f"{data[:16].hex()}"
                )

    except urllib.error.HTTPError as e:
        if e.code == 404:
            body = e.read().decode("utf-8", errors="replace")
            print(f"✗ 404 Not Found")
            print(f"✗ Mirror response: {body}")
            sys.exit(1)
        else:
            print(f"✗ HTTP {e.code}: {e.reason}")
            sys.exit(1)
    except urllib.error.URLError as e:
        print(f"✗ Network error: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")
        sys.exit(1)

    print("\n✓ Mirror client test passed!")
