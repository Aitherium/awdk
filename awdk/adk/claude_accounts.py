"""Claude Code account profile switcher — local credential file management.

This module manages multiple Claude Code (Anthropic) authentication profiles without
needing to re-login. It snapshots the active ~/.claude/.credentials.json file and
lets you switch between saved profiles atomically.

Data stored in ~/.aither/claude-accounts/ are snapshots of the full credentials file,
treated as opaque blobs. No tokens are logged, printed, or transmitted anywhere.

Environment variables (for testing):
- CLAUDE_CREDENTIALS_PATH: Override default ~/.claude/.credentials.json
- AITHER_CLAUDE_ACCOUNTS_DIR: Override default ~/.aither/claude-accounts
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adk.core.logging import get_logger

_log = get_logger("aither_adk.claude_accounts")


class ClaudeAccountError(RuntimeError):
    """Raised on credential or profile errors."""


@dataclass(slots=True)
class AccountLabel:
    """Non-secret metadata for a saved profile."""

    name: str
    email: str = ""
    subscription_type: str = ""
    saved_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "email": self.email,
            "subscription_type": self.subscription_type,
            "saved_at": self.saved_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountLabel:
        return cls(
            name=data.get("name", ""),
            email=data.get("email", ""),
            subscription_type=data.get("subscription_type", ""),
            saved_at=data.get("saved_at", ""),
            expires_at=data.get("expires_at", ""),
        )


@dataclass(slots=True)
class SavedProfile:
    """A saved Claude Code credentials snapshot."""

    label: AccountLabel
    credentials: dict[str, Any]  # Full ~/.claude/.credentials.json

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.to_dict(),
            "credentials": self.credentials,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SavedProfile:
        return cls(
            label=AccountLabel.from_dict(data.get("label", {})),
            credentials=data.get("credentials", {}),
        )


class ClaudeAccountStore:
    """File-backed store for saved Claude Code account profiles.

    Profiles live in ~/.aither/claude-accounts/ (or AITHER_CLAUDE_ACCOUNTS_DIR).
    The active credentials file is ~/.claude/.credentials.json (or
    CLAUDE_CREDENTIALS_PATH).
    """

    def __init__(
        self,
        credentials_path: Path | None = None,
        accounts_dir: Path | None = None,
    ) -> None:
        """Initialize store with optional path overrides (for testing).

        Args:
            credentials_path: Path to Claude Code credentials file.
                Defaults to ~/.claude/.credentials.json or CLAUDE_CREDENTIALS_PATH.
            accounts_dir: Path to saved profiles directory.
                Defaults to ~/.aither/claude-accounts or AITHER_CLAUDE_ACCOUNTS_DIR.
        """
        env_creds = os.environ.get("CLAUDE_CREDENTIALS_PATH", "")
        self.credentials_path = (
            Path(env_creds)
            if env_creds
            else credentials_path
            or (Path.home() / ".claude" / ".credentials.json")
        )

        env_accounts = os.environ.get("AITHER_CLAUDE_ACCOUNTS_DIR", "")
        self.accounts_dir = (
            Path(env_accounts)
            if env_accounts
            else accounts_dir
            or (Path.home() / ".aither" / "claude-accounts")
        )

    def _chmod_0600(self, path: Path) -> None:
        """Best-effort chmod 0600 (owner read/write only).

        On Windows, mode bits are not enforced by the filesystem; we still call
        chmod() for cross-platform consistency. OSError is caught and ignored
        gracefully, allowing the write to proceed even if chmod is unsupported.
        """
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows ignores chmod; carry on (file is still written).
            pass

    def _read_credentials_json(self) -> dict[str, Any]:
        """Read the active credentials file.

        Returns:
            The full JSON object from ~/.claude/.credentials.json.

        Raises:
            ClaudeAccountError: If file missing, malformed, or unreadable.
        """
        if not self.credentials_path.exists():
            raise ClaudeAccountError(
                f"Claude Code credentials file not found: {self.credentials_path}\n"
                "  Please log in to Claude Code first."
            )

        try:
            text = self.credentials_path.read_text(encoding="utf-8")
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ClaudeAccountError(
                f"Claude Code credentials file is malformed: {self.credentials_path}\n"
                f"  JSON error: {e}"
            ) from e
        except OSError as e:
            raise ClaudeAccountError(
                f"Cannot read Claude Code credentials: {e}"
            ) from e

    def _write_credentials_json(self, data: dict[str, Any]) -> None:
        """Atomically write the credentials file (temp + os.replace).

        Args:
            data: Full credentials object to write.

        Raises:
            ClaudeAccountError: If write fails.
        """
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp, then move.
        import tempfile
        temp_path = None  # Initialize before try block to avoid NameError in except.
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.credentials_path.parent,
                delete=False,
                suffix=".tmp",
            ) as f:
                json.dump(data, f, indent=2)
                temp_path = Path(f.name)

            # Move to final location atomically.
            os.replace(temp_path, self.credentials_path)
            self._chmod_0600(self.credentials_path)
        except (OSError, json.JSONDecodeError) as e:
            # Clean up temp file if it was created.
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise ClaudeAccountError(f"Failed to write credentials: {e}") from e

    def _extract_label(self, creds: dict[str, Any]) -> AccountLabel:
        """Extract label metadata from credentials (safe, non-secret).

        Args:
            creds: Full credentials object from ~/.claude/.credentials.json.

        Returns:
            AccountLabel with email, subscription_type, and expires_at if available.
        """
        oauth = creds.get("claudeAiOauth", {})
        return AccountLabel(
            name="",  # Set by caller
            email=oauth.get("email", ""),
            subscription_type=oauth.get("subscriptionType", ""),
            saved_at=datetime.now(timezone.utc).isoformat(),
            expires_at=oauth.get("expiresAt", ""),
        )

    def save_profile(self, name: str, force: bool = False) -> SavedProfile:
        """Save the active credentials as a named profile.

        Args:
            name: Profile name (e.g., "personal", "work").
            force: If True, overwrite existing profile. If False, error if exists.

        Returns:
            The saved profile.

        Raises:
            ClaudeAccountError: If credentials file missing/malformed, profile
                already exists (without --force), or save fails.
        """
        if not name or not name.isidentifier():
            raise ClaudeAccountError(
                f"Profile name must be a valid identifier (alphanumeric + underscore):"
                f" {name!r}"
            )

        # Read active credentials.
        creds = self._read_credentials_json()

        # Build profile.
        label = self._extract_label(creds)
        label.name = name
        profile = SavedProfile(label=label, credentials=creds)

        # Write to disk.
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        profile_path = self.accounts_dir / f"{name}.json"

        # Check if profile already exists.
        if profile_path.exists() and not force:
            raise ClaudeAccountError(
                f"Profile '{name}' already exists. Use --force to overwrite."
            )

        try:
            profile_path.write_text(
                json.dumps(profile.to_dict(), indent=2),
                encoding="utf-8",
            )
            self._chmod_0600(profile_path)
        except OSError as e:
            raise ClaudeAccountError(f"Failed to save profile {name}: {e}") from e

        return profile

    def list_profiles(self) -> list[SavedProfile]:
        """List all saved profiles.

        Returns:
            List of SavedProfile objects, sorted by name.
        """
        if not self.accounts_dir.exists():
            return []

        profiles = []
        for path in sorted(self.accounts_dir.glob("*.json")):
            # Skip backup files.
            if path.name.startswith(".backup-"):
                continue

            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profiles.append(SavedProfile.from_dict(data))
            except (json.JSONDecodeError, OSError, KeyError):
                # Skip malformed profiles.
                _log.warning("Skipping malformed profile", extra={"path": str(path)})

        return profiles

    def get_profile(self, name: str) -> SavedProfile:
        """Get a specific saved profile.

        Args:
            name: Profile name.

        Returns:
            The saved profile.

        Raises:
            ClaudeAccountError: If profile not found.
        """
        profile_path = self.accounts_dir / f"{name}.json"
        if not profile_path.exists():
            raise ClaudeAccountError(f"Profile not found: {name}")

        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            return SavedProfile.from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            raise ClaudeAccountError(f"Failed to read profile {name}: {e}") from e

    def _prune_old_backups(self, max_backups: int = 5) -> None:
        """Delete old backup files, keeping only the most recent N.

        Args:
            max_backups: Maximum number of backup files to retain (default 5).

        Raises:
            ClaudeAccountError: If deletion fails.
        """
        if not self.accounts_dir.exists():
            return

        # Collect backup files (sorted by name, which includes timestamp).
        backups = sorted(self.accounts_dir.glob(".backup-*.json"), reverse=True)

        # Delete all but the newest max_backups.
        if len(backups) > max_backups:
            for old_backup in backups[max_backups:]:
                try:
                    old_backup.unlink()
                except OSError as e:
                    _log.warning(
                        "Failed to delete old backup",
                        extra={"path": str(old_backup), "err": str(e)},
                    )

    def switch_profile(self, name: str) -> None:
        """Switch to a saved profile (atomically).

        Before switching, backs up the current credentials to
        ~/.aither/claude-accounts/.backup-<timestamp>.json.

        Args:
            name: Profile name to switch to.

        Raises:
            ClaudeAccountError: If profile not found or switch fails.
        """
        profile = self.get_profile(name)

        # Back up current credentials (if they exist).
        if self.credentials_path.exists():
            try:
                current_data = self._read_credentials_json()
                timestamp = datetime.now(timezone.utc).isoformat().replace(":", "-")
                backup_path = self.accounts_dir / f".backup-{timestamp}.json"
                backup_path.write_text(
                    json.dumps(current_data, indent=2),
                    encoding="utf-8",
                )
                self._chmod_0600(backup_path)
                # Prune backups beyond the 5 most recent.
                self._prune_old_backups()
            except (OSError, json.JSONDecodeError, ClaudeAccountError) as e:
                # Log backup failure but don't fail the switch.
                _log.warning("Failed to back up current credentials", extra={"err": str(e)})

        # Write new credentials.
        self._write_credentials_json(profile.credentials)

    def remove_profile(self, name: str) -> None:
        """Remove a saved profile.

        Args:
            name: Profile name to remove.

        Raises:
            ClaudeAccountError: If profile not found or delete fails.
        """
        profile_path = self.accounts_dir / f"{name}.json"
        if not profile_path.exists():
            raise ClaudeAccountError(f"Profile not found: {name}")

        try:
            profile_path.unlink()
        except OSError as e:
            raise ClaudeAccountError(f"Failed to remove profile {name}: {e}") from e

    def current_profile_name(self) -> str | None:
        """Identify which saved profile (if any) matches the active credentials.

        Uses a non-secret fingerprint:
        1. Compare email from claudeAiOauth.email if present.
        2. Fall back to sha256 hash of the credentials JSON.

        Returns:
            Profile name if matched, None if no match or credentials missing.
        """
        if not self.credentials_path.exists():
            return None

        try:
            current_creds = self._read_credentials_json()
        except ClaudeAccountError:
            return None

        current_email = current_creds.get("claudeAiOauth", {}).get("email", "")
        saved_profiles = self.list_profiles()

        # Try email match first.
        if current_email:
            for profile in saved_profiles:
                if profile.label.email == current_email:
                    return profile.label.name

        # Fall back to sha256 of credentials. Use 16-char prefix (64 bits of entropy)
        # which is sufficient for collision avoidance in this local-only context.
        import hashlib
        current_json = json.dumps(current_creds, sort_keys=True)
        current_hash = hashlib.sha256(current_json.encode()).hexdigest()[:16]

        for profile in saved_profiles:
            profile_json = json.dumps(profile.credentials, sort_keys=True)
            profile_hash = hashlib.sha256(profile_json.encode()).hexdigest()[:16]
            if current_hash == profile_hash:
                return profile.label.name

        return None


# ── CLI command handlers ──────────────────────────────────────────────────


def cmd_claude_account(args: Any) -> int:
    """Main dispatcher for adk claude-account subcommands."""
    subcommand = getattr(args, "claude_account_command", None)

    try:
        if subcommand == "save":
            return cmd_claude_account_save(args)
        elif subcommand == "list":
            return cmd_claude_account_list(args)
        elif subcommand == "switch":
            return cmd_claude_account_switch(args)
        elif subcommand == "current":
            return cmd_claude_account_current(args)
        elif subcommand == "remove":
            return cmd_claude_account_remove(args)
        elif subcommand == "usage":
            return cmd_claude_account_usage(args)
        else:
            print("Usage: adk claude-account [save|list|switch|current|remove|usage]")
            print()
            print("  save <name>       Save current Claude Code login as a profile")
            print("  list              List all saved profiles")
            print("  switch <name>     Switch to a saved profile")
            print("  current           Show the name of the current profile (if matched)")
            print("  remove <name>     Delete a saved profile")
            print("  usage             Show multi-account usage and scheduling status")
            return 1
    except ClaudeAccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_claude_account_save(args: Any) -> int:
    """adk claude-account save <name> [--force]"""
    store = ClaudeAccountStore()
    name = getattr(args, "name", "").strip()
    force = getattr(args, "force", False)

    if not name:
        print("Error: profile name is required", file=sys.stderr)
        return 1

    try:
        profile = store.save_profile(name, force=force)
        print(f"Saved profile: {profile.label.name}")
        if profile.label.email:
            print(f"  Email: {profile.label.email}")
        if profile.label.subscription_type:
            print(f"  Subscription: {profile.label.subscription_type}")
        if profile.label.expires_at:
            print(f"  Expires: {profile.label.expires_at}")
        return 0
    except ClaudeAccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_claude_account_list(args: Any) -> int:
    """adk claude-account list"""
    store = ClaudeAccountStore()
    current_name = store.current_profile_name()
    profiles = store.list_profiles()

    if not profiles:
        print("No saved profiles. Run 'adk claude-account save <name>' to create one.")
        return 0

    # Table header.
    print()
    print("  Saved Claude Code Profiles")
    print("  " + "=" * 75)
    print(
        f"  {'Name':<20} {'Email':<30} {'Subscription':<12} "
        f"{'Expires':<12}"
    )
    print("  " + "-" * 75)

    # Table rows.
    for profile in profiles:
        marker = " *" if profile.label.name == current_name else "  "
        name = profile.label.name
        email = profile.label.email or ""
        sub = profile.label.subscription_type or ""
        expires = profile.label.expires_at or ""

        # Truncate long fields.
        email = (email[:27] + "...") if len(email) > 30 else email
        sub = (sub[:9] + "...") if len(sub) > 12 else sub
        expires = (expires[:9] + "...") if len(expires) > 12 else expires

        print(f"{marker} {name:<18} {email:<30} {sub:<12} {expires:<12}")

    print("  " + "=" * 75)
    print(f"  * = current active profile")
    print()
    return 0


def cmd_claude_account_switch(args: Any) -> int:
    """adk claude-account switch <name>"""
    store = ClaudeAccountStore()
    name = getattr(args, "name", "").strip()

    if not name:
        print("Error: profile name is required", file=sys.stderr)
        return 1

    try:
        store.switch_profile(name)
        print(f"Switched to profile: {name}")
        return 0
    except ClaudeAccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_claude_account_current(args: Any) -> int:
    """adk claude-account current"""
    store = ClaudeAccountStore()

    current = store.current_profile_name()
    if current:
        print(f"Current profile: {current}")
        # Show a bit more detail.
        profiles = store.list_profiles()
        for p in profiles:
            if p.label.name == current:
                if p.label.email:
                    print(f"  Email: {p.label.email}")
                if p.label.subscription_type:
                    print(f"  Subscription: {p.label.subscription_type}")
                break
        return 0
    else:
        print("Current profile: unknown (not matched to any saved profile)")
        return 0


def cmd_claude_account_remove(args: Any) -> int:
    """adk claude-account remove <name>"""
    store = ClaudeAccountStore()
    name = getattr(args, "name", "").strip()

    if not name:
        print("Error: profile name is required", file=sys.stderr)
        return 1

    try:
        store.remove_profile(name)
        print(f"Removed profile: {name}")
        return 0
    except ClaudeAccountError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_claude_account_usage(args: Any) -> int:
    """adk claude-account usage — show multi-account usage and scheduling status."""
    try:
        from adk.claude_account_usage import UsageMonitor, UsageMonitorError
    except ImportError as e:
        print(f"Error: failed to import usage monitor: {e}", file=sys.stderr)
        return 1

    try:
        monitor = UsageMonitor()
        profiles = monitor.list_profiles()

        if not profiles:
            print("No usage records found. Run 'adk claude spawn' with profiles to create them.")
            return 0

        # Table header.
        print()
        print("  Multi-Account Usage Summary")
        print("  " + "=" * 100)
        print(
            f"  {'Profile':<20} {'Runs':<6} {'Input Tokens':<15} {'Output Tokens':<15} "
            f"{'Total Cost USD':<15} {'Status':<12}"
        )
        print("  " + "-" * 100)

        # Table rows.
        for rec in profiles:
            status = "COOLDOWN" if rec.is_in_cooldown() else "ready"
            print(
                f"  {rec.profile_name:<20} {rec.num_runs:<6} "
                f"{rec.rolling_input_tokens:<15} {rec.rolling_output_tokens:<15} "
                f"${rec.rolling_total_cost_usd:<14.2f} {status:<12}"
            )

        print("  " + "=" * 100)

        # Show cooldown details if any.
        cooldown_profiles = [p for p in profiles if p.is_in_cooldown()]
        if cooldown_profiles:
            print()
            print("  Rate-Limited (Cooldown):")
            for rec in cooldown_profiles:
                from datetime import datetime, timezone
                try:
                    reset_time = datetime.fromisoformat(rec.rate_limit_reset_at)
                    now = datetime.now(timezone.utc)
                    remaining = (reset_time - now).total_seconds()
                    remaining_str = f"{max(0, int(remaining))}s"
                    print(f"    {rec.profile_name:<20} resets in {remaining_str}")
                except (ValueError, TypeError):
                    print(f"    {rec.profile_name:<20} (reset time unknown)")

        print()
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
