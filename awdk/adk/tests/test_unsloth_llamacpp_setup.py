"""
Test Unsloth fork llama.cpp provisioner.

Tests verify:
  1. plan_build dry-run returns command list with fork URL + PR ref
  2. plan_build includes both RPC and CUDA flags
  3. plan_build respects cuda/rpc toggles
  4. install_unsloth_llamacpp idempotent (skip clone if exists)
  5. install_unsloth_llamacpp records SHA to .unsloth-pin.json
  6. verify_kimi_binary on fake bin dir (missing → ok False)
  7. verify_kimi_binary with fake llama-server script → ok True
  8. verify_kimi_binary probes --mmproj presence
  9. Cross-platform binary detection (.exe on Windows)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from adk.unsloth_llamacpp_setup import (
    KIMI_K3_BRANCH_REF,
    KIMI_K3_LOCAL_BRANCH,
    UNSLOTH_LLAMACPP_REPO,
    install_unsloth_llamacpp,
    plan_build,
    verify_kimi_binary,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_build_dir():
    """Temporary directory for build tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: plan_build
# ─────────────────────────────────────────────────────────────────────────────


class TestPlanBuild:
    """Test build plan generation."""

    def test_plan_build_dry_run(self, temp_build_dir):
        """Dry run should return plan without executing."""
        plan = plan_build(temp_build_dir, cuda=True, rpc=True,
                          dry_run=True)

        assert "commands" in plan
        assert plan["build_dir"] == str(temp_build_dir)
        assert plan["cuda_enabled"] is True
        assert plan["rpc_enabled"] is True

    def test_plan_build_contains_fork_url(self, temp_build_dir):
        """Plan should include Unsloth fork URL."""
        plan = plan_build(temp_build_dir, cuda=True, rpc=True)
        commands_str = str(plan["commands"])
        assert UNSLOTH_LLAMACPP_REPO in commands_str

    def test_plan_build_contains_pr_ref(self, temp_build_dir):
        """Plan should include PR#48 reference."""
        plan = plan_build(temp_build_dir, cuda=True, rpc=True)
        commands_str = str(plan["commands"])
        assert KIMI_K3_BRANCH_REF in commands_str
        assert KIMI_K3_LOCAL_BRANCH in commands_str

    def test_plan_build_cuda_flag(self, temp_build_dir):
        """CUDA flag should toggle -DGGML_CUDA."""
        plan_on = plan_build(temp_build_dir, cuda=True, rpc=False)
        plan_off = plan_build(temp_build_dir, cuda=False, rpc=False)

        on_str = str(plan_on["commands"])
        off_str = str(plan_off["commands"])

        assert "-DGGML_CUDA=ON" in on_str
        assert "-DGGML_CUDA=OFF" in off_str

    def test_plan_build_rpc_flag(self, temp_build_dir):
        """RPC flag should toggle -DGGML_RPC."""
        plan_on = plan_build(temp_build_dir, cuda=False, rpc=True)
        plan_off = plan_build(temp_build_dir, cuda=False, rpc=False)

        on_str = str(plan_on["commands"])
        off_str = str(plan_off["commands"])

        assert "-DGGML_RPC=ON" in on_str
        assert "-DGGML_RPC=OFF" in off_str

    def test_plan_build_target_list(self, temp_build_dir):
        """Plan should include all target binaries."""
        plan = plan_build(temp_build_dir)
        commands_str = str(plan["commands"])

        assert "llama-server" in commands_str
        assert "llama-cli" in commands_str
        assert "llama-gguf-split" in commands_str
        assert "rpc-server" in commands_str


# ─────────────────────────────────────────────────────────────────────────────
# Tests: install_unsloth_llamacpp
# ─────────────────────────────────────────────────────────────────────────────


class TestInstallUnslothLlamacpp:
    """Test build execution."""

    @patch("subprocess.run")
    def test_install_skips_clone_if_exists(self, mock_run,
                                           temp_build_dir):
        """Should skip clone if directory exists."""
        # Create dummy directory
        (temp_build_dir / "CMakeLists.txt").touch()

        mock_run.side_effect = [
            # git fetch
            MagicMock(returncode=0, stdout="", stderr=""),
            # git checkout
            MagicMock(returncode=0, stdout="", stderr=""),
            # git rev-parse
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            # cmake configure
            MagicMock(returncode=0, stdout="", stderr=""),
            # cmake build
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        with patch("subprocess.run", mock_run):
            install_unsloth_llamacpp(temp_build_dir)

        # First call should NOT be clone
        first_call = mock_run.call_args_list[0]
        assert first_call[0][0][0] == "git"
        assert "fetch" in first_call[0][0]

    @patch("subprocess.run")
    def test_install_records_sha(self, mock_run, temp_build_dir):
        """Should record HEAD SHA to .unsloth-pin.json.

        The build dir pre-exists, so clone is SKIPPED — the call sequence is
        fetch, checkout, rev-parse, configure, build (5 calls, no clone).
        """
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),        # fetch
            MagicMock(returncode=0, stdout="", stderr=""),        # checkout
            MagicMock(returncode=0, stdout="def456\n", stderr=""),  # rev-parse
            MagicMock(returncode=0, stdout="", stderr=""),        # configure
            MagicMock(returncode=0, stdout="", stderr=""),        # build
        ]

        with patch("subprocess.run", mock_run):
            temp_build_dir.mkdir(exist_ok=True)
            install_unsloth_llamacpp(temp_build_dir, cuda=True, rpc=False)

        pin_path = temp_build_dir / ".unsloth-pin.json"
        assert pin_path.exists(), "pin file must be written on a full run"
        pin_data = json.loads(pin_path.read_text())
        assert pin_data["sha"] == "def456"
        assert pin_data["cuda"] is True
        assert pin_data["rpc"] is False

    @patch("subprocess.run")
    def test_install_fetch_failure(self, mock_run, temp_build_dir):
        """Fetch failure should return error.

        The implementation relies on check=True raising CalledProcessError —
        a mock with returncode=1 does NOT raise, so the failure must be
        modeled as a raising side_effect (that mismatch made the original
        version of this test assert against an empty error string).
        """
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, ["git", "fetch"], stderr="Network error"
            ),
        ]

        with patch("subprocess.run", mock_run):
            temp_build_dir.mkdir(exist_ok=True)
            fetch_result = install_unsloth_llamacpp(temp_build_dir)

        assert fetch_result["success"] is False
        assert "git fetch failed" in fetch_result["error"]
        assert "Network error" in fetch_result["error"]

    @patch("subprocess.run")
    def test_install_cmake_configure_failure(self, mock_run,
                                             temp_build_dir):
        """CMake configure failure should return error (raising side_effect)."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),          # fetch
            MagicMock(returncode=0, stdout="", stderr=""),          # checkout
            MagicMock(returncode=0, stdout="xyz789\n", stderr=""),  # rev-parse
            subprocess.CalledProcessError(
                1, ["cmake"], stderr="CMake not found"
            ),                                                      # configure
        ]

        with patch("subprocess.run", mock_run):
            temp_build_dir.mkdir(exist_ok=True)
            result = install_unsloth_llamacpp(temp_build_dir)

        assert result["success"] is False
        assert "cmake configure failed" in result["error"]
        assert "CMake not found" in result["error"]

    @patch("subprocess.run")
    def test_install_locates_binaries(self, mock_run, temp_build_dir):
        """Should locate built binaries in build/bin directory."""
        mock_run.side_effect = [
            # clone
            MagicMock(returncode=0),
            # fetch
            MagicMock(returncode=0, stdout="", stderr=""),
            # checkout
            MagicMock(returncode=0, stdout="", stderr=""),
            # rev-parse
            MagicMock(returncode=0, stdout="abc123\n", stderr=""),
            # cmake configure
            MagicMock(returncode=0, stdout="", stderr=""),
            # cmake build
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        # Create fake binaries
        build_bin = temp_build_dir / "build" / "bin"
        build_bin.mkdir(parents=True, exist_ok=True)
        (build_bin / "llama-server").touch()
        (build_bin / "rpc-server").touch()

        with patch("subprocess.run", mock_run):
            result = install_unsloth_llamacpp(temp_build_dir)

        assert result["success"] is True
        assert "llama-server" in result["binaries"]
        assert "rpc-server" in result["binaries"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: verify_kimi_binary
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyKimiBinary:
    """Test binary verification."""

    def test_verify_kimi_binary_missing_llama_server(self):
        """Missing llama-server should fail with reason."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_kimi_binary(Path(tmpdir))

        assert result["ok"] is False
        assert "llama-server not found" in result["reason"]

    def test_verify_kimi_binary_missing_rpc_server(self, temp_build_dir):
        """Missing rpc-server should mark has_rpc False."""
        (temp_build_dir / "llama-server").touch()

        result = verify_kimi_binary(temp_build_dir)

        assert result["has_rpc"] is False

    @patch("subprocess.run")
    def test_verify_kimi_binary_no_mmproj_support(self, mock_run,
                                                   temp_build_dir):
        """No --mmproj in help output should fail."""
        (temp_build_dir / "llama-server").touch()
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="usage: llama-server [options]\n  --help",
            stderr="",
        )

        with patch("subprocess.run", mock_run):
            result = verify_kimi_binary(temp_build_dir)

        assert result["ok"] is False
        assert "mmproj" in result["reason"].lower()
        assert result["has_vision"] is False

    @patch("subprocess.run")
    def test_verify_kimi_binary_vision_support_ok(self, mock_run,
                                                   temp_build_dir):
        """--mmproj in help + both binaries present → ok True."""
        (temp_build_dir / "llama-server").touch()
        (temp_build_dir / "rpc-server").touch()

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="usage: llama-server [options]\n  --mmproj model.gguf",
            stderr="",
        )

        with patch("subprocess.run", mock_run):
            result = verify_kimi_binary(temp_build_dir)

        assert result["ok"] is True
        assert result["has_vision"] is True
        assert result["has_rpc"] is True

    @patch("subprocess.run")
    def test_verify_kimi_binary_help_probe_failure(self, mock_run,
                                                    temp_build_dir):
        """Help probe timeout should return error."""
        (temp_build_dir / "llama-server").touch()
        mock_run.side_effect = TimeoutError("Probe timeout")

        result = verify_kimi_binary(temp_build_dir)

        assert result["ok"] is False
        assert "timeout" in result["reason"].lower()

    def test_verify_kimi_binary_windows_exe_detection(self):
        """Should detect .exe binary on any platform."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "llama-server.exe").touch()
            (tmpdir_path / "rpc-server.exe").touch()

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout="--mmproj",
                    stderr="",
                )
                result = verify_kimi_binary(tmpdir_path)

            # Should detect .exe files
            assert result["has_vision"] is True or \
                   "not found" not in result["reason"].lower()
