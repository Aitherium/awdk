"""
Test Unsloth Kimi-K3 GGUF downloader.

Tests verify:
  1. KIMI_K3_QUANTS ladder integrity (sizes ascending, min_total > size)
  2. list_kimi_shards with mocked HF API (14 shards + mmproj)
  3. Unknown quant error lists available quantizations
  4. Missing mmproj raises ValueError
  5. preflight_disk pass/fail with monkeypatched disk_usage
  6. download_shards resume logic with temp dir + mocked fetch
  7. Partial .part files resume from correct offset
  8. SHA256 recorded per shard
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from adk.unsloth_gguf_download import (
    KIMI_K3_QUANTS,
    download_shards,
    list_kimi_shards,
    preflight_disk,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_download_dir():
    """Temporary directory for download tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Quant ladder integrity
# ─────────────────────────────────────────────────────────────────────────────


class TestQuantLadder:
    """Verify KIMI_K3_QUANTS integrity."""

    def test_quant_ladder_sizes_ascending(self):
        """Size GB should increase monotonically."""
        quants = list(KIMI_K3_QUANTS.keys())
        sizes = [KIMI_K3_QUANTS[q]["size_gb"] for q in quants]
        assert sizes == sorted(sizes), \
            f"Sizes not ascending: {dict(zip(quants, sizes))}"

    def test_quant_ladder_min_total_greater_than_size(self):
        """min_total_memory_gb should exceed size_gb for each quant."""
        for quant, spec in KIMI_K3_QUANTS.items():
            assert spec["min_total_memory_gb"] > spec["size_gb"], \
                f"{quant}: min_total {spec['min_total_memory_gb']} " \
                f"not > size {spec['size_gb']}"

    def test_quant_ladder_keys_valid(self):
        """All keys should match expected format."""
        expected_keys = {
            "UD-IQ1_S", "UD-IQ1_M", "UD-IQ2_XXS",
            "UD-Q2_K_XL", "UD-Q8_K_XL",
        }
        assert set(KIMI_K3_QUANTS.keys()) == expected_keys


# ─────────────────────────────────────────────────────────────────────────────
# Tests: list_kimi_shards
# ─────────────────────────────────────────────────────────────────────────────


class TestListKimiShards:
    """Test HuggingFace API enumeration."""

    @patch("urllib.request.urlopen")
    def test_list_kimi_shards_happy_path(self, mock_urlopen):
        """Happy path: enumerate quant dir + mmproj."""
        # Mock quant directory listing
        quant_response = [
            {"type": "file", "name": "model-00001.gguf",
             "size": 100000000},
            {"type": "file", "name": "model-00002.gguf",
             "size": 200000000},
            {"type": "directory", "name": "subdir"},
        ]
        quant_response_bytes = json.dumps(quant_response).encode()

        # Mock root listing with mmproj
        root_response = [
            {"type": "file", "name": "README.md", "size": 1000},
            {"type": "file", "name": "mmproj-BF16.gguf",
             "size": 50000000},
        ]
        root_response_bytes = json.dumps(root_response).encode()

        # Configure mock to return both responses
        mock_urlopen.side_effect = [
            MagicMock(__enter__=lambda s: s,
                      __exit__=lambda s, *a: None,
                      read=lambda: quant_response_bytes),
            MagicMock(__enter__=lambda s: s,
                      __exit__=lambda s, *a: None,
                      read=lambda: root_response_bytes),
        ]

        shards = list_kimi_shards("UD-Q2_K_XL")

        assert len(shards) == 3
        assert any(s["path"] == "UD-Q2_K_XL/model-00001.gguf"
                   for s in shards)
        assert any(s["path"] == "UD-Q2_K_XL/model-00002.gguf"
                   for s in shards)
        assert any(s["path"] == "mmproj-BF16.gguf" for s in shards)

    @patch("urllib.request.urlopen")
    def test_list_kimi_shards_unknown_quant(self, mock_urlopen):
        """Unknown quant should raise ValueError with available quants."""
        with pytest.raises(ValueError) as exc_info:
            list_kimi_shards("UD-INVALID")
        error = str(exc_info.value)
        assert "Unknown quant" in error
        assert "UD-Q2_K_XL" in error
        assert "Available" in error

    @patch("urllib.request.urlopen")
    def test_list_kimi_shards_missing_mmproj(self, mock_urlopen):
        """Missing mmproj should raise ValueError."""
        quant_response = [
            {"type": "file", "name": "model-00001.gguf",
             "size": 100000000},
        ]
        quant_response_bytes = json.dumps(quant_response).encode()

        root_response = [
            {"type": "file", "name": "README.md", "size": 1000},
        ]
        root_response_bytes = json.dumps(root_response).encode()

        mock_urlopen.side_effect = [
            MagicMock(__enter__=lambda s: s,
                      __exit__=lambda s, *a: None,
                      read=lambda: quant_response_bytes),
            MagicMock(__enter__=lambda s: s,
                      __exit__=lambda s, *a: None,
                      read=lambda: root_response_bytes),
        ]

        with pytest.raises(ValueError) as exc_info:
            list_kimi_shards("UD-Q2_K_XL")
        assert "mmproj-BF16.gguf not found" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_list_kimi_shards_hf_404(self, mock_urlopen):
        """404 from HF should raise ValueError with quant list."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "", 404, "Not found", {}, None
        )
        with pytest.raises(ValueError) as exc_info:
            list_kimi_shards("UD-Q2_K_XL")
        assert "not found" in str(exc_info.value).lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: preflight_disk
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightDisk:
    """Test disk space checking."""

    def test_preflight_disk_sufficient_space(self, temp_download_dir):
        """Sufficient space should return True."""
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=1000 * 1024 ** 3
            )
            assert preflight_disk(temp_download_dir, 100 * 1024 ** 3)

    def test_preflight_disk_insufficient_space(self,
                                               temp_download_dir):
        """Insufficient space should raise OSError."""
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=50 * 1024 ** 3
            )
            with pytest.raises(OSError) as exc_info:
                preflight_disk(temp_download_dir, 100 * 1024 ** 3)
            assert "Insufficient disk space" in str(exc_info.value)

    def test_preflight_disk_with_headroom(self, temp_download_dir):
        """Headroom fraction should be factored into requirement."""
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=110 * 1024 ** 3
            )
            # 100 GB needed + 20% headroom = 120 GB required
            with pytest.raises(OSError):
                preflight_disk(temp_download_dir, 100 * 1024 ** 3,
                               headroom_frac=0.20)

    def test_preflight_disk_creates_directory(self, temp_download_dir):
        """Should create directory if it doesn't exist."""
        nested = temp_download_dir / "a" / "b" / "c"
        assert not nested.exists()
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(
                free=1000 * 1024 ** 3
            )
            preflight_disk(nested, 10 * 1024 ** 3)
        assert nested.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: download_shards
# ─────────────────────────────────────────────────────────────────────────────


class TestDownloadShards:
    """Test shard download with resume and verification."""

    @patch("urllib.request.urlopen")
    @patch("adk.unsloth_gguf_download.list_kimi_shards")
    @patch("adk.unsloth_gguf_download.preflight_disk")
    def test_download_shards_basic(self, mock_preflight, mock_list,
                                   mock_urlopen, temp_download_dir):
        """Basic download should create files and record SHA256."""
        shard_data = b"test shard data"
        mock_list.return_value = [
            {"path": "UD-Q2_K_XL/model.gguf",
             "size_bytes": len(shard_data)},
            {"path": "mmproj-BF16.gguf",
             "size_bytes": len(shard_data)},
        ]
        mock_preflight.return_value = True

        # One data chunk then EOF per shard (2 shards). A mock whose read()
        # always returns data makes download_shards' read-until-empty loop
        # write forever — that was this test's original hang.
        chunks = iter([shard_data, b"", shard_data, b""])
        mock_resp = MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: None,
            read=lambda chunk_size=None: next(chunks, b""),
        )
        mock_urlopen.return_value = mock_resp

        result = download_shards("UD-Q2_K_XL", temp_download_dir)

        assert result["total_bytes"] == len(shard_data) * 2
        assert len(result["shards"]) == 2
        assert all("sha256" in s for s in result["shards"])

        # Verify mmproj SHA recorded
        expected_sha = hashlib.sha256(shard_data).hexdigest()
        assert result["mmproj_sha256"] == expected_sha

    @patch("adk.unsloth_gguf_download.list_kimi_shards")
    @patch("adk.unsloth_gguf_download.preflight_disk")
    def test_download_shards_resume_offset(self, mock_preflight, mock_list,
                                           temp_download_dir):
        """Existing .part file should determine resume offset."""
        mock_list.return_value = [
            {"path": "UD-Q2_K_XL/model.gguf",
             "size_bytes": 1000},
        ]
        mock_preflight.return_value = True

        # Create partial .part file
        model_file = temp_download_dir / "model.gguf"
        part_file = model_file.with_suffix(".gguf.part")
        part_file.write_bytes(b"x" * 500)

        with patch("urllib.request.urlopen") as mock_urlopen:
            chunks = iter([b"y" * 500])  # one chunk then EOF — never loop forever
            mock_resp = MagicMock(
                __enter__=lambda s: s,
                __exit__=lambda s, *a: None,
                read=lambda chunk_size=None: next(chunks, b""),
            )
            mock_urlopen.return_value = mock_resp

            download_shards("UD-Q2_K_XL", temp_download_dir, resume=True)

            # Verify Range header was set
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert "Range" in req.headers
            assert "bytes=500-" in req.headers["Range"]

    @patch("urllib.request.urlopen")
    @patch("adk.unsloth_gguf_download.list_kimi_shards")
    @patch("adk.unsloth_gguf_download.preflight_disk")
    def test_download_shards_missing_mmproj_error(self, mock_preflight,
                                                   mock_list, mock_urlopen,
                                                   temp_download_dir):
        """Missing mmproj in result should not occur if list_kimi_shards
           already raises."""
        # This is already tested in list_kimi_shards, but verify end-to-end
        mock_list.side_effect = ValueError("mmproj not found")

        with pytest.raises(ValueError):
            download_shards("UD-Q2_K_XL", temp_download_dir)

    def test_download_shards_insufficient_disk(self, temp_download_dir):
        """Insufficient disk should raise OSError."""
        with patch("adk.unsloth_gguf_download.list_kimi_shards") \
                as mock_list:
            with patch("adk.unsloth_gguf_download.preflight_disk") \
                    as mock_preflight:
                mock_list.return_value = [
                    {"path": "UD-Q2_K_XL/model.gguf",
                     "size_bytes": 1000000000000},
                ]
                mock_preflight.side_effect = OSError("No space")

                with pytest.raises(OSError):
                    download_shards("UD-Q2_K_XL", temp_download_dir)

    @patch("urllib.request.urlopen")
    @patch("adk.unsloth_gguf_download.list_kimi_shards")
    @patch("adk.unsloth_gguf_download.preflight_disk")
    def test_download_shards_with_progress_callback(self, mock_preflight,
                                                     mock_list, mock_urlopen,
                                                     temp_download_dir):
        """Progress callback should be invoked."""
        shard_data = b"test data" * 100
        mock_list.return_value = [
            {"path": "UD-Q2_K_XL/model.gguf",
             "size_bytes": len(shard_data)},
            {"path": "mmproj-BF16.gguf",
             "size_bytes": len(shard_data)},
        ]
        mock_preflight.return_value = True

        # One chunk then EOF per shard (2 shards) — an always-data mock loops forever.
        chunks = iter([shard_data, b"", shard_data, b""])
        mock_resp = MagicMock(
            __enter__=lambda s: s,
            __exit__=lambda s, *a: None,
            read=lambda chunk_size=None: next(chunks, b""),
        )
        mock_urlopen.return_value = mock_resp

        progress_calls = []

        def progress_cb(path, done, total):
            progress_calls.append((path, done, total))

        download_shards("UD-Q2_K_XL", temp_download_dir,
                        progress_cb=progress_cb)

        # Should have been called
        assert len(progress_calls) > 0
