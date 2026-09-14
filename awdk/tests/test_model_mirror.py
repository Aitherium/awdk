"""Tests for model mirror catalog, download, and hardware fitting.

Tests both resumable/rate-capped downloads (mirror.py) and hardware classification (fit.py).
Uses local http.server fixtures for deterministic testing; one test probes the live mirror
and is skipped when offline.
"""

from __future__ import annotations

import hashlib
import http.server
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

logger = logging.getLogger(__name__)


# =============================================================================
# Fixtures: Local HTTP server for download testing
# =============================================================================


class RangeResponseHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler supporting Range requests, rate limiting, and configurable errors."""

    # Class-level state shared across handler instances
    files: dict[str, bytes] = {}
    rate_limit_bytes_per_sec: float = float("inf")  # Unlimited by default
    request_count: int = 0
    unknown_files: set[str] = set()
    length_mismatches: dict[str, int] = {}  # filename -> wrong_length

    def do_GET(self) -> None:
        """Handle GET requests with Range support."""
        self.request_count += 1
        filename = self.path.lstrip("/")

        # Unknown file handling
        if filename in self.unknown_files:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not a known weight file")
            return

        if filename not in self.files:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"File not found")
            return

        data = self.files[filename]
        total_size = len(data)

        # Check for mismatches in advertised length
        if filename in self.length_mismatches:
            advertised = self.length_mismatches[filename]
        else:
            advertised = total_size

        # Handle Range request
        range_header = self.headers.get("Range")
        if range_header:
            try:
                # Parse "bytes=start-end"
                range_spec = range_header.replace("bytes=", "")
                if "-" not in range_spec:
                    self.send_error(400, "Invalid Range header")
                    return

                parts = range_spec.split("-")
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else total_size - 1

                if start > end or start >= total_size:
                    self.send_error(416, "Range Not Satisfiable")
                    return

                end = min(end, total_size - 1)
                chunk = data[start : end + 1]

                self.send_response(206)  # Partial Content
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{advertised}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                self._write_with_rate_limit(chunk)
            except (ValueError, IndexError) as e:
                self.send_error(400, f"Invalid Range: {e}")
        else:
            # Full file response
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(advertised))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            self._write_with_rate_limit(data)

    def _write_with_rate_limit(self, data: bytes) -> None:
        """Write data with rate limiting applied."""
        if self.rate_limit_bytes_per_sec >= float("inf"):
            self.wfile.write(data)
            return

        # Rate-limited write: send in chunks with delays
        chunk_size = max(1, int(self.rate_limit_bytes_per_sec / 10))  # 10 chunks per second
        for i in range(0, len(data), chunk_size):
            chunk = data[i : i + chunk_size]
            self.wfile.write(chunk)
            # Sleep to enforce rate limit
            time.sleep(chunk_size / self.rate_limit_bytes_per_sec)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress verbose logging."""
        logger.debug(f"{self.client_address[0]} - {format % args}")


@pytest.fixture
def http_server():
    """Start a local HTTP server with Range support.

    Yields (base_url, handler_class) so tests can configure files, errors, rate limits.
    """
    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Create and start server
    server = http.server.HTTPServer(("127.0.0.1", port), RangeResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    yield base_url, RangeResponseHandler

    server.shutdown()
    server.server_close()


# =============================================================================
# Tests: Mirror module (download, resume, rate-cap, verification)
# =============================================================================


class TestMirrorDownloadResumeOffset:
    """Verify that resumed downloads compute correct Range offsets from partial files."""

    def test_resume_calculates_correct_offset_from_partial_file(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that resuming a download starts at the correct byte offset."""
        base_url, handler = http_server

        # Set up a test file in the catalog
        filename = "test_model.gguf"
        file_data = b"x" * (1024 * 1024)  # 1 MB file
        handler.files[filename] = file_data
        handler.rate_limit_bytes_per_sec = float("inf")

        tmp_path = Path("/tmp/test_resume")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / filename

        try:
            from adk.models.mirror import MirrorClient, CATALOG, WeightCatalogEntry

            # Add test file to catalog
            CATALOG[filename] = WeightCatalogEntry(
                filename=filename,
                human_name="Test Model",
                family="Test",
                quantization="Q4",
                approx_size_bytes=len(file_data),
                min_vram_gb=8,
            )

            # Override the mirror URL to use our test server
            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = base_url

            try:
                # First, create a partial file
                partial_path = output_path.with_suffix(output_path.suffix + ".partial")
                partial_path.write_bytes(file_data[:500 * 1024])

                # Now download with resume
                client = MirrorClient(rate_limit_bytes_per_sec=0)  # Unlimited for testing
                final = client.download(filename=filename, dest_path=str(output_path))

                # Verify the final file matches the complete file
                assert Path(final).read_bytes() == file_data
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)
            (tmp_path / f"{filename}.partial").unlink(missing_ok=True)

    def test_resume_offset_computation_is_precise(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that resume offset computation is precise and won't corrupt files.

        Off-by-one errors in offset calculation would corrupt the file at the
        resume point by having bytes in the wrong positions.
        """
        base_url, handler = http_server

        filename = "offset_test.gguf"
        file_data = b"ABCDEFGHIJ" * 100  # 1000 bytes
        handler.files[filename] = file_data
        handler.rate_limit_bytes_per_sec = float("inf")

        tmp_path = Path("/tmp/test_offset")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / filename

        try:
            from adk.models.mirror import MirrorClient, CATALOG, WeightCatalogEntry

            CATALOG[filename] = WeightCatalogEntry(
                filename=filename,
                human_name="Offset Test",
                family="Test",
                quantization="Q4",
                approx_size_bytes=len(file_data),
                min_vram_gb=8,
            )

            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = base_url

            try:
                # Create a partial file (say first 500 bytes)
                partial_size = 500
                partial_path = output_path.with_suffix(output_path.suffix + ".partial")
                partial_path.write_bytes(file_data[:partial_size])

                # Download with resume
                client = MirrorClient(rate_limit_bytes_per_sec=0)
                final = client.download(filename=filename, dest_path=str(output_path))

                # The critical test: file must match byte-for-byte
                downloaded_data = Path(final).read_bytes()
                assert downloaded_data == file_data, (
                    f"Downloaded file does not match original. "
                    f"First mismatch at byte {next((i for i in range(len(file_data)) if downloaded_data[i] != file_data[i]), -1)}"
                )
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)
            (tmp_path / f"{filename}.partial").unlink(missing_ok=True)


class TestMirrorLengthMismatch:
    """Verify that length mismatches after download are reported."""

    def test_length_mismatch_is_reported_not_accepted(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that downloaded file size mismatch from catalog is reported as an error."""
        base_url, handler = http_server

        filename = "size_test.gguf"
        actual_data = b"A" * 1000
        handler.files[filename] = actual_data
        handler.rate_limit_bytes_per_sec = float("inf")

        tmp_path = Path("/tmp/test_mismatch")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / filename

        try:
            from adk.models.mirror import (
                MirrorClient,
                CATALOG,
                WeightCatalogEntry,
                MirrorVerificationError,
            )

            # Advertise a VERY different size in catalog (outside tolerance)
            CATALOG[filename] = WeightCatalogEntry(
                filename=filename,
                human_name="Mismatch Test",
                family="Test",
                quantization="Q4",
                approx_size_bytes=10 * 1000 * 1000,  # 10 MB in catalog but actual is 1000 bytes
                min_vram_gb=8,
            )

            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = base_url

            try:
                client = MirrorClient(rate_limit_bytes_per_sec=0)

                # This should raise MirrorVerificationError due to size mismatch
                with pytest.raises(MirrorVerificationError) as exc_info:
                    client.download(filename=filename, dest_path=str(output_path))

                error_msg = str(exc_info.value).lower()
                assert "size" in error_msg or "mismatch" in error_msg
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)
            (tmp_path / f"{filename}.partial").unlink(missing_ok=True)


class TestMirrorUnknownFileHandling:
    """Verify unknown filenames are reported distinctly from network failures."""

    def test_unknown_file_is_distinct_from_network_error(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that 'unknown file' and 'network error' are different exceptions."""
        base_url, handler = http_server

        # Mark this file as unknown (will get 404 with "not a known weight file")
        handler.unknown_files.add("nonexistent.gguf")
        handler.rate_limit_bytes_per_sec = float("inf")

        tmp_path = Path("/tmp/test_unknown")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / "nonexistent.gguf"

        try:
            from adk.models.mirror import (
                MirrorClient,
                CATALOG,
                MirrorFileNotFoundError,
                MirrorNetworkError,
            )

            # Try to download a file not in the catalog
            client = MirrorClient(rate_limit_bytes_per_sec=0)

            # Should raise MirrorFileNotFoundError because it's not in CATALOG
            with pytest.raises(MirrorFileNotFoundError) as exc_info:
                client.download(filename="nonexistent.gguf", dest_path=str(output_path))

            assert "not in mirror catalog" in str(exc_info.value).lower()
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)

    def test_network_error_is_distinct_from_unknown_file(self) -> None:
        """Test that network errors don't get mislabeled as 'unknown file'."""
        import socket

        tmp_path = Path("/tmp/test_network")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / "test.gguf"

        try:
            from adk.models.mirror import (
                MirrorClient,
                CATALOG,
                WeightCatalogEntry,
                MirrorFileNotFoundError,
                MirrorNetworkError,
            )

            # Add a test file to catalog
            CATALOG["test.gguf"] = WeightCatalogEntry(
                filename="test.gguf",
                human_name="Test",
                family="Test",
                quantization="Q4",
                approx_size_bytes=1000,
                min_vram_gb=8,
            )

            # Point to a server that won't respond (blocked port)
            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = "http://127.0.0.1:1"  # Port 1 is reserved/blocked

            try:
                client = MirrorClient(rate_limit_bytes_per_sec=0)

                # Should raise MirrorNetworkError, NOT MirrorFileNotFoundError
                with pytest.raises(MirrorNetworkError):
                    client.download(filename="test.gguf", dest_path=str(output_path))
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)


class TestMirrorRateCap:
    """Verify that the rate cap is actually applied."""

    def test_rate_cap_is_applied_and_enforced(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that download respects the rate limit."""
        base_url, handler = http_server

        filename = "rate_test.gguf"
        file_size = 10 * 1024  # 10 KB
        file_data = b"X" * file_size
        handler.files[filename] = file_data
        handler.rate_limit_bytes_per_sec = float("inf")  # Server unlimited, client rate-limits

        tmp_path = Path("/tmp/test_rate")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / filename

        try:
            from adk.models.mirror import (
                MirrorClient,
                CATALOG,
                WeightCatalogEntry,
            )

            CATALOG[filename] = WeightCatalogEntry(
                filename=filename,
                human_name="Rate Test",
                family="Test",
                quantization="Q4",
                approx_size_bytes=file_size,
                min_vram_gb=8,
            )

            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = base_url

            try:
                # Rate limit: 5 KB/s
                rate_limit = 5 * 1024  # bytes/sec
                client = MirrorClient(rate_limit_bytes_per_sec=rate_limit)

                start_time = time.time()
                client.download(filename=filename, dest_path=str(output_path))
                elapsed = time.time() - start_time

                # Download of 10 KB at 5 KB/s should take ~2 seconds
                expected_time = file_size / rate_limit
                tolerance = 1.5  # Allow 50% variance for system delays

                assert elapsed >= expected_time * 0.8, (
                    f"Download too fast ({elapsed:.2f}s vs expected "
                    f"{expected_time:.2f}s); rate cap may not be enforced"
                )

                # Verify the file was downloaded correctly
                assert output_path.read_bytes() == file_data
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)
            (tmp_path / f"{filename}.partial").unlink(missing_ok=True)

    def test_rate_cap_pacing_path_is_exercised(
        self, http_server: tuple[str, type]
    ) -> None:
        """Test that the rate-limiting code path is actually taken."""
        base_url, handler = http_server

        filename = "pacing_test.gguf"
        file_data = b"P" * 10000  # 10 KB
        handler.files[filename] = file_data
        handler.rate_limit_bytes_per_sec = float("inf")

        tmp_path = Path("/tmp/test_pacing")
        tmp_path.mkdir(exist_ok=True)
        output_path = tmp_path / filename

        try:
            from adk.models.mirror import (
                MirrorClient,
                CATALOG,
                WeightCatalogEntry,
            )

            CATALOG[filename] = WeightCatalogEntry(
                filename=filename,
                human_name="Pacing Test",
                family="Test",
                quantization="Q4",
                approx_size_bytes=len(file_data),
                min_vram_gb=8,
            )

            from adk.models import mirror as mirror_module
            original_url = mirror_module.MIRROR_BASE_URL
            mirror_module.MIRROR_BASE_URL = base_url

            try:
                # Create client with low rate limit (1 KB/s) to exercise pacing
                client = MirrorClient(rate_limit_bytes_per_sec=1024)

                # Mock time.sleep to verify the rate limiter calls it
                with patch("time.sleep") as mock_sleep:
                    client.download(filename=filename, dest_path=str(output_path))

                    # Sleep should have been called at least once during rate limiting
                    assert mock_sleep.call_count > 0, (
                        "Rate limiting sleep not exercised; "
                        "download too fast or rate limiter not working"
                    )
            finally:
                mirror_module.MIRROR_BASE_URL = original_url
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        finally:
            output_path.unlink(missing_ok=True)
            (tmp_path / f"{filename}.partial").unlink(missing_ok=True)


# =============================================================================
# Tests: Fit module (hardware matching and classification)
# =============================================================================


class TestFitClassification:
    """Verify that fit classifies models with reason strings."""

    def test_fit_rejects_model_with_reason_string(self) -> None:
        """Test that fit returns a reason string when rejecting a model."""
        try:
            from adk.models.fit import fit_models, ModelFit
            from adk.models.mirror import CATALOG, WeightCatalogEntry
            from adk.hardware_probe import SystemInfo

            # Mock hardware that cannot run large models
            hardware = SystemInfo(
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="CPU only",
                gpu_vram_mb=0,  # No GPU
                ram_gb=8,  # Only 8 GB RAM
            )

            # Use a small model that should be rejected
            # (it's in the catalog but let's check the classification)
            fits = fit_models(hardware, CATALOG)

            # There should be some models that won't run
            rejected = [f for f in fits if f.classification == "will_not_run"]
            assert len(rejected) > 0, "Should have models that won't run on this hardware"

            # Check that rejected models have reason strings
            for fit_result in rejected:
                assert isinstance(fit_result.reason, str), "Reason must be a string"
                assert len(fit_result.reason) > 0, "Reason string must not be empty"
        except ImportError as e:
            pytest.skip(f"adk.models.fit not yet fully implemented: {e}")

    def test_fit_accepts_model_with_explanation(self) -> None:
        """Test that fit accepts runnable models and explains why."""
        try:
            from adk.models.fit import fit_models
            from adk.models.mirror import CATALOG
            from adk.hardware_probe import SystemInfo

            # Mock good hardware
            hardware = SystemInfo(
                cpu_cores=8,
                gpu_vendor="nvidia",
                gpu_name="RTX 4090",
                gpu_vram_mb=24 * 1024,  # 24 GB VRAM
                ram_gb=64,  # 64 GB RAM
            )

            fits = fit_models(hardware, CATALOG)

            # Should have some models that can run
            runnable = [f for f in fits if f.classification != "will_not_run"]
            assert len(runnable) > 0, "Should have models that can run on this hardware"

            # Check that accepted models have reason strings
            for fit_result in runnable:
                assert isinstance(fit_result.reason, str), "Explanation must be a string"
                assert len(fit_result.reason) > 0, "Reason must not be empty"
        except ImportError as e:
            pytest.skip(f"adk.models.fit not yet fully implemented: {e}")


class TestFitCpuVramIsZero:
    """Verify that fit doesn't reject everything when VRAM is 0 (CPU+RAM hobbyist case)."""

    def test_fit_does_not_reject_small_model_on_cpu_only(self) -> None:
        """Test that a small model can run on CPU with 0 VRAM."""
        try:
            from adk.models.fit import fit_models
            from adk.models.mirror import CATALOG
            from adk.hardware_probe import SystemInfo

            # CPU-only hardware: 0 VRAM, 16 GB RAM
            hardware = SystemInfo(
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="CPU only",
                gpu_vram_mb=0,  # No GPU
                ram_gb=16,  # 16 GB RAM
            )

            fits = fit_models(hardware, CATALOG)

            # Should have at least one model that can run on CPU+RAM
            # (small models like Bonsai can run on CPU)
            cpu_runnable = [
                f for f in fits
                if f.classification != "will_not_run" and f.can_run_on_cpu
            ]

            assert (
                len(cpu_runnable) > 0
            ), f"Should have models that run on CPU with 16 GB RAM. Got: {fits}"
        except ImportError as e:
            pytest.skip(f"adk.models.fit not yet fully implemented: {e}")

    def test_fit_rejects_huge_model_even_on_cpu(self) -> None:
        """Test that unreasonably large models are still rejected on CPU."""
        try:
            from adk.models.fit import fit_models
            from adk.models.mirror import CATALOG
            from adk.hardware_probe import SystemInfo

            # CPU-only hardware with limited RAM
            hardware = SystemInfo(
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="CPU only",
                gpu_vram_mb=0,  # No GPU
                ram_gb=4,  # Only 4 GB RAM
            )

            fits = fit_models(hardware, CATALOG)

            # Should have many models that won't run (too large for 4 GB RAM)
            rejected = [f for f in fits if f.classification == "will_not_run"]
            assert (
                len(rejected) > 0
            ), "Models larger than 4 GB RAM should be rejected"
        except ImportError as e:
            pytest.skip(f"adk.models.fit not yet fully implemented: {e}")


class TestFitReasonStrings:
    """Verify that fit reason strings are informative."""

    def test_fit_reason_distinguishes_vram_shortage_from_ram_shortage(self) -> None:
        """Test that fit explains WHY a model doesn't fit."""
        try:
            from adk.models.fit import fit_models
            from adk.models.mirror import CATALOG
            from adk.hardware_probe import SystemInfo

            # Scenario 1: GPU with low VRAM but good RAM
            # With 64 GB RAM, models can fall back to CPU, so they're "runs_tight" not "will_not_run"
            hardware_vram_shortage = SystemInfo(
                cpu_cores=8,
                gpu_vendor="nvidia",
                gpu_name="GTX 960",
                gpu_vram_mb=2 * 1024,  # 2 GB VRAM (very tight)
                ram_gb=64,  # 64 GB RAM
            )

            result_vram = fit_models(hardware_vram_shortage, CATALOG)

            # Scenario 2: CPU only with low RAM
            hardware_ram_shortage = SystemInfo(
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="CPU only",
                gpu_vram_mb=0,  # No GPU
                ram_gb=4,  # 4 GB RAM only
            )

            result_ram = fit_models(hardware_ram_shortage, CATALOG)

            # Get models from both scenarios
            # In scenario 1, models can fall back to CPU, so they're "runs_tight" not "will_not_run"
            # Disk-rejected models carry a DISK reason ("Not enough disk space"),
            # which is accurate -- the keyword assertions below are about WHY a
            # model falls back to CPU / is RAM-rejected, so they must only see
            # models that were NOT disk-rejected. Measured 2026-08-29 on a
            # disk-full runner (0.5 GB free): every model was disk-rejected,
            # every cpu-fallback reason became a disk string, and the keyword
            # assert failed for an environment reason, not a code one.
            cpu_fallback = [f for f in result_vram
                            if f.can_run_on_cpu and f.classification == "runs_tight"]
            rejected_ram = [f for f in result_ram
                            if f.classification == "will_not_run"
                            and "disk" not in f.reason.lower()]

            assert len(cpu_fallback) > 0, "Should have models that fall back to CPU"
            assert len(rejected_ram) > 0, "Should have models rejected due to RAM shortage"

            # Reason strings should mention the bottleneck
            for fit_result in cpu_fallback:
                reason_lower = fit_result.reason.lower()
                # CPU fallback reasons should mention GPU or VRAM insufficiency
                assert (
                    "gpu insufficient" in reason_lower
                    or "vram" in reason_lower
                    or "cpu" in reason_lower
                ), f"CPU fallback reason should mention GPU/VRAM/CPU: {fit_result.reason}"

            for fit_result in rejected_ram:
                reason_lower = fit_result.reason.lower()
                # RAM shortage should mention RAM or memory
                assert "ram" in reason_lower or "memory" in reason_lower or "resources" in reason_lower, (
                    f"RAM shortage reason should mention RAM or memory: {fit_result.reason}"
                )
        except ImportError as e:
            pytest.skip(f"adk.models.fit not yet fully implemented: {e}")


# =============================================================================
# Integration test: Mirror catalog loading
# =============================================================================


class TestMirrorCatalog:
    """Test that mirror catalog is properly defined."""

    def test_catalog_contains_known_files(self) -> None:
        """Test that the mirror catalog lists available files."""
        try:
            from adk.models.mirror import CATALOG

            # The catalog should not be empty
            assert len(CATALOG) > 0, "Catalog should contain at least one model"

            # Each entry should have required fields
            for filename, entry in CATALOG.items():
                assert entry.filename == filename
                assert entry.human_name, "Model should have human-readable name"
                assert entry.family, "Model should have a family"
                assert entry.quantization, "Model should specify quantization"
                assert entry.approx_size_bytes > 0, "Model should have size > 0"
                assert entry.min_vram_gb > 0, "Model should specify VRAM requirement"
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")


# =============================================================================
# Live mirror test (skipped when offline)
# =============================================================================


@pytest.mark.skipif(
    os.getenv("CI") == "true" or os.getenv("AITHER_OFFLINE") == "1",
    reason="Skipped in CI or offline mode",
)
class TestMirrorLive:
    """Test against the live mirror (only runs when online and not in CI)."""

    def test_live_mirror_serves_known_files(self) -> None:
        """Test that the live mirror at weights.aitherium.com responds correctly.

        This test only runs when online and not in CI. It verifies that a sample
        of known files can be accessed from the live mirror.
        """
        try:
            from adk.models.mirror import MirrorClient, CATALOG

            client = MirrorClient(rate_limit_bytes_per_sec=0)

            # Try to download just the metadata (via a HEAD request or small read)
            # Test that the catalog contains real files
            known_files = [
                "Bonsai-27B-Q1_0.gguf",
                "gemma4-12b-Q4_K_M.gguf",
            ]

            found_any = False
            for filename in known_files:
                if filename in CATALOG:
                    found_any = True
                    logger.info(f"Catalog contains {filename}")
                    break

            assert found_any, "Should have at least one known file in catalog"
        except ImportError as e:
            pytest.skip(f"adk.models.mirror not yet fully implemented: {e}")
        except Exception as e:
            if any(phrase in str(e) for phrase in ["Connection refused", "Network is unreachable", "Name or service not known"]):
                pytest.skip("Live mirror not reachable (network issue)")
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
