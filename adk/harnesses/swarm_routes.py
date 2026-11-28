"""``/swarms/*`` on the AitherShell harness daemon (:8362): SwarmBoard bridge + local harness worker delegation.

Bridges ``awsh`` CLI, ``awsh`` MCP tools, and paired ``awconnect`` browser companions
to Genesis's live ``SwarmBoardEngine`` (``/swarms/*``) while exposing this machine's
local ``awsh`` harness capacity (``claude``, ``codex``, ``gemini``, ``aither``, ``awdk``)
and letting any SwarmBoard worker slot delegate directly to a local harness session
whose ``[done]`` summary and artifacts stream back onto the Swarm Board.

Security (fail-closed):
- Genesis writes (create/steer/pause/control/post) REQUIRE an internal key
  (``AITHER_INTERNAL_SECRET`` / ``AITHER_INTERNAL_KEY`` / ``~/.aither/internal_key``)
  or operator session bearer (``AITHER_SESSION_BEARER`` / ``~/.aither/session-bearer``);
  missing credentials raise :class:`SwarmAuthError` (HTTP 401) and secrets are never
  printed or logged.
- Launch, pause, control, and harness delegation on ``:8362`` are strictly owner-only
  (``require_owner``); ``POST /swarms/{id}/post`` and ``POST /swarms/{id}/nudge``
  require an owner or dedicated ``plan="agent"`` token (``force_escalate=True`` is
  owner-only); paired ``awconnect`` tokens may only read ``GET /swarms*`` and post
  attributed human steers (``POST /swarms/{id}/steer``).
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = Any  # type: ignore[misc, assignment]

logger = logging.getLogger("adk.harnesses.swarm_routes")

__all__ = [
    "LocalSwarmCoordinator",
    "SWARM_HARNESSES",
    "SwarmApiError",
    "SwarmAuthError",
    "SwarmGenesisClient",
    "SwarmLeaseConflictError",
    "attach_engine",
    "bind_slot_worktree_and_leases",
    "delegate_harness_worker",
    "extract_session_artifacts",
    "extract_session_summary",
    "local_swarm_capacity",
    "local_swarms_dir",
    "mount",
    "release_slot_bindings",
    "swarm_ui_url",
    "verify_or_seal_track_artifacts",
]

#: Core coding/agent harnesses a SwarmBoard worker slot can delegate to on this machine.
SWARM_HARNESSES: tuple[str, ...] = ("claude", "codex", "gemini", "aither", "awdk")
VALID_EXECUTION_MODES: tuple[str, ...] = ("inline", "standard", "awrun")
VALID_INFERENCE_RUNTIMES: tuple[str, ...] = ("standard", "asm-llm", "awzeal")

_SWARM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$")
_ARTIFACT_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:Screenshot|Artifact)\s*:\s*([^\s|\n]+)(?:\s*\|\s*([^\n]+))?",
    re.I,
)
_CA_CANDIDATES = (
    "/usr/local/share/aither/ca-chain.pem",
    "/etc/aither/ca-chain.pem",
)
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "aitheros-genesis"}


class SwarmApiError(RuntimeError):
    """Error talking to Genesis ``/swarms/*`` (carries HTTP status)."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class SwarmAuthError(SwarmApiError):
    """Fail-closed auth refusal when no Genesis credential is configured."""

    def __init__(self, message: str = "Swarm write refused: missing AITHER_INTERNAL_SECRET or session bearer") -> None:
        super().__init__(message, status=401)


class SwarmLeaseConflictError(SwarmApiError):
    """Raised when ``awgit lease acquire`` hits an active conflicting lease."""

    def __init__(self, message: str = "lease conflict") -> None:
        super().__init__(message, status=409)


def _load_awrun() -> tuple[Any, Any, Any]:
    """Lazily import ``awrun`` store/confine/handoff behind ``ImportError``."""
    try:
        from awrun import confine as _confine, handoff as _handoff, store as _store

        return _store, _confine, _handoff
    except ImportError:
        return None, None, None


def _load_awseal() -> Any:
    """Lazily import ``awseal.seal`` behind ``ImportError``."""
    try:
        from awseal import seal as _awseal

        return _awseal
    except ImportError:
        return None


def _load_awzeal_provider() -> Any:
    """Lazily import optional ``adk.llm.awzeal.AwzealZeroLibcProvider``."""
    try:
        from adk.llm.awzeal import AwzealZeroLibcProvider

        return AwzealZeroLibcProvider
    except ImportError:
        return None


def _validate_execution_mode(mode: Optional[str]) -> str:
    val = str(mode or "standard").strip().lower() or "standard"
    if val not in VALID_EXECUTION_MODES:
        raise SwarmApiError(
            f"invalid execution_mode {val!r}; expected one of {VALID_EXECUTION_MODES}",
            status=422,
        )
    return val


def _validate_inference_runtime(runtime: Optional[str]) -> str:
    val = str(runtime or "standard").strip().lower() or "standard"
    if val not in VALID_INFERENCE_RUNTIMES:
        raise SwarmApiError(
            f"invalid inference_runtime {val!r}; expected one of {VALID_INFERENCE_RUNTIMES}",
            status=422,
        )
    return val


def _parse_pool_count(raw: Any, *, field: str = "count") -> int:
    if isinstance(raw, bool):
        raise SwarmApiError(f"{field} must be a positive integer, got bool", status=422)
    if isinstance(raw, float):
        raise SwarmApiError(f"{field} must be a positive integer, got float", status=422)
    try:
        count = int(raw)
    except (ValueError, TypeError) as exc:
        raise SwarmApiError(
            f"{field} must be a positive integer, got {raw!r}",
            status=422,
        ) from exc
    if count <= 0:
        raise SwarmApiError(f"{field} must be >= 1, got {count}", status=422)
    return count


def _parse_str_list(raw: Any, *, field: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise SwarmApiError(
            f"{field} must be a list of strings, got {type(raw).__name__}",
            status=422,
        )
    return [str(x).strip() for x in raw if x is not None and str(x).strip()]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_tree_fallback(root: Path) -> Dict[str, Any]:
    """Standalone SHA-256 tree digest fallback when ``awseal`` is absent.

    Explicitly sets ``sealed=False`` and ``ed25519=False`` so callers never mistake
    a plain content digest for a cryptographic Ed25519 signature.
    """
    files_map: Dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "awseal.json":
            continue
        files_map[rel] = _sha256_file(p)
    h = hashlib.sha256()
    for rel in sorted(files_map):
        h.update(f"{rel}:{files_map[rel]}\n".encode("utf-8"))
    return {
        "ok": True,
        "sealed": False,
        "ed25519": False,
        "scheme": "sha256-fallback",
        "tree_digest": h.hexdigest(),
        "files": files_map,
    }


@dataclass
class _CompatSeal:
    version: int
    tree_digest: str
    files: Dict[str, str]
    public_key: str
    signature: str
    created: str
    subject: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def payload(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "tree_digest": self.tree_digest,
            "files": self.files,
            "created": self.created,
            "subject": self.subject,
            "meta": self.meta,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = self.payload()
        d["public_key"] = self.public_key
        d["signature"] = self.signature
        return d


def _compat_canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _compat_tree_digest(files: Dict[str, str]) -> str:
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compat_diff(expected: Dict[str, str], actual: Dict[str, str]) -> Dict[str, List[str]]:
    exp_keys = set(expected)
    act_keys = set(actual)
    return {
        "added": sorted(act_keys - exp_keys),
        "removed": sorted(exp_keys - act_keys),
        "modified": sorted(k for k in (exp_keys & act_keys) if expected[k] != actual[k]),
    }


def _resolve_awseal_primitives(awseal_mod: Any) -> Dict[str, Any]:
    """Resolve ``awseal`` helpers across ``0.1.1`` and ``0.1.2+`` without hard imports."""
    mod_keys = getattr(awseal_mod, "_keys", None)
    seal_error_cls = getattr(awseal_mod, "SealError", None)
    load_public_key = getattr(awseal_mod, "load_public_key", None) or getattr(
        mod_keys, "load_public_key", None
    )
    load_private_key = getattr(awseal_mod, "load_private_key", None) or getattr(
        mod_keys, "load_private_key", None
    )
    public_key_hex = getattr(awseal_mod, "public_key_hex", None) or getattr(
        mod_keys, "public_key_hex", None
    )
    seal_cls = getattr(awseal_mod, "Seal", None)
    seal_version = getattr(awseal_mod, "SEAL_VERSION", 1)
    seal_canonical = getattr(awseal_mod, "canonical", None)
    tree_digest_fn = getattr(awseal_mod, "tree_digest", None)
    diff_fn = getattr(awseal_mod, "diff", None)

    try:
        if getattr(awseal_mod, "__name__", None) == "awseal" or callable(load_public_key):
            import awseal as pkg_awseal  # noqa: PLC0415

            if seal_error_cls is None:
                seal_error_cls = getattr(pkg_awseal, "SealError", None)
            if load_public_key is None:
                load_public_key = getattr(pkg_awseal, "load_public_key", None)
            if load_private_key is None:
                load_private_key = getattr(pkg_awseal, "load_private_key", None)
            if public_key_hex is None:
                public_key_hex = getattr(pkg_awseal, "public_key_hex", None)
            if seal_cls is None:
                seal_cls = getattr(pkg_awseal, "Seal", None)
            if not seal_canonical:
                try:
                    from awseal.seal import canonical as pkg_canonical  # noqa: PLC0415

                    seal_canonical = pkg_canonical
                except ImportError:
                    pass
            if tree_digest_fn is None:
                tree_digest_fn = getattr(pkg_awseal, "tree_digest", None)
            if diff_fn is None:
                diff_fn = getattr(pkg_awseal, "diff", None)
    except ImportError:
        pass

    return {
        "SealError": seal_error_cls or RuntimeError,
        "load_public_key": load_public_key,
        "load_private_key": load_private_key,
        "public_key_hex": public_key_hex,
        "Seal": seal_cls or _CompatSeal,
        "SEAL_VERSION": int(seal_version or 1),
        "canonical": seal_canonical or _compat_canonical,
        "tree_digest": tree_digest_fn or _compat_tree_digest,
        "diff": diff_fn or _compat_diff,
    }


def _from_dict_compat(d: Dict[str, Any], awseal_mod: Any) -> Any:
    prims = _resolve_awseal_primitives(awseal_mod)
    if not callable(prims["load_public_key"]):
        raise SwarmApiError(
            "awseal load_public_key is unavailable in installed awseal version",
            status=503,
        )
    seal_error_cls = prims["SealError"]
    seal_version = prims["SEAL_VERSION"]
    seal_cls = prims["Seal"]
    if not isinstance(d, dict):
        raise seal_error_cls("seal is not a seal document")
    version = d.get("version")
    if version != seal_version:
        raise seal_error_cls(f"seal version {version!r} != {seal_version}")
    for req in ("tree_digest", "files", "public_key", "signature", "created"):
        if req not in d:
            raise seal_error_cls(f"seal missing required field {req!r}")
    if not isinstance(d["files"], dict) or not d["files"]:
        raise seal_error_cls("seal covers no files -- refusing")
    return seal_cls(
        version=version,
        tree_digest=d["tree_digest"],
        files=d["files"],
        public_key=d["public_key"],
        signature=d["signature"],
        created=d["created"],
        subject=d.get("subject", ""),
        meta=d.get("meta") or {},
    )


def _verify_files_compat(
    seal: Any,
    files: Dict[str, str],
    *,
    expect_key: Optional[str],
    awseal_mod: Any,
) -> Dict[str, Any]:
    """Verify ``seal`` against ``files`` using ``awseal >= 0.1.0`` primitives."""
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
    except ImportError as exc:
        raise SwarmApiError(
            f"cryptography is required for awseal verification: {exc}", status=503
        ) from exc

    prims = _resolve_awseal_primitives(awseal_mod)
    load_public_key = prims["load_public_key"]
    if not callable(load_public_key):
        raise SwarmApiError(
            "awseal load_public_key is unavailable in installed awseal version",
            status=503,
        )
    seal_canonical = prims["canonical"]
    tree_digest_fn = prims["tree_digest"]
    seal_diff_fn = prims["diff"]

    result: Dict[str, Any] = {
        "subject": seal.subject,
        "created": seal.created,
        "public_key": seal.public_key,
        "signature_ok": False,
        "content_ok": False,
        "key_trusted": None,
        "diff": {"added": [], "removed": [], "modified": []},
    }
    pub = load_public_key(seal.public_key)
    try:
        pub.verify(bytes.fromhex(seal.signature), seal_canonical(seal.payload()))
        result["signature_ok"] = True
    except (InvalidSignature, ValueError, TypeError):
        result["signature_ok"] = False
    actual = {str(k): str(v) for k, v in dict(files or {}).items()}
    result["content_ok"] = (
        tree_digest_fn(actual) == seal.tree_digest
        and tree_digest_fn(seal.files) == seal.tree_digest
    )
    result["diff"] = seal_diff_fn(seal.files, actual)
    if expect_key is not None:
        result["key_trusted"] = (
            str(seal.public_key).strip().lower() == expect_key.strip().lower()
        )
    result["ok"] = bool(
        result["signature_ok"]
        and result["content_ok"]
        and (result["key_trusted"] is not False)
    )
    return result


def _sign_files_compat(
    files: Dict[str, str],
    *,
    key_path: Optional[Path] = None,
    subject: str = "",
    meta: Optional[Dict[str, Any]] = None,
    awseal_mod: Any,
) -> Any:
    """Sign a ``{posix_path: sha256}`` map using ``awseal >= 0.1.0`` primitives."""
    prims = _resolve_awseal_primitives(awseal_mod)
    seal_error_cls = prims["SealError"]
    load_private_key = prims["load_private_key"]
    public_key_hex = prims["public_key_hex"]
    if not callable(load_private_key) or not callable(public_key_hex):
        raise SwarmApiError(
            "awseal load_private_key/public_key_hex unavailable in installed awseal version",
            status=503,
        )
    if not isinstance(files, dict) or not files:
        raise seal_error_cls(
            "refusing to seal an empty file map: a valid signature over nothing"
        )
    for path_key, file_dig in files.items():
        d = str(file_dig)
        if len(d) != 64 or any(c not in "0123456789abcdef" for c in d):
            raise seal_error_cls(f"{path_key!r}: {file_dig!r} is not a sha256 hex digest")
    private = load_private_key(key_path)
    clean = {str(k): str(v) for k, v in files.items()}
    seal_cls = prims["Seal"]
    seal_obj = seal_cls(
        version=prims["SEAL_VERSION"],
        tree_digest=prims["tree_digest"](clean),
        files=clean,
        public_key=public_key_hex(private),
        signature="",
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        subject=subject,
        meta=dict(meta or {}),
    )
    seal_obj.signature = private.sign(prims["canonical"](seal_obj.payload())).hex()
    return seal_obj


def verify_or_seal_track_artifacts(
    *,
    artifact_dir: str = "",
    artifacts: Sequence[Any] = (),
    seal_artifacts: bool = False,
    expect_key: str = "",
    key_path: str = "",
    seal_doc: Optional[Dict[str, Any]] = None,
    subject: str = "",
    kind: str = "update",
) -> Optional[Dict[str, Any]]:
    """Verify or seal track artifacts via ``awseal`` (or SHA-256 standalone fallback).

    Fails closed whenever ``awseal.json`` or ``expect_key`` is present and
    verification fails or ``awseal`` is unavailable.
    """
    art_dir_str = str(artifact_dir or "").strip()
    exp_key = str(expect_key or "").strip()
    kp_str = str(key_path or "").strip()
    want_seal = bool(seal_artifacts)

    if not art_dir_str and not exp_key and not want_seal and not seal_doc:
        return None

    awseal_mod = _load_awseal()

    if art_dir_str:
        root = Path(art_dir_str).expanduser()
        if not root.is_dir():
            raise SwarmApiError(f"artifact_dir does not exist: {art_dir_str}", status=422)
        seal_file = root / "awseal.json"
        if want_seal and kp_str and kind == "done" and not seal_file.is_file():
            if (
                awseal_mod is not None
                and callable(getattr(awseal_mod, "sign", None))
                and callable(getattr(awseal_mod, "write", None))
            ):
                try:
                    seal_obj = awseal_mod.sign(
                        root,
                        key_path=Path(kp_str),
                        subject=subject or root.name,
                    )
                    if exp_key and str(getattr(seal_obj, "public_key", "")).lower() != exp_key.lower():
                        raise SwarmApiError(
                            "artifact sealing key does not match expect_key",
                            status=422,
                        )
                    awseal_mod.write(seal_obj, root)
                    return {
                        "ok": True,
                        "verified": True,
                        "sealed": True,
                        "ed25519": True,
                        "key_trusted": True if exp_key else None,
                        "scheme": "awseal-ed25519",
                        "tree_digest": seal_obj.tree_digest,
                        "public_key": seal_obj.public_key,
                        "files": dict(seal_obj.files),
                    }
                except SwarmApiError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise SwarmApiError(f"artifact sealing failed: {exc}", status=422) from exc
            if exp_key:
                raise SwarmApiError(
                    "awseal verification required (expect_key present) "
                    "but awseal package is unavailable",
                    status=503,
                )
        if seal_file.is_file() or exp_key:
            if awseal_mod is None or not callable(getattr(awseal_mod, "verify", None)):
                raise SwarmApiError(
                    "awseal verification required (awseal.json or expect_key present) "
                    "but awseal package is unavailable",
                    status=503,
                )
            try:
                verdict = awseal_mod.verify(root, expect_key=exp_key or None)
            except Exception as exc:  # noqa: BLE001
                raise SwarmApiError(f"artifact seal verification failed: {exc}", status=422) from exc
            if not verdict.get("ok") or (exp_key and verdict.get("key_trusted") is not True):
                raise SwarmApiError(
                    "artifact seal verification failed: "
                    + json.dumps(
                        {
                            "signature_ok": verdict.get("signature_ok"),
                            "content_ok": verdict.get("content_ok"),
                            "key_trusted": verdict.get("key_trusted"),
                            "diff": verdict.get("diff"),
                        }
                    ),
                    status=422,
                )
            return {
                "ok": True,
                "verified": True,
                "sealed": True,
                "ed25519": True,
                "scheme": "awseal-ed25519",
                **verdict,
            }

        if want_seal or (kind == "done" and kp_str):
            if (
                awseal_mod is not None
                and callable(getattr(awseal_mod, "sign", None))
                and callable(getattr(awseal_mod, "write", None))
            ):
                try:
                    seal_obj = awseal_mod.sign(
                        root,
                        key_path=Path(kp_str) if kp_str else None,
                        subject=subject or root.name,
                    )
                    awseal_mod.write(seal_obj, root)
                    return {
                        "ok": True,
                        "verified": True,
                        "sealed": True,
                        "ed25519": True,
                        "scheme": "awseal-ed25519",
                        "tree_digest": seal_obj.tree_digest,
                        "public_key": seal_obj.public_key,
                        "files": dict(seal_obj.files),
                    }
                except Exception as exc:  # noqa: BLE001
                    raise SwarmApiError(f"artifact sealing failed: {exc}", status=422) from exc
            return _sha256_tree_fallback(root)

        if kind == "done":
            return _sha256_tree_fallback(root)
        return None

    # File-map manifest verification/sealing when artifact_dir is omitted
    files_map: Dict[str, str] = {}
    for item in artifacts or ():
        if isinstance(item, dict):
            p = str(item.get("path") or "").strip()
            dig = str(item.get("sha256") or "").strip().lower()
            if p:
                if not dig and Path(p).is_file():
                    dig = _sha256_file(Path(p))
                if dig:
                    files_map[p] = dig
        elif isinstance(item, str) and Path(item).is_file():
            files_map[item] = _sha256_file(Path(item))

    if seal_doc is not None or exp_key:
        if awseal_mod is None:
            raise SwarmApiError(
                "awseal verification required (seal or expect_key present) "
                "but awseal package is unavailable",
                status=503,
            )
        if not isinstance(seal_doc, dict):
            raise SwarmApiError("expect_key requires artifact_dir or seal document", status=422)
        from_dict_fn = getattr(awseal_mod, "from_dict", None)
        verify_files_fn = getattr(awseal_mod, "verify_files", None)
        try:
            seal_obj = (
                from_dict_fn(seal_doc)
                if callable(from_dict_fn)
                else _from_dict_compat(seal_doc, awseal_mod)
            )
            verdict = (
                verify_files_fn(seal_obj, files_map, expect_key=exp_key or None)
                if callable(verify_files_fn)
                else _verify_files_compat(
                    seal_obj,
                    files_map,
                    expect_key=exp_key or None,
                    awseal_mod=awseal_mod,
                )
            )
        except SwarmApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SwarmApiError(f"artifact manifest seal verification failed: {exc}", status=422) from exc
        if not verdict.get("ok") or (exp_key and verdict.get("key_trusted") is not True):
            raise SwarmApiError("artifact manifest seal verification failed", status=422)
        return {
            "ok": True,
            "verified": True,
            "sealed": True,
            "ed25519": True,
            "scheme": "awseal-ed25519",
            **verdict,
        }

    if want_seal and files_map:
        if awseal_mod is not None and kp_str:
            sign_files_fn = getattr(awseal_mod, "sign_files", None)
            try:
                if callable(sign_files_fn):
                    seal_obj = sign_files_fn(
                        files_map,
                        key_path=Path(kp_str) if kp_str else None,
                        subject=subject,
                    )
                else:
                    seal_obj = _sign_files_compat(
                        files_map,
                        key_path=Path(kp_str) if kp_str else None,
                        subject=subject,
                        awseal_mod=awseal_mod,
                    )
                return {
                    "ok": True,
                    "verified": True,
                    "sealed": True,
                    "ed25519": True,
                    "scheme": "awseal-ed25519",
                    "seal": seal_obj.to_dict(),
                }
            except SwarmApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SwarmApiError(f"artifact manifest sealing failed: {exc}", status=422) from exc
        h = hashlib.sha256()
        for rel in sorted(files_map):
            h.update(f"{rel}:{files_map[rel]}\n".encode("utf-8"))
        return {
            "ok": True,
            "sealed": False,
            "ed25519": False,
            "scheme": "sha256-fallback",
            "tree_digest": h.hexdigest(),
            "files": files_map,
        }
    return None


def _safe_lineage_id(val: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:/@#-]+", "-", str(val or "").strip()).strip("-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"s{cleaned}"
    return cleaned[:199]


def _prepare_awrun_track(
    *,
    swarm_id: str,
    thread_id: str,
    slot_id: str,
    pool: str = "",
    owns: Sequence[str] = (),
    execution_mode: str = "standard",
    limits: Optional[Dict[str, Any]] = None,
    egress: Optional[Dict[str, Any]] = None,
    isolation: Optional[Dict[str, Any]] = None,
    base_argv: Sequence[str] = (),
    existing_run_id: str = "",
    start_now: bool = False,
    network_probe: Any = None,
) -> Dict[str, Any]:
    """Validate confinement and record/claim an ``awrun`` ``RunItem`` when requested."""
    mode = _validate_execution_mode(execution_mode)
    if (
        mode != "awrun"
        and limits is None
        and egress is None
        and isolation is None
        and not str(existing_run_id or "").strip()
    ):
        return {
            "awrun_available": _load_awrun()[0] is not None,
            "run_id": "",
            "wrapped_argv": [],
            "awrun_dir": "",
        }

    store_mod, confine_mod, _handoff_mod = _load_awrun()
    heavy_demands = bool(
        egress is not None
        or (isinstance(limits, dict) and any(k != "timeout_s" for k in limits))
        or (isinstance(isolation, dict) and isolation.get("mode") == "container")
    )
    if store_mod is None or confine_mod is None:
        if heavy_demands:
            raise SwarmApiError(
                "awrun confinement requested (limits/egress/isolation) but awrun package is unavailable; "
                "refusing to run unconfined",
                status=503,
            )
        return {"awrun_available": False, "run_id": "", "wrapped_argv": [], "awrun_dir": ""}

    spec: Dict[str, Any] = {
        "swarm_lineage": {
            "swarm_id": swarm_id,
            "thread_id": thread_id,
            "slot_id": slot_id,
            "pool": pool,
        },
        "swarm_id": swarm_id,
        "thread_id": thread_id,
        "slot_id": slot_id,
        "pool": pool,
    }
    if isolation is not None and not isinstance(isolation, dict):
        raise SwarmApiError(
            f"isolation must be a mapping, got {type(isolation).__name__}",
            status=422,
        )
    if isolation:
        spec["isolation"] = dict(isolation)
    lineage: Dict[str, str] = {"goal": _safe_lineage_id(swarm_id)}
    if thread_id:
        lineage["plan"] = _safe_lineage_id(thread_id)

    try:
        clean_limits = store_mod.validate_limits(limits)
        clean_egress = store_mod.validate_egress(egress)
        clean_lineage = store_mod.validate_lineage(lineage)
    except store_mod.RunError as exc:
        raise SwarmApiError(str(exc), status=422) from exc

    cand = store_mod.RunItem(
        id="r-2222",
        kind="agent",
        spec=spec,
        paths=[str(p) for p in (owns or ()) if str(p or "").strip()],
        lineage=clean_lineage,
        limits=clean_limits,
        egress=clean_egress,
    )
    gap = (
        confine_mod.enforcement_gap(cand, probe=network_probe)
        if network_probe is not None
        else confine_mod.enforcement_gap(cand)
    )
    if gap is not None:
        raise SwarmApiError(gap, status=422)

    wrapped = (
        confine_mod.wrap_argv(cand, list(base_argv))
        if (base_argv and confine_mod.isolation(cand) is not None)
        else []
    )
    if wrapped:
        spec["wrapped_argv"] = wrapped

    store = store_mod.RunStore()
    worker_id = f"swarm:{swarm_id}:{slot_id}"
    prior_id = str(existing_run_id or "").strip()
    item: Any = None
    if prior_id:
        try:
            cur = store.get(prior_id)
        except Exception:  # noqa: BLE001
            cur = None
        if cur is not None and cur.status in store_mod.OPEN_STATUSES:
            if cur.status == store_mod.STATUS_RUNNING:
                # Re-delegating an already-running slot: finish/cancel prior run cleanly so none leak.
                try:
                    store.cancel(prior_id)
                except Exception:  # noqa: BLE001
                    pass
            else:
                if cur.status == store_mod.STATUS_SUSPENDED:
                    try:
                        store.resume(prior_id)
                        cur = store.get(prior_id)
                    except Exception:  # noqa: BLE001
                        cur = None
                if cur is not None and start_now:
                    if cur.status == store_mod.STATUS_QUEUED:
                        store.claim(prior_id, worker_id=worker_id)
                        cur = store.get(prior_id)
                    if cur is not None and cur.status == store_mod.STATUS_CLAIMED:
                        cur = store.start(prior_id)
                if cur is not None and cur.status in store_mod.OPEN_STATUSES:
                    cur.spec = dict(spec)
                    cur.paths = list(cand.paths)
                    cur.lineage = clean_lineage
                    cur.limits = clean_limits
                    cur.egress = clean_egress
                    cur.updated_at = time.time()
                    if hasattr(store, "_write"):
                        store._write(cur.status, cur)
                    item = cur

    if item is None:
        try:
            item = store.submit(
                "agent",
                spec,
                paths=cand.paths,
                lineage=clean_lineage,
                limits=clean_limits,
                egress=clean_egress,
            )
        except store_mod.RunError as exc:
            raise SwarmApiError(str(exc), status=422) from exc

        if start_now:
            if store.claim(item.id, worker_id=worker_id) is not None:
                store.start(item.id)

    return {
        "awrun_available": True,
        "run_id": item.id,
        "wrapped_argv": wrapped,
        "awrun_dir": str(getattr(store, "path", "") or os.environ.get("AITHER_AWRUN_DIR") or ""),
    }


def _resolve_awzeal_metadata(
    *,
    inference_runtime: str = "standard",
    shared_ro_mmap: bool = False,
    model_path: str = "",
    model_sha256: str = "",
    cpu_pin: str = "",
) -> Dict[str, Any]:
    rt = _validate_inference_runtime(inference_runtime)
    provider_cls = _load_awzeal_provider()
    shared = bool(shared_ro_mmap)
    return {
        "inference_runtime": rt,
        "shared_ro_mmap": shared,
        "shared_ro_mode": "D5_shared_ro" if shared else "",
        "model_path": str(model_path or "").strip(),
        "model_sha256": str(model_sha256 or "").strip(),
        "cpu_pin": str(cpu_pin or "").strip(),
        "awzeal_provider_available": provider_cls is not None,
    }


def _genesis_base() -> str:
    """Resolve the host-reachable Genesis Swarm API base URL.

    Resolution order:
    1. ``AITHER_SWARM_API_URL`` (explicit Swarm API override, e.g. an in-network
       ``https://aitheros-genesis:8001`` or a custom portal/gateway bridge).
    2. ``adk.control_plane.genesis_override()`` (``AITHER_GENESIS_URL`` /
       ``AITHER_API_URL``, skipping MCP gateway hosts that do not serve Genesis).
    3. ``adk.control_plane.genesis_data_base()`` (``<control plane>/api/bridge/genesis``,
       default ``https://api.aitherium.com/api/bridge/genesis``) — NEVER
       ``https://127.0.0.1:8001``, since Genesis publishes no host port.
    """
    explicit = (os.environ.get("AITHER_SWARM_API_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from adk.control_plane import genesis_data_base  # noqa: PLC0415

        return genesis_data_base().rstrip("/")
    except Exception:  # noqa: BLE001
        fallback = (
            os.environ.get("AITHER_GENESIS_URL")
            or os.environ.get("AITHER_API_URL")
            or "https://api.aitherium.com/api/bridge/genesis"
        ).strip()
        return fallback.rstrip("/")


def swarm_ui_url(swarm: Optional[Dict[str, Any]] = None, swarm_id: str = "") -> str:
    """Canonical 4-tab Live Swarm UI URL (explicit override -> swarm payload -> default)."""
    base = (os.environ.get("AITHER_SWARM_UI_URL") or "https://app.aitherium.com/swarm").strip().rstrip("/")
    if isinstance(swarm, dict):
        raw = str(swarm.get("ui_url") or "").strip()
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith("/"):
            return urllib.parse.urljoin(base + "/", raw)
    sid = str(swarm_id or (swarm.get("swarm_id") if isinstance(swarm, dict) else "") or "").strip()
    return f"{base}?id={urllib.parse.quote(sid)}" if sid else base


def _internal_key() -> str:
    for var in ("AITHER_INTERNAL_SECRET", "AITHER_INTERNAL_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        return (Path.home() / ".aither" / "internal_key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _session_bearer() -> str:
    for var in ("AITHER_SESSION_BEARER", "AITHER_API_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        tok = (Path.home() / ".aither" / "session-bearer").read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    try:
        from adk.config import load_saved_config  # noqa: PLC0415

        cfg = load_saved_config()
        tok = str(cfg.get("access_token") or cfg.get("api_key") or "").strip()
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        pass
    return ""


def _ssl_context_for(url: str) -> Optional[ssl.SSLContext]:
    if not url.lower().startswith("https://"):
        return None
    ca = (os.environ.get("AITHER_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or "").strip()
    if ca and Path(ca).is_file():
        return ssl.create_default_context(cafile=ca)
    for cand in _CA_CANDIDATES:
        if Path(cand).is_file():
            return ssl.create_default_context(cafile=cand)
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _validate_swarm_id(swarm_id: str) -> str:
    sid = str(swarm_id or "").strip()
    if not _SWARM_ID_RE.match(sid) or ".." in sid:
        raise SwarmApiError(f"invalid swarm_id {sid!r}", status=422)
    return sid


class SwarmGenesisClient:
    """Thin fail-closed HTTP client for Genesis ``/swarms/*``."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        internal_key: Optional[str] = None,
        bearer: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.base_url = (base_url if base_url is not None else _genesis_base()).rstrip("/")
        self._explicit_key = internal_key
        self._explicit_bearer = bearer
        self._opener = opener
        self.timeout_s = timeout_s

    def _auth_headers(self, *, require_auth: bool) -> Dict[str, str]:
        key = _internal_key() if self._explicit_key is None else self._explicit_key
        bearer = _session_bearer() if self._explicit_bearer is None else self._explicit_bearer
        headers: Dict[str, str] = {"Accept": "application/json"}
        if key:
            headers["X-Internal-Key"] = key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if require_auth and not (key or bearer):
            raise SwarmAuthError()
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        require_auth: bool = False,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers(require_auth=require_auth)
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        opener = self._opener or urllib.request.urlopen
        try:
            if self._opener is not None:
                resp = opener(req, timeout=self.timeout_s)
            else:
                resp = opener(req, timeout=self.timeout_s, context=_ssl_context_for(url))
            with resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err_raw = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(err_raw) if err_raw else {}
                detail = str(parsed.get("detail") or parsed.get("error") or err_raw[:240])
            except Exception:  # noqa: BLE001
                detail = str(exc.reason or exc.code)
            raise SwarmApiError(detail or f"Genesis HTTP {exc.code}", status=int(exc.code)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SwarmApiError(f"Genesis unreachable at {self.base_url}: {exc}", status=503) from exc
        if not raw.strip():
            return {}
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SwarmApiError(f"Genesis returned non-JSON for {path}", status=502) from exc
        return out if isinstance(out, dict) else {"data": out}

    def list_swarms(
        self,
        *,
        status: Optional[str] = None,
        guild: Optional[str] = None,
        channel: Optional[str] = None,
        thread: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            params["status"] = status
        if guild:
            params["guild"] = guild
        if channel:
            params["channel"] = channel
        if thread:
            params["thread"] = thread
        qs = urllib.parse.urlencode(params)
        return self.request("GET", f"/swarms?{qs}", require_auth=False)

    def get_swarm(self, swarm_id: str) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("GET", f"/swarms/{urllib.parse.quote(sid)}", require_auth=False)

    def get_board(self, swarm_id: str, *, since: int = 0, limit: int = 100) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        qs = urllib.parse.urlencode({"since": max(0, int(since)), "limit": max(1, min(int(limit), 500))})
        return self.request("GET", f"/swarms/{urllib.parse.quote(sid)}/board?{qs}", require_auth=False)

    def create_swarm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/swarms", payload, require_auth=True)

    def steer_swarm(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/steer", payload, require_auth=True)

    def pause_swarm(self, swarm_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/pause", payload or {}, require_auth=True)

    def control_swarm(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/control", payload, require_auth=True)

    def post_message(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/post", payload, require_auth=True)

    def nudge_slot(self, swarm_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        sid = _validate_swarm_id(swarm_id)
        return self.request("POST", f"/swarms/{urllib.parse.quote(sid)}/nudge", payload or {}, require_auth=True)

    def probe_reachable(self, timeout_s: float = 3.0) -> Dict[str, Any]:
        prev_timeout = self.timeout_s
        self.timeout_s = min(prev_timeout, max(0.5, float(timeout_s)))
        try:
            data = self.list_swarms(limit=1)
            return {
                "reachable": True,
                "url": f"{self.base_url}/swarms",
                "count": int(data.get("count") or len(data.get("swarms") or [])),
            }
        except SwarmApiError as exc:
            return {
                "reachable": False,
                "url": f"{self.base_url}/swarms",
                "status": exc.status,
                "error": str(exc),
                "hint": "Set AITHER_SWARM_API_URL to a reachable Genesis or control-plane bridge URL.",
            }
        finally:
            self.timeout_s = prev_timeout


def _detect_installed_harnesses() -> List[str]:
    try:
        from adk.harnesses.registry import SPECS, detect

        out: List[str] = []
        for hid in SPECS:
            info = detect(hid)
            if info.get("available"):
                out.append(hid)
        return sorted(out)
    except Exception:  # noqa: BLE001
        return []


def local_swarm_capacity(
    mgr: Any = None,
    *,
    genesis: Optional[SwarmGenesisClient] = None,
    installed_ids: Optional[Sequence[str]] = None,
    probe_genesis: bool = False,
) -> Dict[str, Any]:
    """Advertise this machine's local Swarm + ``awsh`` harness execution capacity."""
    from adk.harnesses.manager import MAX_SESSIONS

    installed = list(installed_ids) if installed_ids is not None else _detect_installed_harnesses()
    swarm_harnesses = [h for h in SWARM_HARNESSES if h in installed]
    sessions = mgr.list_sessions() if mgr is not None and callable(getattr(mgr, "list_sessions", None)) else []
    active = sum(
        1 for s in sessions
        if str(getattr(s, "state", "") or (s.get("state") if isinstance(s, dict) else "")).lower()
        not in ("exited", "failed", "sessionstate.exited", "sessionstate.failed")
    )
    host = os.environ.get("AITHER_HARNESS_HOST", "127.0.0.1")
    port = os.environ.get("AITHER_HARNESS_PORT", "8362")
    harness_url = (os.environ.get("AITHER_HARNESS_URL") or f"http://{host}:{port}").rstrip("/")
    local_model_url = (
        os.environ.get("AITHER_LOCAL_MODEL_URL") or os.environ.get("AITHER_LLM_URL") or ""
    ).strip() or None
    client = genesis or SwarmGenesisClient()
    store_mod, _confine_mod, _handoff_mod = _load_awrun()
    awrun_available = store_mod is not None
    awrun_active_runs: List[str] = []
    if store_mod is not None:
        try:
            open_statuses = list(getattr(store_mod, "OPEN_STATUSES", ("queued", "claimed", "running", "suspended")))
            awrun_active_runs = [
                it.id for it in store_mod.RunStore().list(statuses=open_statuses, kind="agent")
            ]
        except Exception:  # noqa: BLE001
            awrun_active_runs = []
    cap: Dict[str, Any] = {
        "node": socket.gethostname(),
        "harness_url": harness_url,
        "genesis_url": client.base_url,
        "swarms_endpoint": "/swarms",
        "ui_url": swarm_ui_url(),
        "harnesses_installed": installed,
        "swarm_harnesses": swarm_harnesses,
        "active_sessions": active,
        "max_sessions": MAX_SESSIONS,
        "available_slots": max(0, MAX_SESSIONS - active),
        "local_model_url": local_model_url,
        "auth_configured": bool(_internal_key() or _session_bearer()),
        "awrun_available": awrun_available,
        "awrun_active_runs": awrun_active_runs,
        "awseal_available": _load_awseal() is not None,
        "awzeal_available": _load_awzeal_provider() is not None,
        "supported_inference_runtimes": list(VALID_INFERENCE_RUNTIMES),
    }
    if probe_genesis:
        cap["genesis"] = client.probe_reachable()
    return cap


def extract_session_artifacts(text: str) -> List[Dict[str, str]]:
    """Parse ``Screenshot: <path> | <caption>`` and ``Artifact: <path> | <caption>`` lines."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for m in _ARTIFACT_LINE_RE.finditer(str(text or "")):
        path = m.group(1).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        caption = (m.group(2) or "").strip()
        kind = "screenshot" if m.group(0).lstrip().lower().startswith("screenshot") else "artifact"
        item: Dict[str, str] = {"path": path, "kind": kind}
        if caption:
            item["caption"] = caption
        out.append(item)
    return out


def extract_session_summary(events: Sequence[Dict[str, Any]], max_chars: int = 1800) -> str:
    """Build a concise summary from a harness session's recorded event stream."""
    chunks: List[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or ev.get("kind") or "").lower()
        if etype in (
            "assistant",
            "text",
            "text.delta",
            "output",
            "result",
            "done",
            "message",
            "delta",
            "notice",
            "turn.completed",
        ):
            txt = str(ev.get("text") or ev.get("content") or ev.get("message") or "").strip()
            if txt:
                chunks.append(txt)
        elif etype == "error":
            err = str(ev.get("error") or ev.get("message") or ev.get("text") or "").strip()
            if err:
                chunks.append(f"ERROR: {err}")
    joined = "\n".join(chunks).strip()
    if len(joined) > max_chars:
        return joined[-max_chars:]
    return joined


def _session_events(session: Any) -> List[Dict[str, Any]]:
    if hasattr(session, "recent_events") and callable(session.recent_events):
        try:
            return list(session.recent_events())
        except Exception:  # noqa: BLE001
            pass
    if hasattr(session, "events") and callable(session.events):
        try:
            return list(session.events())
        except Exception:  # noqa: BLE001
            pass
    log_path = getattr(session, "log_path", None)
    if log_path is not None and Path(log_path).is_file():
        out: List[Dict[str, Any]] = []
        try:
            for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
        return out
    return []


def _session_state_str(session: Any) -> str:
    st = getattr(session, "state", "")
    val = getattr(st, "value", st)
    return str(val or "").strip().lower()


def _emit_harness_trace_span(
    *,
    swarm_id: str,
    agent: str,
    harness: str,
    session_id: str,
    prompt: str,
    summary: str,
    artifacts: List[Dict[str, str]],
    cwd: str,
    model_profile: str,
    thread_id: str,
    started_at: float,
    ended_at: float,
    status: str,
    error: Optional[str],
    board_seq: Optional[int],
) -> Optional[str]:
    """Fail-soft emission of one redacted ``harness`` span into ``aither.trace/v1``."""
    try:
        from lib.orchestration.swarm_trace_hooks import record_harness_span  # noqa: PLC0415

        return record_harness_span(
            swarm_id=swarm_id,
            agent=agent,
            harness=harness,
            session_id=session_id,
            prompt=prompt,
            summary=summary,
            artifacts=artifacts,
            cwd=cwd,
            model_profile=model_profile,
            thread_id=thread_id,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            error=error,
            board_seq=board_seq,
        )
    except Exception as exc:  # noqa: BLE001 - awdk standalone box may not carry AitherOS lib
        logger.debug("harness trace span skipped (fail-soft): %s", exc)
        return None


def local_swarms_dir() -> Path:
    """Storage root for standalone local Room-backed SwarmBoards."""
    for var in ("AITHER_LOCAL_SWARMS_DIR", "AITHER_SWARMS_DIR"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            return Path(raw)
    return Path.home() / ".aither" / "local-swarms"


def _awgit_argv() -> Optional[List[str]]:
    """Resolve ``awgit`` CLI without hard-importing AitherOS packages."""
    exe = shutil.which("awgit")
    if exe:
        return [exe]
    if importlib.util.find_spec("awgit") is not None:
        return [sys.executable, "-m", "awgit.cli"]
    return None


def _subprocess_kwargs(cwd: str) -> Dict[str, Any]:
    kw: Dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32" or os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def _safe_wt_slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "").strip()).strip("-.")
    return cleaned[:64] or "slot"


def _parse_worktree_path(stdout: str, stderr: str, cwd: str, wt_name: str, argv: List[str]) -> str:
    combined = f"{stdout}\n{stderr}"
    for line in combined.splitlines():
        m = re.search(r"(?:path|worktree)\s*[:=]\s*(\S+)", line, re.I)
        if m:
            cand = Path(m.group(1).strip().strip("'\""))
            if cand.exists():
                return str(cand)
        for token in line.split():
            tok = token.strip("'\",()[]")
            if tok and ("/" in tok or "\\" in tok) and Path(tok).is_dir():
                if wt_name in tok:
                    return str(Path(tok))
    try:
        listed = subprocess.run([*argv, "wt", "list", "--json"], **_subprocess_kwargs(cwd), timeout=15)
        if listed.returncode == 0 and listed.stdout.strip():
            data = json.loads(listed.stdout)
            for row in data.get("worktrees") or []:
                p = str(row.get("path") or "")
                b = str(row.get("branch") or "")
                if p and (wt_name in p or wt_name in b) and Path(p).is_dir():
                    return str(Path(p))
    except Exception:  # noqa: BLE001
        pass
    for candidate in (Path(cwd) / ".worktrees" / wt_name, Path(cwd).parent / wt_name):
        if candidate.is_dir():
            return str(candidate)
    return ""


def _rollback_worktree(
    argv: List[str], cwd: str, wt_name: str, wt_path: str, branch: str = ""
) -> None:
    target = wt_name or wt_path
    if not target:
        return
    for cmd in (
        [*argv, "wt", "rm", target, "--force"],
        [*argv, "wt", "done", target, "--force", "--reason", "lease-conflict-rollback"],
    ):
        try:
            res = subprocess.run(cmd, **_subprocess_kwargs(cwd), timeout=20)
            if res.returncode == 0:
                break
        except Exception:  # noqa: BLE001
            continue
    if branch and shutil.which("git"):
        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                **_subprocess_kwargs(cwd),
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass


def bind_slot_worktree_and_leases(
    *,
    cwd: str,
    swarm_id: str,
    slot_id: str,
    owns: Sequence[str] = (),
    use_worktree: bool = False,
    use_leases: bool = False,
    lease_ttl_s: int = 3600,
    branch: str = "",
) -> Dict[str, Any]:
    """Create an isolated ``awgit`` worktree and/or acquire ``awgit`` path leases.

    On ``LeaseConflictError`` (exit code 1 / ``lease conflict``), rolls back any
    newly created worktree and raises :class:`SwarmLeaseConflictError` (HTTP 409).
    """
    base_cwd = str(cwd or "").strip() or os.getcwd()
    result: Dict[str, Any] = {
        "cwd": base_cwd,
        "worktree_path": "",
        "worktree_name": "",
        "branch": "",
        "lease_ids": [],
        "lease_actor": f"swarm:{swarm_id}:{slot_id}",
    }
    clean_owns = [str(p).strip() for p in (owns or ()) if str(p or "").strip()]
    if not use_worktree and not (use_leases and clean_owns):
        return result

    argv = _awgit_argv()
    if not argv:
        raise SwarmApiError("awgit CLI is not available on this host", status=503)

    wt_name = ""
    wt_path = ""
    wt_branch = ""
    if use_worktree:
        wt_name = _safe_wt_slug(f"swarm-{swarm_id}-{slot_id}")
        wt_branch = str(branch or "").strip() or f"swarm/{swarm_id}/{slot_id}"
        purpose = f"{swarm_id}:{slot_id}"
        cmd = [*argv, "wt", "new", wt_name, "--branch", wt_branch, "--purpose", purpose]
        res = subprocess.run(cmd, **_subprocess_kwargs(base_cwd), timeout=30)
        combined = f"{res.stdout}\n{res.stderr}".lower()
        if res.returncode != 0 and ("no trunk" in combined or "pass --at" in combined or res.returncode == 2):
            cmd_head = [*argv, "wt", "new", wt_name, "--at", "HEAD", "--branch", wt_branch, "--purpose", purpose]
            res = subprocess.run(cmd_head, **_subprocess_kwargs(base_cwd), timeout=30)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "awgit wt new failed").strip()
            raise SwarmApiError(err, status=422)
        wt_path = _parse_worktree_path(res.stdout, res.stderr, base_cwd, wt_name, argv)
        if wt_path:
            result["cwd"] = wt_path
            result["worktree_path"] = wt_path
        result["worktree_name"] = wt_name
        result["branch"] = wt_branch

    if use_leases and clean_owns:
        actor = result["lease_actor"]
        lease_cmd = [
            *argv,
            "lease",
            "acquire",
            "--actor",
            actor,
            "--ttl",
            str(max(60, int(lease_ttl_s))),
            "--reason",
            f"{swarm_id}:{slot_id}",
            *clean_owns,
        ]
        res = subprocess.run(lease_cmd, **_subprocess_kwargs(result["cwd"]), timeout=20)
        combined = f"{res.stdout}\n{res.stderr}"
        if res.returncode != 0:
            if wt_name:
                _rollback_worktree(argv, base_cwd, wt_name, wt_path, branch=wt_branch)
            if res.returncode == 1 or "lease conflict" in combined.lower():
                raise SwarmLeaseConflictError(combined.strip() or "lease conflict")
            raise SwarmApiError(combined.strip() or "awgit lease acquire failed", status=502)
        lease_ids: List[str] = []
        for line in res.stdout.splitlines():
            m = re.search(r"lease\s+([A-Za-z0-9._:-]+)\s+", line)
            if m:
                lease_ids.append(m.group(1))
        result["lease_ids"] = lease_ids

    return result


def release_slot_bindings(
    *,
    cwd: str,
    swarm_id: str,
    slot_id: str,
    lease_ids: Sequence[str] = (),
    worktree_name: str = "",
    worktree_path: str = "",
    branch: str = "",
    remove_worktree: bool = False,
) -> Dict[str, Any]:
    """Best-effort release of ``awgit`` leases and optional worktree cleanup."""
    argv = _awgit_argv()
    if not argv:
        return {"released_leases": False, "removed_worktree": False}
    base_cwd = str(cwd or "").strip() or os.getcwd()
    if not Path(base_cwd).is_dir():
        base_cwd = os.getcwd()
    released_leases = False
    clean_leases = [str(x).strip() for x in (lease_ids or ()) if str(x or "").strip()]
    if clean_leases:
        actor = f"swarm:{swarm_id}:{slot_id}"
        try:
            res = subprocess.run(
                [*argv, "lease", "release", "--actor", actor, *clean_leases],
                **_subprocess_kwargs(base_cwd),
                timeout=20,
            )
            released_leases = res.returncode == 0
        except Exception:  # noqa: BLE001
            released_leases = False
    removed_wt = False
    if remove_worktree and (worktree_name or worktree_path):
        _rollback_worktree(argv, base_cwd, worktree_name, worktree_path, branch=branch)
        removed_wt = True
    return {"released_leases": released_leases, "removed_worktree": removed_wt}


def _extract_mentions_from_text(text: str = "", mentions: Optional[Sequence[Any]] = None) -> List[str]:
    try:
        from adk.harnesses.rooms import extract_swarm_mentions

        return extract_swarm_mentions(text, mentions)
    except Exception:  # noqa: BLE001
        out: List[str] = []
        seen: set[str] = set()
        for item in (mentions or ()):
            tok = str(item or "").strip().lstrip("@")
            if tok and tok.lower() not in seen:
                seen.add(tok.lower())
                out.append("all" if tok.lower() == "all" else tok)
        for m in re.finditer(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9._:-]{1,128})", str(text or "")):
            tok = m.group(1)
            if tok.lower() not in seen:
                seen.add(tok.lower())
                out.append("all" if tok.lower() == "all" else tok)
        return out


def _derive_pool_name(slot_id: str) -> str:
    m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)-(\d+)$", str(slot_id or "").strip())
    return m.group(1) if m else ""


def _compute_sibling_digests(
    threads: Sequence[Dict[str, Any]], exclude_slot_id: str = ""
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for th in threads:
        slot = str(th.get("slot_id") or th.get("owner") or "").strip()
        if exclude_slot_id and slot == exclude_slot_id:
            continue
        out.append(
            {
                "thread_id": str(th.get("id") or th.get("thread_id") or ""),
                "slot_id": slot,
                "owner": str(th.get("owner") or slot),
                "title": str(th.get("title") or ""),
                "owns": list(th.get("owns") or []),
                "summary": str(th.get("goal") or th.get("title") or "")[:160],
            }
        )
    return out


class LocalSwarmCoordinator:
    """Standalone Room-backed SwarmBoard coordinator (zero Genesis dependency)."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        *,
        rooms: Any = None,
        swarm_registry: Any = None,
        steer_dispatcher: Any = None,
        mgr: Any = None,
    ) -> None:
        self._dir = Path(storage_dir) if storage_dir is not None else local_swarms_dir()
        self._lock = threading.RLock()
        self._swarms: Dict[str, Dict[str, Any]] = {}
        self._rooms = rooms
        self._mgr = mgr
        if swarm_registry is None and steer_dispatcher is not None:
            swarm_registry = getattr(steer_dispatcher, "_swarm_registry", None)
        if swarm_registry is None:
            from adk.harnesses.steer_dispatch import SwarmSlotRegistry

            swarm_registry = SwarmSlotRegistry()
        self._swarm_registry = swarm_registry
        self._steer_dispatcher = steer_dispatcher
        self._ensure_room_dispatcher()
        self._load_all()

    def _ensure_room_dispatcher(self) -> None:
        if self._rooms is None:
            from adk.harnesses.rooms import default_registry

            self._rooms = default_registry()
        if self._steer_dispatcher is None:
            from adk.harnesses.steer_dispatch import SteerDispatcher

            for fn in getattr(self._rooms, "_listeners", ()) or ():
                owner = getattr(fn, "__self__", None)
                if isinstance(owner, SteerDispatcher):
                    self._steer_dispatcher = owner
                    break
        if self._steer_dispatcher is None and self._mgr is not None:
            from adk.harnesses.steer_dispatch import register as register_steer

            def _send(sid: str, text: str) -> bool:
                try:
                    sess = self._mgr.get_session(sid)
                except Exception:
                    return False
                if hasattr(sess, "submit") and callable(sess.submit):
                    return bool(sess.submit(text))
                if hasattr(sess, "send") and callable(sess.send):
                    sess.send(text)
                    return True
                return False

            wake_fn = getattr(self._mgr, "wake", None)
            self._steer_dispatcher = register_steer(
                self._rooms,
                send_managed_input=_send,
                swarm_registry=self._swarm_registry,
                wake_session_fn=wake_fn,
            )

    @property
    def swarm_registry(self) -> Any:
        return self._swarm_registry

    @property
    def rooms(self) -> Any:
        return self._rooms

    def _swarm_path(self, swarm_id: str) -> Path:
        return self._dir / f"{swarm_id}.json"

    def _save(self, rec: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._swarm_path(rec["swarm_id"])
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _load_all(self) -> None:
        if not self._dir.is_dir():
            return
        for child in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(child.read_text(encoding="utf-8"))
                sid = str(data.get("swarm_id") or "").strip()
                if sid:
                    self._swarms[sid] = data
            except Exception:  # noqa: BLE001
                continue

    def _get_rec(self, swarm_id: str) -> Optional[Dict[str, Any]]:
        sid = _validate_swarm_id(swarm_id)
        with self._lock:
            rec = self._swarms.get(sid)
            if rec is not None:
                return rec
            path = self._swarm_path(sid)
            if path.is_file():
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                    self._swarms[sid] = rec
                    return rec
                except Exception:  # noqa: BLE001
                    return None
        return None

    def has_swarm(self, swarm_id: str) -> bool:
        try:
            return self._get_rec(swarm_id) is not None
        except SwarmApiError:
            return False

    @staticmethod
    def _validate_unique_slots(threads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen_slots: set[str] = set()
        seen_threads: set[str] = set()
        for th in threads:
            slot_raw = str(th.get("slot_id") or "").strip()
            if not slot_raw:
                raise SwarmApiError("worker slot_id cannot be empty", status=422)
            slot_key = slot_raw.lower()
            if slot_key in seen_slots:
                raise SwarmApiError(f"duplicate worker slot_id: {slot_raw!r}", status=422)
            seen_slots.add(slot_key)
            tid = str(th.get("id") or "").strip()
            if tid in seen_threads:
                raise SwarmApiError(f"duplicate thread_id: {tid!r}", status=422)
            seen_threads.add(tid)
        return threads

    @staticmethod
    def _expand_tasks(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        goal = str(payload.get("goal") or "").strip()
        coordinator = str(payload.get("coordinator") or "aither").strip().lower() or "aither"
        default_exec_mode = _validate_execution_mode(payload.get("execution_mode"))
        default_limits = payload.get("limits")
        default_egress = payload.get("egress")
        default_isolation = payload.get("isolation")
        default_rt = _validate_inference_runtime(payload.get("inference_runtime"))
        default_shared_ro = bool(payload.get("shared_ro_mmap", False))
        default_model_path = str(payload.get("model_path") or "").strip()
        default_model_sha = str(payload.get("model_sha256") or "").strip()
        default_cpu_pin = str(payload.get("cpu_pin") or "").strip()

        def _f16_fields(src: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            d = src if isinstance(src, dict) else {}
            rt = _validate_inference_runtime(d.get("inference_runtime") or default_rt)
            exec_mode = _validate_execution_mode(d.get("execution_mode") or default_exec_mode)
            shared_ro = bool(d.get("shared_ro_mmap", default_shared_ro))
            return {
                "execution_mode": exec_mode,
                "limits": d.get("limits") if "limits" in d else default_limits,
                "egress": d.get("egress") if "egress" in d else default_egress,
                "isolation": d.get("isolation") if "isolation" in d else default_isolation,
                "inference_runtime": rt,
                "shared_ro_mmap": shared_ro,
                "shared_ro_mode": "D5_shared_ro" if shared_ro else "",
                "model_path": str(d.get("model_path") or default_model_path).strip(),
                "model_sha256": str(d.get("model_sha256") or default_model_sha).strip(),
                "cpu_pin": str(d.get("cpu_pin") or default_cpu_pin).strip(),
            }

        raw_tasks: List[Any] = [
            *(payload.get("tasks") or ()),
            *(payload.get("slots") or ()),
            *(payload.get("workers") or ()),
        ]
        raw_pools = payload.get("pools")
        if isinstance(raw_pools, dict) and raw_pools:
            for raw_pool, spec in raw_pools.items():
                pname = str(raw_pool or "").strip().lower()
                if not pname:
                    continue
                if isinstance(spec, dict):
                    if "count" in spec and spec["count"] is not None:
                        raw_count = spec["count"]
                        count_field = f"pools.{pname}.count"
                    elif "size" in spec and spec["size"] is not None:
                        raw_count = spec["size"]
                        count_field = f"pools.{pname}.size"
                    else:
                        raw_count = 1
                        count_field = f"pools.{pname}.count"
                    count = _parse_pool_count(raw_count, field=count_field)
                    pool_agent = str(spec.get("owner") or spec.get("agent") or pname).strip().lower()
                    pool_owns = _parse_str_list(spec.get("owns"), field=f"pools.{pname}.owns")
                    pool_mnt = _parse_str_list(
                        spec.get("must_not_touch"), field=f"pools.{pname}.must_not_touch"
                    )
                    pool_goal = str(spec.get("goal") or "").strip()
                    pool_extra = {
                        k: spec[k]
                        for k in (
                            "execution_mode",
                            "limits",
                            "egress",
                            "isolation",
                            "inference_runtime",
                            "shared_ro_mmap",
                            "model_path",
                            "model_sha256",
                            "cpu_pin",
                        )
                        if k in spec
                    }
                    for n in range(1, count + 1):
                        slot = f"{pname}-{n}"
                        own_slice = (
                            [pool_owns[n - 1]]
                            if len(pool_owns) == count
                            else (list(pool_owns) if n == 1 else [])
                        )
                        raw_tasks.append(
                            {
                                "slot_id": slot,
                                "pool": pname,
                                "agent": pool_agent,
                                "title": str(spec.get("title") or f"{slot}: {goal[:50]}"),
                                "goal": pool_goal or goal,
                                "owns": own_slice,
                                "must_not_touch": list(pool_mnt),
                                **pool_extra,
                            }
                        )
                else:
                    count = _parse_pool_count(spec, field=f"pools.{pname}")
                    raw_tasks.append({"pool": pname, "count": count})

        expanded: List[Dict[str, Any]] = []
        if raw_tasks:
            idx = 0
            for item in raw_tasks:
                if isinstance(item, str):
                    idx += 1
                    slot = item.strip().lstrip("@")
                    expanded.append(
                        {
                            "id": f"th_{idx}",
                            "slot_id": slot,
                            "member_id": slot,
                            "owner": slot,
                            "pool": _derive_pool_name(slot),
                            "title": f"{slot}: {goal[:60]}",
                            "goal": goal,
                            "owns": [],
                            "must_not_touch": [],
                            **_f16_fields(),
                        }
                    )
                    continue
                if not isinstance(item, dict):
                    continue
                pool = str(item.get("pool") or "").strip()
                has_count = "count" in item and item["count"] is not None
                count = _parse_pool_count(item["count"], field="workers.count") if has_count else 0
                if pool and count > 0 and not item.get("slot_id"):
                    for n in range(1, count + 1):
                        idx += 1
                        slot = f"{pool}-{n}"
                        expanded.append(
                            {
                                "id": f"th_{idx}",
                                "slot_id": slot,
                                "member_id": slot,
                                "owner": str(item.get("agent") or item.get("owner") or pool).strip().lower(),
                                "pool": pool,
                                "title": str(item.get("title") or f"{slot}: {goal[:50]}"),
                                "goal": str(item.get("goal") or goal),
                                "owns": _parse_str_list(item.get("owns"), field="workers.owns"),
                                "must_not_touch": _parse_str_list(
                                    item.get("must_not_touch"), field="workers.must_not_touch"
                                ),
                                "harness": str(item.get("harness") or ""),
                                **_f16_fields(item),
                            }
                        )
                else:
                    idx += 1
                    owner = str(item.get("agent") or item.get("owner") or f"worker-{idx}").strip().lower()
                    slot = str(item.get("slot_id") or item.get("member_id") or owner).strip().lstrip("@")
                    expanded.append(
                        {
                            "id": str(item.get("thread_id") or item.get("id") or f"th_{idx}"),
                            "slot_id": slot,
                            "member_id": str(item.get("member_id") or slot),
                            "owner": owner,
                            "pool": pool or _derive_pool_name(slot),
                            "title": str(item.get("title") or f"{slot}: {goal[:50]}"),
                            "goal": str(item.get("goal") or goal),
                            "owns": _parse_str_list(item.get("owns"), field="workers.owns"),
                            "must_not_touch": _parse_str_list(
                                item.get("must_not_touch"), field="workers.must_not_touch"
                            ),
                            "harness": str(item.get("harness") or ""),
                            **_f16_fields(item),
                        }
                    )
            return LocalSwarmCoordinator._validate_unique_slots(expanded)

        leads = payload.get("leads")
        if isinstance(leads, list) and leads:
            for idx, raw_lead in enumerate(leads, start=1):
                slot = str(raw_lead or "").strip().lstrip("@")
                if not slot:
                    continue
                expanded.append(
                    {
                        "id": f"th_{idx}",
                        "slot_id": slot,
                        "member_id": slot,
                        "owner": slot.lower(),
                        "pool": _derive_pool_name(slot),
                        "title": f"{slot}: {goal[:60]}",
                        "goal": goal,
                        "owns": [],
                        "must_not_touch": [],
                        **_f16_fields(),
                    }
                )
            if expanded:
                return LocalSwarmCoordinator._validate_unique_slots(expanded)

        return [
            {
                "id": "th_1",
                "slot_id": coordinator,
                "member_id": coordinator,
                "owner": coordinator,
                "pool": "",
                "title": str(payload.get("title") or goal[:60]),
                "goal": goal,
                "owns": [],
                "must_not_touch": [],
                **_f16_fields(),
            }
        ]

    def create_swarm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise SwarmApiError("goal is required", status=422)
        sid = str(payload.get("swarm_id") or f"sw_{uuid.uuid4().hex[:10]}").strip()
        sid = _validate_swarm_id(sid)
        room_id = str(payload.get("room_id") or f"swarm-{sid.lower()}").strip().lower()
        if room_id == "main":
            raise SwarmApiError("swarm cannot bind to room_id 'main'", status=422)
        coordinator = str(payload.get("coordinator") or "aither").strip().lower() or "aither"
        threads = self._expand_tasks(payload)

        members: Dict[str, Dict[str, Any]] = {}
        now = time.time()
        awrun_used = False
        created_runs: List[tuple[str, str]] = []
        try:
            for th in threads:
                slot = th["slot_id"]
                own_set = set(th.get("owns") or ())
                inferred_mnt = list(th.get("must_not_touch") or ())
                for sib in threads:
                    if sib["slot_id"] == slot:
                        continue
                    for p in sib.get("owns") or ():
                        if p not in own_set and p not in inferred_mnt:
                            inferred_mnt.append(p)
                th["must_not_touch"] = inferred_mnt
                th["sibling_digests"] = _compute_sibling_digests(threads, exclude_slot_id=slot)
                th.setdefault("status", "queued")
                th.setdefault("worktree_path", "")
                th.setdefault("lease_ids", [])
                aw_info = _prepare_awrun_track(
                    swarm_id=sid,
                    thread_id=th["id"],
                    slot_id=slot,
                    pool=th.get("pool", ""),
                    owns=th.get("owns") or (),
                    execution_mode=th.get("execution_mode", "standard"),
                    limits=th.get("limits"),
                    egress=th.get("egress"),
                    isolation=th.get("isolation"),
                    start_now=False,
                )
                if aw_info["run_id"]:
                    awrun_used = True
                    created_runs.append((aw_info["run_id"], str(aw_info.get("awrun_dir") or "")))
                th["run_id"] = aw_info["run_id"]
                zeal_meta = _resolve_awzeal_metadata(
                    inference_runtime=th.get("inference_runtime", "standard"),
                    shared_ro_mmap=bool(th.get("shared_ro_mmap", False)),
                    model_path=th.get("model_path", ""),
                    model_sha256=th.get("model_sha256", ""),
                    cpu_pin=th.get("cpu_pin", ""),
                )
                members[slot] = {
                    "slot_id": slot,
                    "member_id": th["member_id"],
                    "name": th["owner"],
                    "agent": th["owner"],
                    "pool": th["pool"],
                    "role": "coordinator" if slot == coordinator else "worker",
                    "state": "waiting",
                    "session_state": "waiting",
                    "session_id": "",
                    "thread_id": th["id"],
                    "owns": list(th["owns"]),
                    "must_not_touch": list(th["must_not_touch"]),
                    "sibling_digests": list(th["sibling_digests"]),
                    "worktree_path": "",
                    "worktree_name": "",
                    "lease_ids": [],
                    "cwd": "",
                    "execution_mode": th.get("execution_mode", "standard"),
                    "run_id": aw_info["run_id"],
                    "inference_runtime": zeal_meta["inference_runtime"],
                    "shared_ro_mmap": zeal_meta["shared_ro_mmap"],
                    "shared_ro_mode": zeal_meta["shared_ro_mode"],
                    "model_path": zeal_meta["model_path"],
                    "model_sha256": zeal_meta["model_sha256"],
                    "cpu_pin": zeal_meta["cpu_pin"],
                    "awzeal_provider_available": zeal_meta["awzeal_provider_available"],
                    "last_activity_ts": now,
                    "stall_nudged": False,
                    "stall_nudged_at": 0.0,
                }

            meta = dict(payload.get("metadata") or {})
            if awrun_used or _validate_execution_mode(payload.get("execution_mode")) == "awrun":
                meta.setdefault("execution_mode", "awrun")
            raw_exp_key = str(
                payload.get("awseal_expect_key")
                or payload.get("expect_key")
                or meta.get("awseal_expect_key")
                or meta.get("expect_key")
                or ""
            ).strip()
            if raw_exp_key:
                meta["expect_key"] = raw_exp_key
                meta["awseal_expect_key"] = raw_exp_key
            if payload.get("seal_artifacts") is not None:
                meta["seal_artifacts"] = bool(payload.get("seal_artifacts"))
            top_zeal = _resolve_awzeal_metadata(
                inference_runtime=str(payload.get("inference_runtime") or "standard"),
                shared_ro_mmap=bool(payload.get("shared_ro_mmap", False)),
                model_path=str(payload.get("model_path") or ""),
                model_sha256=str(payload.get("model_sha256") or ""),
                cpu_pin=str(payload.get("cpu_pin") or ""),
            )
            if top_zeal["inference_runtime"] != "standard" or top_zeal["shared_ro_mmap"]:
                meta.update(top_zeal)

            rec: Dict[str, Any] = {
                "swarm_id": sid,
                "room_id": room_id,
                "title": str(payload.get("title") or goal[:60]),
                "goal": {"title": str(payload.get("title") or goal[:60]), "line": goal},
                "status": "live",
                "coordinator": coordinator,
                "requested_by": str(payload.get("requested_by") or "awsh:owner"),
                "local_only": True,
                "local_room": True,
                "stall_timeout_s": float(payload.get("stall_timeout_s") or 180.0),
                "created_at": now,
                "updated_at": now,
                "threads": threads,
                "members": members,
                "board": [],
                "stall": {"active": False, "blocking_agent": "", "message": "", "reason": ""},
                "metadata": meta,
            }
            with self._lock:
                self._save(rec)
                self._swarms[sid] = rec
        except Exception as exc:
            for rid, awrun_dir in created_runs:
                _finish_awrun_item(
                    rid,
                    failed=True,
                    summary=f"create_swarm rolled back: {exc}",
                    awrun_dir=awrun_dir,
                )
            raise
        # Materialize backing Room immediately
        self._rooms.get_or_create(room_id, title=rec["title"])
        return self._to_view(rec)

    def _to_view(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        board = list(rec.get("board") or [])
        members = rec.get("members") or {}
        threads = list(rec.get("threads") or [])
        done_count = sum(1 for t in threads if t.get("status") == "done")
        return {
            "swarm_id": rec["swarm_id"],
            "room_id": rec.get("room_id", ""),
            "title": rec.get("title", ""),
            "goal": rec.get("goal", {}),
            "status": rec.get("status", "live"),
            "coordinator": rec.get("coordinator", "aither"),
            "requested_by": rec.get("requested_by", ""),
            "local_only": True,
            "local_room": True,
            "stall_timeout_s": rec.get("stall_timeout_s", 180.0),
            "ui_url": swarm_ui_url(rec, rec["swarm_id"]),
            "progress": {"done": done_count, "total": len(threads)},
            "stall": dict(rec.get("stall") or {}),
            "threads": [dict(t) for t in threads],
            "members": [dict(m) for m in members.values()],
            "members_by_slot": {k: dict(v) for k, v in members.items()},
            "board": board,
            "head": board[-1]["seq"] if board else 0,
            "metadata": dict(rec.get("metadata") or {}),
        }

    def list_swarms(self, *, status: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        with self._lock:
            items = list(self._swarms.values())
        items.sort(key=lambda r: float(r.get("updated_at") or 0.0), reverse=True)
        if status:
            items = [r for r in items if r.get("status") == status]
        items = items[: max(1, min(int(limit), 200))]
        views = [self._to_view(r) for r in items]
        return {"swarms": views, "count": len(views)}

    def get_swarm(self, swarm_id: str) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        with self._lock:
            return self._to_view(rec)

    def get_board(self, swarm_id: str, *, since: int = 0, limit: int = 100) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        with self._lock:
            board = list(rec.get("board") or [])
            posts = [dict(p) for p in board if int(p.get("seq") or 0) > since][
                -max(1, min(int(limit), 500)) :
            ]
            head = int(board[-1]["seq"]) if board else 0
            return {
                "swarm_id": rec["swarm_id"],
                "room_id": rec.get("room_id", ""),
                "status": rec.get("status", "live"),
                "head": head,
                "last_seq": head,
                "posts": posts,
                "board": posts,
            }

    def _resolve_member(self, rec: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
        needle = str(key or "").strip().lstrip("@")
        if not needle:
            return None
        members = rec.get("members") or {}
        if needle in members:
            return members[needle]
        matches = [
            m
            for m in members.values()
            if str(m.get("member_id") or "") == needle
            or str(m.get("session_id") or "") == needle
        ]
        if len(matches) == 1:
            return matches[0]
        agent_matches = [
            m for m in members.values() if str(m.get("agent") or "").lower() == needle.lower()
        ]
        if len(agent_matches) == 1:
            return agent_matches[0]
        return None

    def register_member_session(
        self,
        swarm_id: str,
        *,
        slot_id: str,
        member_id: str,
        agent: str,
        session_id: str,
        thread_id: str = "",
        pool: str = "",
        owns: Sequence[str] = (),
        must_not_touch: Sequence[str] = (),
        sibling_digests: Sequence[Any] = (),
        worktree_path: str = "",
        worktree_name: str = "",
        branch: str = "",
        lease_ids: Sequence[str] = (),
        cwd: str = "",
        allow_peer_input: bool = True,
        execution_mode: str = "standard",
        run_id: str = "",
        inference_runtime: str = "standard",
        shared_ro_mmap: bool = False,
        model_path: str = "",
        model_sha256: str = "",
        cpu_pin: str = "",
        overwrite_zeal_meta: bool = False,
    ) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            # Auto-materialize a lightweight local swarm shell if delegating to a fresh local id
            self.create_swarm(
                {
                    "swarm_id": swarm_id,
                    "goal": f"Local delegated swarm {swarm_id}",
                    "tasks": [
                        {
                            "slot_id": slot_id,
                            "member_id": member_id,
                            "agent": agent,
                            "pool": pool,
                            "owns": list(owns),
                            "must_not_touch": list(must_not_touch),
                            "inference_runtime": inference_runtime,
                            "shared_ro_mmap": shared_ro_mmap,
                            "model_path": model_path,
                            "model_sha256": model_sha256,
                            "cpu_pin": cpu_pin,
                        }
                    ],
                }
            )
            rec = self._get_rec(swarm_id)
        assert rec is not None
        zeal_meta = _resolve_awzeal_metadata(
            inference_runtime=inference_runtime,
            shared_ro_mmap=shared_ro_mmap,
            model_path=model_path,
            model_sha256=model_sha256,
            cpu_pin=cpu_pin,
        )
        exec_mode = _validate_execution_mode(execution_mode)
        now = time.time()
        with self._lock:
            members = rec.setdefault("members", {})
            threads = rec.setdefault("threads", [])
            th_id = str(thread_id or "").strip()
            existing = members.get(slot_id)
            if existing is None:
                th_id = th_id or f"th_{len(threads) + 1}"
                threads.append(
                    {
                        "id": th_id,
                        "slot_id": slot_id,
                        "member_id": member_id or slot_id,
                        "owner": agent,
                        "pool": pool or _derive_pool_name(slot_id),
                        "title": f"{slot_id} ({agent})",
                        "goal": "",
                        "owns": list(owns),
                        "must_not_touch": list(must_not_touch),
                        "status": "moving",
                        "worktree_path": worktree_path,
                        "lease_ids": list(lease_ids),
                        "execution_mode": exec_mode,
                        "run_id": run_id,
                        **zeal_meta,
                    }
                )
                existing = {
                    "slot_id": slot_id,
                    "member_id": member_id or slot_id,
                    "name": agent,
                    "agent": agent,
                    "pool": pool or _derive_pool_name(slot_id),
                    "role": "coordinator" if slot_id == rec.get("coordinator") else "worker",
                    "thread_id": th_id,
                }
                members[slot_id] = existing
            else:
                th_id = str(existing.get("thread_id") or th_id or f"th_{slot_id}")
                for th in threads:
                    if th.get("slot_id") == slot_id or th.get("id") == th_id:
                        th["status"] = "moving"
                        if owns:
                            th["owns"] = list(owns)
                        if must_not_touch:
                            th["must_not_touch"] = list(must_not_touch)
                        if worktree_path:
                            th["worktree_path"] = worktree_path
                        if lease_ids:
                            th["lease_ids"] = list(lease_ids)
                        if run_id:
                            th["run_id"] = run_id
                        if exec_mode != "standard":
                            th["execution_mode"] = exec_mode
                        if (
                            overwrite_zeal_meta
                            or zeal_meta["inference_runtime"] != "standard"
                            or zeal_meta["shared_ro_mmap"]
                        ):
                            th.update(zeal_meta)

            digests = list(sibling_digests) if sibling_digests else _compute_sibling_digests(threads, exclude_slot_id=slot_id)
            eff_run_id = run_id or str(existing.get("run_id") or "")
            eff_exec_mode = exec_mode if exec_mode != "standard" else str(existing.get("execution_mode") or "standard")
            existing.update(
                {
                    "session_id": str(session_id),
                    "state": "working",
                    "session_state": "working",
                    "owns": list(owns) if owns else list(existing.get("owns") or []),
                    "must_not_touch": list(must_not_touch) if must_not_touch else list(existing.get("must_not_touch") or []),
                    "sibling_digests": digests,
                    "worktree_path": worktree_path or existing.get("worktree_path", ""),
                    "worktree_name": worktree_name or existing.get("worktree_name", ""),
                    "branch": branch or existing.get("branch", ""),
                    "lease_ids": list(lease_ids) if lease_ids else list(existing.get("lease_ids") or []),
                    "cwd": cwd or existing.get("cwd", ""),
                    "execution_mode": eff_exec_mode,
                    "run_id": eff_run_id,
                    "last_activity_ts": now,
                    "stall_nudged": False,
                }
            )
            if (
                overwrite_zeal_meta
                or zeal_meta["inference_runtime"] != "standard"
                or zeal_meta["shared_ro_mmap"]
                or "inference_runtime" not in existing
            ):
                existing.update(zeal_meta)
            rec["updated_at"] = now
            self._save(rec)
            room_id = rec["room_id"]

        self._swarm_registry.register_slot(
            swarm_id=rec["swarm_id"],
            slot_id=slot_id,
            member_id=member_id or slot_id,
            agent=agent,
            session_id=str(session_id),
            room_id=room_id,
            thread_id=th_id,
            pool=pool or _derive_pool_name(slot_id),
            role=existing.get("role", "worker"),
            allow_peer_input=allow_peer_input,
            session_state="working",
            metadata={
                "worktree_path": worktree_path,
                "owns": list(owns),
                "run_id": eff_run_id,
                **zeal_meta,
            },
        )
        return {
            "thread_id": th_id,
            "sibling_digests": digests,
            "room_id": room_id,
        }

    def set_member_session_state(
        self, swarm_id: str, slot_or_session: str, session_state: str
    ) -> None:
        self._swarm_registry.set_session_state(
            slot_or_session, session_state, swarm_id=swarm_id
        )
        rec = self._get_rec(swarm_id)
        if rec is None:
            return
        with self._lock:
            member = self._resolve_member(rec, slot_or_session)
            if member is not None:
                member["session_state"] = session_state
                member["state"] = session_state
                member["last_activity_ts"] = time.time()
                if session_state in ("working", "awake"):
                    member["stall_nudged"] = False
                rec["updated_at"] = time.time()
                self._save(rec)

    def post_message(
        self,
        swarm_id: str,
        payload: Dict[str, Any],
        *,
        auth: tuple = (),
    ) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        sender_raw = str(
            payload.get("slot_id") or payload.get("agent") or payload.get("sender") or "coordinator"
        ).strip().lstrip("@")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise SwarmApiError("post text is required", status=422)
        kind = str(payload.get("kind") or "update").strip().lower()
        thread_id = str(payload.get("thread_id") or "").strip()
        artifacts = list(payload.get("artifacts") or [])
        extract_mentions = payload.get("extract_mentions", True)
        explicit_only = bool(payload.get("explicit_mentions_only", False))
        mentions = _extract_mentions_from_text(
            text if (extract_mentions and not explicit_only) else "",
            payload.get("mentions"),
        )
        hops = int(payload.get("hops") or 0)
        has_artifact_payload = bool(
            str(payload.get("artifact_dir") or "").strip()
            or artifacts
            or isinstance(payload.get("seal"), dict)
        )
        seal_receipt = verify_or_seal_track_artifacts(
            artifact_dir=str(payload.get("artifact_dir") or ""),
            artifacts=artifacts,
            seal_artifacts=bool(
                payload.get("seal_artifacts")
                if payload.get("seal_artifacts") is not None
                else (
                    (rec.get("metadata") or {}).get("seal_artifacts", False)
                    if has_artifact_payload
                    else False
                )
            ),
            expect_key=str(
                payload.get("awseal_expect_key")
                or payload.get("expect_key")
                or (
                    (
                        (rec.get("metadata") or {}).get("awseal_expect_key")
                        or (rec.get("metadata") or {}).get("expect_key")
                    )
                    if has_artifact_payload
                    else ""
                )
                or ""
            ),
            key_path=str(payload.get("key_path") or ""),
            seal_doc=payload.get("seal") if isinstance(payload.get("seal"), dict) else None,
            subject=f"swarm:{rec['swarm_id']}:{sender_raw}",
            kind=kind,
        )
        now = time.time()

        non_self_mentions = [
            m for m in mentions if m.lower() != sender_raw.lower()
        ]
        with self._lock:
            board = rec.setdefault("board", [])
            seq = (int(board[-1]["seq"]) + 1) if board else 1
            post: Dict[str, Any] = {
                "seq": seq,
                "ts": now,
                "agent": sender_raw,
                "slot_id": sender_raw,
                "text": text,
                "kind": kind,
                "mentions": mentions,
                "artifacts": artifacts,
                "thread_id": thread_id,
            }
            if isinstance(auth, (tuple, list)) and len(auth) >= 2 and auth[0]:
                post["principal_id"] = str(auth[0])
                post["principal_plan"] = str(auth[1])
            if seal_receipt is not None:
                post["seal"] = seal_receipt
            board.append(post)
            rec["updated_at"] = now

            sender_member = self._resolve_member(rec, sender_raw)
            if sender_member is not None:
                sender_member["last_activity_ts"] = now
                if kind != "stall":
                    sender_member["stall_nudged"] = False
                if kind == "done":
                    next_st = "waiting" if payload.get("keep_warm", True) else "done"
                    sender_member["session_state"] = next_st
                    sender_member["state"] = next_st
                    self._swarm_registry.set_session_state(
                        sender_member.get("slot_id") or sender_raw, next_st, swarm_id=rec["swarm_id"]
                    )
                for th in rec.get("threads") or []:
                    if th.get("slot_id") == sender_member.get("slot_id") or (
                        thread_id and th.get("id") == thread_id
                    ):
                        if kind == "done":
                            th["status"] = (
                                "in_review"
                                if any(m.lower().startswith("verifier") for m in mentions)
                                else "done"
                            )
                        elif kind == "stall":
                            th["status"] = "stalled"

            if kind not in ("done", "stall") and hops < 2:
                wake_targets: List[Dict[str, Any]] = []
                if any(m.lower() == "all" for m in non_self_mentions):
                    sender_slot_low = (
                        str(sender_member.get("slot_id") or sender_raw).lower()
                        if sender_member is not None
                        else sender_raw.lower()
                    )
                    for m in (rec.get("members") or {}).values():
                        if str(m.get("slot_id") or "").lower() != sender_slot_low:
                            wake_targets.append(m)
                else:
                    seen_wake: set[str] = set()
                    for target in non_self_mentions:
                        tm = self._resolve_member(rec, target)
                        if tm is not None:
                            tslot = str(tm.get("slot_id") or "")
                            if tslot not in seen_wake:
                                seen_wake.add(tslot)
                                wake_targets.append(tm)
                for tm in wake_targets:
                    if tm.get("session_id") or kind == "bounce":
                        tm["session_state"] = "awake"
                        tm["state"] = "awake"
                        tm["last_activity_ts"] = now
                    if kind == "bounce":
                        for th in rec.get("threads") or []:
                            if th.get("slot_id") == tm.get("slot_id"):
                                th["status"] = "moving"
            self._save(rec)
            room_id = rec["room_id"]

        # Publish into Room so SteerDispatcher wakes any mentioned warm sessions
        room = self._rooms.get_or_create(room_id, title=rec.get("title", ""))
        actor_kind = "human" if str(payload.get("actor_kind") or "") == "human" else "adk_agent"
        event: Dict[str, Any] = {
            "type": "agent_message" if actor_kind != "human" else "steering",
            "actor": {"kind": actor_kind, "id": sender_raw, "name": sender_raw},
            "payload": {
                "swarm_id": rec["swarm_id"],
                "thread_id": thread_id,
                "slot_id": sender_raw,
                "kind": kind,
                "text": text,
                "mentions": mentions,
                "artifacts": artifacts,
            },
            "hops": hops,
        }
        if any(m.lower() == "all" for m in non_self_mentions):
            event["to"] = ["all"]
        elif 1 <= len(non_self_mentions) <= 8:
            event["to"] = non_self_mentions
        # If >8 explicit targets, omit `to` (preserving rooms.MAX_TO_TARGETS=8) so
        # SteerDispatcher._handle_swarm_mentions fans out across payload["mentions"].
        try:
            room_event = room.publish(event, auth=auth)
        except Exception as exc:  # noqa: BLE001
            logger.warning("room publish failed for swarm %s: %s", rec["swarm_id"], exc)
            room_event = {}

        out: Dict[str, Any] = {
            "ok": True,
            "swarm_id": rec["swarm_id"],
            "room_id": room_id,
            "seq": seq,
            "post": post,
            "room_seq": room_event.get("seq"),
        }
        if seal_receipt is not None:
            out["seal"] = seal_receipt
        return out

    def nudge_stalled_slot(
        self,
        swarm_id: str,
        slot_id: str,
        *,
        reason: str = "",
        escalate_after_s: float = 0.0,
        force_escalate: bool = False,
    ) -> Dict[str, Any]:
        """@monitor stall watchdog: nudge warm blocking worker BEFORE escalating."""
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        now = time.time()
        with self._lock:
            member = self._resolve_member(rec, slot_id)
            if member is None:
                raise SwarmApiError(f"Unknown swarm slot {slot_id!r}", status=404)
            already_nudged = bool(member.get("stall_nudged"))
            nudged_at = float(member.get("stall_nudged_at") or 0.0)
            should_escalate = force_escalate or (
                already_nudged and (now - nudged_at) >= max(0.0, float(escalate_after_s))
            )
            target_slot = str(member.get("slot_id") or slot_id)
            if not should_escalate:
                member["stall_nudged"] = True
                member["stall_nudged_at"] = now
                self._save(rec)

        if not should_escalate:
            nudge_text = (
                f"@{target_slot} [stall] {reason or 'No recent board progress — please reply with status or unblock.'}"
            )
            posted = self.post_message(
                swarm_id,
                {
                    "agent": "monitor",
                    "text": nudge_text,
                    "kind": "stall",
                    "mentions": [target_slot],
                    "thread_id": str(member.get("thread_id") or ""),
                },
            )
            return {
                "ok": True,
                "action": "nudged",
                "escalated": False,
                "slot_id": target_slot,
                "seq": posted["seq"],
                "status": "live",
            }

        with self._lock:
            member["session_state"] = "stalled"
            member["state"] = "stalled"
            rec["status"] = "stalled"
            rec["stall"] = {
                "active": True,
                "blocking_agent": target_slot,
                "message": reason or f"Slot {target_slot} remained silent after @monitor nudge",
                "reason": reason or "stall_escalated",
                "since": now,
            }
            for th in rec.get("threads") or []:
                if th.get("slot_id") == target_slot:
                    th["status"] = "stalled"
            self._save(rec)
            coord = rec.get("coordinator") or "coordinator"

        posted = self.post_message(
            swarm_id,
            {
                "agent": "monitor",
                "text": f"@{coord} [stall] Escalating stalled slot @{target_slot}: {reason or 'no response after nudge'}.",
                "kind": "stall",
                "mentions": [coord],
                "thread_id": str(member.get("thread_id") or ""),
            },
        )
        return {
            "ok": True,
            "action": "escalated",
            "escalated": True,
            "slot_id": target_slot,
            "seq": posted["seq"],
            "status": "stalled",
        }

    def check_stalls(
        self,
        swarm_id: str,
        *,
        stall_timeout_s: Optional[float] = None,
        nudge_grace_s: float = 30.0,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        cur = float(now) if now is not None else time.time()
        timeout = float(stall_timeout_s if stall_timeout_s is not None else rec.get("stall_timeout_s") or 180.0)
        actions: List[Dict[str, Any]] = []
        with self._lock:
            slots = [
                (slot, dict(m))
                for slot, m in (rec.get("members") or {}).items()
                if m.get("session_state") in ("working", "awake")
            ]
        for slot, m in slots:
            idle_for = cur - float(m.get("last_activity_ts") or cur)
            if idle_for >= timeout:
                actions.append(
                    self.nudge_stalled_slot(
                        swarm_id,
                        slot,
                        reason=f"idle for {int(idle_for)}s",
                        escalate_after_s=nudge_grace_s,
                    )
                )
        return {"swarm_id": swarm_id, "actions": actions}

    def steer_swarm(
        self, swarm_id: str, payload: Dict[str, Any], *, auth: tuple = ()
    ) -> Dict[str, Any]:
        msg = str(payload.get("message") or payload.get("text") or "").strip()
        if not msg:
            raise SwarmApiError("message is required", status=422)
        user = str(payload.get("user") or "human").strip()
        mentions = _extract_mentions_from_text(msg, payload.get("mentions"))
        if not mentions:
            rec = self._get_rec(swarm_id)
            if rec is not None:
                mentions = [str(rec.get("coordinator") or "aither")]
        is_owner = bool(auth and len(auth) >= 2 and str(auth[1]) == "owner")
        eff_actor_kind = (
            "human"
            if (is_owner and str(payload.get("actor_kind") or "human") == "human")
            else "adk_agent"
        )
        eff_auth = (
            auth
            if (auth and len(auth) >= 2 and (is_owner or str(auth[1]) != "owner"))
            else ()
        )
        res = self.post_message(
            swarm_id,
            {
                "agent": user,
                "actor_kind": eff_actor_kind,
                "text": msg,
                "kind": "steer",
                "mentions": mentions,
            },
            auth=eff_auth,
        )
        return {
            "ok": True,
            "swarm_id": swarm_id,
            "seq": res["seq"],
            "status": self.get_swarm(swarm_id)["status"],
        }

    def pause_swarm(self, swarm_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.control_swarm(swarm_id, {"action": "pause", **(payload or {})})

    def control_swarm(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        rec = self._get_rec(swarm_id)
        if rec is None:
            raise SwarmApiError(f"Swarm '{swarm_id}' not found", status=404)
        action = str(payload.get("action") or "pause").strip().lower()
        status_map = {
            "pause": "paused",
            "paused": "paused",
            "export": "paused",
            "resume": "live",
            "live": "live",
            "cancel": "cancelled",
            "cancelled": "cancelled",
            "stop": "cancelled",
            "done": "done",
            "complete": "done",
        }
        new_status = status_map.get(action, action)
        bindings_to_release: List[Dict[str, Any]] = []
        run_ids: List[tuple[str, str]] = []
        with self._lock:
            rec["status"] = new_status
            rec["updated_at"] = time.time()
            for slot_id, member in (rec.get("members") or {}).items():
                rid = str(member.get("run_id") or "").strip()
                if rid:
                    run_ids.append((slot_id, rid))
                if new_status in ("cancelled", "done", "failed"):
                    self._swarm_registry.set_session_state(
                        slot_id, "stopped", swarm_id=rec["swarm_id"]
                    )
                    if member.get("lease_ids") or member.get("worktree_name"):
                        bindings_to_release.append(
                            {
                                "cwd": member.get("cwd") or "",
                                "swarm_id": rec["swarm_id"],
                                "slot_id": slot_id,
                                "lease_ids": list(member.get("lease_ids") or []),
                                "worktree_name": member.get("worktree_name") or "",
                                "worktree_path": member.get("worktree_path") or "",
                                "branch": member.get("branch") or "",
                                "remove_worktree": new_status == "cancelled",
                            }
                        )
            self._save(rec)
        for item in bindings_to_release:
            release_slot_bindings(**item)

        exported_runs: List[Dict[str, Any]] = []
        if run_ids:
            store_mod, _confine_mod, handoff_mod = _load_awrun()
            if store_mod is not None:
                store = store_mod.RunStore()
                want_export = bool(
                    payload.get("export_runs")
                    or action == "export"
                    or str(payload.get("export_dir") or "").strip()
                )
                export_dir = Path(
                    str(payload.get("export_dir") or (self._dir / "exports" / rec["swarm_id"]))
                )
                for slot_id, rid in run_ids:
                    try:
                        cur = store.get(rid)
                        if cur is None:
                            continue
                        if new_status == "paused":
                            if cur.status == store_mod.STATUS_RUNNING:
                                store.park(
                                    rid,
                                    checkpoint={"swarm_id": rec["swarm_id"], "slot_id": slot_id},
                                )
                            elif cur.status in (store_mod.STATUS_QUEUED, store_mod.STATUS_CLAIMED):
                                store.suspend(rid)
                            if want_export and handoff_mod is not None:
                                exported = handoff_mod.export_run(store, rid, export_dir)
                                exported["run_id"] = rid
                                exported["slot_id"] = slot_id
                                exported_runs.append(exported)
                        elif new_status == "cancelled" and cur.status in store_mod.OPEN_STATUSES:
                            store.cancel(rid)
                        elif new_status in ("done", "failed"):
                            if cur.status == store_mod.STATUS_RUNNING:
                                store.finish(
                                    rid,
                                    status=(
                                        store_mod.STATUS_FAILED
                                        if new_status == "failed"
                                        else store_mod.STATUS_DONE
                                    ),
                                    result={
                                        "ok": new_status == "done",
                                        "message": f"swarm {new_status}",
                                    },
                                )
                            elif cur.status in store_mod.OPEN_STATUSES:
                                store.cancel(rid)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("awrun control hook skipped for %s: %s", rid, exc)

        resp: Dict[str, Any] = {"ok": True, "swarm_id": rec["swarm_id"], "status": new_status}
        if exported_runs:
            resp["exported_runs"] = exported_runs
        return resp


def attach_engine(
    engine: Any,
    *,
    mgr: Any = None,
    rooms: Any = None,
    steer_dispatcher: Any = None,
    swarm_registry: Any = None,
) -> Dict[str, Any]:
    """Optional fail-soft bridge attaching M1.1 ``SwarmBoardEngine`` hooks to ``awsh``."""
    if engine is None:
        return {"attached": False, "reason": "no engine"}
    if rooms is None:
        from adk.harnesses.rooms import default_registry

        rooms = default_registry()
    if swarm_registry is None and steer_dispatcher is not None:
        swarm_registry = getattr(steer_dispatcher, "_swarm_registry", None)
    if swarm_registry is None:
        from adk.harnesses.steer_dispatch import SwarmSlotRegistry

        swarm_registry = SwarmSlotRegistry()

    def _publish_engine_mention(session_obj: Any, msg_obj: Any, target_slot_ids: Sequence[str]) -> None:
        sid = str(getattr(session_obj, "swarm_id", "") or "").strip()
        if not sid:
            return
        room = rooms.get_or_create(f"swarm-{sid.lower()}")
        sender = str(getattr(msg_obj, "agent", None) or getattr(msg_obj, "slot_id", None) or "swarm")
        text = str(getattr(msg_obj, "text", None) or str(msg_obj))
        targets = [str(t).strip().lstrip("@") for t in (target_slot_ids or ()) if str(t).strip()]
        ev: Dict[str, Any] = {
            "type": "agent_message",
            "actor": {"kind": "adk_agent", "id": sender, "name": sender},
            "payload": {
                "swarm_id": sid,
                "slot_id": sender,
                "text": text,
                "mentions": targets,
                "kind": str(getattr(msg_obj, "kind", "update")),
                "thread_id": str(getattr(msg_obj, "thread_id", "")),
            },
        }
        if any(t.lower() == "all" for t in targets):
            ev["to"] = ["all"]
        elif 1 <= len(targets) <= 8:
            ev["to"] = targets
        room.publish(ev)

    def _publish_stall_nudge(session_obj: Any, *args: Any, **kwargs: Any) -> None:
        sid = str(getattr(session_obj, "swarm_id", "") or "").strip()
        if not sid:
            return
        slot = ""
        reason = ""
        thread_id = ""
        if len(args) >= 3:
            # M1.1 SwarmBoardEngine 4-arg dispatch: (session, bm, target_th, post)
            bm, target_th, post = args[0], args[1], args[2]
            if isinstance(bm, str):
                slot = bm
            else:
                slot = str(
                    getattr(bm, "slot_id", None)
                    or getattr(bm, "member_id", None)
                    or getattr(bm, "name", None)
                    or ""
                )
            if not slot and target_th is not None:
                slot = str(
                    getattr(target_th, "slot_id", None)
                    or getattr(target_th, "owner", None)
                    or ""
                )
            if not slot and post is not None:
                mentions = getattr(post, "mentions", None) or []
                if mentions:
                    slot = str(mentions[0])
            if not slot:
                stall_obj = getattr(session_obj, "stall", None)
                slot = str(getattr(stall_obj, "blocking_agent", None) or "")
            reason = str(getattr(post, "text", None) or (post if isinstance(post, str) else "") or "")
            thread_id = str(
                getattr(post, "thread_id", None) or getattr(target_th, "id", None) or ""
            )
        else:
            raw_slot = args[0] if len(args) >= 1 else kwargs.get("blocking_slot_id", "")
            raw_reason = args[1] if len(args) >= 2 else kwargs.get("reason", "")
            if isinstance(raw_slot, str):
                slot = raw_slot
            else:
                slot = str(
                    getattr(raw_slot, "slot_id", None)
                    or getattr(raw_slot, "member_id", None)
                    or getattr(raw_slot, "name", None)
                    or ""
                )
            reason = str(
                getattr(raw_reason, "text", None) or raw_reason or ""
            )
            thread_id = str(kwargs.get("thread_id") or "")

        slot = slot.strip().lstrip("@")
        if not slot:
            return
        text = (
            reason.strip()
            if reason.strip().lower().startswith(f"@{slot.lower()}")
            else f"@{slot} [stall] {reason or 'Please report progress or unblock.'}"
        )
        room = rooms.get_or_create(f"swarm-{sid.lower()}")
        room.publish(
            {
                "type": "agent_message",
                "actor": {"kind": "adk_agent", "id": "monitor", "name": "monitor"},
                "to": [slot],
                "payload": {
                    "swarm_id": sid,
                    "slot_id": "monitor",
                    "kind": "stall",
                    "thread_id": thread_id,
                    "mentions": [slot],
                    "text": text,
                },
            }
        )

    wired = {"mention": False, "stall": False}
    if callable(getattr(engine, "register_mention_listener", None)):
        engine.register_mention_listener(_publish_engine_mention)
        wired["mention"] = True
    elif hasattr(engine, "on_mention_wakeup"):
        engine.on_mention_wakeup = _publish_engine_mention
        wired["mention"] = True

    if callable(getattr(engine, "register_stall_nudge_listener", None)):
        engine.register_stall_nudge_listener(_publish_stall_nudge)
        wired["stall"] = True
    elif hasattr(engine, "on_stall_nudge"):
        engine.on_stall_nudge = _publish_stall_nudge
        wired["stall"] = True

    return {"attached": True, **wired}


def _format_scope_prompt(
    prompt: str,
    *,
    slot_id: str = "",
    pool: str = "",
    owns: Sequence[str] = (),
    must_not_touch: Sequence[str] = (),
    sibling_digests: Sequence[Any] = (),
    worktree_path: str = "",
    lease_ids: Sequence[str] = (),
    execution_mode: str = "standard",
    run_id: str = "",
    inference_runtime: str = "standard",
    shared_ro_mmap: bool = False,
    model_path: str = "",
    model_sha256: str = "",
    cpu_pin: str = "",
) -> str:
    sections: List[str] = []
    if slot_id:
        sections.append(f"- slot_id: {slot_id}")
    if pool:
        sections.append(f"- pool: {pool}")
    clean_owns = [str(x).strip() for x in (owns or ()) if str(x or "").strip()]
    if clean_owns:
        sections.append(f"- owns: {', '.join(clean_owns)}")
    clean_forbidden = [str(x).strip() for x in (must_not_touch or ()) if str(x or "").strip()]
    if clean_forbidden:
        sections.append(f"- must_not_touch: {', '.join(clean_forbidden)}")
    if worktree_path:
        sections.append(f"- worktree_path: {worktree_path}")
    clean_leases = [str(x).strip() for x in (lease_ids or ()) if str(x or "").strip()]
    if clean_leases:
        sections.append(f"- lease_ids: {', '.join(clean_leases)}")
    if execution_mode and execution_mode != "standard":
        sections.append(f"- execution_mode: {execution_mode}")
    if run_id:
        sections.append(f"- awrun_run_id: {run_id}")
    if inference_runtime and inference_runtime != "standard":
        sections.append(f"- inference_runtime: {inference_runtime}")
    if shared_ro_mmap:
        sections.append("- shared_ro_mmap: true (D5_shared_ro)")
    if model_path:
        sections.append(f"- model_path: {model_path}")
    if model_sha256:
        sections.append(f"- model_sha256: {model_sha256}")
    if cpu_pin:
        sections.append(f"- cpu_pin: {cpu_pin}")
    if sibling_digests:
        digest_lines: List[str] = []
        for item in sibling_digests:
            if isinstance(item, dict):
                s_slot = str(item.get("slot_id") or item.get("owner") or item.get("thread_id") or "")
                s_title = str(item.get("title") or item.get("summary") or "")
                s_owns = ", ".join(str(x) for x in (item.get("owns") or []))
                line = f"  * {s_slot}: {s_title}" + (f" [owns: {s_owns}]" if s_owns else "")
            else:
                line = f"  * {item}"
            if line.strip("* "):
                digest_lines.append(line)
        if digest_lines:
            sections.append("- sibling_digests:\n" + "\n".join(digest_lines))
    if not sections:
        return prompt
    return f"{prompt}\n\n[Swarm Slot & Scope]\n" + "\n".join(sections)


def _finish_awrun_item(
    run_id: str, *, failed: bool, summary: str, awrun_dir: str = ""
) -> None:
    if not run_id:
        return
    store_mod, _c, _h = _load_awrun()
    if store_mod is None:
        return
    try:
        store = store_mod.RunStore(awrun_dir) if awrun_dir else store_mod.RunStore()
        cur = store.get(run_id)
        if cur is None:
            return
        if cur.status == store_mod.STATUS_QUEUED:
            store.claim(run_id, worker_id="swarm:cleanup")
            cur = store.get(run_id)
        if cur is not None and cur.status == store_mod.STATUS_CLAIMED:
            store.start(run_id)
            cur = store.get(run_id)
        if cur is not None and cur.status == store_mod.STATUS_RUNNING:
            store.finish(
                run_id,
                status=store_mod.STATUS_FAILED if failed else store_mod.STATUS_DONE,
                result={"ok": not failed, "message": summary[:500]},
            )
        elif cur is not None and cur.status in getattr(store_mod, "OPEN_STATUSES", ()):
            store.cancel(run_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("awrun finish skipped for %s: %s", run_id, exc)


def _watch_and_report(
    session: Any,
    poster: Any,
    *,
    swarm_id: str,
    agent: str,
    slot_id: str = "",
    harness: str,
    prompt: str = "",
    cwd: str = "",
    model_profile: str = "",
    started_at: float = 0.0,
    thread_id: str,
    report_to: str,
    timeout_s: float,
    poll_s: float,
    result_box: Dict[str, Any],
    keep_warm: bool = True,
    turn1_done: Optional[threading.Event] = None,
    swarm_registry: Any = None,
    local_coordinator: Optional[LocalSwarmCoordinator] = None,
    run_id: str = "",
    awrun_dir: str = "",
) -> None:
    actor_name = slot_id or agent
    seen_turn = 0
    seen_event_count = 0
    failed = False
    final_state = ""
    try:
        t0 = started_at or time.time()
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        final_state = _session_state_str(session)
        while time.monotonic() < deadline:
            final_state = _session_state_str(session)
            if final_state in ("exited", "failed", "idle", "done", "completed"):
                break
            time.sleep(max(0.02, poll_s))
        t1 = time.time()
        events = _session_events(session)
        # Snapshot turn and event count BEFORE post_message so a synchronous bounce
        # triggered inside post_message cannot clobber the turn-1 watermark.
        seen_turn = int(getattr(session, "turn", 0) or 0)
        seen_event_count = len(events)
        summary = extract_session_summary(events)
        artifacts = extract_session_artifacts(summary)
        failed = final_state == "failed" or (
            final_state not in ("exited", "idle", "done", "completed")
        )
        if failed:
            kind = "stall"
            body = (
                f"@{report_to} [stall] Local `{harness}` harness session `{session.id}` "
                f"ended with state `{final_state}`."
                + (f"\n{summary}" if summary else "")
            )
        else:
            kind = "done"
            body = (
                f"@{report_to} [done] Local `{harness}` harness session `{session.id}` finished."
                + (f"\n{summary}" if summary else "")
            )
        warm_waiting = bool(keep_warm and not failed and final_state in ("idle", "ready"))
        try:
            # Mark waiting BEFORE post_message so a synchronous bounce inside
            # post_message transitioning the slot to "awake" is never clobbered.
            if warm_waiting:
                if local_coordinator is not None:
                    local_coordinator.set_member_session_state(
                        swarm_id, str(session.id), "waiting"
                    )
                elif swarm_registry is not None:
                    swarm_registry.set_session_state(
                        str(session.id), "waiting", swarm_id=swarm_id
                    )
            posted = poster.post_message(
                swarm_id,
                {
                    "agent": actor_name,
                    "slot_id": actor_name,
                    "text": body[:4000],
                    "kind": kind,
                    "mentions": [report_to],
                    "extract_mentions": False,
                    "artifacts": artifacts,
                    "thread_id": thread_id,
                    "keep_warm": warm_waiting,
                },
            )
            if not warm_waiting:
                _finish_awrun_item(
                    run_id, failed=failed, summary=summary, awrun_dir=awrun_dir
                )
            effective_sess_state = "waiting" if warm_waiting else final_state
            if warm_waiting and local_coordinator is not None:
                try:
                    cur_view = local_coordinator.get_swarm(swarm_id)
                    cur_m = (cur_view.get("members_by_slot") or {}).get(actor_name)
                    if isinstance(cur_m, dict) and cur_m.get("session_state"):
                        effective_sess_state = str(cur_m["session_state"])
                except Exception:  # noqa: BLE001
                    pass
            # Populate result_box BEFORE cold lib.* trace span import so callers
            # waiting on turn1_done never observe an empty result_box.
            result_box.update(
                {
                    "reported": True,
                    "final_state": final_state,
                    "session_state": effective_sess_state,
                    "warm": warm_waiting,
                    "kind": kind,
                    "seq": posted.get("seq"),
                    "span_id": None,
                    "artifacts": artifacts,
                    "summary": summary,
                    "run_id": run_id,
                }
            )
            span_id = _emit_harness_trace_span(
                swarm_id=swarm_id,
                agent=actor_name,
                harness=harness,
                session_id=str(session.id),
                prompt=prompt,
                summary=summary,
                artifacts=artifacts,
                cwd=cwd,
                model_profile=model_profile,
                thread_id=thread_id,
                started_at=t0,
                ended_at=t1,
                status="error" if failed else "ok",
                error=f"harness ended with state={final_state}" if failed else None,
                board_seq=posted.get("seq"),
            )
            if span_id is not None:
                result_box["span_id"] = span_id
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "swarm harness report failed swarm=%s session=%s: %s",
                swarm_id,
                session.id,
                exc,
            )
            _finish_awrun_item(
                run_id, failed=True, summary=str(exc), awrun_dir=awrun_dir
            )
            result_box.update(
                {"reported": False, "final_state": final_state, "error": str(exc)}
            )
            _emit_harness_trace_span(
                swarm_id=swarm_id,
                agent=actor_name,
                harness=harness,
                session_id=str(session.id),
                prompt=prompt,
                summary=summary,
                artifacts=artifacts,
                cwd=cwd,
                model_profile=model_profile,
                thread_id=thread_id,
                started_at=t0,
                ended_at=t1,
                status="error",
                error=str(exc),
                board_seq=None,
            )
    finally:
        if turn1_done is not None:
            turn1_done.set()

    # Keep watching warm sessions across subsequent @mention / bounce / @monitor wakes
    if not (keep_warm and not failed and final_state in ("idle", "ready")):
        return

    while True:
        time.sleep(max(0.02, poll_s))
        if local_coordinator is not None:
            try:
                view = local_coordinator.get_swarm(swarm_id)
                if view.get("status") in ("done", "cancelled", "failed"):
                    break
            except SwarmApiError:
                break
        st = _session_state_str(session)
        if st in ("exited", "failed"):
            if local_coordinator is not None:
                local_coordinator.set_member_session_state(
                    swarm_id, str(session.id), "stopped"
                )
            elif swarm_registry is not None:
                swarm_registry.set_session_state(
                    str(session.id), "stopped", swarm_id=swarm_id
                )
            _finish_awrun_item(
                run_id,
                failed=(st == "failed"),
                summary=f"session {st}",
                awrun_dir=awrun_dir,
            )
            break
        cur_turn = int(getattr(session, "turn", 0) or 0)
        if st not in ("busy", "running", "working", "starting") and cur_turn <= seen_turn:
            continue

        seen_turn = max(seen_turn, cur_turn)
        if local_coordinator is not None:
            local_coordinator.set_member_session_state(swarm_id, str(session.id), "working")
        elif swarm_registry is not None:
            swarm_registry.set_session_state(str(session.id), "working", swarm_id=swarm_id)

        turn_deadline = time.monotonic() + max(1.0, float(timeout_s))
        turn_state = _session_state_str(session)
        while time.monotonic() < turn_deadline:
            turn_state = _session_state_str(session)
            if turn_state in ("exited", "failed", "idle", "done", "completed"):
                break
            time.sleep(max(0.02, poll_s))

        all_events = _session_events(session)
        new_events = all_events[seen_event_count:] if len(all_events) > seen_event_count else all_events
        seen_event_count = len(all_events)
        turn_summary = extract_session_summary(new_events)
        turn_artifacts = extract_session_artifacts(turn_summary)
        turn_failed = turn_state == "failed" or (
            turn_state not in ("exited", "idle", "done", "completed")
        )
        turn_kind = "stall" if turn_failed else "done"
        turn_body = (
            f"@{report_to} [{turn_kind}] Local `{harness}` harness session `{session.id}` "
            + ("ended with state `{}`.".format(turn_state) if turn_failed else "finished follow-up turn.")
            + (f"\n{turn_summary}" if turn_summary else "")
        )
        stay_warm = bool(not turn_failed and turn_state in ("idle", "ready"))
        if stay_warm:
            if local_coordinator is not None:
                local_coordinator.set_member_session_state(swarm_id, str(session.id), "waiting")
            elif swarm_registry is not None:
                swarm_registry.set_session_state(str(session.id), "waiting", swarm_id=swarm_id)
        try:
            posted = poster.post_message(
                swarm_id,
                {
                    "agent": actor_name,
                    "slot_id": actor_name,
                    "text": turn_body[:4000],
                    "kind": turn_kind,
                    "mentions": [report_to],
                    "extract_mentions": False,
                    "artifacts": turn_artifacts,
                    "thread_id": thread_id,
                    "keep_warm": stay_warm,
                },
            )
            eff_follow_state = "waiting" if stay_warm else turn_state
            if stay_warm and local_coordinator is not None:
                try:
                    cur_view = local_coordinator.get_swarm(swarm_id)
                    cur_m = (cur_view.get("members_by_slot") or {}).get(actor_name)
                    if isinstance(cur_m, dict) and cur_m.get("session_state"):
                        eff_follow_state = str(cur_m["session_state"])
                except Exception:  # noqa: BLE001
                    pass
            result_box.update(
                {
                    "reported": True,
                    "final_state": turn_state,
                    "session_state": eff_follow_state,
                    "warm": stay_warm,
                    "kind": turn_kind,
                    "seq": posted.get("seq"),
                    "artifacts": turn_artifacts,
                    "summary": turn_summary,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm follow-up report failed swarm=%s session=%s: %s", swarm_id, session.id, exc)
        if not stay_warm:
            _finish_awrun_item(
                run_id,
                failed=turn_failed,
                summary=turn_summary,
                awrun_dir=awrun_dir,
            )
            break


def _invoke_confined_runner(runner_fn: Any, cfg: Any, **kwargs: Any) -> Any:
    """Invoke a container-confinement runner, passing only parameters it accepts."""
    try:
        sig = inspect.signature(runner_fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return runner_fn(cfg, **kwargs)
        filtered = {k: v for k, v in kwargs.items() if k in params}
        return runner_fn(cfg, **filtered)
    except (TypeError, ValueError):
        return runner_fn(cfg, **kwargs)


def delegate_harness_worker(
    mgr: Any,
    genesis: Optional[SwarmGenesisClient] = None,
    *,
    swarm_id: str,
    agent: str,
    harness: str,
    prompt: str,
    cwd: str = "",
    model_profile: str = "",
    title: str = "",
    thread_id: str = "",
    report_to: str = "coordinator",
    wait: bool = False,
    timeout_s: float = 300.0,
    poll_s: float = 0.5,
    slot_id: str = "",
    member_id: str = "",
    pool: str = "",
    owns: Sequence[str] = (),
    must_not_touch: Sequence[str] = (),
    sibling_digests: Sequence[Any] = (),
    use_worktree: bool = False,
    use_leases: bool = False,
    lease_ttl_s: int = 3600,
    allow_peer_input: bool = True,
    keep_warm: bool = True,
    local_only: Optional[bool] = None,
    local_coordinator: Optional[LocalSwarmCoordinator] = None,
    rooms: Any = None,
    swarm_registry: Any = None,
    steer_dispatcher: Any = None,
    execution_mode: Optional[str] = None,
    limits: Optional[Dict[str, Any]] = None,
    egress: Optional[Dict[str, Any]] = None,
    isolation: Optional[Dict[str, Any]] = None,
    inference_runtime: Optional[str] = None,
    shared_ro_mmap: Optional[bool] = None,
    model_path: Optional[str] = None,
    model_sha256: Optional[str] = None,
    cpu_pin: Optional[str] = None,
    container_runner: Optional[Callable[..., Any]] = None,
    awrun_runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Delegate a SwarmBoard worker slot to a real local ``awsh`` harness session and
    stream its ``[done]`` summary + artifacts back onto the Swarm Board."""
    from adk.harnesses.manager import ManagerError
    from adk.harnesses.registry import get as get_spec
    from adk.harnesses.session import SessionConfig

    sid = _validate_swarm_id(swarm_id)
    h = str(harness or "").strip().lower()
    try:
        get_spec(h)
    except KeyError as exc:
        raise SwarmApiError(str(exc), status=422) from exc
    task_prompt = str(prompt or "").strip()
    if not task_prompt:
        raise SwarmApiError("prompt is required for harness delegation", status=422)
    who = str(agent or h).strip().lower() or h
    slot = str(slot_id or member_id or who).strip().lstrip("@") or who
    member = str(member_id or slot).strip().lstrip("@") or slot

    use_local = bool(
        local_only is True
        or genesis is None
        or (local_coordinator is not None and local_coordinator.has_swarm(sid))
    )
    if use_local and local_coordinator is None:
        local_coordinator = LocalSwarmCoordinator(
            rooms=rooms,
            swarm_registry=swarm_registry,
            steer_dispatcher=steer_dispatcher,
            mgr=mgr,
        )
    if not use_local:
        assert genesis is not None
        genesis._auth_headers(require_auth=True)

    if swarm_registry is None and local_coordinator is not None:
        swarm_registry = local_coordinator.swarm_registry

    stored_member: Dict[str, Any] = {}
    stored_thread: Dict[str, Any] = {}
    stored_meta: Dict[str, Any] = {}
    resolved_digests = list(sibling_digests or [])
    if use_local and local_coordinator is not None and local_coordinator.has_swarm(sid):
        try:
            view = local_coordinator.get_swarm(sid)
            stored_meta = dict(view.get("metadata") or {})
            by_slot = view.get("members_by_slot") or {}
            if slot in by_slot and isinstance(by_slot[slot], dict):
                stored_member = dict(by_slot[slot])
            else:
                for cand_m in view.get("members") or []:
                    if isinstance(cand_m, dict) and (
                        cand_m.get("slot_id") == slot
                        or cand_m.get("member_id") == member
                        or str(cand_m.get("agent") or "").lower() == who
                    ):
                        stored_member = dict(cand_m)
                        break
            target_th_id = str(thread_id or stored_member.get("thread_id") or "")
            for cand_t in view.get("threads") or []:
                if isinstance(cand_t, dict) and (
                    cand_t.get("slot_id") == slot
                    or (target_th_id and cand_t.get("id") == target_th_id)
                ):
                    stored_thread = dict(cand_t)
                    break
            if not resolved_digests:
                resolved_digests = _compute_sibling_digests(
                    view.get("threads") or [], exclude_slot_id=slot
                )
        except Exception:  # noqa: BLE001
            pass

    eff_exec_mode = _validate_execution_mode(
        execution_mode
        if execution_mode is not None
        else (
            stored_member.get("execution_mode")
            or stored_thread.get("execution_mode")
            or stored_meta.get("execution_mode")
            or "standard"
        )
    )
    eff_limits = limits if limits is not None else stored_thread.get("limits")
    eff_egress = egress if egress is not None else stored_thread.get("egress")
    eff_isolation = isolation if isolation is not None else stored_thread.get("isolation")
    eff_pool = (
        pool
        or str(stored_member.get("pool") or stored_thread.get("pool") or "")
        or _derive_pool_name(slot)
    )
    eff_owns = (
        list(owns)
        if owns
        else list(stored_member.get("owns") or stored_thread.get("owns") or [])
    )
    eff_must_not_touch = (
        list(must_not_touch)
        if must_not_touch
        else list(
            stored_member.get("must_not_touch")
            or stored_thread.get("must_not_touch")
            or []
        )
    )
    existing_run_id = str(
        stored_member.get("run_id") or stored_thread.get("run_id") or ""
    ).strip()

    eff_rt = (
        inference_runtime
        if inference_runtime is not None
        else str(
            stored_member.get("inference_runtime")
            or stored_thread.get("inference_runtime")
            or stored_meta.get("inference_runtime")
            or "standard"
        )
    )
    eff_shared = (
        bool(shared_ro_mmap)
        if shared_ro_mmap is not None
        else bool(
            stored_member.get(
                "shared_ro_mmap",
                stored_thread.get(
                    "shared_ro_mmap", stored_meta.get("shared_ro_mmap", False)
                ),
            )
        )
    )
    eff_model_path = (
        model_path
        if model_path is not None
        else str(
            stored_member["model_path"]
            if "model_path" in stored_member
            else stored_thread["model_path"]
            if "model_path" in stored_thread
            else stored_meta.get("model_path") or ""
        )
    )
    eff_model_sha = (
        model_sha256
        if model_sha256 is not None
        else str(
            stored_member["model_sha256"]
            if "model_sha256" in stored_member
            else stored_thread["model_sha256"]
            if "model_sha256" in stored_thread
            else stored_meta.get("model_sha256") or ""
        )
    )
    eff_cpu_pin = (
        cpu_pin
        if cpu_pin is not None
        else str(
            stored_member["cpu_pin"]
            if "cpu_pin" in stored_member
            else stored_thread["cpu_pin"]
            if "cpu_pin" in stored_thread
            else stored_meta.get("cpu_pin") or ""
        )
    )
    zeal_meta = _resolve_awzeal_metadata(
        inference_runtime=eff_rt,
        shared_ro_mmap=eff_shared,
        model_path=eff_model_path,
        model_sha256=eff_model_sha,
        cpu_pin=eff_cpu_pin,
    )

    base_cwd = str(cwd or "").strip() or os.getcwd()
    bindings = bind_slot_worktree_and_leases(
        cwd=base_cwd,
        swarm_id=sid,
        slot_id=slot,
        owns=eff_owns,
        use_worktree=use_worktree,
        use_leases=use_leases,
        lease_ttl_s=lease_ttl_s,
    )
    effective_cwd = bindings["cwd"]

    try:
        aw_info = _prepare_awrun_track(
            swarm_id=sid,
            thread_id=thread_id,
            slot_id=slot,
            pool=eff_pool,
            owns=eff_owns,
            execution_mode=eff_exec_mode,
            limits=eff_limits,
            egress=eff_egress,
            isolation=eff_isolation,
            base_argv=[h],
            existing_run_id=existing_run_id,
            start_now=True,
        )
    except Exception:
        if bindings["lease_ids"] or bindings["worktree_name"]:
            release_slot_bindings(
                cwd=base_cwd,
                swarm_id=sid,
                slot_id=slot,
                lease_ids=bindings["lease_ids"],
                worktree_name=bindings["worktree_name"],
                worktree_path=bindings["worktree_path"],
                branch=bindings["branch"],
                remove_worktree=True,
            )
        raise

    full_prompt = _format_scope_prompt(
        task_prompt,
        slot_id=slot
        if (
            slot_id
            or eff_pool
            or eff_owns
            or eff_must_not_touch
            or bindings["worktree_path"]
            or aw_info["run_id"]
            or zeal_meta["inference_runtime"] != "standard"
            or zeal_meta["shared_ro_mmap"]
            or zeal_meta["model_path"]
            or zeal_meta["model_sha256"]
            or zeal_meta["cpu_pin"]
        )
        else "",
        pool=eff_pool,
        owns=eff_owns,
        must_not_touch=eff_must_not_touch,
        sibling_digests=resolved_digests,
        worktree_path=bindings["worktree_path"],
        lease_ids=bindings["lease_ids"],
        execution_mode=eff_exec_mode,
        run_id=aw_info["run_id"],
        inference_runtime=zeal_meta["inference_runtime"],
        shared_ro_mmap=zeal_meta["shared_ro_mmap"],
        model_path=zeal_meta["model_path"],
        model_sha256=zeal_meta["model_sha256"],
        cpu_pin=zeal_meta["cpu_pin"],
    )

    cfg = SessionConfig(
        harness=h,
        cwd=effective_cwd,
        title=(title or f"swarm:{sid}:{slot}")[:80],
        agent=who,
        model_profile=str(model_profile or "").strip(),
        allow_peer_input=bool(allow_peer_input),
    )

    wrapped_argv = list(aw_info["wrapped_argv"] or [])
    container_required = bool(
        (wrapped_argv and wrapped_argv != [h])
        or (isinstance(eff_isolation, dict) and eff_isolation.get("mode") == "container")
    )
    confined_runner = (
        container_runner
        if callable(container_runner)
        else awrun_runner
        if callable(awrun_runner)
        else getattr(mgr, "create_confined", None)
        if callable(getattr(mgr, "create_confined", None))
        else getattr(mgr, "container_runner", None)
        if callable(getattr(mgr, "container_runner", None))
        else getattr(mgr, "awrun_runner", None)
        if callable(getattr(mgr, "awrun_runner", None))
        else None
    )
    mgr_enforces = getattr(mgr, "awrun_enforces", None) or ()
    mgr_supports_container = bool(
        getattr(mgr, "supports_container_isolation", False)
        or "container" in mgr_enforces
    )

    def _cleanup_on_spawn_fail(summary_msg: str) -> None:
        _finish_awrun_item(
            aw_info["run_id"],
            failed=True,
            summary=summary_msg,
            awrun_dir=str(aw_info.get("awrun_dir") or ""),
        )
        if bindings["lease_ids"] or bindings["worktree_name"]:
            release_slot_bindings(
                cwd=base_cwd,
                swarm_id=sid,
                slot_id=slot,
                lease_ids=bindings["lease_ids"],
                worktree_name=bindings["worktree_name"],
                worktree_path=bindings["worktree_path"],
                branch=bindings["branch"],
                remove_worktree=True,
            )

    if container_required and confined_runner is None and not mgr_supports_container:
        msg = (
            f"awrun container isolation requested for slot {slot!r} "
            f"(wrapped_argv={wrapped_argv}), but SessionManager cannot enforce "
            "container execution and no container_runner was provided; refusing to run unconfined"
        )
        _cleanup_on_spawn_fail(msg)
        raise SwarmApiError(msg, status=422)

    try:
        if container_required and confined_runner is not None:
            session = _invoke_confined_runner(
                confined_runner,
                cfg,
                wrapped_argv=wrapped_argv,
                isolation=dict(eff_isolation) if isinstance(eff_isolation, dict) else {},
                limits=dict(eff_limits) if isinstance(eff_limits, dict) else None,
                egress=dict(eff_egress) if isinstance(eff_egress, dict) else None,
                run_id=aw_info["run_id"],
            )
        elif container_required and mgr_supports_container:
            session = _invoke_confined_runner(
                mgr.create,
                cfg,
                wrapped_argv=wrapped_argv,
                isolation=dict(eff_isolation) if isinstance(eff_isolation, dict) else {},
                limits=dict(eff_limits) if isinstance(eff_limits, dict) else None,
                egress=dict(eff_egress) if isinstance(eff_egress, dict) else None,
                run_id=aw_info["run_id"],
            )
        else:
            session = mgr.create(cfg)
    except SwarmApiError as exc:
        _cleanup_on_spawn_fail(str(exc))
        raise
    except (ManagerError, OSError, RuntimeError) as exc:
        _cleanup_on_spawn_fail(str(exc))
        raise SwarmApiError(str(exc), status=422) from exc

    effective_thread_id = thread_id
    if use_local and local_coordinator is not None:
        reg = local_coordinator.register_member_session(
            sid,
            slot_id=slot,
            member_id=member,
            agent=who,
            session_id=str(session.id),
            thread_id=thread_id,
            pool=eff_pool,
            owns=eff_owns,
            must_not_touch=eff_must_not_touch,
            sibling_digests=resolved_digests,
            worktree_path=bindings["worktree_path"],
            worktree_name=bindings["worktree_name"],
            branch=bindings["branch"],
            lease_ids=bindings["lease_ids"],
            cwd=effective_cwd,
            allow_peer_input=allow_peer_input,
            execution_mode=eff_exec_mode,
            run_id=aw_info["run_id"],
            inference_runtime=zeal_meta["inference_runtime"],
            shared_ro_mmap=zeal_meta["shared_ro_mmap"],
            model_path=zeal_meta["model_path"],
            model_sha256=zeal_meta["model_sha256"],
            cpu_pin=zeal_meta["cpu_pin"],
            overwrite_zeal_meta=True,
        )
        effective_thread_id = effective_thread_id or str(reg.get("thread_id") or "")
    elif swarm_registry is not None:
        swarm_registry.register_slot(
            swarm_id=sid,
            slot_id=slot,
            member_id=member,
            agent=who,
            session_id=str(session.id),
            thread_id=effective_thread_id,
            pool=eff_pool,
            role=who,
            allow_peer_input=allow_peer_input,
            session_state="working",
            metadata={
                "worktree_path": bindings["worktree_path"],
                "owns": list(eff_owns),
                "run_id": aw_info["run_id"],
                **zeal_meta,
            },
        )

    if hasattr(session, "submit") and callable(session.submit):
        try:
            session.submit(full_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("initial submit on session %s failed: %s", session.id, exc)
    elif hasattr(session, "send") and callable(session.send):
        try:
            session.send(full_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("initial send on session %s failed: %s", session.id, exc)

    poster: Any = local_coordinator if (use_local and local_coordinator is not None) else genesis

    start_seq = None
    try:
        started = poster.post_message(
            sid,
            {
                "agent": slot if use_local else who,
                "slot_id": slot,
                "text": (
                    f"Delegated worker slot `{slot}` to local `{h}` harness session `{session.id}` "
                    f"on `{socket.gethostname()}`."
                ),
                "kind": "update",
                "thread_id": effective_thread_id,
                "extract_mentions": False,
            },
        )
        start_seq = started.get("seq")
    except SwarmApiError as exc:
        logger.debug("swarm start post skipped for %s: %s", sid, exc)

    result_box: Dict[str, Any] = {}
    turn1_done = threading.Event()
    watcher = threading.Thread(
        target=_watch_and_report,
        kwargs={
            "session": session,
            "poster": poster,
            "swarm_id": sid,
            "agent": who,
            "slot_id": slot if use_local else who,
            "harness": h,
            "prompt": full_prompt,
            "cwd": cfg.cwd,
            "model_profile": str(model_profile or ""),
            "started_at": time.time(),
            "thread_id": effective_thread_id,
            "report_to": str(report_to or "coordinator").strip().lstrip("@") or "coordinator",
            "timeout_s": timeout_s,
            "poll_s": poll_s,
            "result_box": result_box,
            "keep_warm": keep_warm,
            "turn1_done": turn1_done,
            "swarm_registry": swarm_registry,
            "local_coordinator": local_coordinator if use_local else None,
            "run_id": aw_info["run_id"],
            "awrun_dir": str(aw_info.get("awrun_dir") or ""),
        },
        name=f"swarm-harness-{sid}-{session.id}",
        daemon=True,
    )
    watcher.start()
    if wait:
        turn1_done.wait(timeout=max(1.0, float(timeout_s)) + 2.0)
        if not result_box.get("warm"):
            watcher.join(timeout=1.0)

    out: Dict[str, Any] = {
        "ok": True,
        "swarm_id": sid,
        "session_id": str(session.id),
        "slot_id": slot,
        "member_id": member,
        "pool": eff_pool,
        "harness": h,
        "agent": who,
        "thread_id": effective_thread_id,
        "cwd": effective_cwd,
        "worktree_path": bindings["worktree_path"],
        "lease_ids": bindings["lease_ids"],
        "local_only": use_local,
        "status": _session_state_str(session) or "running",
        "start_seq": start_seq,
        "ui_url": swarm_ui_url(swarm_id=sid),
        "execution_mode": eff_exec_mode,
        "run_id": aw_info["run_id"],
        "wrapped_argv": wrapped_argv,
        **zeal_meta,
    }
    if wait:
        out["result"] = result_box
    return out


def mount(
    app: Any,
    auth: Any,
    require_owner: Any,
    *,
    mgr: Any = None,
    genesis_factory: Optional[Callable[[], SwarmGenesisClient]] = None,
    rooms: Any = None,
    steer_dispatcher: Any = None,
    swarm_registry: Any = None,
    local_coordinator: Optional[LocalSwarmCoordinator] = None,
    container_runner: Optional[Callable[..., Any]] = None,
    awrun_runner: Optional[Callable[..., Any]] = None,
) -> None:
    """Mount ``/swarms/*`` bridge + local Room-backed SwarmBoard routes onto ``:8362``."""
    from fastapi import Body, Depends, HTTPException, Query, Request
    from pydantic import BaseModel, Field, ValidationError

    local_coord = local_coordinator or LocalSwarmCoordinator(
        rooms=rooms,
        swarm_registry=swarm_registry,
        steer_dispatcher=steer_dispatcher,
        mgr=mgr,
    )
    if hasattr(app, "state"):
        app.state.local_swarm_coordinator = local_coord
        app.state.swarm_registry = local_coord.swarm_registry

    class LaunchBody(BaseModel):
        goal: str = Field(..., min_length=1, max_length=6000)
        title: str = Field("", max_length=120)
        coordinator: str = Field("aither", max_length=40)
        leads: Optional[List[str]] = None
        tasks: Optional[List[Any]] = None
        slots: Optional[List[Any]] = None
        workers: Optional[List[Any]] = None
        pools: Optional[Dict[str, Any]] = None
        effort: int = Field(7, ge=6, le=10)
        local_only: bool = True
        local_room: bool = False
        mode: str = Field("", max_length=24)
        room_id: str = Field("", max_length=64)
        stall_timeout_s: float = Field(180.0, ge=5.0, le=3600.0)
        nudge_timeout_s: Optional[float] = Field(None, ge=1.0, le=3600.0)
        bind_awgit: Optional[bool] = None
        verifier_pool_size: Optional[int] = Field(None, ge=1, le=16)
        streaming_verify: Optional[bool] = None
        work_elevated: bool = True
        auto_start: bool = True
        use_worktree: bool = False
        use_leases: bool = False
        requested_by: str = Field("awsh:owner", max_length=80)
        metadata: Optional[Dict[str, Any]] = None
        execution_mode: str = Field("standard", max_length=24)
        limits: Optional[Dict[str, Any]] = None
        egress: Optional[Dict[str, Any]] = None
        isolation: Optional[Dict[str, Any]] = None
        inference_runtime: str = Field("standard", max_length=24)
        shared_ro_mmap: bool = False
        model_path: str = Field("", max_length=500)
        model_sha256: str = Field("", max_length=128)
        cpu_pin: str = Field("", max_length=64)
        awseal_expect_key: str = Field("", max_length=256)
        seal_artifacts: bool = False

    class SteerBody(BaseModel):
        message: str = Field(..., min_length=1, max_length=4000)
        user: str = Field("", max_length=80)
        role: str = Field("admin", max_length=40)
        mentions: Optional[List[str]] = None

    class ControlBody(BaseModel):
        action: str = Field("pause", max_length=20)
        user: str = Field("", max_length=80)
        reason: str = Field("", max_length=300)
        export_runs: bool = False
        export_dir: str = Field("", max_length=500)

    class PostBody(BaseModel):
        agent: str = Field("coordinator", max_length=80)
        slot_id: Optional[str] = Field(None, max_length=80)
        text: str = Field(..., min_length=1, max_length=8000)
        kind: str = Field("update", max_length=32)
        mentions: Optional[List[str]] = None
        artifacts: Optional[List[Dict[str, Any]]] = None
        thread_id: str = Field("", max_length=80)
        hops: int = Field(0, ge=0, le=2)
        keep_warm: bool = True
        local_only: Optional[bool] = None
        artifact_dir: str = Field("", max_length=500)
        seal_artifacts: Optional[bool] = None
        expect_key: str = Field("", max_length=256)
        awseal_expect_key: str = Field("", max_length=256)
        key_path: str = Field("", max_length=500)
        seal: Optional[Dict[str, Any]] = None

    class NudgeBody(BaseModel):
        slot_id: Optional[str] = Field(None, max_length=80)
        thread_id: str = Field("", max_length=80)
        reason: str = Field("", max_length=500)
        message: str = Field("", max_length=500)
        escalate_after_s: float = Field(30.0, ge=0.0, le=3600.0)
        force_escalate: bool = False
        local_only: Optional[bool] = None

    class DelegateHarnessBody(BaseModel):
        harness: str = Field(..., min_length=1, max_length=40)
        prompt: str = Field(..., min_length=1, max_length=12000)
        agent: str = Field("demiurge", max_length=40)
        slot_id: str = Field("", max_length=80)
        member_id: str = Field("", max_length=80)
        pool: str = Field("", max_length=40)
        owns: Optional[List[str]] = None
        must_not_touch: Optional[List[str]] = None
        sibling_digests: Optional[List[Any]] = None
        cwd: str = Field("", max_length=500)
        model_profile: str = Field("", max_length=80)
        title: str = Field("", max_length=100)
        thread_id: str = Field("", max_length=80)
        report_to: str = Field("coordinator", max_length=40)
        wait: bool = False
        timeout_s: float = Field(300.0, ge=1.0, le=3600.0)
        use_worktree: bool = False
        use_leases: bool = False
        lease_ttl_s: int = Field(3600, ge=60, le=86400)
        allow_peer_input: bool = True
        keep_warm: bool = True
        local_only: Optional[bool] = None
        local_room: bool = False
        execution_mode: Optional[str] = Field(None, max_length=24)
        limits: Optional[Dict[str, Any]] = None
        egress: Optional[Dict[str, Any]] = None
        isolation: Optional[Dict[str, Any]] = None
        inference_runtime: Optional[str] = Field(None, max_length=24)
        shared_ro_mmap: Optional[bool] = None
        model_path: Optional[str] = Field(None, max_length=500)
        model_sha256: Optional[str] = Field(None, max_length=128)
        cpu_pin: Optional[str] = Field(None, max_length=64)

    def _is_owner_principal(principal: Any) -> bool:
        if hasattr(principal, "plan"):
            return str(getattr(principal, "plan") or "").strip().lower() == "owner"
        return bool(getattr(principal, "owner", False))

    def _require_swarm_actor(principal: Any = Depends(auth)) -> Any:
        """Allow owner or dedicated ``plan='agent'`` tokens; deny ``awconnect`` and other scoped tokens (403)."""
        if _is_owner_principal(principal):
            return principal
        pid = str(getattr(principal, "id", "") or "").strip().lower()
        plan = str(getattr(principal, "plan", "") or "").strip().lower()
        if plan == "agent" and not pid.startswith("awconnect"):
            return principal
        raise HTTPException(
            status_code=403,
            detail="swarm post/nudge requires owner or scoped agent token",
        )

    def _resolve_caller_auth(principal: Any, request: Optional[Request] = None) -> tuple[tuple[str, str], str]:
        """Resolve real ``(principal_id, plan)`` and ``actor_kind`` without laundering."""
        principal_id = str(getattr(principal, "id", "") or "agent")
        if hasattr(principal, "plan"):
            plan = str(getattr(principal, "plan") or "").strip().lower() or "agent"
            link_actor = (
                str(request.headers.get("x-aither-link-actor", "")).strip().lower()
                if request is not None
                else ""
            )
            is_owner = (plan == "owner") or (plan == "link" and link_actor == "owner")
        else:
            is_owner = bool(getattr(principal, "owner", False))
            plan = "owner" if is_owner else "peer"
        if is_owner:
            return (principal_id, "owner"), "human"
        return (principal_id, plan), "adk_agent"

    def _parse(model_cls: Any, raw: Any) -> Any:
        try:
            return model_cls.model_validate(raw or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _client() -> SwarmGenesisClient:
        return genesis_factory() if genesis_factory is not None else SwarmGenesisClient()

    def _http_err(exc: SwarmApiError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    def _force_local() -> bool:
        return os.environ.get("AITHER_SWARM_LOCAL", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    @app.get("/swarms/capacity", dependencies=[Depends(auth)])
    def swarm_capacity(probe: bool = Query(default=True)) -> Dict[str, Any]:
        return local_swarm_capacity(mgr, genesis=_client(), probe_genesis=probe)

    @app.get("/swarms", dependencies=[Depends(auth)])
    def list_swarms(
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        local_only: Optional[bool] = Query(default=None),
    ) -> Dict[str, Any]:
        local_rows = local_coord.list_swarms(status=status, limit=limit)["swarms"]
        remote_rows: List[Dict[str, Any]] = []
        client = _client()
        if not (_force_local() or local_only is True):
            try:
                data = client.list_swarms(status=status, limit=limit)
                remote_rows = data.get("swarms") if isinstance(data.get("swarms"), list) else []
            except SwarmApiError as exc:
                if genesis_factory is not None and not local_rows:
                    raise _http_err(exc)
        seen_ids: set[str] = set()
        merged: List[Dict[str, Any]] = []
        for row in [*local_rows, *remote_rows]:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("swarm_id") or "")
            if sid and sid in seen_ids:
                continue
            if sid:
                seen_ids.add(sid)
            if not row.get("ui_url"):
                row["ui_url"] = swarm_ui_url(row)
            merged.append(row)
        merged = merged[:limit]
        return {
            "swarms": merged,
            "count": len(merged),
            "ui_url": swarm_ui_url(),
            "capacity": local_swarm_capacity(mgr, genesis=client),
        }

    @app.get("/swarms/{swarm_id}", dependencies=[Depends(auth)])
    def get_swarm(swarm_id: str) -> Dict[str, Any]:
        if local_coord.has_swarm(swarm_id):
            return local_coord.get_swarm(swarm_id)
        client = _client()
        try:
            view = client.get_swarm(swarm_id)
        except SwarmApiError as exc:
            raise _http_err(exc)
        if isinstance(view, dict) and not view.get("ui_url"):
            view["ui_url"] = swarm_ui_url(view, swarm_id)
        return view

    @app.get("/swarms/{swarm_id}/board", dependencies=[Depends(auth)])
    def get_swarm_board(
        swarm_id: str,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Dict[str, Any]:
        if local_coord.has_swarm(swarm_id):
            return local_coord.get_board(swarm_id, since=since, limit=limit)
        client = _client()
        try:
            return client.get_board(swarm_id, since=since, limit=limit)
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms")
    def launch_swarm(
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(LaunchBody, raw)
        payload = body.model_dump()
        if not payload.get("requested_by"):
            payload["requested_by"] = f"awsh:{getattr(principal, 'id', 'owner')}"
        want_local = bool(
            body.local_room
            or str(body.mode or "").strip().lower() == "local"
            or _force_local()
        )
        if want_local:
            try:
                return local_coord.create_swarm(payload)
            except SwarmApiError as exc:
                raise _http_err(exc)

        client = _client()
        try:
            view = client.create_swarm(payload)
        except SwarmApiError as exc:
            if genesis_factory is None and body.local_only and (
                isinstance(exc, SwarmAuthError) or exc.status == 503
            ):
                try:
                    return local_coord.create_swarm(payload)
                except SwarmApiError as local_exc:
                    raise _http_err(local_exc) from local_exc
            raise _http_err(exc)
        if isinstance(view, dict) and not view.get("ui_url"):
            view["ui_url"] = swarm_ui_url(view)
        return view

    @app.post("/swarms/{swarm_id}/post")
    def post_swarm_message(
        swarm_id: str,
        request: Request,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(_require_swarm_actor),
    ) -> Dict[str, Any]:
        body = _parse(PostBody, raw)
        payload = body.model_dump()
        if (
            not _is_owner_principal(principal)
            and "agent" not in raw
            and "slot_id" not in raw
            and "sender" not in raw
        ):
            pid = str(getattr(principal, "id", "") or "").strip()
            if pid.lower().startswith("agent:") and pid.split(":", 1)[1].strip():
                payload["agent"] = pid.split(":", 1)[1].strip()
        auth_tuple, _actor_kind = _resolve_caller_auth(principal, request)
        if local_coord.has_swarm(swarm_id) or body.local_only is True or _force_local():
            try:
                return local_coord.post_message(
                    swarm_id,
                    payload,
                    auth=auth_tuple,
                )
            except SwarmApiError as exc:
                raise _http_err(exc)
        client = _client()
        try:
            return client.post_message(swarm_id, body.model_dump(exclude_none=True))
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/steer")
    def steer_swarm(
        swarm_id: str,
        request: Request,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(auth),
    ) -> Dict[str, Any]:
        body = _parse(SteerBody, raw)
        who = body.user.strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        auth_tuple, actor_kind = _resolve_caller_auth(principal, request)
        if local_coord.has_swarm(swarm_id) or _force_local():
            try:
                return local_coord.steer_swarm(
                    swarm_id,
                    {
                        "message": body.message,
                        "user": who,
                        "role": body.role,
                        "mentions": body.mentions,
                        "actor_kind": actor_kind,
                    },
                    auth=auth_tuple,
                )
            except SwarmApiError as exc:
                raise _http_err(exc)
        client = _client()
        try:
            steer_payload: Dict[str, Any] = {
                "message": body.message,
                "user": who,
                "role": body.role,
                "source": "awsh",
            }
            if body.mentions is not None:
                steer_payload["mentions"] = body.mentions
            return client.steer_swarm(swarm_id, steer_payload)
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/pause")
    def pause_swarm(
        swarm_id: str,
        raw: Optional[Dict[str, Any]] = Body(default=None),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(ControlBody, raw) if raw else None
        who = (body.user if body else "").strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        if local_coord.has_swarm(swarm_id) or _force_local():
            try:
                return local_coord.pause_swarm(
                    swarm_id,
                    {
                        "action": "pause",
                        "user": who,
                        "reason": body.reason if body else "",
                        "export_runs": body.export_runs if body else False,
                        "export_dir": body.export_dir if body else "",
                    },
                )
            except SwarmApiError as exc:
                raise _http_err(exc)
        client = _client()
        try:
            return client.pause_swarm(
                swarm_id,
                {"action": "pause", "user": who, "reason": body.reason if body else ""},
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/control")
    def control_swarm(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(ControlBody, raw)
        who = body.user.strip() or f"awsh:{getattr(principal, 'id', 'owner')}"
        if local_coord.has_swarm(swarm_id) or _force_local():
            try:
                return local_coord.control_swarm(
                    swarm_id,
                    {
                        "action": body.action,
                        "user": who,
                        "reason": body.reason,
                        "export_runs": body.export_runs,
                        "export_dir": body.export_dir,
                    },
                )
            except SwarmApiError as exc:
                raise _http_err(exc)
        client = _client()
        try:
            return client.control_swarm(
                swarm_id,
                {"action": body.action, "user": who, "reason": body.reason},
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/nudge")
    def nudge_swarm_slot(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(_require_swarm_actor),
    ) -> Dict[str, Any]:
        body = _parse(NudgeBody, raw)
        if body.force_escalate and not _is_owner_principal(principal):
            raise HTTPException(
                status_code=403,
                detail="force_escalate requires owner credential",
            )
        try:
            swarm_id = _validate_swarm_id(swarm_id)
        except SwarmApiError as exc:
            raise _http_err(exc)
        slot = str(body.slot_id or "").strip()
        tid = str(body.thread_id or "").strip()
        reason_text = str(body.reason or body.message or "").strip()
        if not slot and not tid:
            raise HTTPException(status_code=422, detail="slot_id or thread_id is required")
        if local_coord.has_swarm(swarm_id):
            if not slot and tid:
                view = local_coord.get_swarm(swarm_id)
                for th in view.get("threads") or ():
                    if str(th.get("id") or "") == tid:
                        slot = str(th.get("slot_id") or "")
                        break
            if not slot:
                raise HTTPException(status_code=404, detail=f"Unknown thread_id {tid!r}")
            try:
                return local_coord.nudge_stalled_slot(
                    swarm_id,
                    slot,
                    reason=reason_text,
                    escalate_after_s=body.escalate_after_s,
                    force_escalate=body.force_escalate,
                )
            except SwarmApiError as exc:
                raise _http_err(exc)
        is_local_only = (
            body.local_only is True
            or _force_local()
            or (
                body.local_only is None
                and genesis_factory is None
                and not (_internal_key() or _session_bearer())
            )
        )
        if is_local_only:
            raise HTTPException(status_code=404, detail=f"Local swarm '{swarm_id}' not found")
        client = _client()
        try:
            return client.nudge_slot(
                swarm_id,
                {
                    "slot_id": slot or None,
                    "thread_id": tid,
                    "message": reason_text,
                },
            )
        except SwarmApiError as exc:
            raise _http_err(exc)

    @app.post("/swarms/{swarm_id}/delegate-harness")
    def delegate_harness(
        swarm_id: str,
        raw: Dict[str, Any] = Body(...),
        principal: Any = Depends(require_owner),
    ) -> Dict[str, Any]:
        body = _parse(DelegateHarnessBody, raw)
        if mgr is None:
            raise HTTPException(status_code=503, detail="harness SessionManager unavailable")
        want_local = bool(
            body.local_room
            or body.local_only is True
            or _force_local()
            or local_coord.has_swarm(swarm_id)
            or (
                body.local_only is None
                and genesis_factory is None
                and not (_internal_key() or _session_bearer())
            )
        )
        try:
            return delegate_harness_worker(
                mgr,
                _client(),
                swarm_id=swarm_id,
                agent=body.agent,
                harness=body.harness,
                prompt=body.prompt,
                cwd=body.cwd,
                model_profile=body.model_profile,
                title=body.title,
                thread_id=body.thread_id,
                report_to=body.report_to,
                wait=body.wait,
                timeout_s=body.timeout_s,
                slot_id=body.slot_id,
                member_id=body.member_id,
                pool=body.pool,
                owns=body.owns or (),
                must_not_touch=body.must_not_touch or (),
                sibling_digests=body.sibling_digests or (),
                use_worktree=body.use_worktree,
                use_leases=body.use_leases,
                lease_ttl_s=body.lease_ttl_s,
                allow_peer_input=body.allow_peer_input,
                keep_warm=body.keep_warm,
                local_only=want_local,
                local_coordinator=local_coord,
                rooms=local_coord.rooms,
                swarm_registry=local_coord.swarm_registry,
                steer_dispatcher=steer_dispatcher,
                execution_mode=body.execution_mode,
                limits=body.limits,
                egress=body.egress,
                isolation=body.isolation,
                inference_runtime=body.inference_runtime,
                shared_ro_mmap=body.shared_ro_mmap,
                model_path=body.model_path,
                model_sha256=body.model_sha256,
                cpu_pin=body.cpu_pin,
                container_runner=container_runner,
                awrun_runner=awrun_runner,
            )
        except SwarmApiError as exc:
            raise _http_err(exc)
