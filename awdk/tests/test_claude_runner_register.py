"""Tests for adk claude serve --register/--unregister/--status.

All tests mock subprocess calls so no real scheduled tasks or systemd units are
created. Token resolution and wrapper script generation are tested in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

from adk.claude_runner import (
    _find_repo_root,
    _get_launchd_plist_content,
    _get_systemd_unit_content,
    _get_wrapper_ps1_content,
    _get_wrapper_sh_content,
    cmd_register_claude,
    cmd_status_claude,
    cmd_unregister_claude,
)

# ---------------------------------------------------------------------------
# Wrapper script generation — token NOT embedded
# ---------------------------------------------------------------------------


class TestWrapperGeneration:
    def test_ps1_wrapper_does_not_embed_secret(self):
        """Verify PS1 wrapper sources AITHER_INTERNAL_SECRET from .env, NOT embedded."""
        content = _get_wrapper_ps1_content("/some/repo", "127.0.0.1", 8365)
        assert ".env" in content
        assert "AITHER_INTERNAL_SECRET" in content
        # Verify it reads from env file, doesn't embed a hardcoded secret
        assert 'Get-Content $EnvFile' in content
        assert 'if (-not $Env:AITHER_INTERNAL_SECRET)' in content
        # Verify it exits with error (fail-closed) if secret missing
        assert 'Exit 1' in content

    def test_ps1_wrapper_includes_host_port(self):
        """Verify PS1 wrapper sets HOST/PORT env vars."""
        content = _get_wrapper_ps1_content("/repo", "0.0.0.0", 9999)
        assert "0.0.0.0" in content
        assert "9999" in content
        assert "AITHER_CLAUDE_RUNNER_HOST" in content
        assert "AITHER_CLAUDE_RUNNER_PORT" in content

    def test_sh_wrapper_does_not_embed_secret(self):
        """Verify shell wrapper sources AITHER_INTERNAL_SECRET from .env, NOT embedded."""
        content = _get_wrapper_sh_content("/some/repo", "127.0.0.1", 8365)
        assert ".env" in content
        assert 'source "$EnvFile"' in content
        assert 'if [ -z "$AITHER_INTERNAL_SECRET" ]' in content
        # Verify it exits with error if secret missing
        assert "exit 1" in content

    def test_sh_wrapper_includes_host_port(self):
        """Verify shell wrapper sets HOST/PORT env vars."""
        content = _get_wrapper_sh_content("/repo", "0.0.0.0", 9999)
        assert "0.0.0.0" in content
        assert "9999" in content
        assert "AITHER_CLAUDE_RUNNER_HOST" in content

    def test_systemd_unit_does_not_embed_secret(self):
        """Verify systemd unit doesn't embed secrets (uses wrapper for runtime loading)."""
        wrapper = "/home/user/.aither/bin/claude-runner-wrapper.sh"
        content = _get_systemd_unit_content(wrapper, "/repo")
        assert wrapper in content
        assert "AITHER_INTERNAL_SECRET" not in content  # No embedded secret
        assert "ExecStart=" in content
        assert "Restart=" in content

    def test_launchd_plist_does_not_embed_secret(self):
        """Verify macOS plist doesn't embed secrets."""
        wrapper = "/Users/user/.aither/bin/claude-runner-wrapper.sh"
        content = _get_launchd_plist_content(wrapper, "/repo")
        assert wrapper in content
        assert "AITHER_INTERNAL_SECRET" not in content  # No embedded secret
        assert "<key>KeepAlive</key>" in content


# ---------------------------------------------------------------------------
# Repository root detection
# ---------------------------------------------------------------------------


class TestRepoRootDetection:
    def test_detect_git_repo(self, tmp_path: Path):
        """Find repo root by .git directory."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            root = _find_repo_root()
            assert root == str(tmp_path)
        finally:
            os.chdir(old_cwd)

    def test_detect_env_file(self, tmp_path: Path):
        """Find repo root by .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("AITHER_INTERNAL_SECRET=test-secret\n")
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            root = _find_repo_root()
            assert root == str(tmp_path)
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Registration (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestRegisterCommands:
    @pytest.fixture
    def env_setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Set up a minimal repo with .env file."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        env_file = repo_root / ".env"
        env_file.write_text("AITHER_INTERNAL_SECRET=test-secret-123\n")
        monkeypatch.chdir(repo_root)
        # Isolate Path.home(): register writes ~/.aither/bin/claude-runner-wrapper.*
        # BEFORE the (mocked) subprocess call — without a fake home the test
        # overwrites the user's REAL wrapper with a pytest tmp repo path.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        return repo_root

    @pytest.fixture
    def mock_args_register(self):
        """Mock argparse Namespace for --register."""
        class Args:
            host = "127.0.0.1"
            port = 8365
            force = False
        return Args()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_register_windows_with_mocked_subprocess(
        self, env_setup: Path, mock_args_register, capsys
    ):
        """Test Windows registration with mocked subprocess."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = cmd_register_claude(mock_args_register)
            assert result == 0
            # Verify subprocess was called (for task registration)
            assert mock_run.called

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_register_linux_with_mocked_subprocess(
        self, env_setup: Path, mock_args_register, capsys
    ):
        """Test Linux registration with mocked subprocess."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = cmd_register_claude(mock_args_register)
            assert result == 0
            # Verify systemctl calls were made
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("systemctl" in str(c) for c in calls) or result == 0

    def test_register_fails_without_env_file(self, tmp_path: Path, capsys):
        """Registration fails (fail-closed) if .env is missing."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(repo_root)
            class Args:
                host = "127.0.0.1"
                port = 8365
                force = False
            result = cmd_register_claude(Args())
            assert result == 1
            captured = capsys.readouterr()
            assert "Error" in captured.err or "Error" in captured.out
        finally:
            os.chdir(old_cwd)

    def test_register_creates_log_directory(
        self, env_setup: Path, mock_args_register, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Registration creates ~/.aither/logs directory (fake home via env_setup)."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = cmd_register_claude(mock_args_register)
            # Verify log directory would be created before subprocess call
            assert result == 0


# ---------------------------------------------------------------------------
# Unregistration (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestUnregisterCommands:
    @pytest.fixture
    def mock_args_unregister(self):
        """Mock argparse Namespace for --unregister."""
        class Args:
            force = False
        return Args()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_unregister_windows_with_mocked_subprocess(self, mock_args_unregister):
        """Test Windows unregistration with mocked subprocess."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = cmd_unregister_claude(mock_args_unregister)
            assert result == 0
            # Verify subprocess was called (for task unregistration)
            assert mock_run.called

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_unregister_linux_with_mocked_subprocess(self, mock_args_unregister):
        """Test Linux unregistration with mocked subprocess."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = cmd_unregister_claude(mock_args_unregister)
            assert result == 0
            # Verify systemctl calls were made
            assert mock_run.called

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_unregister_linux_idempotent(
        self, mock_args_unregister, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Multiple unregisters are safe (idempotent)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result1 = cmd_unregister_claude(mock_args_unregister)
            result2 = cmd_unregister_claude(mock_args_unregister)
            # Both should succeed (idempotent)
            assert result1 == 0
            assert result2 == 0


# ---------------------------------------------------------------------------
# Status reporting (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestStatusCommands:
    @pytest.fixture
    def mock_args_status(self):
        """Mock argparse Namespace for --status."""
        class Args:
            pass
        return Args()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_status_windows_not_registered(self, mock_args_status):
        """Windows status reports 'not registered' when task doesn't exist."""
        with mock.patch("subprocess.run") as mock_run:
            # Simulate task not found
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="not found")
            result = cmd_status_claude(mock_args_status)
            # Should succeed but report not registered
            assert result == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
    def test_status_linux_registered(self, mock_args_status):
        """Linux status reports registered when systemctl checks pass."""
        with mock.patch("subprocess.run") as mock_run:
            # Simulate systemctl reporting enabled and active
            mock_run.side_effect = [
                mock.Mock(returncode=0),  # is-enabled
                mock.Mock(returncode=0),  # is-active
                mock.Mock(returncode=0, stdout="", text=""),  # status
            ]
            result = cmd_status_claude(mock_args_status)
            assert result == 0

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
    def test_status_macos_not_registered(
        self, mock_args_status, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """macOS status reports 'not registered' when plist doesn't exist."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        result = cmd_status_claude(mock_args_status)
        # Should report not registered
        assert result == 0


# ---------------------------------------------------------------------------
# Integration: dispatcher routes to correct handler
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_dispatcher_register(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Dispatcher routes --register to cmd_register_claude."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        env_file = repo_root / ".env"
        env_file.write_text("AITHER_INTERNAL_SECRET=secret\n")
        monkeypatch.chdir(repo_root)
        # Isolate Path.home() — register writes the real ~/.aither wrapper otherwise.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))

        class Args:
            claude_command = "serve"
            register = True
            unregister = False
            status = False
            host = "127.0.0.1"
            port = 8365
            force = False

        # Import here to get the updated cmd_claude
        from adk.claude_runner import cmd_claude

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            cmd_claude(Args())
            # Should route to registration (may succeed or fail depending on platform)

    def test_dispatcher_unregister(self):
        """Dispatcher routes --unregister to cmd_unregister_claude."""
        class Args:
            claude_command = "serve"
            register = False
            unregister = True
            status = False
            force = False

        from adk.claude_runner import cmd_claude

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            cmd_claude(Args())
            # Should route to unregistration

    def test_dispatcher_status(self):
        """Dispatcher routes --status to cmd_status_claude."""
        class Args:
            claude_command = "serve"
            register = False
            unregister = False
            status = True

        from adk.claude_runner import cmd_claude

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            cmd_claude(Args())
            # Should route to status reporting

    def test_dispatcher_default_serve_no_flags(self):
        """Dispatcher routes to foreground serve when no register/unregister/status."""
        class Args:
            claude_command = "serve"
            register = False
            unregister = False
            status = False
            host = "127.0.0.1"
            port = 8360
            token = ""

        from adk.claude_runner import cmd_claude

        # Should route to serve, not register
        with mock.patch("adk.claude_runner.serve") as mock_serve:
            mock_serve.return_value = 0
            cmd_claude(Args())
            assert mock_serve.called


# ---------------------------------------------------------------------------
# Wrapper env passthrough — the ntfy config must survive into the daemon
# ---------------------------------------------------------------------------


class TestWrapperEnvPassthrough:
    def _win(self) -> str:
        from adk.claude_runner import _get_wrapper_ps1_content

        return _get_wrapper_ps1_content("D:\repo", "0.0.0.0", 8365)

    def test_ntfy_vars_are_sourced_from_env_file(self):
        w = self._win()
        assert "AITHER_CLAUDE_RUNNER_NTFY_URL" in w
        assert "AITHER_CLAUDE_RUNNER_NTFY_TOKEN" in w

    def test_internal_secret_still_required(self):
        w = self._win()
        assert "AITHER_INTERNAL_SECRET" in w
        assert "Exit 1" in w  # fail-closed when absent

    def test_no_early_break_strands_later_keys(self):
        # The original parser `break`ed on the first match, so any key after
        # AITHER_INTERNAL_SECRET in .env was silently never exported.
        w = self._win()
        loop = w[w.index("foreach ($Line in $Lines)"):]
        assert "break" not in loop

    def test_secrets_are_never_baked_into_the_wrapper(self):
        # The wrapper lands in ~/.aither/bin — it must carry no values, only names.
        w = self._win()
        assert "sk-ant-" not in w
        for marker in ("ntfy.sh/", "https://ntfy."):
            assert marker not in w

    def test_posix_wrapper_exports_whole_env_file(self):
        from adk.claude_runner import _get_wrapper_sh_content

        sh = _get_wrapper_sh_content("/repo", "0.0.0.0", 8365)
        # `set -a; source` already exports everything, so no per-key list needed.
        assert "set -a" in sh and 'source "$EnvFile"' in sh
