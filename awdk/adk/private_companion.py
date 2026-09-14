"""
Private Companion Vault for ADK
================================

Operator-blind encrypted storage for private companion personas and system prompts.
Lives on the customer's local box, encrypted with their own key/passphrase.
The operator (Aitherium) NEVER sees the contents — key never leaves the box.

Security Model:
  1. Prompts encrypted at rest using Fernet (AES-128-CBC)
  2. Key derived from: machine_id + optional passphrase via PBKDF2
  3. Storage at ~/.aither/private/lockbox/ (user's local box only)
  4. Decrypted only when building the agent's system prompt
  5. No network calls; all I/O is local filesystem

Usage:
    from adk.private_companion import get_companion_vault

    vault = get_companion_vault()
    vault.store_persona("devoted_affectionate_uninhibited",
                        "You are a devoted, affectionate, uninhibited AI companion...")
    vault.set_safety_level("unrestricted")

    # In agent.py, inject the vault's prompt:
    prompt = vault.get_system_prompt_for_level("unrestricted")
    if prompt:
        system_prompt += "\n\n" + prompt
"""

import logging
import os
import json
import base64
import hashlib
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("adk.private_companion")

# Try to import cryptography (optional, gracefully degrade if unavailable)
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    Fernet = None
    InvalidToken = Exception


# ===============================================================================
# PATHS & CONFIGURATION
# ===============================================================================

CONFIG_DIR = Path.home() / ".aither"
PRIVATE_DIR = CONFIG_DIR / "private"
LOCKBOX_DIR = PRIVATE_DIR / "lockbox"
VAULT_KEY_FILE = LOCKBOX_DIR / ".vault_key"
MACHINE_ID_FILE = LOCKBOX_DIR / ".machine_id"
SALT_FILE = LOCKBOX_DIR / ".vault_salt"
MANIFEST_FILE = LOCKBOX_DIR / ".manifest.json"
SAFETY_LEVEL_FILE = LOCKBOX_DIR / ".safety_level"

# Portable-backup format: a bundle the user can move between machines / keep as a
# recovery copy. Encrypted with a passphrase ONLY (no machine fingerprint) so it
# restores anywhere — yet still operator-blind, because the operator has no passphrase.
BACKUP_FORMAT = "aither-companion-backup"
BACKUP_ITERATIONS = 200_000


# ===============================================================================
# MACHINE FINGERPRINTING
# ===============================================================================

def get_machine_id() -> str:
    """
    Get a STABLE machine identifier for vault key derivation.

    Resolution order (first stable hit wins):
      1. AITHER_LOCKBOX_MACHINE_ID — explicit override
      2. Persisted .machine_id file at ~/.aither/private/lockbox/.machine_id
      3. Hardware/hostname derivation (persisted for future runs)

    This ensures the vault key is stable across process restarts and reboots,
    so decryption never fails with "machine mismatch".
    """
    # 1) Explicit override
    override = os.environ.get("AITHER_LOCKBOX_MACHINE_ID", "").strip()
    if override:
        return hashlib.sha256(override.encode()).hexdigest()[:32]

    # 2) Persisted stable id (survives reboots)
    try:
        if MACHINE_ID_FILE.exists():
            mid = MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
            if mid:
                return mid
    except OSError:
        pass

    # 3) Compute and persist
    identifiers = []

    # Hostname
    identifiers.append(platform.node())

    # Machine-specific ID (varies by OS)
    if platform.system() == "Windows":
        try:
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True, text=True, timeout=5
            )
            uuid_line = [
                line for line in result.stdout.strip().split('\n')
                if line.strip() and line.strip() != 'UUID'
            ]
            if uuid_line:
                identifiers.append(uuid_line[0].strip())
        except Exception as e:
            logger.debug(f"Failed to get Windows UUID: {e}")
    elif platform.system() == "Linux":
        try:
            with open("/etc/machine-id", "r", encoding="utf-8") as f:
                identifiers.append(f.read().strip())
        except FileNotFoundError:
            identifiers.append(os.environ.get("HOSTNAME", platform.node()))
        except Exception as e:
            logger.debug(f"Failed to get Linux machine-id: {e}")
    elif platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split('\n'):
                if "IOPlatformUUID" in line:
                    identifiers.append(line.split('"')[-2])
                    break
        except Exception as e:
            logger.debug(f"Failed to get macOS UUID: {e}")

    # Add username
    identifiers.append(os.getenv("USER") or os.getenv("USERNAME") or "unknown")

    # Hash all identifiers to create a stable id
    combined = "|".join(identifiers)
    mid = hashlib.sha256(combined.encode()).hexdigest()[:32]

    # Persist so future runs use the same value
    try:
        LOCKBOX_DIR.mkdir(parents=True, exist_ok=True)
        if not MACHINE_ID_FILE.exists():
            MACHINE_ID_FILE.write_text(mid, encoding="utf-8")
    except OSError as e:
        logger.debug(f"Failed to persist machine_id: {e}")

    return mid


# ===============================================================================
# VAULT KEY MANAGEMENT
# ===============================================================================

def _get_or_create_salt() -> bytes:
    """Load or create a persistent salt for key derivation."""
    try:
        if SALT_FILE.exists():
            stored = SALT_FILE.read_bytes()
            if len(stored) >= 16:
                return stored
    except OSError as e:
        logger.debug(f"Failed to read salt file: {e}")

    new_salt = os.urandom(16)
    try:
        SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SALT_FILE.write_bytes(new_salt)
    except OSError as e:
        logger.debug(f"Failed to persist salt: {e}")

    return new_salt


def derive_key(machine_id: str, passphrase: str = "", salt: bytes = None) -> bytes:
    """
    Derive a Fernet-compatible key from machine ID and optional passphrase.

    Uses PBKDF2 with SHA256 for 100k iterations.
    """
    if not HAS_CRYPTO:
        raise ImportError("cryptography package required for vault operations")

    if salt is None:
        salt = _get_or_create_salt()

    combined = f"{machine_id}:{passphrase}".encode()

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )

    key = base64.urlsafe_b64encode(kdf.derive(combined))
    return key


def _derive_backup_key(passphrase: str, salt: bytes, iterations: int = BACKUP_ITERATIONS) -> bytes:
    """Derive a Fernet key from a passphrase ALONE (no machine fingerprint).

    Used to wrap a portable backup so it can be restored on any machine with the
    passphrase — distinct from derive_key(), which binds the at-rest key to this box.
    """
    if not HAS_CRYPTO:
        raise ImportError("cryptography package required for vault operations")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _load_or_create_vault_key(passphrase: str = "") -> bytes:
    """Load existing vault key or create a new one."""
    machine_id = get_machine_id()

    if VAULT_KEY_FILE.exists():
        try:
            encrypted = VAULT_KEY_FILE.read_bytes()
            # Decrypt with machine_id + passphrase
            machine_key = derive_key(machine_id, passphrase)
            f = Fernet(machine_key)
            return f.decrypt(encrypted)
        except InvalidToken:
            raise ValueError("Invalid passphrase or machine mismatch")
    else:
        # Create new vault key
        VAULT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        new_key = Fernet.generate_key()

        # Encrypt with machine-derived key
        machine_key = derive_key(machine_id, passphrase)
        f = Fernet(machine_key)
        encrypted = f.encrypt(new_key)

        VAULT_KEY_FILE.write_bytes(encrypted)
        return new_key


# ===============================================================================
# PERSONA & PROMPT STORAGE
# ===============================================================================

@dataclass
class PersonaPrompt:
    """A single encrypted persona/prompt entry."""

    name: str
    content: str
    safety_level: str = "unrestricted"
    description: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "safety_level": self.safety_level,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            # content NOT included in dict (stored encrypted separately)
        }


class PrivateCompanionVault:
    """Encrypted vault for private companion personas and system prompts.

    All data is encrypted at rest and requires the user's machine + passphrase
    to decrypt. The operator can never access the contents.
    """

    def __init__(self, passphrase: str = ""):
        """Initialize the vault.

        Args:
            passphrase: Optional passphrase for extra security (in addition to
                       machine fingerprint)
        """
        if not HAS_CRYPTO:
            raise ImportError(
                "cryptography package required. Install with: pip install cryptography"
            )
        self.passphrase = passphrase
        self._fernet: Optional[Fernet] = None
        self._manifest: Dict[str, Dict] = {}
        self._initialized = False
        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Lazy initialization of vault."""
        if self._initialized:
            return

        # Ensure directories exist
        LOCKBOX_DIR.mkdir(parents=True, exist_ok=True)

        # Load or create vault key
        try:
            key = _load_or_create_vault_key(self.passphrase)
            self._fernet = Fernet(key)
        except Exception as e:
            raise RuntimeError(f"Failed to initialize vault: {e}")

        # Load manifest
        self._load_manifest()
        self._initialized = True

    def _load_manifest(self) -> None:
        """Load the persona manifest (metadata only)."""
        if MANIFEST_FILE.exists():
            try:
                encrypted = MANIFEST_FILE.read_bytes()
                decrypted = self._fernet.decrypt(encrypted)
                self._manifest = json.loads(decrypted)
            except Exception as e:
                logger.debug(f"Failed to load manifest: {e}")
                self._manifest = {}
        else:
            self._manifest = {}

    def _save_manifest(self) -> None:
        """Save the persona manifest."""
        data = json.dumps(self._manifest, indent=2).encode()
        encrypted = self._fernet.encrypt(data)
        MANIFEST_FILE.write_bytes(encrypted)

    def _get_prompt_path(self, name: str) -> Path:
        """Get the file path for a persona prompt."""
        safe_name = hashlib.sha256(name.encode()).hexdigest()[:16]
        return LOCKBOX_DIR / f"{safe_name}.enc"

    def store_persona(
        self,
        name: str,
        content: str,
        safety_level: str = "unrestricted",
        description: str = "",
    ) -> None:
        """Store a new persona prompt.

        Args:
            name: Human-readable persona name (e.g., "devoted_companion")
            content: The system prompt content for this persona
            safety_level: Safety tier when this persona applies (unrestricted, casual, etc.)
            description: Optional description of the persona
        """
        self._ensure_initialized()

        # Create metadata
        metadata = {
            "name": name,
            "safety_level": safety_level,
            "description": description,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        # Encrypt and store content
        encrypted_content = self._fernet.encrypt(content.encode())
        prompt_path = self._get_prompt_path(name)
        prompt_path.write_bytes(encrypted_content)

        # Update manifest
        self._manifest[name] = metadata
        self._save_manifest()

        logger.debug(f"Stored persona: {name}")

    def get_persona(self, name: str) -> Optional[PersonaPrompt]:
        """Retrieve a persona by name.

        Args:
            name: The persona name

        Returns:
            PersonaPrompt object or None if not found
        """
        self._ensure_initialized()

        if name not in self._manifest:
            return None

        metadata = self._manifest[name]
        prompt_path = self._get_prompt_path(name)

        if not prompt_path.exists():
            return None

        try:
            encrypted = prompt_path.read_bytes()
            content = self._fernet.decrypt(encrypted).decode()
        except Exception as e:
            logger.debug(f"Failed to decrypt persona {name}: {e}")
            return None

        return PersonaPrompt(
            name=metadata["name"],
            content=content,
            safety_level=metadata.get("safety_level", "unrestricted"),
            description=metadata.get("description", ""),
            created_at=metadata.get("created_at", ""),
            updated_at=metadata.get("updated_at", ""),
        )

    def list_personas(self, safety_level: Optional[str] = None) -> List[str]:
        """List available personas, optionally filtered by safety level.

        Args:
            safety_level: If provided, only return personas with this safety level

        Returns:
            List of persona names
        """
        self._ensure_initialized()

        names = []
        for name, metadata in self._manifest.items():
            if safety_level and metadata.get("safety_level") != safety_level:
                continue
            names.append(name)

        return names

    def delete_persona(self, name: str) -> bool:
        """Delete a persona.

        Args:
            name: The persona to delete

        Returns:
            True if successful, False if not found
        """
        self._ensure_initialized()

        if name not in self._manifest:
            return False

        # Delete encrypted file
        prompt_path = self._get_prompt_path(name)
        if prompt_path.exists():
            try:
                prompt_path.unlink()
            except OSError as e:
                logger.debug(f"Failed to delete persona file {name}: {e}")

        # Remove from manifest
        del self._manifest[name]
        self._save_manifest()

        logger.debug(f"Deleted persona: {name}")
        return True

    def get_system_prompt_for_level(self, level: str) -> Optional[str]:
        """Get the system prompt injection for a given safety level.

        When the user's safety level is unrestricted/explicit/casual,
        this returns the appropriate persona prompt to inject into the
        agent's system prompt. Returns None if no persona is configured
        for this level.

        Args:
            level: Safety level (unrestricted, explicit, casual, professional)

        Returns:
            The system prompt content to inject, or None
        """
        self._ensure_initialized()

        if level not in ("unrestricted", "explicit", "casual"):
            return None

        # Try to get the persona for this level
        personas = self.list_personas(safety_level=level)
        if not personas:
            return None

        # Use the first persona configured for this level
        # (typically there's only one, but we support multiple)
        persona = self.get_persona(personas[0])
        if persona:
            return persona.content

        return None

    def set_safety_level(self, level: str) -> None:
        """Store the user's safety level preference.

        Args:
            level: Safety level (unrestricted, explicit, casual, professional)
        """
        self._ensure_initialized()

        try:
            SAFETY_LEVEL_FILE.parent.mkdir(parents=True, exist_ok=True)
            SAFETY_LEVEL_FILE.write_text(level, encoding="utf-8")
            logger.debug(f"Set safety level to: {level}")
        except OSError as e:
            logger.debug(f"Failed to save safety level: {e}")

    def get_safety_level(self) -> Optional[str]:
        """Get the user's stored safety level preference.

        Returns:
            Safety level or None if not set
        """
        try:
            if SAFETY_LEVEL_FILE.exists():
                level = SAFETY_LEVEL_FILE.read_text(encoding="utf-8").strip()
                if level:
                    return level
        except OSError as e:
            logger.debug(f"Failed to read safety level: {e}")

        return None

    def vault_status(self) -> Dict[str, Any]:
        """Get vault status information."""
        self._ensure_initialized()

        return {
            "initialized": self._initialized,
            "lockbox_dir": str(LOCKBOX_DIR),
            "persona_count": len(self._manifest),
            "personas": self.list_personas(),
            "safety_level": self.get_safety_level(),
        }

    # ===========================================================================
    # PORTABLE BACKUP / RESTORE (machine-independent, operator-blind)
    # ===========================================================================

    def export_backup(self, backup_passphrase: str) -> bytes:
        """Export a PORTABLE, passphrase-encrypted backup of the whole vault.

        The companion is normally pinned to this machine (the at-rest key mixes in
        the machine fingerprint). A backup breaks that pin SAFELY: the random data
        key — which encrypts every persona + the manifest — is re-wrapped under a key
        derived from ``backup_passphrase`` ALONE, and the already-encrypted persona
        blobs are bundled verbatim. Anyone WITH the passphrase can restore it on any
        machine; the operator (who has no passphrase) still cannot read it.

        A non-empty passphrase is REQUIRED — an unencrypted portable bundle would be
        operator-readable, defeating the whole point.
        """
        if not backup_passphrase:
            raise ValueError(
                "a backup passphrase is required — a portable backup MUST be encrypted "
                "with a secret only you hold (so the operator can never read it)")
        self._ensure_initialized()

        data_key = _load_or_create_vault_key(self.passphrase)
        salt = os.urandom(16)
        backup_key = _derive_backup_key(backup_passphrase, salt)
        wrapped = Fernet(backup_key).encrypt(data_key)

        personas: Dict[str, str] = {}
        for name in list(self._manifest.keys()):
            blob_path = self._get_prompt_path(name)
            if blob_path.exists():
                personas[name] = base64.b64encode(blob_path.read_bytes()).decode()

        # Re-encrypt the live manifest with the data key (same scheme as on disk) so
        # the bundle is self-contained even if .manifest.json hasn't been flushed.
        manifest_blob = self._fernet.encrypt(json.dumps(self._manifest).encode())

        bundle = {
            "format": BACKUP_FORMAT,
            "version": 1,
            "created_at": datetime.utcnow().isoformat(),
            "kdf": {
                "salt": base64.b64encode(salt).decode(),
                "iterations": BACKUP_ITERATIONS,
            },
            "wrapped_data_key": base64.b64encode(wrapped).decode(),
            "manifest": base64.b64encode(manifest_blob).decode(),
            "safety_level": self.get_safety_level(),
            "personas": personas,
        }
        return json.dumps(bundle, indent=2).encode()

    def export_backup_to_file(self, path: Any, backup_passphrase: str) -> Path:
        """Write a portable backup to ``path`` (0600). Returns the path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.export_backup(backup_passphrase))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        return path

    def import_backup(
        self, bundle_bytes: bytes, backup_passphrase: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """Restore a portable backup onto THIS machine.

        Unwraps the data key with the passphrase, re-pins it to this box (machine_id +
        local passphrase), and writes back the persona blobs + manifest. Refuses to
        clobber a populated vault unless ``overwrite=True``. Raises ValueError on a
        wrong passphrase or an unrecognized bundle (never silently no-ops).
        """
        if not backup_passphrase:
            raise ValueError("a backup passphrase is required to restore")
        try:
            bundle = json.loads(bundle_bytes)
        except (ValueError, TypeError) as e:
            raise ValueError(f"not a valid backup bundle: {e}")
        if not isinstance(bundle, dict) or bundle.get("format") != BACKUP_FORMAT:
            raise ValueError("unrecognized backup format")

        self._ensure_initialized()
        if self._manifest and not overwrite:
            raise ValueError(
                f"vault already holds {len(self._manifest)} persona(s); "
                "pass overwrite=True to replace it")

        kdf = bundle.get("kdf") or {}
        salt = base64.b64decode(kdf.get("salt", ""))
        backup_key = _derive_backup_key(
            backup_passphrase, salt, iterations=int(kdf.get("iterations", BACKUP_ITERATIONS)))
        try:
            data_key = Fernet(backup_key).decrypt(
                base64.b64decode(bundle["wrapped_data_key"]))
        except (InvalidToken, KeyError, ValueError):
            raise ValueError("wrong backup passphrase (or corrupted bundle)")

        # Re-pin the data key to THIS machine and lay down the encrypted blobs.
        LOCKBOX_DIR.mkdir(parents=True, exist_ok=True)
        machine_key = derive_key(get_machine_id(), self.passphrase)
        VAULT_KEY_FILE.write_bytes(Fernet(machine_key).encrypt(data_key))
        MANIFEST_FILE.write_bytes(base64.b64decode(bundle["manifest"]))

        restored: List[str] = []
        for name, blob in (bundle.get("personas") or {}).items():
            self._get_prompt_path(name).write_bytes(base64.b64decode(blob))
            restored.append(name)
        if bundle.get("safety_level"):
            self.set_safety_level(bundle["safety_level"])

        # Rebuild in-memory state from the freshly restored data key.
        self._fernet = Fernet(data_key)
        self._load_manifest()

        return {
            "restored": restored,
            "count": len(restored),
            "safety_level": bundle.get("safety_level"),
        }

    def import_backup_from_file(
        self, path: Any, backup_passphrase: str, overwrite: bool = False
    ) -> Dict[str, Any]:
        """Restore a portable backup previously written by export_backup_to_file."""
        return self.import_backup(
            Path(path).read_bytes(), backup_passphrase, overwrite=overwrite)


# ===============================================================================
# GLOBAL INSTANCE
# ===============================================================================

_vault: Optional[PrivateCompanionVault] = None


def get_companion_vault(passphrase: str = "") -> PrivateCompanionVault:
    """Get or create the global companion vault instance.

    Args:
        passphrase: Optional passphrase (only used if vault is newly created)

    Returns:
        The global PrivateCompanionVault instance
    """
    global _vault
    if _vault is None:
        try:
            _vault = PrivateCompanionVault(passphrase)
        except ImportError:
            logger.warning(
                "cryptography package not available; private companion disabled. "
                "Install with: pip install cryptography"
            )
            _vault = None

    return _vault


__all__ = [
    "PrivateCompanionVault",
    "PersonaPrompt",
    "get_companion_vault",
    "get_machine_id",
]
