"""Tests for backend switch feature — switch between inference backends."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.cli import cmd_backend
from adk.config import load_saved_config, save_saved_config


class TestBackendStatus:
    """Test `adk backend status` command."""

    def test_status_shows_current_backend(self, tmp_path, monkeypatch):
        """Status shows the currently configured backend."""
        cfg_path = tmp_path / "config.json"
        save_saved_config({
            "setup_backend": "ollama",
            "inference_url": "http://localhost:11434/v1",
            "inference_model": "gemma4:e2b",
        }, cfg_path)

        monkeypatch.setenv("HOME", str(tmp_path))
        args = argparse.Namespace(backend_command="status")

        with patch("adk.cli.load_saved_config") as mock_load:
            mock_load.return_value = load_saved_config(cfg_path)
            with patch("builtins.print") as mock_print:
                result = cmd_backend(args)
                assert result == 0
                # Verify status was printed
                calls = [str(c) for c in mock_print.call_args_list]
                output = " ".join(calls)
                assert "ollama" in output.lower() or "Backend" in output

    def test_status_handles_missing_config(self, tmp_path, monkeypatch):
        """Status handles gracefully when no config exists."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))
        args = argparse.Namespace(backend_command="status")

        with patch("adk.cli.load_saved_config") as mock_load:
            mock_load.return_value = {}
            result = cmd_backend(args)
            assert result == 0


class TestBackendSwitch:
    """Test `adk backend switch <backend>` command."""

    def test_switch_ollama_happy_path(self, tmp_path, monkeypatch):
        """Successfully switch to ollama backend."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="ollama"
        )

        # Mock the ollama setup functions
        with patch("adk.cli.load_saved_config") as mock_load:
            with patch("adk.cli.save_saved_config") as mock_save:
                with patch("adk.ollama_setup.ensure_installed") as mock_install:
                    with patch("adk.ollama_setup.ensure_running") as mock_run:
                        with patch("adk.ollama_setup.smoke_test") as mock_smoke:
                            mock_load.return_value = {}
                            mock_install.return_value = True
                            mock_run.return_value = True
                            mock_smoke.return_value = True

                            result = cmd_backend(args)
                            assert result == 0
                            # Verify config was saved
                            assert mock_save.called
                            # First call has the main config
                            saved_data = mock_save.call_args_list[0][0][0]
                            assert saved_data.get("setup_backend") == "ollama"
                            assert "11434" in saved_data.get("inference_url", "")

    def test_switch_ollama_install_failure(self, tmp_path, monkeypatch):
        """Handle ollama install failure gracefully."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="ollama"
        )

        with patch("adk.cli.load_saved_config") as mock_load:
            with patch("adk.ollama_setup.ensure_installed") as mock_install:
                mock_load.return_value = {}
                mock_install.return_value = False

                result = cmd_backend(args)
                assert result == 1

    def test_switch_ollama_smoke_test_failure(self, tmp_path, monkeypatch):
        """Handle smoke test failure gracefully."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="ollama"
        )

        with patch("adk.cli.load_saved_config") as mock_load:
            with patch("adk.ollama_setup.ensure_installed") as mock_install:
                with patch("adk.ollama_setup.ensure_running") as mock_run:
                    with patch("adk.ollama_setup.smoke_test") as mock_smoke:
                        mock_load.return_value = {}
                        mock_install.return_value = True
                        mock_run.return_value = True
                        mock_smoke.return_value = False

                        result = cmd_backend(args)
                        assert result == 1

    def test_switch_llamacpp_happy_path(self, tmp_path, monkeypatch):
        """Successfully switch to llamacpp backend."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="llamacpp"
        )

        # Mock the llamacpp setup
        MockResult = MagicMock()
        MockResult.success = True
        MockResult.port = 8200
        MockResult.quant = "q5"

        with patch("adk.cli.load_saved_config") as mock_load:
            with patch("adk.cli.save_saved_config") as mock_save:
                with patch("adk.llamacpp_setup.install") as mock_install:
                    with patch("adk.llamacpp_setup.status") as mock_status:
                        with patch("adk.llamacpp_setup.smoke_test") as mock_smoke:
                            mock_load.return_value = {}
                            mock_install.return_value = MockResult
                            mock_status.return_value = MagicMock(running=True)
                            mock_smoke.return_value = True

                            result = cmd_backend(args)
                            assert result == 0
                            assert mock_save.called
                            saved_data = mock_save.call_args[0][0]
                            assert saved_data.get("setup_backend") == "llamacpp"

    def test_switch_vllm_happy_path(self, tmp_path, monkeypatch):
        """Successfully switch to vllm backend."""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="vllm"
        )

        with patch("adk.cli.load_saved_config") as mock_load:
            with patch("adk.cli.save_saved_config") as mock_save:
                with patch("adk.setup_cli.cmd_setup") as mock_setup:
                    with patch("adk.llamacpp_setup.status") as mock_status:
                        with patch("adk.llamacpp_setup.smoke_test") as mock_smoke:
                            mock_load.return_value = {}
                            mock_setup.return_value = 0
                            mock_status.return_value = MagicMock(running=True)
                            mock_smoke.return_value = True

                            result = cmd_backend(args)
                            assert result == 0
                            assert mock_save.called
                            saved_data = mock_save.call_args[0][0]
                            assert saved_data.get("setup_backend") == "vllm"

    def test_switch_same_backend_noop(self, tmp_path, monkeypatch):
        """Switching to same backend is a no-op."""
        cfg_path = tmp_path / "config.json"
        save_saved_config({
            "setup_backend": "ollama",
            "inference_url": "http://localhost:11434/v1",
            "inference_model": "gemma4:e2b",
        }, cfg_path)

        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="ollama"
        )

        with patch("adk.cli.load_saved_config") as mock_load:
            mock_load.return_value = load_saved_config(cfg_path)
            result = cmd_backend(args)
            assert result == 0

    def test_switch_invalid_backend(self, tmp_path, monkeypatch):
        """Invalid backend choice is rejected."""
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(
            backend_command="switch",
            target_backend="invalid"
        )

        result = cmd_backend(args)
        assert result == 1


class TestBackendList:
    """Test `adk backend list` command (existing functionality)."""

    def test_list_shows_backends(self, tmp_path, monkeypatch):
        """List command shows available backends."""
        monkeypatch.setenv("HOME", str(tmp_path))

        args = argparse.Namespace(backend_command="list")

        # Mock async list function. Close the coroutine `asyncio.run` is handed
        # (it is created before the call, so the mock discards it live) —
        # otherwise GC later warns "coroutine '_list' was never awaited" in
        # whichever test happens to run next.
        with patch("asyncio.run") as mock_run:
            mock_run.side_effect = lambda coro: coro.close()
            result = cmd_backend(args)
            assert result == 0
            assert mock_run.called


class TestBackendAddAcp:
    """Test `adk backend add acp` — register an external ACP agent as a backend."""

    def test_add_acp_saves_command_and_args(self, tmp_path, monkeypatch):
        """Registers the command, its args and model into the saved config."""
        monkeypatch.setenv("HOME", str(tmp_path))
        args = argparse.Namespace(
            backend_command="add",
            kind="acp",
            command="claude",
            args=["-p", "claude-agent-acp"],
            model="acp",
        )

        with patch("adk.cli.save_saved_config") as mock_save:
            with patch("builtins.print"):
                result = cmd_backend(args)

        assert result == 0
        saved = mock_save.call_args[0][0]
        assert saved["default_backend"] == "acp"
        assert saved["acp_command"] == "claude"
        assert saved["acp_args"] == ["-p", "claude-agent-acp"]
        assert saved["default_model"] == "acp"

    def test_add_acp_without_command_fails(self, tmp_path, monkeypatch):
        """A missing command must not be silently persisted."""
        monkeypatch.setenv("HOME", str(tmp_path))
        args = argparse.Namespace(
            backend_command="add", kind="acp", command="", args=[], model=None
        )

        with patch("adk.cli.save_saved_config") as mock_save:
            with patch("builtins.print"):
                result = cmd_backend(args)

        assert result == 1
        mock_save.assert_not_called()

    def test_add_unknown_kind_fails(self, tmp_path, monkeypatch):
        """An unsupported backend kind is rejected loudly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        args = argparse.Namespace(
            backend_command="add", kind="voodoo", command="x", args=[], model=None
        )

        with patch("adk.cli.save_saved_config") as mock_save:
            with patch("builtins.print"):
                result = cmd_backend(args)

        assert result == 1
        mock_save.assert_not_called()
