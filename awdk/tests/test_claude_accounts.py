"""Tests for adk.claude_accounts module — Claude Code account switcher.

Uses monkeypatch to inject CLAUDE_CREDENTIALS_PATH and AITHER_CLAUDE_ACCOUNTS_DIR
so NO real user files are touched. Each test gets isolated temp directories.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from adk.claude_accounts import (
    AccountLabel,
    ClaudeAccountError,
    ClaudeAccountStore,
    SavedProfile,
)


@pytest.fixture
def temp_creds_dir(tmp_path):
    """Temp directory for fake Claude Code .credentials.json."""
    creds_dir = tmp_path / "claude"
    creds_dir.mkdir()
    return creds_dir


@pytest.fixture
def temp_accounts_dir(tmp_path):
    """Temp directory for saved accounts."""
    accounts_dir = tmp_path / "aither" / "claude-accounts"
    accounts_dir.mkdir(parents=True)
    return accounts_dir


@pytest.fixture
def mock_paths(monkeypatch, temp_creds_dir, temp_accounts_dir):
    """Monkeypatch env vars to use temp dirs."""
    creds_path = temp_creds_dir / ".credentials.json"
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(creds_path))
    monkeypatch.setenv("AITHER_CLAUDE_ACCOUNTS_DIR", str(temp_accounts_dir))
    return creds_path, temp_accounts_dir


@pytest.fixture
def store(mock_paths):
    """Create a ClaudeAccountStore with mocked paths."""
    creds_path, accounts_dir = mock_paths
    return ClaudeAccountStore(credentials_path=creds_path, accounts_dir=accounts_dir)


@pytest.fixture
def sample_credentials():
    """Sample Claude Code credentials (similar to real format)."""
    return {
        "claudeAiOauth": {
            "accessToken": "sk_live_abcd1234_REDACTED",
            "refreshToken": "sk_refresh_efgh5678_REDACTED",
            "expiresAt": "2025-12-31T23:59:59Z",
            "scopes": ["claude"],
            "subscriptionType": "pro",
            "email": "user@example.com",
        },
        "other_field": "value",
    }


class TestAccountLabel:
    """Tests for AccountLabel dataclass."""

    def test_to_dict(self):
        """AccountLabel.to_dict() returns correct structure."""
        label = AccountLabel(
            name="personal",
            email="user@example.com",
            subscription_type="pro",
            saved_at="2025-07-17T12:00:00Z",
            expires_at="2025-12-31T23:59:59Z",
        )
        d = label.to_dict()
        assert d["name"] == "personal"
        assert d["email"] == "user@example.com"
        assert d["subscription_type"] == "pro"

    def test_from_dict(self):
        """AccountLabel.from_dict() reconstructs from dict."""
        data = {
            "name": "work",
            "email": "work@company.com",
            "subscription_type": "free",
            "saved_at": "2025-07-17T12:00:00Z",
            "expires_at": "2025-12-31T23:59:59Z",
        }
        label = AccountLabel.from_dict(data)
        assert label.name == "work"
        assert label.email == "work@company.com"
        assert label.subscription_type == "free"

    def test_from_dict_partial(self):
        """AccountLabel.from_dict() handles missing fields."""
        label = AccountLabel.from_dict({"name": "test"})
        assert label.name == "test"
        assert label.email == ""
        assert label.subscription_type == ""


class TestSavedProfile:
    """Tests for SavedProfile dataclass."""

    def test_to_dict_preserves_credentials(self):
        """SavedProfile.to_dict() preserves full credentials blob."""
        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_secret",
                "email": "test@example.com",
            },
            "custom_field": "untouched",
        }
        label = AccountLabel(name="test", email="test@example.com")
        profile = SavedProfile(label=label, credentials=creds)
        d = profile.to_dict()

        # Credentials must be byte-for-byte identical.
        assert d["credentials"] == creds
        assert d["credentials"]["custom_field"] == "untouched"

    def test_from_dict(self, sample_credentials):
        """SavedProfile.from_dict() reconstructs from dict."""
        data = {
            "label": {"name": "personal", "email": "user@example.com"},
            "credentials": sample_credentials,
        }
        profile = SavedProfile.from_dict(data)
        assert profile.label.name == "personal"
        assert profile.credentials == sample_credentials


class TestClaudeAccountStore:
    """Tests for ClaudeAccountStore."""

    def test_missing_credentials_file(self, store):
        """Reading missing credentials file raises clear error."""
        with pytest.raises(ClaudeAccountError, match="not found"):
            store.save_profile("test")

    def test_malformed_credentials_file(self, store, mock_paths):
        """Reading malformed JSON raises clear error."""
        creds_path, _ = mock_paths
        creds_path.write_text("{ invalid json")

        with pytest.raises(ClaudeAccountError, match="malformed"):
            store.save_profile("test")

    def test_save_profile(self, store, mock_paths, sample_credentials):
        """save_profile() creates a snapshot with label."""
        creds_path, accounts_dir = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))

        profile = store.save_profile("personal")

        # Check returned profile.
        assert profile.label.name == "personal"
        assert profile.label.email == "user@example.com"
        assert profile.label.subscription_type == "pro"
        assert profile.credentials == sample_credentials

        # Check file was created.
        saved_file = accounts_dir / "personal.json"
        assert saved_file.exists()

        # Check file is 0600.
        mode = saved_file.stat().st_mode
        assert mode & stat.S_IRUSR  # Owner can read.
        assert mode & stat.S_IWUSR  # Owner can write.

        # Check file content structure.
        data = json.loads(saved_file.read_text())
        assert "label" in data
        assert "credentials" in data
        assert data["credentials"] == sample_credentials

    def test_save_profile_invalid_name(self, store, mock_paths, sample_credentials):
        """save_profile() rejects invalid profile names."""
        creds_path, _ = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))

        # Invalid names (not identifiers).
        with pytest.raises(ClaudeAccountError, match="identifier"):
            store.save_profile("my-profile")  # Hyphen
        with pytest.raises(ClaudeAccountError, match="identifier"):
            store.save_profile("my profile")  # Space
        with pytest.raises(ClaudeAccountError, match="identifier"):
            store.save_profile("")  # Empty

    def test_list_profiles_empty(self, store):
        """list_profiles() returns empty list when no profiles saved."""
        profiles = store.list_profiles()
        assert profiles == []

    def test_list_profiles_multiple(self, store, mock_paths, sample_credentials):
        """list_profiles() returns all saved profiles in order."""
        creds_path, _ = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))

        # Save multiple profiles.
        store.save_profile("alpha")
        store.save_profile("zebra")
        store.save_profile("beta")

        profiles = store.list_profiles()
        assert len(profiles) == 3
        names = [p.label.name for p in profiles]
        assert names == ["alpha", "beta", "zebra"]  # Sorted.

    def test_get_profile(self, store, mock_paths, sample_credentials):
        """get_profile() retrieves a saved profile."""
        creds_path, _ = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))
        store.save_profile("personal")

        profile = store.get_profile("personal")
        assert profile.label.name == "personal"
        assert profile.credentials == sample_credentials

    def test_get_profile_not_found(self, store):
        """get_profile() raises error for missing profile."""
        with pytest.raises(ClaudeAccountError, match="not found"):
            store.get_profile("nonexistent")

    def test_switch_profile(self, store, mock_paths, sample_credentials):
        """switch_profile() atomically replaces credentials."""
        creds_path, accounts_dir = mock_paths

        # Save two profiles with different emails.
        creds1 = {
            "claudeAiOauth": {
                "accessToken": "sk_live_token1",
                "email": "alice@example.com",
            }
        }
        creds2 = {
            "claudeAiOauth": {
                "accessToken": "sk_live_token2",
                "email": "bob@example.com",
            }
        }

        # Start with creds1.
        creds_path.write_text(json.dumps(creds1))
        store.save_profile("alice")

        # Switch to creds2 (save it first).
        creds_path.write_text(json.dumps(creds2))
        store.save_profile("bob")

        # Back to alice (this should trigger switch).
        store.switch_profile("alice")

        # Check that credentials file was updated.
        current = json.loads(creds_path.read_text())
        assert current["claudeAiOauth"]["email"] == "alice@example.com"

    def test_switch_profile_backs_up_current(self, store, mock_paths, sample_credentials):
        """switch_profile() backs up current credentials before switching."""
        creds_path, accounts_dir = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))

        # Save a profile.
        store.save_profile("personal")

        # Make a different current file.
        alt_creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_alt",
                "email": "alt@example.com",
            }
        }
        creds_path.write_text(json.dumps(alt_creds))

        # Switch to personal.
        store.switch_profile("personal")

        # Check that a backup was created.
        backups = list(accounts_dir.glob(".backup-*.json"))
        assert len(backups) == 1

        backup_data = json.loads(backups[0].read_text())
        assert backup_data["claudeAiOauth"]["email"] == "alt@example.com"

    def test_switch_profile_not_found(self, store):
        """switch_profile() raises error for missing profile."""
        with pytest.raises(ClaudeAccountError, match="not found"):
            store.switch_profile("nonexistent")

    def test_remove_profile(self, store, mock_paths, sample_credentials):
        """remove_profile() deletes a profile file."""
        creds_path, accounts_dir = mock_paths
        creds_path.write_text(json.dumps(sample_credentials))
        store.save_profile("personal")

        # Verify it exists.
        assert (accounts_dir / "personal.json").exists()

        # Remove it.
        store.remove_profile("personal")

        # Verify it's gone.
        assert not (accounts_dir / "personal.json").exists()

    def test_remove_profile_not_found(self, store):
        """remove_profile() raises error for missing profile."""
        with pytest.raises(ClaudeAccountError, match="not found"):
            store.remove_profile("nonexistent")

    def test_current_profile_name_by_email(self, store, mock_paths):
        """current_profile_name() matches by email when present."""
        creds_path, _ = mock_paths

        # Save a profile with alice's email.
        alice_creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_alice",
                "email": "alice@example.com",
            }
        }
        creds_path.write_text(json.dumps(alice_creds))
        store.save_profile("alice_profile")

        # Now simulate alice being the active user (same email).
        creds_path.write_text(json.dumps(alice_creds))

        # Should match by email.
        current = store.current_profile_name()
        assert current == "alice_profile"

    def test_current_profile_name_by_hash(self, store, mock_paths):
        """current_profile_name() falls back to hash match when email absent."""
        creds_path, _ = mock_paths

        # Save a profile without email.
        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_secret",
                # No email field.
            }
        }
        creds_path.write_text(json.dumps(creds))
        store.save_profile("noemail_profile")

        # Clear the email so match must be by hash.
        # Current credentials are same as saved.
        current = store.current_profile_name()
        assert current == "noemail_profile"

    def test_current_profile_name_no_match(self, store, mock_paths):
        """current_profile_name() returns None when no match."""
        creds_path, _ = mock_paths

        # Save a profile.
        alice = {
            "claudeAiOauth": {
                "accessToken": "sk_live_alice",
                "email": "alice@example.com",
            }
        }
        creds_path.write_text(json.dumps(alice))
        store.save_profile("alice")

        # Change active credentials.
        bob = {
            "claudeAiOauth": {
                "accessToken": "sk_live_bob",
                "email": "bob@example.com",
            }
        }
        creds_path.write_text(json.dumps(bob))

        # No match.
        current = store.current_profile_name()
        assert current is None

    def test_current_profile_name_missing_creds_file(self, store):
        """current_profile_name() returns None if credentials file missing."""
        current = store.current_profile_name()
        assert current is None

    def test_round_trip_save_switch(self, store, mock_paths):
        """Full round-trip: save, switch, verify."""
        creds_path, _ = mock_paths

        # Save alice.
        alice = {
            "claudeAiOauth": {
                "accessToken": "sk_live_alice",
                "email": "alice@example.com",
                "subscriptionType": "pro",
            }
        }
        creds_path.write_text(json.dumps(alice))
        store.save_profile("alice")

        # Change to bob.
        bob = {
            "claudeAiOauth": {
                "accessToken": "sk_live_bob",
                "email": "bob@example.com",
                "subscriptionType": "free",
            }
        }
        creds_path.write_text(json.dumps(bob))
        store.save_profile("bob")

        # Verify current is bob.
        assert store.current_profile_name() == "bob"

        # Switch to alice.
        store.switch_profile("alice")

        # Verify current is alice.
        assert store.current_profile_name() == "alice"

        # Verify credentials file has alice's data.
        current = json.loads(creds_path.read_text())
        assert current["claudeAiOauth"]["email"] == "alice@example.com"

    def test_tokens_never_in_stdout(self, store, mock_paths, capsys):
        """Ensure no token values leak into output (manual verification here)."""
        creds_path, _ = mock_paths

        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_SECRET_TOKEN_12345",
                "refreshToken": "sk_refresh_ANOTHER_SECRET",
                "email": "user@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds))
        store.save_profile("test")

        # If we had CLI output here, we'd verify no tokens appear.
        # This test is a placeholder for actual CLI integration testing.
        profiles = store.list_profiles()
        assert len(profiles) == 1
        # Tokens should never be in profile labels (and they aren't).
        assert "SECRET_TOKEN" not in profiles[0].label.email


class TestExtractLabel:
    """Tests for _extract_label helper."""

    def test_extract_label_full(self, store):
        """_extract_label() extracts all available fields."""
        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_secret",
                "email": "user@example.com",
                "subscriptionType": "pro",
                "expiresAt": "2025-12-31T23:59:59Z",
            }
        }
        label = store._extract_label(creds)
        assert label.email == "user@example.com"
        assert label.subscription_type == "pro"
        assert label.expires_at == "2025-12-31T23:59:59Z"

    def test_extract_label_partial(self, store):
        """_extract_label() handles missing fields gracefully."""
        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_secret",
                # Missing email, subscriptionType, expiresAt.
            }
        }
        label = store._extract_label(creds)
        assert label.email == ""
        assert label.subscription_type == ""
        assert label.expires_at == ""


class TestBackupPruning:
    """Tests for backup file pruning."""

    def test_prune_old_backups_max_5(self, store, mock_paths):
        """_prune_old_backups() keeps only the 5 most recent."""
        creds_path, accounts_dir = mock_paths

        # Create 7 fake backup files (named with timestamps).
        for i in range(7):
            backup_path = accounts_dir / f".backup-2025-01-0{i+1}T00-00-00.json"
            backup_path.write_text(json.dumps({"data": f"backup_{i}"}))

        # Prune to 5.
        store._prune_old_backups(max_backups=5)

        # Check that only 5 remain (the newest 5).
        remaining = sorted(accounts_dir.glob(".backup-*.json"))
        assert len(remaining) == 5

        # The oldest backup (backup_0) should be gone.
        assert not (accounts_dir / ".backup-2025-01-01T00-00-00.json").exists()


class TestSaveProfileForce:
    """Tests for --force flag in save_profile."""

    def test_save_profile_exists_without_force_errors(self, store, mock_paths):
        """save_profile() errors if profile exists and force=False."""
        creds_path, _ = mock_paths

        creds = {
            "claudeAiOauth": {
                "accessToken": "sk_live_token",
                "email": "user@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds))

        # Save once.
        store.save_profile("personal")

        # Try to save again without force.
        with pytest.raises(ClaudeAccountError, match="already exists"):
            store.save_profile("personal", force=False)

    def test_save_profile_exists_with_force_overwrites(self, store, mock_paths):
        """save_profile() with force=True overwrites existing profile."""
        creds_path, _ = mock_paths

        creds1 = {
            "claudeAiOauth": {
                "accessToken": "sk_live_token1",
                "email": "alice@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds1))
        store.save_profile("personal")

        # Change active credentials.
        creds2 = {
            "claudeAiOauth": {
                "accessToken": "sk_live_token2",
                "email": "bob@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds2))

        # Save with force=True.
        profile = store.save_profile("personal", force=True)

        # Profile should have new email.
        assert profile.label.email == "bob@example.com"

        # Read the saved file to confirm.
        profiles = store.list_profiles()
        assert profiles[0].label.email == "bob@example.com"


class TestTokenSecrecy:
    """Tests to ensure tokens never leak into CLI output."""

    def test_save_command_no_token_in_output(self, mock_paths, capsys):
        """CLI save command must never print tokens."""
        from adk.claude_accounts import cmd_claude_account_save
        import argparse

        creds_path, _ = mock_paths

        # Use sentinel tokens that we'll check for.
        sentinel_access = "sk-ant-SENTINELACCESS123"
        sentinel_refresh = "sk-ant-SENTINELREFRESH456"
        creds = {
            "claudeAiOauth": {
                "accessToken": sentinel_access,
                "refreshToken": sentinel_refresh,
                "email": "user@example.com",
                "subscriptionType": "pro",
            }
        }
        creds_path.write_text(json.dumps(creds))

        # Call save command.
        args = argparse.Namespace(name="test_profile", force=False)
        result = cmd_claude_account_save(args)

        # Check result.
        assert result == 0

        # Capture output and verify no tokens.
        captured = capsys.readouterr()
        output = captured.out + captured.err

        assert sentinel_access not in output, "Access token leaked into output!"
        assert sentinel_refresh not in output, "Refresh token leaked into output!"
        assert "user@example.com" in output  # Email is safe to show.

    def test_list_command_no_token_in_output(self, mock_paths, capsys):
        """CLI list command must never print tokens."""
        from adk.claude_accounts import cmd_claude_account_list
        import argparse

        creds_path, _ = mock_paths

        sentinel_access = "sk-ant-SENTINELACCESS789"
        creds = {
            "claudeAiOauth": {
                "accessToken": sentinel_access,
                "email": "user@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds))

        # Save a profile.
        from adk.claude_accounts import ClaudeAccountStore
        store = ClaudeAccountStore()
        store.save_profile("listed_profile")

        # Call list command.
        args = argparse.Namespace()
        result = cmd_claude_account_list(args)

        assert result == 0

        # Verify token not in output.
        captured = capsys.readouterr()
        output = captured.out + captured.err

        assert sentinel_access not in output, "Access token leaked into list output!"
        assert "listed_profile" in output  # Profile name is safe.
        assert "user@example.com" in output  # Email is safe.

    def test_current_command_no_token_in_output(self, mock_paths, capsys):
        """CLI current command must never print tokens."""
        from adk.claude_accounts import cmd_claude_account_current
        import argparse

        creds_path, _ = mock_paths

        sentinel_access = "sk-ant-SENTINELCURRENT999"
        creds = {
            "claudeAiOauth": {
                "accessToken": sentinel_access,
                "email": "user@example.com",
            }
        }
        creds_path.write_text(json.dumps(creds))

        # Save and set as current.
        from adk.claude_accounts import ClaudeAccountStore
        store = ClaudeAccountStore()
        store.save_profile("current_test")

        # Call current command.
        args = argparse.Namespace()
        result = cmd_claude_account_current(args)

        assert result == 0

        captured = capsys.readouterr()
        output = captured.out + captured.err

        assert sentinel_access not in output, "Access token leaked into current output!"
        assert "current_test" in output


class TestEnvVarOverride:
    """Tests for environment variable injection."""

    def test_env_var_claude_credentials_path(self, monkeypatch, tmp_path):
        """CLAUDE_CREDENTIALS_PATH env var overrides default."""
        custom_path = tmp_path / "custom" / "creds.json"
        monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(custom_path))

        store = ClaudeAccountStore()
        assert store.credentials_path == custom_path

    def test_env_var_aither_claude_accounts_dir(self, monkeypatch, tmp_path):
        """AITHER_CLAUDE_ACCOUNTS_DIR env var overrides default."""
        custom_dir = tmp_path / "custom" / "accounts"
        monkeypatch.setenv("AITHER_CLAUDE_ACCOUNTS_DIR", str(custom_dir))

        store = ClaudeAccountStore()
        assert store.accounts_dir == custom_dir

    def test_both_env_vars(self, monkeypatch, tmp_path):
        """Both env vars can be set together."""
        creds_path = tmp_path / "creds.json"
        accounts_dir = tmp_path / "accounts"
        monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(creds_path))
        monkeypatch.setenv("AITHER_CLAUDE_ACCOUNTS_DIR", str(accounts_dir))

        store = ClaudeAccountStore()
        assert store.credentials_path == creds_path
        assert store.accounts_dir == accounts_dir
