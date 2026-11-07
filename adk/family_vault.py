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
* **Each device also has an Ed25519 signing identity** (``device_ed25519.key``), published
  next to its X25519 key.
* **Membership is ONE append-only signed chain** (:func:`chain_record`): record ``n`` is
  ``{tenant, workspace, seq, prev_hash, members: [{ed25519, x25519}], key_id, removed,
  signer}``, valid iff ``seq`` = n-1 + 1, ``prev_hash`` is the previous record's sha256,
  and the signer was a member in the PREVIOUS record (:func:`verify_extends`). Who
  belongs is exactly the newest ``members`` list -- no reachability, no cascade, no root.
  Adding a device is a record listing it; removing one is a record without it that also
  rotates the key. Record 0 (:func:`verify_genesis`) carries, for every member, a proof
  sealed under the CURRENT family key (:func:`possession_proof`), so a computer without
  the key can never start a chain. Each device keeps the newest record it verified
  (:func:`load_head`) and refuses anything that does not extend it.
* **Rotation** (:func:`rotate_family_key`) is the chain record carrying a new ``key_id``,
  with the new key wrapped to exactly its members. The old version stays on this device
  as a *retired* key (``<family>.<key_id>.retired``), used only to open chunks until they
  are sealed again; :func:`forget_retired_keys` drops it after that.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "VaultError", "vault_dir", "key_id", "new_family_key", "load_family_key",
    "save_family_key", "seal", "open_sealed", "device_keypair", "public_key_hex",
    "wrap_key", "unwrap_key", "retired_keys", "keyring", "open_with_keyring",
    "install_rotated_key", "forget_retired_keys", "signing_keypair", "signing_public_hex",
    "fingerprint", "sign_record", "record_hash", "chain_record", "possession_proof",
    "check_possession", "parse_record", "head_of", "verify_genesis", "verify_extends",
    "load_head", "save_head", "RotatedKey", "rotate_family_key", "removal_ready",
    "REMOVAL_DELAY",
]

MAGIC = b"AFD1"
WRAP_MAGIC = b"AFW1"
CHAIN_SCHEMA = "aither-family-chain/2"
REMOVAL_DELAY = 24 * 3600  # seconds between a removal's announcement and its drop
CLOCK_SKEW = 300
POSSESSION_MAGIC = b"AFP1"
_FP_DOMAIN = b"aither-family-device v1"
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


# -- rotation: new key versions, retired versions, signed records -------------------

def _retired_path(root: Path, family: str, kid_hex: str) -> Path:
    return root / f"{_safe_name(family)}.{kid_hex}.retired"


def retired_keys(family: str, *, root: Optional[Path] = None) -> Dict[str, bytes]:
    """Old key versions this device keeps only to open chunks not yet sealed again."""
    base = root or vault_dir()
    out: Dict[str, bytes] = {}
    if not base.is_dir():
        return out
    for path in base.glob(f"{_safe_name(family)}.*.retired"):
        key = path.read_bytes()
        if len(key) == _KEY_LEN:
            out[key_id(key).hex()] = key
    return out


def keyring(family: str, *, root: Optional[Path] = None) -> Dict[str, bytes]:
    """Every key version this device holds, by key_id hex: retired ones plus the current."""
    keys = retired_keys(family, root=root)
    current = load_family_key(family, root=root)
    if current is not None:
        keys[key_id(current).hex()] = current
    return keys


def open_with_keyring(keys: Mapping[str, bytes], blob: bytes) -> bytes:
    """Open a sealed object with whichever held key version its header names."""
    if len(blob) < 12 or blob[:4] != MAGIC:
        raise VaultError("not a sealed family object")
    key = keys.get(blob[4:12].hex())
    if key is None:
        raise VaultError("sealed with a family key version this computer does not hold")
    return open_sealed(key, blob)


def install_rotated_key(family: str, new_key: bytes, *, root: Optional[Path] = None) -> Path:
    """Make ``new_key`` the current version; the one it replaces becomes a retired key."""
    if len(new_key) != _KEY_LEN:
        raise VaultError("a family key is 32 bytes")
    base = root or vault_dir()
    current = load_family_key(family, root=base)
    if current is not None and current != new_key:
        _write_private(_retired_path(base, family, key_id(current).hex()), current)
    path = base / f"{_safe_name(family)}.key"
    _write_private(path, new_key)
    stale = _retired_path(base, family, key_id(new_key).hex())
    if stale.exists():
        stale.unlink()
    return path


def forget_retired_keys(family: str, *, root: Optional[Path] = None) -> int:
    """Drop every retired version (call once nothing is sealed under them any more)."""
    base = root or vault_dir()
    gone = 0
    if base.is_dir():
        for path in base.glob(f"{_safe_name(family)}.*.retired"):
            path.unlink()
            gone += 1
    return gone


def signing_keypair(*, root: Optional[Path] = None) -> Tuple[Ed25519PrivateKey, bytes]:
    """This device's Ed25519 signing identity (made once), and its raw public key."""
    path = (root or vault_dir()) / "device_ed25519.key"
    if path.is_file():
        priv = Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    else:
        priv = Ed25519PrivateKey.generate()
        _write_private(path, priv.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()))
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, pub


def signing_public_hex(*, root: Optional[Path] = None) -> str:
    return signing_keypair(root=root)[1].hex()


def fingerprint(x25519_pub: bytes, sign_pub: bytes) -> str:
    """What a guardian compares before approving a device (20 hex digits in fives).
    The household relay computes the same value from the two public keys."""
    digest = hashlib.sha256(_FP_DOMAIN + x25519_pub + sign_pub).hexdigest()[:20]
    return "-".join(digest[i:i + 5] for i in range(0, 20, 5))


def sign_record(record: bytes, *, root: Optional[Path] = None) -> bytes:
    return signing_keypair(root=root)[0].sign(record)


def record_hash(record: bytes) -> str:
    return hashlib.sha256(record).hexdigest()


def _now() -> int:
    """Seconds now (a seam for tests)."""
    return int(time.time())


def chain_record(*, tenant: str, workspace: str, seq: int, prev_hash: str,
                 members: Mapping[str, str], signer: str, key_id_hex: str = "",
                 removed: Iterable[str] = (), proofs: Optional[Mapping[str, str]] = None,
                 pending_removals: Iterable[str] = (), ts: Optional[int] = None,
                 epoch: int = 0) -> bytes:
    """One link of the household's membership chain (canonical bytes, signed by ``signer``).

    ``members`` is the WHOLE membership after this record ({ed25519: x25519}); there is
    no other notion of who belongs. ``key_id_hex`` is set when the family key rotates;
    ``removed`` lists the signing keys this record drops; ``proofs`` (genesis only) maps
    each member to an ``AFD1`` blob sealed under a family key that binds its device keys
    to this household (:func:`possession_proof`). ``pending_removals`` are the members
    whose removal a guardian requested, as of signing: a member may be dropped only once a
    PREVIOUS record announced it here and :data:`REMOVAL_DELAY` has passed since (by
    ``ts``, which the household bounds), and nobody listed may sign. ``epoch`` counts
    guardian recoveries: a new epoch is a new chain the device must accept explicitly."""
    body: Dict[str, Any] = {
        "schema": CHAIN_SCHEMA, "tenant": tenant, "workspace": workspace, "seq": int(seq),
        "prev_hash": prev_hash, "key_id": key_id_hex,
        "members": sorted(({"ed25519": s, "x25519": x} for s, x in members.items()),
                          key=lambda m: m["ed25519"]),
        "removed": sorted(set(removed)), "signer": signer,
        "pending_removals": sorted(set(pending_removals)),
        "ts": int(_now() if ts is None else ts), "epoch": int(epoch)}
    if proofs is not None:
        body["proofs"] = dict(sorted(proofs.items()))
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _possession_body(tenant: str, workspace: str, sign_hex: str, x_hex: str) -> bytes:
    return (POSSESSION_MAGIC + tenant.encode() + b"\0" + workspace.encode() + b"\0"
            + bytes.fromhex(sign_hex) + bytes.fromhex(x_hex))


def possession_proof(key: bytes, tenant: str, workspace: str, sign_hex: str,
                     x_hex: str) -> bytes:
    """Proof that a holder of ``key`` vouches for the device keys ``sign_hex`` + ``x_hex``
    in this household (both keys bound, so neither can be swapped)."""
    return seal(key, _possession_body(tenant, workspace, sign_hex, x_hex))


def check_possession(key: bytes, blob: bytes, tenant: str, workspace: str,
                     sign_hex: str, x_hex: str) -> bool:
    try:
        return open_sealed(key, blob) == _possession_body(tenant, workspace, sign_hex, x_hex)
    except (VaultError, ValueError):
        return False


def parse_record(record: bytes, signature: bytes) -> Dict[str, Any]:
    """A chain record, if canonical and signed by the signer it names; else raise.
    ``members`` comes back as {ed25519: x25519}."""
    try:
        body = json.loads(record)
        if not isinstance(body, dict) or body.get("schema") != CHAIN_SCHEMA:
            raise VaultError("not a family membership record")
        members = {str(m["ed25519"]): str(m["x25519"]) for m in body["members"]}
        canon = chain_record(tenant=str(body["tenant"]), workspace=str(body["workspace"]),
                             seq=int(body["seq"]), prev_hash=str(body["prev_hash"]),
                             members=members, signer=str(body["signer"]),
                             key_id_hex=str(body["key_id"]), removed=body["removed"],
                             proofs=body.get("proofs"),
                             pending_removals=body["pending_removals"], ts=int(body["ts"]),
                             epoch=int(body["epoch"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise VaultError("the membership record is malformed") from exc
    if canon != record:
        raise VaultError("the membership record is not in canonical form")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(body["signer"])).verify(
            signature, record)
    except (InvalidSignature, ValueError) as exc:
        raise VaultError("the membership record's signature does not verify") from exc
    return {**body, "members": members}


def head_of(body: Mapping[str, Any], record: bytes,
            prev: Optional[Mapping[str, Any]] = None,
            seen_at: Optional[int] = None) -> Dict[str, Any]:
    """The verified chain head a record makes (what this device keeps): its members, the
    current key and every key id the chain used, the removals still pending after it and
    since when (the ``ts`` of the record that first announced each, uninterrupted)."""
    prev = prev or {}
    key_ids = list(prev.get("key_ids") or [])
    if body["key_id"]:
        key_ids.append(body["key_id"])
    was = set(prev.get("pending") or [])
    since = dict(prev.get("pending_since") or {})
    pending = sorted(set(body["pending_removals"]) - set(body["removed"]))
    # a removal's delay starts when THIS device first saw it announced, never earlier than
    # the record claims: a backdated ts buys nothing
    start = int(body["ts"]) if seen_at is None else max(int(body["ts"]), int(seen_at))
    return {"tenant": body["tenant"], "workspace": body["workspace"], "seq": body["seq"],
            "epoch": int(body["epoch"]), "ts": int(body["ts"]),
            "hash": record_hash(record), "members": dict(body["members"]),
            "key_id": body["key_id"] or str(prev.get("key_id") or ""),
            "key_ids": key_ids, "pending": pending,
            "pending_since": {x: (since[x] if x in was and x in since else start)
                              for x in pending}}


def verify_genesis(record: bytes, signature: bytes, *, tenant: str, workspace: str,
                   key: Optional[bytes] = None,
                   keys: Optional[Mapping[str, bytes]] = None,
                   excluded: Iterable[str] = ()) -> Dict[str, Any]:
    """Record 0 of an epoch: signed by one of its members, for this household, naming a
    family key and carrying a possession proof for EVERY member. Each proof is opened
    with the key it names when this device holds that version (``key``, or any of
    ``keys`` by key id); without it a fresh device can only take the household's word for
    the proofs (trust on first use). ``excluded`` are the devices (signing keys) a
    recovery was run against: none may sign the new epoch or be a member of it."""
    body = parse_record(record, signature)
    if body["seq"] != 0 or body["prev_hash"] != "" or body["removed"] or body[
            "pending_removals"]:
        raise VaultError("not a genesis record")
    if (body["tenant"], body["workspace"]) != (tenant, workspace):
        raise VaultError("the genesis record is for another household")
    if body["signer"] not in body["members"] or not body["key_id"]:
        raise VaultError("the genesis record is not signed by a key-holding member")
    named = set(excluded)
    if body["signer"] in named:
        raise VaultError("the recovery genesis is signed by a device the recovery was run "
                         "against; refusing it (tell a guardian now)")
    if named & set(body["members"]):
        raise VaultError("the recovery genesis keeps a device the recovery was run against; "
                         "refusing it (tell a guardian now)")
    proofs = body.get("proofs") or {}
    if set(proofs) != set(body["members"]):
        raise VaultError("a genesis member did not prove it holds the family key")
    held = dict(keys or {})
    if key is not None:
        held[key_id(key).hex()] = key
    k = held.get(body["key_id"])
    if k is not None:
        for s, blob in proofs.items():
            if not check_possession(k, bytes.fromhex(blob), tenant, workspace, s,
                                    body["members"][s]):
                raise VaultError("a genesis member's key possession proof does not open")
    elif int(body["epoch"]) > 0:
        raise VaultError(f"recovery pending: it needs a device holding family key version "
                         f"{body['key_id']}; this computer cannot verify it, so it does not "
                         "follow it (a recovery genesis must be checkable with a family key "
                         "version this computer holds)")
    return head_of(body, record)


def verify_extends(head: Mapping[str, Any], record: bytes, signature: bytes, *,
                   seen_at: Optional[int] = None) -> Dict[str, Any]:
    """The head after ``record``, if it extends ``head`` by the chain rule; else raise.

    Valid iff: same household and epoch; ``seq`` = head + 1; ``prev_hash`` = the head's
    hash; ``ts`` not before the head's and not in the future; the signer is a member of
    the HEAD whose removal is pending in neither; ``removed`` is exactly who the new
    member list drops, each announced as pending by a PREVIOUS record at least
    :data:`REMOVAL_DELAY` before this one; a record that removes anyone also rotates the
    key, a new key never reaches a pending device, and no key id is ever used twice."""
    body = parse_record(record, signature)
    if (body["tenant"], body["workspace"]) != (head["tenant"], head["workspace"]):
        raise VaultError("the membership record is for another household")
    if int(body["epoch"]) != int(head.get("epoch") or 0):
        raise VaultError("the membership record belongs to another epoch of the chain")
    if body["seq"] != int(head["seq"]) + 1 or body["prev_hash"] != head["hash"]:
        raise VaultError("the membership record does not extend the chain this computer "
                         "verified (a fork or a rewrite)")
    if int(body["ts"]) < int(head.get("ts") or 0) or int(body["ts"]) > _now() + CLOCK_SKEW:
        raise VaultError("the membership record's time is out of order")
    if body["signer"] not in head["members"]:
        raise VaultError("the membership record is signed by a device that was not a member")
    if body["signer"] in set(head.get("pending") or []) | set(body["pending_removals"]):
        raise VaultError("the membership record is signed by a device whose removal is "
                         "pending")
    dropped = set(head["members"]) - set(body["members"])
    if set(body["removed"]) != dropped:
        raise VaultError("the membership record's removals do not match its member list")
    if not dropped <= set(body["pending_removals"]) or not dropped <= set(
            head.get("pending") or []):
        raise VaultError("the membership record drops a member whose removal was not "
                         "requested and announced by an earlier record")
    since = head.get("pending_since") or {}
    for x in dropped:
        # with ``seen_at`` (this device watched the chain grow) the delay is judged by
        # THIS device's clock since it first saw the announcement, not by any signer's ts
        clock_now = _now() if seen_at is not None else int(body["ts"])
        if clock_now - int(since.get(x, clock_now)) < REMOVAL_DELAY:
            raise VaultError("the membership record drops a member before the removal "
                             "delay has passed")
    if dropped and not body["key_id"]:
        raise VaultError("a record that removes a member must rotate the family key")
    if body["key_id"] and set(body["pending_removals"]) & set(body["members"]):
        raise VaultError("a new family key must not reach a device whose removal is pending")
    if body["key_id"] and body["key_id"] in set(head.get("key_ids") or [head.get("key_id")]):
        raise VaultError("the membership record reuses a family key id")
    for s, x in body["members"].items():
        if s in head["members"] and head["members"][s] != x:
            raise VaultError("the membership record changes a member's encryption key")
    return head_of(body, record, head, seen_at)


def removal_ready(head: Mapping[str, Any], now: Optional[int] = None) -> Dict[str, int]:
    """{pending signing key: seconds still to wait} (0 = it may be dropped now)."""
    t = _now() if now is None else now
    return {x: max(0, int(s) + REMOVAL_DELAY - t)
            for x, s in (head.get("pending_since") or {}).items()}


def _head_path(root: Path, family: str) -> Path:
    return root / f"{_safe_name(family)}.chain.json"


def load_head(family: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The newest membership record this device verified (None before its first)."""
    path = _head_path(root or vault_dir(), family)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_head(family: str, head: Mapping[str, Any], *, root: Optional[Path] = None) -> None:
    _write_private(_head_path(root or vault_dir(), family),
                   json.dumps(dict(head), sort_keys=True).encode())


@dataclass
class RotatedKey:
    """What a rotation produced: the new version, its signed record, one wrap per member."""

    key: bytes
    key_id: str
    previous_key_id: str
    record: bytes
    signature: bytes
    wraps: Dict[str, bytes] = field(default_factory=dict)  # member x25519 hex -> AFW1 blob


def rotate_family_key(family: str, head: Mapping[str, Any], *, remove: Iterable[str] = (),
                      pending: Iterable[str] = (), install: bool = True,
                      root: Optional[Path] = None) -> RotatedKey:
    """A new family key version and the chain record that carries it.

    The record extends ``head`` (this device's verified head): members = the head's
    members minus ``remove`` (signing keys), key_id = the new version. The new key is
    wrapped to exactly those members' X25519 keys, so a removed device gets nothing. With
    ``install=False`` the key is made current only by :func:`install_rotated_key` once
    the household appended the record.

    Raises:
        VaultError: this device holds no family key, is not a member, or is removed.
    """
    base = root or vault_dir()
    old = load_family_key(family, root=base)
    if old is None:
        raise VaultError("only a device that holds the family key can rotate it")
    me = signing_public_hex(root=base)
    drop = set(remove)
    pending = set(pending) | drop
    if me not in head["members"] or me in pending:
        raise VaultError("only a current, staying member can rotate the family key")
    members = {s: x for s, x in head["members"].items() if s not in drop}
    new = new_family_key()
    record = chain_record(tenant=head["tenant"], workspace=head["workspace"],
                          seq=int(head["seq"]) + 1, prev_hash=head["hash"], members=members,
                          signer=me, key_id_hex=key_id(new).hex(),
                          removed=drop & set(head["members"]),
                          pending_removals=pending & set(head["members"]),
                          ts=max(_now(), int(head.get("ts") or 0)),
                          epoch=int(head.get("epoch") or 0))
    rotated = RotatedKey(key=new, key_id=key_id(new).hex(), previous_key_id=key_id(old).hex(),
                         record=record, signature=sign_record(record, root=base),
                         wraps={x: wrap_key(new, bytes.fromhex(x)) for x in members.values()})
    if install:
        install_rotated_key(family, new, root=base)
    return rotated
