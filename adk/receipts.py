"""Signed receipts: an append-only, hash-chained log of what the agent did.

Every tool call, outbound message or approval the home agent makes lands here as
one JSON line in ``~/.aither/agent-home/actions.jsonl``. Each row carries the
sha256 of the previous line (``prev_sha256``) and an Ed25519 signature over its
own canonical form, so editing a byte, deleting a line or reordering lines
breaks the chain and ``verify`` says so.

WHAT IS STORED, AND WHAT IS NOT

Arguments and results are stored ONLY as a sha256 digest plus a short redacted
preview (at most ``PREVIEW_CHARS`` characters). The full payload never reaches
the log: a receipt of "sent the 2FA code" must not become a second copy of the
code. The digest still lets anyone holding the original payload prove it is the
one the agent acted on.

KEYS

Signing prefers the device's awseal key (``awseal.keys.load_private_key``); if
awseal is not installed or has no key, a plain ``cryptography`` Ed25519 key at
``~/.aither/agent-home/receipt.key`` (mode 600) is created once and used. If
neither is possible the row is written with ``signed: false`` — never with a
fake signature. Each row names the key that signed it (``key_id``), so a log
that spans a key change still verifies.

LIMITS, STATED

The key lives on disk. Anyone with root on this box can re-sign a rewritten
chain. Truncating the TAIL of the log is not detectable from the log alone, so
``anchor`` (``adk home receipts --anchor``) writes the chain head -- ``seq`` and
the sha256 of that line, signed -- to ``receipts.anchor`` beside the log. When an
anchor exists, ``verify`` reports a log that ends before the anchored seq, or
whose anchored line no longer hashes to the anchored digest, as tampered (1). An
anchor covers rows up to the moment it was written; later rows are covered by
the next ``--anchor``. Deleting the anchor file removes the check -- copy it off
the box when that matters. Receipts are tamper-evident against everyone else.

EXIT CODES (``verify``)

0 intact · 1 tampered (chain, sequence or signature broken, or the log is
shorter than / diverges from its anchor) · 2 cannot judge (missing or
unreadable file, no key to check a signature, or unsigned rows or anchor).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Where the log lives when no path is given. Overridable by ``AITHER_RECEIPTS_PATH``.
PATH_ENV = "AITHER_RECEIPTS_PATH"
#: A file holding a private key to sign with, overriding the key ladder.
KEY_ENV = "AITHER_RECEIPTS_KEY"
#: A hex public key a verifier trusts, in addition to the local keys.
PUBKEY_ENV = "AITHER_RECEIPTS_PUBKEY"

GENESIS = "0" * 64
PREVIEW_CHARS = 120
#: The signed chain head lives beside the log under this name.
ANCHOR_NAME = "receipts.anchor"

_LOCK = threading.Lock()


def _home_dir() -> Path:
    return Path.home() / ".aither" / "agent-home"


def default_path() -> Path:
    env = (os.environ.get(PATH_ENV) or "").strip()
    return Path(env) if env else _home_dir() / "actions.jsonl"


def fallback_key_path() -> Path:
    return _home_dir() / "receipt.key"


def _resolve(path) -> Path:
    return Path(path) if path else default_path()


def anchor_path_for(path=None) -> Path:
    """Where the anchor for the log at ``path`` lives: ``<log dir>/receipts.anchor``."""
    return _resolve(path).parent / ANCHOR_NAME


# --------------------------------------------------------------------------- #
# Digests and redaction
# --------------------------------------------------------------------------- #

def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def digest(obj: Any) -> str:
    """sha256 of a payload's canonical JSON (bytes and str are hashed as-is)."""
    if isinstance(obj, bytes):
        data = obj
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
    else:
        data = _canonical(obj)
    return hashlib.sha256(data).hexdigest()


_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|auth|credential|cookie|"
    r"session|private|otp|pin|code|cvv|card|ssn|bearer|key$",
    re.IGNORECASE)
_TOKEN_PATTERNS = [
    re.compile(r"(sk-ant-|sk-|ghp_|ghs_|gho_|xox[bpas]-|pk_live_|sk_live_|"
               r"aither_sk_\w*?_|AKIA)[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"),  # JWT
    re.compile(r"[A-Za-z0-9+/_\-]{24,}={0,2}"),  # long opaque blobs / keys
    re.compile(r"\d{4,}"),  # OTP codes, card and account numbers
]


def _scrub(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(obj, dict):
        return {str(k): ("***" if _SENSITIVE_KEY.search(str(k)) else _scrub(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, str):
        return _scrub_text(obj)
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _scrub_text(str(obj))


def _scrub_text(text: str) -> str:
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return text


def preview(obj: Any, limit: int = PREVIEW_CHARS) -> str:
    """A short, redacted, human-readable view of a payload. Never the payload."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        text = _scrub_text(obj)
    else:
        text = json.dumps(_scrub(obj), sort_keys=True, ensure_ascii=False, default=str)
        # a second pass catches secrets split across the JSON rendering
        text = _scrub_text(text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError:
        return None
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def _load_pem(path: Path):
    crypto = _crypto()
    if crypto is None or not path.is_file():
        return None
    serialization = crypto[0]
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception:  # noqa: BLE001 -- an unreadable key is simply not a candidate
        return None
    return key if isinstance(key, crypto[1]) else None


def _awseal_private_key():
    """The device's awseal key, or None. Never creates one."""
    try:
        from awseal import keys
    except Exception:  # noqa: BLE001 -- optional dependency
        return None
    try:
        return keys.load_private_key()
    except Exception:  # noqa: BLE001 -- no key / no cryptography
        return None


def _fallback_private_key(create: bool):
    path = fallback_key_path()
    key = _load_pem(path)
    if key is not None or not create:
        return key
    crypto = _crypto()
    if crypto is None:
        return None
    serialization, private_cls, _ = crypto
    key = private_cls.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    from adk._private_file import PrivateFileError, create_private_empty

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created EMPTY and restricted (0600 / icacls) before the key is written,
        # so the key never sits in a file carrying the folder's inherited ACL.
        create_private_empty(path)
    except FileExistsError:
        return _load_pem(path)  # a concurrent writer won; use its key
    except PrivateFileError as exc:
        # Fail closed: no key on disk that other users could read. Rows are then
        # written ``signed: false`` -- visible, never a fake signature.
        warnings.warn(f"receipt key NOT created: {exc}", RuntimeWarning, stacklevel=2)
        return None
    except OSError:
        return None
    try:
        with open(path, "wb") as fh:
            fh.write(pem)
    except OSError as exc:
        warnings.warn(f"receipt key NOT written to {path}: {exc}", RuntimeWarning,
                      stacklevel=2)
        path.unlink(missing_ok=True)
        return None
    return key


def _signing_key(key_path=None, create: bool = True):
    """The key ladder: explicit path / env -> awseal -> receipt.key -> None."""
    explicit = key_path or (os.environ.get(KEY_ENV) or "").strip()
    if explicit:
        return _load_pem(Path(explicit))
    return _awseal_private_key() or _fallback_private_key(create)


def _pub_hex(private_key) -> str:
    serialization = _crypto()[0]
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return raw.hex()


def _key_id(pub_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:16]


def _trusted_pubkeys(pubkey: Optional[str], key_path) -> Dict[str, str]:
    """key_id -> pub hex for every key a verifier can check against."""
    found: Dict[str, str] = {}
    hexes: List[str] = []
    for candidate in (pubkey, (os.environ.get(PUBKEY_ENV) or "").strip()):
        if candidate:
            hexes.append(candidate.strip())
    if _crypto() is not None:
        explicit = key_path or (os.environ.get(KEY_ENV) or "").strip()
        privs = [_load_pem(Path(explicit))] if explicit else [
            _awseal_private_key(), _fallback_private_key(create=False)]
        hexes.extend(_pub_hex(k) for k in privs if k is not None)
    for h in hexes:
        try:
            if len(bytes.fromhex(h)) == 32:
                found[_key_id(h)] = h
        except ValueError:
            continue
    return found


# --------------------------------------------------------------------------- #
# Append / read
# --------------------------------------------------------------------------- #

def _last_line(path: Path) -> Optional[bytes]:
    """The last non-empty line of the file, read from the end."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    with path.open("rb") as fh:
        block = 4096
        buf = b""
        pos = size
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            stripped = buf.rstrip(b"\r\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1]
        stripped = buf.rstrip(b"\r\n")
        return stripped or None


def _signable(row: Dict[str, Any]) -> bytes:
    return _canonical({k: v for k, v in row.items() if k != "sig"})


def _ensure_private_log(target: Path) -> None:
    """Create the log owner-only (0600 / icacls) before its first row.

    A log another local user can read carries argument and result previews; one
    they can write lets them splice rows. If it cannot be restricted,
    :class:`adk._private_file.PrivateFileError` is raised and no row is written.
    An existing log is left as it is (it was created by this function, or by an
    older version -- ``adk home trust`` reports it).
    """
    from adk._private_file import create_private_empty

    if target.exists():
        return
    try:
        create_private_empty(target)
    except FileExistsError:
        return  # another process created it first, through this same path


def append(kind: str, name: str, args: Any = None, result: Any = None,
           approval: Any = None, path=None, key_path=None) -> Dict[str, Any]:
    """Write one receipt and return the row as written.

    ``args`` and ``result`` are digested and previewed; the raw values are never
    stored. ``approval`` should be a short token (``"auto"``, ``"owner:yes:ab12"``)
    or a small dict — it is stored as given, so never put a secret in it.
    """
    target = _resolve(path)
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_log(target)
        last = _last_line(target)
        if last is None:
            seq, prev = 0, GENESIS
        else:
            try:
                seq = int(json.loads(last.decode("utf-8"))["seq"]) + 1
            except Exception:  # noqa: BLE001 -- a corrupt tail still gets chained to
                seq = sum(1 for _ in target.open("rb"))
            prev = hashlib.sha256(last).hexdigest()
        row: Dict[str, Any] = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prev_sha256": prev,
            "kind": str(kind),
            "name": str(name),
            "args_sha256": digest(args),
            "args_preview": preview(args),
            "result_sha256": digest(result),
            "result_preview": preview(result),
            "approval": approval,
            "key_id": "",
            "signed": False,
        }
        key = _signing_key(key_path)
        if key is not None:
            row["key_id"] = _key_id(_pub_hex(key))
            row["signed"] = True
            row["sig"] = key.sign(_signable(row)).hex()
        else:
            row["sig"] = ""
        line = json.dumps(row, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str).encode("utf-8")
        with target.open("ab") as fh:
            fh.write(line + b"\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass  # fsync unsupported (some network FS); the write itself landed
    return row


def tail(n: int = 10, path=None) -> List[Dict[str, Any]]:
    """The last ``n`` receipts, oldest first — for the "what did you do?" tool."""
    target = _resolve(path)
    if n <= 0 or not target.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    with _LOCK:
        lines = [ln for ln in target.read_bytes().split(b"\n") if ln.strip()]
    for raw in lines[-n:]:
        try:
            rows.append(json.loads(raw.decode("utf-8")))
        except Exception:  # noqa: BLE001
            rows.append({"error": "unparseable receipt line"})
    return rows


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #

def _load_anchor(anchor_file: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(anchor, "") when present and well-formed; (None, "") when absent;
    (None, reason) when present but unreadable or malformed (= tampered)."""
    if not anchor_file.is_file():
        return None, ""
    try:
        data = json.loads(anchor_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"anchor {anchor_file} unreadable ({type(exc).__name__})"
    if (not isinstance(data, dict) or type(data.get("seq")) is not int
            or data["seq"] < 0 or not isinstance(data.get("sha256"), str)):
        return None, f"anchor {anchor_file} is malformed"
    return data, ""


def _anchor_signature(anc: Dict[str, Any], trusted: Dict[str, str]) -> Tuple[int, str]:
    """0 good · 1 bad signature · 2 unsigned, or signed by a key we cannot check."""
    if not anc.get("signed"):
        return 2, "the anchor is unsigned"
    crypto = _crypto()
    pub_hex = trusted.get(str(anc.get("key_id") or ""))
    if pub_hex is None or crypto is None:
        return 2, f"no key to check the anchor signature (key_id {anc.get('key_id')!r})"
    try:
        crypto[2].from_public_bytes(bytes.fromhex(pub_hex)).verify(
            bytes.fromhex(str(anc.get("sig") or "")), _signable(anc))
    except Exception:  # noqa: BLE001 -- InvalidSignature or malformed hex
        return 1, "anchor: bad signature"
    return 0, ""


def check(path=None, pubkey: Optional[str] = None,
          key_path=None, anchor_path=None) -> Tuple[int, str]:
    """Verify the log. Returns (exit code, one-line reason).

    When an anchor exists (``anchor_path``, default :func:`anchor_path_for`), a log
    that ends before the anchored seq -- or whose anchored line no longer hashes to
    the anchored digest -- is tampered (1) even though its own chain is intact.
    """
    target = _resolve(path)
    anchor_file = Path(anchor_path) if anchor_path else anchor_path_for(target)
    anc, anc_err = _load_anchor(anchor_file)
    if anc_err:
        return 1, anc_err
    if not target.is_file():
        if anc is not None:
            return 1, (f"no receipt log at {target}, but {anchor_file} anchors seq "
                       f"{anc['seq']}: the whole log was removed")
        return 2, f"no receipt log at {target}"
    try:
        data = target.read_bytes()
    except OSError as exc:
        return 2, f"cannot read {target}: {exc}"
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        if anc is not None:
            return 1, (f"{target} is empty, but {anchor_file} anchors seq {anc['seq']}: "
                       "the log was truncated")
        return 2, f"{target} is empty"

    crypto = _crypto()
    trusted = _trusted_pubkeys(pubkey, key_path)
    public_cls = crypto[2] if crypto else None
    prev = GENESIS
    unsigned = 0
    unknown_keys = set()
    anchored_hash = ""
    for index, raw in enumerate(lines):
        where = f"line {index + 1}"
        if raw.endswith(b"\r"):
            return 1, f"{where}: altered line ending"
        try:
            row = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return 1, f"{where}: not valid JSON"
        if not isinstance(row, dict):
            return 1, f"{where}: not a receipt object"
        if row.get("seq") != index:
            return 1, f"{where}: seq {row.get('seq')!r}, expected {index} (line deleted or reordered)"
        if row.get("prev_sha256") != prev:
            return 1, f"{where}: chain broken (prev_sha256 does not match line {index})"
        prev = hashlib.sha256(raw).hexdigest()
        if anc is not None and index == anc["seq"]:
            anchored_hash = prev
        if not row.get("signed"):
            unsigned += 1
            continue
        key_id = str(row.get("key_id") or "")
        pub_hex = trusted.get(key_id)
        if pub_hex is None or public_cls is None:
            unknown_keys.add(key_id or "?")
            continue
        try:
            public_cls.from_public_bytes(bytes.fromhex(pub_hex)).verify(
                bytes.fromhex(str(row.get("sig") or "")), _signable(row))
        except Exception:  # noqa: BLE001 -- InvalidSignature or malformed hex
            return 1, f"{where}: bad signature"
    anchor_note = ""
    if anc is not None:
        last_seq = len(lines) - 1
        if anc["seq"] > last_seq:
            return 1, (f"tail truncated: {anchor_file} anchors seq {anc['seq']}, "
                       f"but the log ends at seq {last_seq}")
        if not hmac.compare_digest(anchored_hash, str(anc["sha256"])):
            return 1, (f"line {anc['seq'] + 1}: does not match the anchored head "
                       f"(sha256 differs from {anchor_file})")
        sig_code, sig_reason = _anchor_signature(anc, trusted)
        if sig_code == 1:
            return 1, sig_reason
        anchor_note = sig_reason
    if unknown_keys:
        return 2, (f"chain intact over {len(lines)} rows, but no key to check "
                   f"signatures from key_id {', '.join(sorted(unknown_keys))}")
    if unsigned:
        return 2, (f"chain intact over {len(lines)} rows, but {unsigned} row(s) "
                   f"are unsigned, so authenticity cannot be judged")
    if anchor_note:
        return 2, f"chain intact over {len(lines)} rows, but {anchor_note}"
    if anc is not None:
        return 0, f"intact: {len(lines)} signed rows, anchored at seq {anc['seq']}"
    return 0, f"intact: {len(lines)} signed rows"


def verify(path=None, pubkey: Optional[str] = None, key_path=None,
           anchor_path=None) -> int:
    """0 intact · 1 tampered · 2 cannot judge. See ``check`` for the reason."""
    return check(path, pubkey=pubkey, key_path=key_path, anchor_path=anchor_path)[0]


# --------------------------------------------------------------------------- #
# Anchor (the signed chain head that makes tail truncation visible)
# --------------------------------------------------------------------------- #

class AnchorError(RuntimeError):
    """The head could not be anchored. ``code`` is the exit code to report (1 or 2)."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def anchor(path=None, anchor_path=None, key_path=None) -> Dict[str, Any]:
    """Sign the current chain head (seq, sha256 of that line) into the anchor file.

    Refuses (:class:`AnchorError`) to anchor a log that is missing, empty or
    already tampered -- including one shorter than an existing anchor -- because
    anchoring it would bless the damage. An intact log is anchored; the anchor is
    signed when a key exists, else written ``signed: false`` (never a fake
    signature, and ``verify`` then says it cannot judge). Returns the anchor.
    """
    from adk._private_file import write_private_text

    target = _resolve(path)
    anchor_file = Path(anchor_path) if anchor_path else anchor_path_for(target)
    code, reason = check(target, key_path=key_path, anchor_path=anchor_file)
    if code == 1:
        raise AnchorError(1, f"not anchoring a tampered log: {reason}")
    with _LOCK:
        last = _last_line(target)
        if last is None:
            raise AnchorError(2, f"nothing to anchor: no receipts at {target}")
        try:
            seq = int(json.loads(last.decode("utf-8"))["seq"])
        except Exception as exc:  # noqa: BLE001 -- reported as cannot-judge
            raise AnchorError(2, f"last receipt unreadable ({type(exc).__name__})") from exc
        row: Dict[str, Any] = {
            "v": 1,
            "log": target.name,
            "seq": seq,
            "sha256": hashlib.sha256(last).hexdigest(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "key_id": "",
            "signed": False,
        }
        key = _signing_key(key_path)
        if key is not None:
            row["key_id"] = _key_id(_pub_hex(key))
            row["signed"] = True
            row["sig"] = key.sign(_signable(row)).hex()
        else:
            row["sig"] = ""
        anchor_file.parent.mkdir(parents=True, exist_ok=True)
        write_private_text(anchor_file, json.dumps(row, sort_keys=True, indent=2) + "\n")
    return row


__all__ = ["append", "tail", "verify", "check", "digest", "preview",
           "default_path", "fallback_key_path", "anchor", "anchor_path_for",
           "AnchorError", "ANCHOR_NAME"]
