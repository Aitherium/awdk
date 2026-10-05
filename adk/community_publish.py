"""Publish to, and install from, the Aitherium community marketplace.

``adk pack publish <dir>`` = ``adk pack build`` -> community submit -> bundle upload.
``adk pack install community:<listing-id>`` = purchase-gated download -> sha256 and
signature check -> safe extract into ``~/.aitheros/packs``.

Both talk to the Genesis community plane (``/v1/marketplace/community/*``): it holds
publisher approval, the bundle security scan, platform signing, Stripe checkout and
payouts. Every non-2xx answer is a :class:`CommunityError`, so a caller can never
report a publish that did not happen (``aither publish`` used to print PUBLISHED
after a 404 from a route that did not exist).

Pure functions over an ``httpx.Client`` so tests run them against a mock transport.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("adk.community_publish")

COMMUNITY_PREFIX = "community:"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class CommunityError(Exception):
    """A community-marketplace call did not succeed. ``status`` is the HTTP code
    (0 when the server was never reached)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class PublishResult:
    listing_id: str
    sha256: str
    signed: bool
    scan_decision: str
    status: str


@dataclass
class Download:
    data: bytes
    sha256: str
    signature: Optional[str]
    version: str


def _detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 -- a non-JSON error page
        return (resp.text or "")[:300]
    if isinstance(body, dict):
        d = body.get("detail", body.get("error", body))
        if isinstance(d, dict):
            return str(d.get("message") or d.get("error") or d)
        return str(d)
    return str(body)[:300]


def _check(resp: Any, what: str) -> Dict[str, Any]:
    if resp.status_code < 200 or resp.status_code >= 300:
        raise CommunityError(resp.status_code,
                             f"{what} failed ({resp.status_code}): {_detail(resp)}")
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise CommunityError(resp.status_code, f"{what}: response was not JSON") from exc
    return body if isinstance(body, dict) else {}


def _json_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    out = dict(headers)
    out["Content-Type"] = "application/json"
    return out


def _multipart_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    # httpx sets the multipart boundary itself; a JSON Content-Type would break it.
    return {k: v for k, v in headers.items() if k.lower() != "content-type"}


def publish_bundle(
    client: Any,
    genesis_url: str,
    headers: Mapping[str, str],
    *,
    bundle: Path,
    kind: str,
    name: str,
    summary: str,
    description: str = "",
    version: str = "0.1.0",
    one_time_cents: int = 0,
    subscription_cents: int = 0,
    tags: Optional[list] = None,
) -> PublishResult:
    """Submit a listing, then upload its bundle. Raises CommunityError on any non-2xx.

    The listing starts ``pending``: a moderator approves it before it is visible.
    Prices are whatever the publisher passed; nothing here picks one.
    """
    genesis_url = genesis_url.rstrip("/")
    submit = client.post(
        f"{genesis_url}/v1/marketplace/community/submit",
        json={
            "kind": kind, "name": name, "summary": summary, "description": description,
            "version": version, "tags": list(tags or []),
            "one_time_cents": int(one_time_cents), "subscription_cents": int(subscription_cents),
        },
        headers=_json_headers(headers),
    )
    body = _check(submit, "submit")
    listing_id = str((body.get("listing") or {}).get("id") or "")
    if not listing_id:
        raise CommunityError(submit.status_code, "submit returned no listing id")
    with open(bundle, "rb") as fh:
        up = client.post(
            f"{genesis_url}/v1/marketplace/community/{listing_id}/upload",
            files={"file": (bundle.name, fh, "application/octet-stream")},
            headers=_multipart_headers(headers),
        )
    ub = _check(up, f"upload of {bundle.name} to listing {listing_id}")
    local = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if ub.get("sha256") and ub["sha256"] != local:
        raise CommunityError(up.status_code,
                             f"server stored sha256 {ub['sha256']} but the bundle is {local}")
    listing = ub.get("listing") or {}
    return PublishResult(
        listing_id=listing_id,
        sha256=local,
        signed=bool(ub.get("signed")),
        scan_decision=str((ub.get("scan") or {}).get("decision") or "needs_review"),
        status=str(listing.get("status") or "pending"),
    )


def download_listing(
    client: Any, genesis_url: str, headers: Mapping[str, str], listing_id: str,
) -> Download:
    """Fetch a purchased listing's bundle and prove it is the published bytes.

    The sha256 is checked against BOTH the response header and the listing record
    (fetched separately); the Ed25519 signature follows the same policy as
    ``adk pack install`` (``adk.pack_verifier``).
    """
    genesis_url = genesis_url.rstrip("/")
    resp = client.get(f"{genesis_url}/v1/marketplace/community/{listing_id}/download",
                      headers=dict(headers))
    if resp.status_code == 403:
        raise CommunityError(403, f"you have not bought {listing_id}: {_detail(resp)}")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise CommunityError(resp.status_code,
                             f"download failed ({resp.status_code}): {_detail(resp)}")
    data = resp.content
    actual = hashlib.sha256(data).hexdigest()
    header_sha = resp.headers.get("X-Pack-SHA256", "")
    if not header_sha or header_sha != actual:
        raise CommunityError(resp.status_code,
                             f"sha256 mismatch: header {header_sha or '<none>'} vs bytes {actual}")
    detail = client.get(f"{genesis_url}/v1/marketplace/community/listings/{listing_id}",
                        headers=dict(headers))
    record = _check(detail, "listing lookup")
    if record.get("artifact_sha256") and record["artifact_sha256"] != actual:
        raise CommunityError(resp.status_code,
                             f"sha256 mismatch: listing records {record['artifact_sha256']}")
    signature = resp.headers.get("X-Aither-Pack-Signature") or None
    try:
        from adk.pack_verifier import verify_pack_tarball
    except ImportError:  # pragma: no cover -- verifier ships with adk
        verify_pack_tarball = None
    if verify_pack_tarball is not None:
        ok, msg = verify_pack_tarball(data, signature)
        if not ok:
            raise CommunityError(resp.status_code, f"signature check failed: {msg}")
    return Download(data=data, sha256=actual, signature=signature,
                    version=(resp.headers.get("X-Pack-Version", "")
                             or str(record.get("version") or "")))


def _safe_members_tar(tf: tarfile.TarFile) -> None:
    for m in tf.getmembers():
        if m.name.startswith(("/", "\\")) or ".." in Path(m.name).parts or m.issym() or m.islnk():
            raise CommunityError(0, f"unsafe path in bundle: {m.name}")


def _extract(data: bytes, dest: Path) -> None:
    buf = io.BytesIO(data)
    if tarfile.is_tarfile(buf):
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:*") as tf:
            _safe_members_tar(tf)
            try:  # 3.12+: the data filter refuses device files and odd modes too
                tf.extractall(dest, filter="data")
            except TypeError:  # 3.10/3.11 without the backport: members vetted above
                tf.extractall(dest)  # noqa: S202
        return
    buf.seek(0)
    if zipfile.is_zipfile(buf):
        with zipfile.ZipFile(buf) as zf:
            for n in zf.namelist():
                if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                    raise CommunityError(0, f"unsafe path in bundle: {n}")
            zf.extractall(dest)
        return
    raise CommunityError(0, "bundle is neither a tar nor a zip archive")


#: Marker written into every community install: the listing id it came from.
_INSTALL_MARKER = ".community-listing"


def _manifest_name(pid: object) -> Optional[str]:
    """A folder name from a manifest ``id``, or None when it is unusable.

    The id is publisher-controlled bytes: ``..`` survives ``_SAFE_ID`` (dots are
    allowed) and made ``packs_dir/..`` the target of an ``rmtree``. Anything empty,
    dot-led, or not a single plain path component is refused.
    """
    name = _SAFE_ID.sub("-", str(pid or "")).strip("-")
    if not name or name.startswith(".") or name in (".", ".."):
        return None
    return name


def _installed_listing(target: Path) -> Optional[str]:
    try:
        return (target / _INSTALL_MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def install_bundle(download: Download, listing_id: str, packs_dir: Path) -> Path:
    """Extract into ``packs_dir/<name>``; a single top-level folder is unwrapped.

    The folder is the pack's manifest id when it is a safe name, else
    ``community-<id>``. An existing folder is replaced ONLY when it is a previous
    install of this same listing (its ``.community-listing`` marker, or the
    listing-derived name itself); anything else -- a first-party pack, another
    listing -- is refused, never overwritten.
    """
    packs_dir.mkdir(parents=True, exist_ok=True)
    packs_root = packs_dir.resolve()
    default_name = f"community-{_SAFE_ID.sub('-', listing_id).strip('.')}"
    with tempfile.TemporaryDirectory(dir=packs_dir) as tmp:
        stage = Path(tmp) / "x"
        stage.mkdir()
        _extract(download.data, stage)
        entries = list(stage.iterdir())
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else stage
        name = default_name
        manifest = root / ".toolpack.yaml"
        if manifest.is_file():
            try:
                import yaml

                pid = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("id")
                if pid:
                    safe = _manifest_name(pid)
                    if safe:
                        name = safe
                    else:
                        logger.warning("unsafe pack id %r in %s; installing as %s",
                                       pid, listing_id, name)
            except Exception as exc:  # noqa: BLE001 -- keep the listing-derived name
                logger.warning("unreadable .toolpack.yaml in %s (%s); installing as %s",
                               listing_id, exc, name)
        target = (packs_dir / name).resolve()
        if target.parent != packs_root:
            raise CommunityError(0, f"refusing to install outside {packs_root}: {name}")
        if target.exists():
            owner = _installed_listing(target)
            if owner != listing_id and not (owner is None and name == default_name):
                raise CommunityError(
                    0, f"{target} already holds a different pack"
                       f"{f' (listing {owner})' if owner else ''}; not overwriting it")
            shutil.rmtree(target)
        shutil.move(str(root), str(target))
        (target / _INSTALL_MARKER).write_text(f"{listing_id}{chr(10)}", encoding="utf-8")
    return target


__all__ = [
    "COMMUNITY_PREFIX", "CommunityError", "Download", "PublishResult",
    "download_listing", "install_bundle", "publish_bundle",
]
