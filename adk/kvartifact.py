"""adk kvartifact — a KV cache as a portable, verified, model-revision-bound file.

A KV snapshot (a llama-server slot file, a kvslot store entry) is only meaningful on the
exact serve configuration that produced it: same weights, same build, same KV cache type,
same RoPE settings, same layer split. Loaded anywhere else it does not crash, it produces
fluent, confidently wrong text. This module makes that binding explicit and checks it
BEFORE any byte reaches a server:

* :class:`KVBinding` names the serve configuration field by field, so a refusal says WHICH
  field differs ("cache_type_k: f16 != q8_0"), not just "fingerprint mismatch".
* :func:`pack` writes one self-describing file: magic, a JSON header (binding, its digest,
  the sharing-scope fingerprint, kvpool key, token count, payload sha256 and size), then
  the payload bytes.
* :func:`verify` / :func:`unpack` are fail-closed and ordered: header format, header
  integrity (binding digest recomputed), binding vs the expected one, scope, then the
  payload sha256 streamed in full. Any failure raises; ``unpack`` writes to a temp file and
  only renames it into place after the digest matched.

The idea is adapted from phantom-kv's phantom.lib container (github.com/lordx64/phantom-kv,
MIT): per-entry sha256 re-verified on every load and a hard model-id refusal before
inference. No code was taken; its refusal-removal application is not part of this.

Standard library only; Python 3.10.

CLI::

    python -m adk.kvartifact inspect FILE
    python -m adk.kvartifact verify FILE --binding binding.json [--scope-fp HEX]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional, Sequence

MAGIC = b"AWKVART1\n"
FORMAT = "awkv-artifact"
VERSION = 1
MAX_HEADER = 1 << 20  # a header is a few hundred bytes; anything huge is not ours
_CHUNK = 1 << 20

#: KVBinding fields compared for validity (``note`` is informational only).
_BOUND_FIELDS = (
    "model", "weights_sha256", "build", "cache_type_k", "cache_type_v",
    "n_ctx", "rope", "split", "extra",
)


class KVArtifactError(RuntimeError):
    """The artifact cannot be used. Never a silent miss: callers decide to fall back."""


class FormatError(KVArtifactError):
    """Not an artifact, an unknown version, or a header that does not hold together."""


class BindingMismatchError(KVArtifactError):
    """The artifact was built on a different serve configuration.

    Attributes:
        fields: ``{field: (artifact value, expected value)}`` for every differing field.
    """

    def __init__(self, diffs: dict[str, tuple[Any, Any]]):
        self.fields = diffs
        detail = "; ".join(f"{k}: {a!r} != {b!r}" for k, (a, b) in sorted(diffs.items()))
        super().__init__(f"KV artifact bound to a different serve config ({detail})")


class ScopeMismatchError(KVArtifactError):
    """The artifact belongs to another sharing scope (tenant/workspace)."""


class DigestMismatchError(KVArtifactError):
    """The payload bytes are not the bytes the header promises."""


@dataclass(frozen=True)
class KVBinding:
    """Everything that changes the bytes of a KV cache for the same tokens.

    ``model`` is the served model path or alias; ``weights_sha256`` is better when the
    caller has it. ``split`` describes how layers are placed (``-ts``/``--rpc`` devices,
    tensor split); a cache written under one split is not valid under another. ``extra``
    holds anything else the caller knows matters (``{"swa": "full"}``).
    """

    model: str
    weights_sha256: str = ""
    build: str = ""
    cache_type_k: str = ""
    cache_type_v: str = ""
    n_ctx: int = 0
    rope: str = ""
    split: str = ""
    extra: dict = field(default_factory=dict)
    note: str = ""

    def canonical(self) -> dict:
        """The bound fields only, with ``extra`` stringified and key-sorted."""
        out: dict[str, Any] = {}
        for name in _BOUND_FIELDS:
            value = getattr(self, name)
            if name == "extra":
                value = {str(k): str(v) for k, v in sorted(dict(value).items())}
            out[name] = value
        return out

    def digest(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def diff(self, other: "KVBinding") -> dict[str, tuple[Any, Any]]:
        """``{field: (self, other)}`` for each bound field that differs."""
        a, b = self.canonical(), other.canonical()
        return {k: (a[k], b[k]) for k in _BOUND_FIELDS if a[k] != b[k]}

    @classmethod
    def from_dict(cls, data: dict) -> "KVBinding":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in dict(data).items() if k in known}
        if "model" not in clean:
            raise FormatError("binding has no model")
        clean["n_ctx"] = int(clean.get("n_ctx", 0) or 0)
        clean["extra"] = dict(clean.get("extra") or {})
        return cls(**clean)

    @classmethod
    def from_llama_props(cls, props: dict, **known: Any) -> "KVBinding":
        """A binding from llama-server ``GET /props`` plus what the caller launched with.

        ``/props`` reports the model path, the build and ``n_ctx``; it does NOT report the
        KV cache type, RoPE overrides or the layer split, so pass those as ``known``
        (``cache_type_k="q8_0", split="rpc:dgx,5090 ts=3,1"``). Omitting one makes two
        servers that differ only in it look identical -- the binding can only be as
        complete as what the caller tells it.
        """
        dgs = props.get("default_generation_settings") or {}
        n_ctx = dgs.get("n_ctx") or props.get("n_ctx") or 0
        base = {
            "model": str(props.get("model_path") or props.get("model_alias") or ""),
            "build": str(props.get("build_info") or ""),
            "n_ctx": int(n_ctx or 0),
        }
        base.update(known)
        if not base["model"]:
            raise ValueError("props name no model (model_path/model_alias)")
        return cls.from_dict(base)


@dataclass
class Header:
    """The JSON header of an artifact file."""

    binding: dict
    binding_digest: str
    payload_sha256: str
    payload_nbytes: int
    key: str = ""
    tokens: int = 0
    scope_fp: str = ""
    created: float = 0.0
    source: str = ""
    meta: dict = field(default_factory=dict)
    format: str = FORMAT
    version: int = VERSION

    @property
    def kv_binding(self) -> KVBinding:
        return KVBinding.from_dict(self.binding)


def _sha256_file(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def pack(
    payload: os.PathLike | str,
    out: os.PathLike | str,
    *,
    binding: KVBinding,
    key: str = "",
    tokens: int = 0,
    scope_fp: str = "",
    source: str = "",
    meta: Optional[dict] = None,
) -> Header:
    """Wrap a KV payload file (e.g. a llama-server slot file) into an artifact at ``out``.

    Written to ``<out>.tmp`` and renamed, so a reader never sees a torn artifact.
    """
    src = Path(payload)
    digest, nbytes = _sha256_file(src)
    header = Header(
        binding=binding.canonical(),
        binding_digest=binding.digest(),
        payload_sha256=digest,
        payload_nbytes=nbytes,
        key=key,
        tokens=int(tokens),
        scope_fp=scope_fp,
        created=time.time(),
        source=source,
        meta=dict(meta or {}),
    )
    blob = json.dumps(asdict(header), sort_keys=True).encode("utf-8")
    dst = Path(out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    with open(tmp, "wb") as w, open(src, "rb") as r:
        w.write(MAGIC)
        w.write(struct.pack(">Q", len(blob)))
        w.write(blob)
        for chunk in iter(lambda: r.read(_CHUNK), b""):
            w.write(chunk)
    os.replace(tmp, dst)
    return header


def _read_header(f: Any) -> tuple[Header, int]:
    if f.read(len(MAGIC)) != MAGIC:
        raise FormatError("not a KV artifact (bad magic)")
    raw_len = f.read(8)
    if len(raw_len) != 8:
        raise FormatError("truncated header length")
    (hlen,) = struct.unpack(">Q", raw_len)
    if not 0 < hlen <= MAX_HEADER:
        raise FormatError(f"implausible header length {hlen}")
    blob = f.read(hlen)
    if len(blob) != hlen:
        raise FormatError("truncated header")
    try:
        data = json.loads(blob.decode("utf-8"))
        header = Header(**data)
    except (ValueError, TypeError) as exc:
        raise FormatError(f"unreadable header: {exc}") from exc
    if header.format != FORMAT or int(header.version) != VERSION:
        raise FormatError(f"unsupported artifact {header.format!r} v{header.version!r}")
    if KVBinding.from_dict(header.binding).digest() != header.binding_digest:
        raise FormatError("header binding does not match its own digest (tampered?)")
    return header, len(MAGIC) + 8 + hlen


def read_header(path: os.PathLike | str) -> Header:
    """Parse and integrity-check the header only (no payload read)."""
    with open(path, "rb") as f:
        header, _ = _read_header(f)
    return header


def _check(header: Header, expect: Optional[KVBinding], scope_fp: Optional[str]) -> None:
    if expect is not None:
        diffs = header.kv_binding.diff(expect)
        if diffs:
            raise BindingMismatchError(diffs)
    if scope_fp is not None and header.scope_fp != scope_fp:
        raise ScopeMismatchError(
            f"artifact scope {header.scope_fp[:16]!r} != expected {scope_fp[:16]!r}"
        )


def _stream_payload(f: Any, header: Header, sink: Any = None) -> None:
    h, n = hashlib.sha256(), 0
    for chunk in iter(lambda: f.read(_CHUNK), b""):
        h.update(chunk)
        n += len(chunk)
        if sink is not None:
            sink.write(chunk)
    if n != header.payload_nbytes or h.hexdigest() != header.payload_sha256:
        raise DigestMismatchError(
            f"payload {n} B sha256 {h.hexdigest()[:16]} != header "
            f"{header.payload_nbytes} B {header.payload_sha256[:16]}"
        )


def verify(
    path: os.PathLike | str,
    expect: Optional[KVBinding] = None,
    *,
    scope_fp: Optional[str] = None,
) -> Header:
    """Check an artifact end to end; return its header or raise :class:`KVArtifactError`.

    Order: format, header integrity, binding (``expect``), scope (``scope_fp``), payload
    digest. The binding and scope are refused before a single payload byte is read.
    """
    with open(path, "rb") as f:
        header, _ = _read_header(f)
        _check(header, expect, scope_fp)
        _stream_payload(f, header)
    return header


def unpack(
    path: os.PathLike | str,
    dst: os.PathLike | str,
    expect: Optional[KVBinding] = None,
    *,
    scope_fp: Optional[str] = None,
) -> Header:
    """Verify and extract the payload to ``dst``; nothing lands at ``dst`` on any failure."""
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".unpack.tmp")
    try:
        with open(path, "rb") as f:
            header, _ = _read_header(f)
            _check(header, expect, scope_fp)
            with open(tmp, "wb") as w:
                _stream_payload(f, header, sink=w)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return header


# ---------------------------------------------------------------- CLI


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``inspect`` prints the header; ``verify`` exits 0 valid, 1 refused, 2 unreadable."""
    p = argparse.ArgumentParser(prog="python -m adk.kvartifact")
    sub = p.add_subparsers(dest="cmd", required=True)
    ins = sub.add_parser("inspect", help="print the header (no payload check)")
    ins.add_argument("file")
    ver = sub.add_parser("verify", help="full check against an expected binding")
    ver.add_argument("file")
    ver.add_argument("--binding", help="JSON file holding the expected KVBinding")
    ver.add_argument("--scope-fp", help="expected sharing-scope fingerprint (hex)")
    a = p.parse_args(argv)
    try:
        if a.cmd == "inspect":
            print(json.dumps(asdict(read_header(a.file)), indent=1, sort_keys=True))
            return 0
        expect = None
        if a.binding:
            expect = KVBinding.from_dict(json.loads(Path(a.binding).read_text("utf-8")))
        h = verify(a.file, expect, scope_fp=a.scope_fp)
    except (FormatError, OSError) as exc:
        print(json.dumps({"verdict": "UNREADABLE", "why": str(exc)}))
        return 2
    except KVArtifactError as exc:
        print(json.dumps({"verdict": "REFUSED", "why": str(exc)}))
        return 1
    print(json.dumps({"verdict": "VALID", "tokens": h.tokens, "bytes": h.payload_nbytes,
                      "binding_digest": h.binding_digest}))
    return 0


__all__ = [
    "BindingMismatchError",
    "DigestMismatchError",
    "FormatError",
    "Header",
    "KVArtifactError",
    "KVBinding",
    "ScopeMismatchError",
    "pack",
    "read_header",
    "unpack",
    "verify",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
