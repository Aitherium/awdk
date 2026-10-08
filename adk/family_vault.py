"""The family drive's key: made on a family device, used on family devices, nowhere else (B7).

Everything the family stores on each other's devices (the mesh storage pool) is sealed
HERE, before it leaves this computer: the household block plane, the phones that keep
copies and Genesis only ever see ciphertext.

* **One key per family**, 32 random bytes made by the first family device that needs it
  (``adk storage drive key init``) and kept in ``~/.aither/family_storage/<family>.key``
  (owner-only file). It is never derived from any platform secret (not
  ``AITHER_MASTER_KEY``, whose rotation must not touch family data) and never sent
  anywhere in the clear.
* **Sealing**: AES-256-GCM. A sealed object is ``AFD1 | key_id(8) | nonce(12) | ct+tag``;
  the header is the associated data, so a wrong key or a tampered header fails loudly.
  ``key_id`` (first 8 bytes of sha256(key)) says which family key opens it.
* **Another family device** gets the key wrapped to ITS X25519 public key
  (``AFW1 | ephemeral_pub(32) | nonce(12) | ct+tag``, HKDF-SHA256 from the X25519 secret).
  The server may relay that blob but cannot open it; only the device holding the private
  key can (:func:`unwrap_key`). Each device's X25519 key lives in
  ``~/.aither/family_storage/device_x25519.key``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "VaultError", "vault_dir", "key_id", "new_family_key", "load_family_key",
    "save_family_key", "seal", "open_sealed", "device_keypair", "public_key_hex",
    "wrap_key", "unwrap_key",
]

MAGIC = b"AFD1"
WRAP_MAGIC = b"AFW1"
_HKDF_INFO = b"aither-family-drive key wrap v1"
_KEY_LEN = 32


class VaultError(Exception):
    """A key is missing, wrong, or a sealed blob does not open."""


def vault_dir() -> Path:
    home = os.environ.get("AITHER_HOME") or str(Path.home() / ".aither")
    return Path(home) / "family_storage"


def _safe_name(family: str) -> str:
    keep = "".join(ch for ch in (family or "") if ch.isalnum() or ch in "-_")[:64]
    if not keep:
        raise VaultError("a family id is required")
    return keep


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows: the profile folder's ACL is what protects it
        pass
    os.replace(tmp, path)


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:8]


def new_family_key() -> bytes:
    return secrets.token_bytes(_KEY_LEN)


def save_family_key(family: str, key: bytes, *, root: Optional[Path] = None) -> Path:
    if len(key) != _KEY_LEN:
        raise VaultError("a family key is 32 bytes")
    path = (root or vault_dir()) / f"{_safe_name(family)}.key"
    if path.exists() and path.read_bytes() != key:
        raise VaultError("this device already holds a different key for this family")
    _write_private(path, key)
    return path


def load_family_key(family: str, *, root: Optional[Path] = None) -> Optional[bytes]:
    path = (root or vault_dir()) / f"{_safe_name(family)}.key"
    if not path.is_file():
        return None
    key = path.read_bytes()
    if len(key) != _KEY_LEN:
        raise VaultError(f"{path} is not a family key")
    return key


def seal(key: bytes, plaintext: bytes) -> bytes:
    if len(key) != _KEY_LEN:
        raise VaultError("a family key is 32 bytes")
    nonce = secrets.token_bytes(12)
    header = MAGIC + key_id(key) + nonce
    return header + AESGCM(key).encrypt(nonce, plaintext, header)


def open_sealed(key: bytes, blob: bytes) -> bytes:
    if len(blob) < 4 + 8 + 12 + 16 or blob[:4] != MAGIC:
        raise VaultError("not a sealed family object")
    if blob[4:12] != key_id(key):
        raise VaultError("sealed with a different family key")
    header, body = blob[:24], blob[24:]
    try:
        return AESGCM(key).decrypt(blob[12:24], body, header)
    except Exception as exc:  # cryptography's InvalidTag: tampered or truncated
        raise VaultError("the sealed object does not open (tampered or truncated)") from exc


# -- device keys and wrapping ------------------------------------------------------

def device_keypair(*, root: Optional[Path] = None) -> Tuple[X25519PrivateKey, bytes]:
    """This device's X25519 key (made once), and its raw 32-byte public key."""
    path = (root or vault_dir()) / "device_x25519.key"
    if path.is_file():
        priv = X25519PrivateKey.from_private_bytes(path.read_bytes())
    else:
        priv = X25519PrivateKey.generate()
        _write_private(path, priv.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()))
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, pub


def public_key_hex(*, root: Optional[Path] = None) -> str:
    return device_keypair(root=root)[1].hex()


def _wrap_secret(shared: bytes, eph_pub: bytes, to_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + to_pub,
                info=_HKDF_INFO).derive(shared)


def wrap_key(key: bytes, to_public: bytes) -> bytes:
    """The family key, sealed to one device's X25519 public key (only it can open it)."""
    if len(to_public) != 32:
        raise VaultError("a device public key is 32 bytes")
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = eph.exchange(X25519PublicKey.from_public_bytes(to_public))
    nonce = secrets.token_bytes(12)
    header = WRAP_MAGIC + eph_pub + nonce
    return header + AESGCM(_wrap_secret(shared, eph_pub, to_public)).encrypt(nonce, key, header)


def unwrap_key(blob: bytes, *, root: Optional[Path] = None) -> bytes:
    if len(blob) < 4 + 32 + 12 + 16 or blob[:4] != WRAP_MAGIC:
        raise VaultError("not a wrapped family key")
    priv, my_pub = device_keypair(root=root)
    eph_pub, nonce = blob[4:36], blob[36:48]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    try:
        key = AESGCM(_wrap_secret(shared, eph_pub, my_pub)).decrypt(nonce, blob[48:], blob[:48])
    except Exception as exc:
        raise VaultError("this wrapped key is for another device, or was tampered with") from exc
    if len(key) != _KEY_LEN:
        raise VaultError("the unwrapped key is not a family key")
    return key
