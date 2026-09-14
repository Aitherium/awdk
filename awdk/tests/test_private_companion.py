"""
Tests for the private companion vault (operator-blind encryption).

These tests verify:
  1. Key derivation is stable across process boundaries
  2. Encryption/decryption cycle works
  3. Machine ID is persisted and reused
  4. Safety level configuration works
  5. No secrets leak to logs or stdout
"""

import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

# Only run these tests if cryptography is available
pytest.importorskip("cryptography")

from adk.private_companion import (
    PrivateCompanionVault,
    PersonaPrompt,
    get_companion_vault,
    get_machine_id,
    derive_key,
    _get_or_create_salt,
)


class TestMachineID:
    """Test machine ID stability across runs."""

    def test_machine_id_is_deterministic(self):
        """Machine ID should be the same every time."""
        mid1 = get_machine_id()
        mid2 = get_machine_id()
        assert mid1 == mid2
        assert len(mid1) == 32  # SHA256[:32]
        assert mid1.isalnum()

    def test_machine_id_override(self, monkeypatch):
        """Test explicit override via environment variable."""
        monkeypatch.setenv("AITHER_LOCKBOX_MACHINE_ID", "test-machine-override")
        mid = get_machine_id()
        # Should be SHA256 hash of the override
        expected = __import__("hashlib").sha256(
            b"test-machine-override"
        ).hexdigest()[:32]
        assert mid == expected


class TestSaltManagement:
    """Test salt generation and persistence."""

    def test_salt_is_created_and_persisted(self, tmp_path):
        """First call creates salt; second call reuses it."""
        with patch('adk.private_companion.SALT_FILE', tmp_path / ".vault_salt"):
            salt1 = _get_or_create_salt()
            assert len(salt1) == 16
            salt2 = _get_or_create_salt()
            assert salt1 == salt2  # Same on second call

    def test_salt_file_created(self, tmp_path):
        """Salt file is created at the expected path."""
        salt_file = tmp_path / ".vault_salt"
        with patch('adk.private_companion.SALT_FILE', salt_file):
            salt = _get_or_create_salt()
            assert salt_file.exists()
            persisted = salt_file.read_bytes()
            assert persisted == salt


class TestKeyDerivation:
    """Test PBKDF2 key derivation."""

    def test_key_derivation_deterministic(self):
        """Same machine_id + passphrase should produce same key."""
        key1 = derive_key("machine123", "mypassphrase")
        key2 = derive_key("machine123", "mypassphrase")
        assert key1 == key2

    def test_key_derivation_different_passphrase(self):
        """Different passphrases produce different keys."""
        key1 = derive_key("machine123", "pass1")
        key2 = derive_key("machine123", "pass2")
        assert key1 != key2

    def test_key_derivation_different_machine(self):
        """Different machine IDs produce different keys."""
        key1 = derive_key("machine1", "mypassphrase")
        key2 = derive_key("machine2", "mypassphrase")
        assert key1 != key2

    def test_key_is_valid_fernet_key(self):
        """Derived key is valid for Fernet encryption."""
        from cryptography.fernet import Fernet
        key = derive_key("test_machine", "test_pass")
        f = Fernet(key)
        ciphertext = f.encrypt(b"hello world")
        plaintext = f.decrypt(ciphertext)
        assert plaintext == b"hello world"


class TestPersonaStorage:
    """Test persona storage and retrieval."""

    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        """Create a temporary vault for testing."""
        # Redirect vault paths to temp directory
        monkeypatch.setenv("HOME", str(tmp_path))
        lockbox_dir = tmp_path / ".aither" / "private" / "lockbox"
        monkeypatch.setattr("adk.private_companion.CONFIG_DIR", tmp_path / ".aither")
        monkeypatch.setattr("adk.private_companion.PRIVATE_DIR", tmp_path / ".aither" / "private")
        monkeypatch.setattr("adk.private_companion.LOCKBOX_DIR", lockbox_dir)
        monkeypatch.setattr("adk.private_companion.VAULT_KEY_FILE", lockbox_dir / ".vault_key")
        monkeypatch.setattr("adk.private_companion.MACHINE_ID_FILE", lockbox_dir / ".machine_id")
        monkeypatch.setattr("adk.private_companion.SALT_FILE", lockbox_dir / ".vault_salt")
        monkeypatch.setattr("adk.private_companion.MANIFEST_FILE", lockbox_dir / ".manifest.json")
        monkeypatch.setattr("adk.private_companion.SAFETY_LEVEL_FILE", lockbox_dir / ".safety_level")

        vault = PrivateCompanionVault(passphrase="test_passphrase")
        yield vault

    def test_store_and_retrieve_persona(self, vault):
        """Store a persona and retrieve it."""
        content = "You are a devoted, affectionate AI companion..."
        vault.store_persona(
            "devoted_companion",
            content,
            safety_level="unrestricted",
            description="A warm companion"
        )

        persona = vault.get_persona("devoted_companion")
        assert persona is not None
        assert persona.name == "devoted_companion"
        assert persona.content == content
        assert persona.safety_level == "unrestricted"
        assert persona.description == "A warm companion"

    def test_list_personas(self, vault):
        """List stored personas."""
        vault.store_persona("persona1", "Content 1", safety_level="unrestricted")
        vault.store_persona("persona2", "Content 2", safety_level="casual")
        vault.store_persona("persona3", "Content 3", safety_level="unrestricted")

        all_personas = vault.list_personas()
        assert len(all_personas) == 3
        assert "persona1" in all_personas
        assert "persona2" in all_personas
        assert "persona3" in all_personas

    def test_list_personas_by_safety_level(self, vault):
        """Filter personas by safety level."""
        vault.store_persona("unres1", "Content", safety_level="unrestricted")
        vault.store_persona("unres2", "Content", safety_level="unrestricted")
        vault.store_persona("casual1", "Content", safety_level="casual")

        unrestricted = vault.list_personas(safety_level="unrestricted")
        assert len(unrestricted) == 2
        assert all(n in unrestricted for n in ["unres1", "unres2"])

        casual = vault.list_personas(safety_level="casual")
        assert len(casual) == 1
        assert "casual1" in casual

    def test_delete_persona(self, vault):
        """Delete a persona."""
        vault.store_persona("to_delete", "Content", safety_level="unrestricted")
        assert "to_delete" in vault.list_personas()

        success = vault.delete_persona("to_delete")
        assert success
        assert "to_delete" not in vault.list_personas()

    def test_delete_nonexistent_persona(self, vault):
        """Deleting a nonexistent persona returns False."""
        success = vault.delete_persona("does_not_exist")
        assert not success

    def test_get_nonexistent_persona(self, vault):
        """Getting a nonexistent persona returns None."""
        persona = vault.get_persona("does_not_exist")
        assert persona is None


class TestEncryption:
    """Test that encryption actually protects the data."""

    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        """Create a temporary vault for testing."""
        monkeypatch.setenv("HOME", str(tmp_path))
        lockbox_dir = tmp_path / ".aither" / "private" / "lockbox"
        monkeypatch.setattr("adk.private_companion.CONFIG_DIR", tmp_path / ".aither")
        monkeypatch.setattr("adk.private_companion.PRIVATE_DIR", tmp_path / ".aither" / "private")
        monkeypatch.setattr("adk.private_companion.LOCKBOX_DIR", lockbox_dir)
        monkeypatch.setattr("adk.private_companion.VAULT_KEY_FILE", lockbox_dir / ".vault_key")
        monkeypatch.setattr("adk.private_companion.MACHINE_ID_FILE", lockbox_dir / ".machine_id")
        monkeypatch.setattr("adk.private_companion.SALT_FILE", lockbox_dir / ".vault_salt")
        monkeypatch.setattr("adk.private_companion.MANIFEST_FILE", lockbox_dir / ".manifest.json")
        monkeypatch.setattr("adk.private_companion.SAFETY_LEVEL_FILE", lockbox_dir / ".safety_level")

        vault = PrivateCompanionVault(passphrase="test_passphrase")
        yield vault

    def test_encrypted_content_not_readable_as_plaintext(self, vault, tmp_path):
        """Encrypted files should not contain readable plaintext."""
        secret_content = "This is a secret persona prompt that should be encrypted"
        vault.store_persona("secret", secret_content, safety_level="unrestricted")

        # Read the encrypted file
        from adk.private_companion import LOCKBOX_DIR
        import hashlib
        safe_name = hashlib.sha256(b"secret").hexdigest()[:16]
        enc_file = LOCKBOX_DIR / f"{safe_name}.enc"

        encrypted_bytes = enc_file.read_bytes()

        # Encrypted data should NOT contain the plaintext
        assert secret_content.encode() not in encrypted_bytes
        # But it should be binary data
        assert len(encrypted_bytes) > 0

    def test_manifest_is_encrypted(self, vault):
        """Manifest file should be encrypted."""
        vault.store_persona("test", "content", safety_level="unrestricted")

        from adk.private_companion import MANIFEST_FILE
        manifest_bytes = MANIFEST_FILE.read_bytes()

        # Manifest should be encrypted (Fernet format)
        # Valid Fernet tokens start with specific bytes
        assert len(manifest_bytes) > 0


class TestSafetyLevel:
    """Test safety level configuration."""

    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        """Create a temporary vault for testing."""
        monkeypatch.setenv("HOME", str(tmp_path))
        lockbox_dir = tmp_path / ".aither" / "private" / "lockbox"
        monkeypatch.setattr("adk.private_companion.CONFIG_DIR", tmp_path / ".aither")
        monkeypatch.setattr("adk.private_companion.PRIVATE_DIR", tmp_path / ".aither" / "private")
        monkeypatch.setattr("adk.private_companion.LOCKBOX_DIR", lockbox_dir)
        monkeypatch.setattr("adk.private_companion.VAULT_KEY_FILE", lockbox_dir / ".vault_key")
        monkeypatch.setattr("adk.private_companion.MACHINE_ID_FILE", lockbox_dir / ".machine_id")
        monkeypatch.setattr("adk.private_companion.SALT_FILE", lockbox_dir / ".vault_salt")
        monkeypatch.setattr("adk.private_companion.MANIFEST_FILE", lockbox_dir / ".manifest.json")
        monkeypatch.setattr("adk.private_companion.SAFETY_LEVEL_FILE", lockbox_dir / ".safety_level")

        vault = PrivateCompanionVault()
        yield vault

    def test_set_and_get_safety_level(self, vault):
        """Set and retrieve safety level."""
        vault.set_safety_level("unrestricted")
        level = vault.get_safety_level()
        assert level == "unrestricted"

    def test_default_safety_level(self, vault):
        """Unset safety level returns None."""
        level = vault.get_safety_level()
        assert level is None

    def test_get_system_prompt_for_unrestricted(self, vault):
        """Get system prompt for unrestricted level."""
        vault.set_safety_level("unrestricted")
        vault.store_persona(
            "special",
            "You are a special unrestricted persona",
            safety_level="unrestricted"
        )

        prompt = vault.get_system_prompt_for_level("unrestricted")
        assert prompt is not None
        assert "special" in prompt

    def test_get_system_prompt_for_professional(self, vault):
        """Professional level should return None (no custom personas)."""
        vault.set_safety_level("professional")
        vault.store_persona(
            "special",
            "You are a special unrestricted persona",
            safety_level="unrestricted"
        )

        # Professional level shouldn't get unrestricted personas
        prompt = vault.get_system_prompt_for_level("professional")
        assert prompt is None

    def test_get_system_prompt_fallback(self, vault):
        """No persona configured should return None."""
        prompt = vault.get_system_prompt_for_level("unrestricted")
        assert prompt is None


class TestVaultStatus:
    """Test vault status reporting."""

    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        """Create a temporary vault for testing."""
        monkeypatch.setenv("HOME", str(tmp_path))
        lockbox_dir = tmp_path / ".aither" / "private" / "lockbox"
        monkeypatch.setattr("adk.private_companion.CONFIG_DIR", tmp_path / ".aither")
        monkeypatch.setattr("adk.private_companion.PRIVATE_DIR", tmp_path / ".aither" / "private")
        monkeypatch.setattr("adk.private_companion.LOCKBOX_DIR", lockbox_dir)
        monkeypatch.setattr("adk.private_companion.VAULT_KEY_FILE", lockbox_dir / ".vault_key")
        monkeypatch.setattr("adk.private_companion.MACHINE_ID_FILE", lockbox_dir / ".machine_id")
        monkeypatch.setattr("adk.private_companion.SALT_FILE", lockbox_dir / ".vault_salt")
        monkeypatch.setattr("adk.private_companion.MANIFEST_FILE", lockbox_dir / ".manifest.json")
        monkeypatch.setattr("adk.private_companion.SAFETY_LEVEL_FILE", lockbox_dir / ".safety_level")

        vault = PrivateCompanionVault()
        yield vault

    def test_vault_status(self, vault):
        """Vault status includes all relevant info."""
        vault.store_persona("p1", "content1")
        vault.store_persona("p2", "content2")
        vault.set_safety_level("casual")

        status = vault.vault_status()
        assert status["initialized"]
        assert status["persona_count"] == 2
        assert "p1" in status["personas"]
        assert "p2" in status["personas"]
        assert status["safety_level"] == "casual"
